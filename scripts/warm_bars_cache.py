"""把一段（或几段）回测窗口的行情**离线预热**到落盘缓存里。

为什么要有这个脚本：一次真回测里，取数是**唯一**需要 Wind 额度的步骤（其余全是 LLM 与本地
计算）。额度耗尽时回测会在取数那一步整轮失败，而它可能已经跑了几个小时。预先把行情缓存下来，
之后的多轮回测就是纯离线的——`scripts/verify_bars_cache.py` 是这件事的**证明**。

## 必须镜像真实回测的取数路径

参数怎么算由生产代码决定，这里**绝不自己拼缓存文件名**。本脚本调用的就是 `_drive` 用的那三行
（`app/backtest.py` 的取数块）：

    with asof_scope(end):
        days = bd.trading_days(start, end)
        bd.load_bars(codes, days, start, end, on_progress=...)
        bd.load_benchmark(start, end)

`asof_scope` 是承重墙：**缓存只在 as-of 作用域内启用**（`_fetch_cached` 的注释）。不在作用域里
跑，这个脚本会真的打 Wind 却什么都不落盘——看起来"预热成功"，实际一无所有。

## ⚠️ 两个容易被误解的点

1. **缓存按 `(pad_start, eff_end)` 成对落盘**，不是按 `end_date` 单独落盘——
   `eff_end = min(end_date, asof)`。所以**预热一个"大窗口"不会命中它的子区间**：
   `08-11~09-11` 的文件与 `09-07~09-11` 的文件是两个不同的键。**预热的窗口清单 = 之后能
   离线跑的窗口清单**，多一个少一个都不行。

2. `_PAD_DAYS` 与 `_CACHE_VERSION` 参与缓存键，**改任何一个 = 全部缓存作废**。生产缓存目录里
   `..._2026-08-19_...` 与 `..._2026-08-26_...` 两套并存，就是 `_PAD_DAYS` 从 19 改成 12 留下
   的痕迹。所以本脚本每次都把这两个值打出来——将来改动时一眼能看见代价。

代码层面的覆盖只要**一次全集**就够：缓存文件是**按票**落盘的，回测实际用哪个分组（`bt_scope`
/ `bt_groups`）都不影响命中。**唯一必须对齐的是窗口。**

用法：
    # 三窗口矩阵（近一月），标的取某发起人的自选票全集
    .venv/bin/python scripts/warm_bars_cache.py \\
        --owner-user-id 1 \\
        --windows "2026-09-07:2026-09-11,2026-08-25:2026-09-11,2026-08-11:2026-09-11"

    # 指定标的
    .venv/bin/python scripts/warm_bars_cache.py --codes 600519.SS,000001.SZ --windows "..."

    # 只探一次网，确认 key 可用（不写缓存）
    .venv/bin/python scripts/warm_bars_cache.py --probe-only
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backtest_data as bd  # noqa: E402
from tradingagents.asof import asof_scope  # noqa: E402


def _parse_windows(spec: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise SystemExit(f"--windows 片段格式应为 start:end，收到 {item!r}")
        start, end = (s.strip() for s in item.split(":", 1))
        if start > end:
            raise SystemExit(f"窗口起点晚于终点：{start} > {end}")
        out.append((start, end))
    if not out:
        raise SystemExit("--windows 为空")
    return out


def _expected_paths(codes: list[str], start: str, end: str) -> dict[str, Path]:
    """这批 (code, 窗口) 在**当前**口径下应当命中的缓存文件路径。

    `pad_start` 的算法照抄 `load_bars`：这里是为了**统计**命中数才复算一遍，真正的取数仍然
    只走 `bd.load_bars`。两边一旦分家，本脚本会报出错误的命中率——但那不会污染缓存本身，
    只会让这份报告不可信。（`eff_end` 由 as-of 封顶，回测窗口的 `end_date` 必然 ≤ as-of，
    所以这里直接用 `end`。）
    """
    import pandas as pd

    pad_start = (pd.Timestamp(start) - pd.Timedelta(days=bd._PAD_DAYS)).strftime("%Y-%m-%d")
    return {c: bd._cache_file(c, pad_start, end, "stk", adj="hfq") for c in codes}


def _owner_universe(owner_user_id: int) -> list[str]:
    from app import trust

    return [s["code"] for s in trust.get_analysis_universe(owner_user_id) if s.get("code")]


def _owner_holdings(owner_user_id: int) -> list[str]:
    """发起人托管簿里**有股数**的持仓。

    这些票是 ``init_mode=copy`` 起跑时要**额外**取一条不复权线的：回测全空间是后复权，
    而真实簿的成本价是不复权，复制持仓必须按 ``f = 后复权/不复权`` 换算（见
    ``backtest._rescale_copied_book``）。不预热这条线，copy 轮次就会在起跑时打 Wind——
    额度一旦用完，那正是"预热过了却还是跑不起来"的形态。
    """
    from app import account

    out: list[str] = []
    for p in account.get_positions(owner_user_id, 1) or []:
        code = str(p.get("stock_code") or "")
        if code and int(p.get("hold_qty") or 0) > 0:
            out.append(code)
    return out


def _expected_raw_paths(codes: list[str], start: str, end: str) -> dict[str, Path]:
    """不复权线的缓存路径。算法与 ``_expected_paths`` 一致，只换 ``adj``。"""
    import pandas as pd

    pad_start = (pd.Timestamp(start) - pd.Timedelta(days=bd._PAD_DAYS)).strftime("%Y-%m-%d")
    return {c: bd._cache_file(c, pad_start, end, "stk", adj="raw") for c in codes}


def _probe() -> bool:
    """一次真实探数，确认 key 可用。**在 as-of 作用域外**跑，所以不会写缓存。"""
    from tradingagents.dataflows import wind as _wind

    try:
        print(f"  key 已配置：{bool(_wind._get_api_key())}")
    except Exception as e:  # noqa: BLE001 —— 未配置 key 也要给出可读的结论
        print(f"  ❌ key 不可用：{type(e).__name__}: {e}")
        return False
    try:
        days = bd.trading_days("2026-09-07", "2026-09-11")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 探数失败：{type(e).__name__}: {e}")
        return False
    print(f"  ✅ 探数成功，2026-09-07~2026-09-11 交易日 = {days}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="回测行情离线预热")
    ap.add_argument("--windows", default="", help="逗号分隔的 start:end 列表")
    ap.add_argument("--codes", default="", help="逗号分隔的标的代码；缺省取 --owner-user-id 的自选票全集")
    ap.add_argument("--owner-user-id", type=int, default=0, help="标的池来源用户（自选票全集）")
    ap.add_argument("--probe-only", action="store_true", help="只探一次网，不预热")
    ap.add_argument("--no-raw-holdings", action="store_true",
                    help="不为发起人的持仓预热不复权线（copy 起步的回测将无法离线跑）")
    args = ap.parse_args()

    print("=" * 72)
    print("回测行情离线预热")
    print("=" * 72)
    print(f"缓存目录      {bd._CACHE_DIR}")
    print(f"_PAD_DAYS     {bd._PAD_DAYS}      ← 参与缓存键，改它 = 全部缓存作废")
    print(f"_CACHE_VERSION {bd._CACHE_VERSION}     ← 同上")
    print(f"_CACHE_TTL_DAYS {bd._CACHE_TTL_DAYS}")
    print()

    print("[探数] 确认 Wind 可用")
    if not _probe():
        return 1
    print()
    if args.probe_only:
        return 0

    if not args.windows:
        raise SystemExit("需要 --windows（或用 --probe-only）")
    windows = _parse_windows(args.windows)

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.owner_user_id:
        codes = _owner_universe(args.owner_user_id)
    else:
        raise SystemExit("需要 --codes 或 --owner-user-id")
    if not codes:
        raise SystemExit("标的池为空——检查 --codes / 该用户的自选股与持仓")
    print(f"标的 {len(codes)} 只：{', '.join(codes)}")
    # 持仓的不复权线：只有 copy 起步用得上，且只在 --owner-user-id 给定时才知道是谁的持仓
    holdings: list[str] = []
    if args.no_raw_holdings:
        print("不复权线：**本次不预热**（--no-raw-holdings）——copy 起步的回测将无法离线跑")
    elif args.owner_user_id:
        holdings = _owner_holdings(args.owner_user_id)
        print(f"不复权线（copy 起步的复权因子要用）{len(holdings)} 只："
              f"{', '.join(holdings) if holdings else '（该用户当前无持仓）'}")
    else:
        print("不复权线：跳过（未给 --owner-user-id，无从知道持仓是哪几只）")
    print()

    print("⚠️  缓存按 (pad_start, eff_end) 成对落盘：预热一个「大窗口」**不会**命中它的子区间。")
    print("    下面这份窗口清单 = 之后能离线跑的窗口清单。")
    print()

    rc = 0
    grand_new = 0
    for wi, (start, end) in enumerate(windows, 1):
        print("-" * 72)
        print(f"[窗口 {wi}/{len(windows)}] {start} ~ {end}")
        expected = _expected_paths(codes, start, end)
        hit_before = [c for c, p in expected.items() if p.exists()]
        miss_before = [c for c in codes if c not in hit_before]
        raw_expected = _expected_raw_paths(holdings, start, end) if holdings else {}
        raw_before = [c for c, p in raw_expected.items() if p.exists()]
        print(f"  预热前：命中 {len(hit_before)} / 待取 {len(miss_before)}")

        t0 = time.time()
        try:
            with asof_scope(end):
                days = bd.trading_days(start, end)
                if not days:
                    print(f"  ❌ 区间内没有交易日")
                    rc = 1
                    continue
                all_bars, warnings = bd.load_bars(
                    codes, days, start, end,
                    on_progress=lambda i, n, c: print(f"    {i}/{n} {c}"),
                )
                bd.load_benchmark(start, end)
                if holdings:
                    _raw_closes, raw_warnings = bd.load_unadjusted_closes(holdings, start, end)
                    warnings = warnings + raw_warnings
        except Exception as e:  # noqa: BLE001 —— 一个窗口失败不该丢掉其它窗口的结论
            print(f"  ❌ 取数失败：{type(e).__name__}: {e}")
            rc = 1
            continue
        dt = time.time() - t0

        hit_after = [c for c, p in expected.items() if p.exists()]
        new = len(hit_after) - len(hit_before)
        grand_new += new
        empty = [c for c in codes if not all_bars.get(c)]
        print(f"  预热后：缓存文件 {len(hit_after)} / {len(codes)}（本次新落盘 {new}），耗时 {dt:.1f}s")
        if holdings:
            raw_after = [c for c, p in raw_expected.items() if p.exists()]
            raw_new = len(raw_after) - len(raw_before)
            grand_new += raw_new
            print(f"  不复权线：{len(raw_after)} / {len(holdings)}（本次新落盘 {raw_new}）")
            if len(raw_after) < len(holdings):
                miss_raw = [c for c in holdings if c not in raw_after]
                print(f"  ⚠️  不复权线仍未落盘（copy 起步会在起跑时打 Wind）：{', '.join(miss_raw)}")
        if warnings:
            print(f"  ⚠️  {len(warnings)} 条警告：")
            for w in warnings:
                print(f"      - {w}")
        if empty:
            print(f"  ⚠️  区间内无任何行情（回测会全程跳过）：{', '.join(empty)}")
        # 「这票压根取不到」是数据问题，不是本脚本的失败；但它会让回测的完整性门禁拦下起跑，
        # 所以在这里就报出来，别等用户点了起跑才发现。
        if len(hit_after) < len(codes):
            missing = [c for c in codes if c not in hit_after]
            print(f"  ⚠️  仍未落盘（下次仍会打 Wind）：{', '.join(missing)}")

    print("-" * 72)
    print(f"完成：{len(windows)} 个窗口，本次新落盘 {grand_new} 个缓存文件。")
    print("下一步：`.venv/bin/python scripts/verify_bars_cache.py --windows \"...\"` —— 断网重跑，")
    print("        0 次网络调用才算真的预热好了。")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
