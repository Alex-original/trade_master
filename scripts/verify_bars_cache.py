"""断网验收：证明「行情真的都在本地缓存里了」，而不是"以为缓存了、跑起来还在打 Wind"。

`warm_bars_cache.py` 跑完只说明**它跑了**，不说明**下一个人能离线跑**。这个脚本把
`_wind._call_tool` 换成**一被调用就 raise** 的替身（不是返回哨兵——哨兵会让"触网了"
伪装成"没数据"），然后在同一批窗口里重跑 `trading_days` + `load_bars` + `load_benchmark`。

**通过判据（三条缺一不可）**：

1. **0 次网络调用** —— 替身一次都没被碰。
2. **0 条 warning** —— `load_bars` 的单只失败会记 warning。有任何一条，就说明有票没缓存到。
3. **取回的 bars 与缓存文件逐字节相同** —— 直接 `_read_cache` 读原文件、
   `build_bars_from_frame` 重建一遍，与 `load_bars` 的返回值算 sha1 对比。这一条堵的是
   「走了网络但恰好也能返回同样的数据」（比如 key 还有额度时的假绿灯）。

**第 4 条（`--owner-user-id` 给定时）**：持仓票的**不复权线**也要 0 触网、0 warning、并与
缓存文件一致。`init_mode=copy` 的回测在**起跑那一刻**就要它来算复权因子
（`backtest._rescale_copied_book`），所以它和主线的地位一样——不验它，copy 轮次就会在
"以为预热好了"之后，于起跑时打 Wind。想显式跳过用 `--no-raw-holdings`（会打印代价）。

不通过 = 有票没覆盖 / 窗口没对上 / `_PAD_DAYS` 或 `_CACHE_VERSION` 变了
（后两者会换掉缓存键，旧文件自动失效）。

⚠️ 本脚本**必须在 as-of 作用域内**调用，否则 `_fetch_cached` 直接穿透到网络（缓存只在
as-of 内启用），而替身会立刻炸——这正是它要暴露的事情。

用法（与预热同一份窗口清单）：
    .venv/bin/python scripts/verify_bars_cache.py \\
        --owner-user-id 1 \\
        --windows "2026-09-07:2026-09-11,2026-08-25:2026-09-11,2026-08-11:2026-09-11"
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backtest_data as bd  # noqa: E402
from tradingagents.asof import asof_scope  # noqa: E402
from tradingagents.dataflows import wind as _wind  # noqa: E402

#: 被替身拦下的请求。**空列表**就是本脚本的头号通过判据。
CALLS: list[tuple] = []


def _forbidden_tool(server_type, tool_name, params, timeout=120):
    """一被调用就炸。返回哨兵会让"触网了"伪装成"没数据"，所以必须 raise。"""
    CALLS.append((server_type, tool_name, dict(params)))
    raise AssertionError(f"⚠️ 断网验收失败：不应触网 {server_type}/{tool_name} {params}")


def _digest(bars: dict) -> str:
    """bar 字典的确定性指纹。``Bar`` 是 frozen dataclass，字段顺序稳定。"""
    payload = {
        d: dataclasses.asdict(b) for d, b in sorted(bars.items())
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _parse_windows(spec: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        start, end = (s.strip() for s in item.split(":", 1))
        out.append((start, end))
    if not out:
        raise SystemExit("--windows 为空")
    return out


def _owner_universe(owner_user_id: int) -> list[str]:
    from app import trust

    return [s["code"] for s in trust.get_analysis_universe(owner_user_id) if s.get("code")]


def _owner_holdings(owner_user_id: int) -> list[str]:
    """持仓票——``init_mode=copy`` 起跑还要一条**不复权**线（算复权因子），也必须离线可取。"""
    from app import account

    out: list[str] = []
    for p in account.get_positions(owner_user_id, 1) or []:
        code = str(p.get("stock_code") or "")
        if code and int(p.get("hold_qty") or 0) > 0:
            out.append(code)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="回测行情缓存断网验收")
    ap.add_argument("--windows", required=True, help="逗号分隔的 start:end 列表")
    ap.add_argument("--codes", default="", help="逗号分隔；缺省取 --owner-user-id 的自选票全集")
    ap.add_argument("--owner-user-id", type=int, default=0)
    ap.add_argument("--no-raw-holdings", action="store_true",
                    help="不验收持仓的不复权线（copy 起步的回测将无法离线跑）")
    args = ap.parse_args()

    print("=" * 72)
    print("断网验收：缓存是否真的覆盖了这些窗口")
    print("=" * 72)
    print(f"缓存目录       {bd._CACHE_DIR}")
    print(f"_PAD_DAYS      {bd._PAD_DAYS}")
    print(f"_CACHE_VERSION {bd._CACHE_VERSION}")
    print()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.owner_user_id:
        codes = _owner_universe(args.owner_user_id)
    else:
        raise SystemExit("需要 --codes 或 --owner-user-id")
    if not codes:
        raise SystemExit("标的池为空")
    #: copy 起步要用的不复权线。只有知道是谁的持仓时才能验——它同样必须 0 次触网。
    holdings = ([] if (args.no_raw_holdings or not args.owner_user_id)
                else _owner_holdings(args.owner_user_id))
    windows = _parse_windows(args.windows)
    if holdings:
        print(f"另验 {len(holdings)} 只持仓的不复权线（copy 起步的复权因子）：{', '.join(holdings)}")
        print()

    # 装替身。**必须在任何 as-of 作用域之前**——包括 trading_days。
    _wind._call_tool = _forbidden_tool  # type: ignore[assignment]

    bad = 0
    for wi, (start, end) in enumerate(windows, 1):
        print("-" * 72)
        print(f"[窗口 {wi}/{len(windows)}] {start} ~ {end}")
        try:
            with asof_scope(end):
                days = bd.trading_days(start, end)
                all_bars, warnings = bd.load_bars(codes, days, start, end)
                bd.load_benchmark(start, end)
                raw_closes, raw_warnings = (
                    bd.load_unadjusted_closes(holdings, start, end) if holdings else ({}, [])
                )
        except AssertionError as e:
            print(f"  ❌ {e}")
            bad += 1
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ 取数失败：{type(e).__name__}: {e}")
            bad += 1
            continue

        print(f"  交易日 {len(days)} 天，取回 {len(all_bars)} / {len(codes)} 只")

        if warnings:
            bad += 1
            print(f"  ❌ {len(warnings)} 条警告（= 有票没缓存到）：")
            for w in warnings:
                print(f"      - {w}")

        empty = [c for c in codes if not all_bars.get(c)]
        if empty:
            bad += 1
            print(f"  ❌ 区间内无行情：{', '.join(empty)}")

        # 逐票：把 load_bars 的返回值 与 「直接读那个缓存文件再重建」的产物对比。
        # 这一条堵的是「走了网络但恰好返回同样的数据」——断网替身已经让那种情况不可能发生，
        # 但万一将来谁把替身拆了，这条断言还在。
        import pandas as pd

        pad_start = (pd.Timestamp(start) - pd.Timedelta(days=bd._PAD_DAYS)).strftime("%Y-%m-%d")
        mismatch: list[str] = []
        for c in codes:
            path = bd._cache_file(c, pad_start, end, "stk", adj="hfq")
            if not path.exists():
                mismatch.append(f"{c}: 缓存文件不存在 {path.name}")
                continue
            df = bd._read_cache(path)
            if df is None:
                mismatch.append(f"{c}: 缓存文件读不出来 {path.name}")
                continue
            disk = bd.build_bars_from_frame(df, days)
            if _digest(disk) != _digest(all_bars.get(c) or {}):
                mismatch.append(f"{c}: 返回值与缓存文件不一致")
        if mismatch:
            bad += 1
            print(f"  ❌ {len(mismatch)} 只与缓存文件不一致：")
            for m in mismatch[:10]:
                print(f"      - {m}")

        # ---- 持仓的不复权线：copy 起步在起跑时就要它，断了网也得取得到 ----
        if holdings:
            if raw_warnings:
                bad += 1
                print(f"  ❌ 不复权线 {len(raw_warnings)} 条警告（= 有票没缓存到）：")
                for w in raw_warnings:
                    print(f"      - {w}")
            raw_mismatch: list[str] = []
            for c in holdings:
                rpath = bd._cache_file(c, pad_start, end, "stk", adj="raw")
                if not rpath.exists():
                    raw_mismatch.append(f"{c}: 缓存文件不存在 {rpath.name}")
                    continue
                rdf = bd._read_cache(rpath)
                if rdf is None:
                    raw_mismatch.append(f"{c}: 缓存文件读不出来 {rpath.name}")
                    continue
                # 与取数路径共用同一个纯函数（``closes_from_frame``），所以"逐字节相同"
                # 这条断言不会被两处各写一份的转换代码悄悄架空。
                if bd.closes_from_frame(rdf) != (raw_closes.get(c) or {}):
                    raw_mismatch.append(f"{c}: 返回值与缓存文件不一致")
            if raw_mismatch:
                bad += 1
                print(f"  ❌ 不复权线 {len(raw_mismatch)} 只有问题：")
                for m in raw_mismatch[:10]:
                    print(f"      - {m}")
            else:
                print(f"  ✅ 不复权线 {len(raw_closes)} / {len(holdings)} 只全部离线可取")

    print("-" * 72)
    if CALLS:
        bad += 1
        print(f"❌ 触网 {len(CALLS)} 次（前 5 条）：")
        for c in CALLS[:5]:
            print(f"    {c}")
    else:
        print("✅ 全程 0 次网络调用")
    if bad:
        print(f"❌ 断网验收**未通过**（{bad} 处问题）——缓存没有真正覆盖这些窗口，不能离线跑回测。")
        return 1
    print("✅ 断网验收**通过**：这些窗口已可完全离线跑回测。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
