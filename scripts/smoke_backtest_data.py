"""数据层 as-of 守卫离线冒烟脚本（回测未来函数防线）。

确定性、离线：**不跑 LLM、不连数据库、不碰 Wind**——`_call_tool` 全程被替身接管，
其中一个替身"一被调用就 raise"，用来证明被禁用的工具**真的没触网**。

覆盖：
  1. 实时路径的请求参数**逐字节不变**（没有 aftype、end_date 不被改写）
  2. as-of 作用域内：K 线请求强制后复权 + end_date 封顶
  3. 十个日期盲入口在回测中全部降级为哨兵，且**一次网都没触**
  4. 时点快照 → 空、公司名称 → None、时间戳 → 模拟日
  5. 作用域的正确性：可嵌套、退出后恢复、不污染实时路径
  6. bar 构造：代理量比可手算、停牌顺延、上市前不生成（纯函数）
  7. 基准与交易日历：指数 K 线的请求口径 + 日期解析（走替身，仍离线）
  8. 复权探针（**需网络**，无网自动跳过）
  9. 落盘缓存：只在 as-of 内启用、命中即等价、键含复权口径与区间、坏文件按未命中
 10. 退避重试的判据：**重试有没有可能成功**（限流/服务抖动重试，额度耗尽/参数错不重试）

⚠️ **第 1、2、7 节断的是"请求长什么样"，不是"Wind 收不收"**——这两者会分家，已经分家过两次：
`get_index_ohlcv` 的 `period="1d"`、以及把 `aftype` 发成**整数**（schema 里是 `string`）。
两次都是替身下全绿、真打接口才暴露，而**回测一只标的都取不到数**。所以第 1、2 节现在
除了断言取值，还断言 `isinstance(..., str)`；但真正的把关在打一次真接口（见
`docs/历史回测_设计与验证_v1.0.md` §10.1 第 5 条）。

用法：
    .venv/bin/python scripts/smoke_backtest_data.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backtest_data as _bd  # noqa: E402
from tradingagents.asof import ASOF, asof_scope, current_asof  # noqa: E402
from tradingagents.dataflows import wind as _wind  # noqa: E402
from tradingagents.dataflows.errors import (  # noqa: E402
    NoMarketDataError,
    VendorRateLimitError,
    VendorRejectedError,
)

# ---------------------------------------------------------------- 断言脚手架
# 与 smoke_ladder.py 同一套：本仓库 tests/ 为空且无 pytest，约定用确定性离线脚本。

_FAILS: list[str] = []
_COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _COUNT
    _COUNT += 1
    if cond:
        print(f"  ✅ {name}")
    else:
        _FAILS.append(name)
        print(f"  ❌ {name}{('  →  ' + detail) if detail else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- Wind 替身

_COLS = [{"name": n} for n in ("TIME", "OPEN", "MATCH", "HIGH", "LOW", "VOLUME")]
_ROWS = [
    ["2026-03-02", 10.0, 10.50, 10.60, 9.90, 1_000_000],
    ["2026-03-03", 10.5, 10.80, 10.90, 10.40, 1_100_000],
]

CALLS: list[tuple] = []


def _recording_tool(server_type, tool_name, params, timeout=120):
    """记录请求并回一份合法 K 线。用于"允许触网"的场景。"""
    CALLS.append((server_type, tool_name, dict(params)))
    return {"data": {"columns": _COLS, "rows": _ROWS}}


def _forbidden_tool(server_type, tool_name, params, timeout=120):
    """一被调用就炸。用来证明被禁用的工具**确实没有触网**，而不是只返回了哨兵。"""
    CALLS.append(("!!触网!!", server_type, tool_name, dict(params)))
    raise AssertionError(f"回测中不应触网：{server_type}/{tool_name} {params}")


_REAL_CALL_TOOL = _wind._call_tool  # 装替身之前先留一份真的，复权探针要用


def _install(fn) -> None:
    _wind._call_tool = fn  # type: ignore[assignment]


def _reset() -> None:
    CALLS.clear()


def _clear_cache() -> None:
    """清掉落盘缓存。

    **断言"请求长什么样"的段落必须先清**：缓存命中时压根不发请求，那些 ``CALLS[0]`` 会以
    ``IndexError`` 的形式炸掉——一个完全指不到方向的失败。更要命的是不清的话，第一次运行
    会把**替身返回的假行情**写进真实缓存目录，第二次运行拿假数据当真数据用，脚本从此
    不再确定。所以整个脚本跑在临时目录里（见 ``main``），这里只负责段落之间互不干扰。
    """
    for p in _bd._CACHE_DIR.glob("*.json"):
        p.unlink()


#: 回测中必须降级的**全部入口**。用一个显式清单而不是遍历模块，是为了将来新加工具时
#: 这里不会"自动通过"——漏加一个就必须有人来改这份清单。
DISABLED_ENTRIES = [
    ("基本面", lambda: _wind.get_fundamentals("600519.SH")),
    ("资产负债表", lambda: _wind.get_balance_sheet("600519.SH")),
    ("现金流量表", lambda: _wind.get_cashflow("600519.SH")),
    ("利润表", lambda: _wind.get_income_statement("600519.SH")),
    ("个股新闻", lambda: _wind.get_news("600519.SH")),
    ("全球新闻", lambda: _wind.get_global_news()),
    ("内部人交易/公司事件", lambda: _wind.get_insider_transactions("600519.SH")),
    ("宏观指标", lambda: _wind.get_macro_indicators("社会融资规模")),
    ("公司公告", lambda: _wind.get_company_announcements("600519.SH")),
    ("风险指标", lambda: _wind.get_risk_metrics("600519.SH")),
]

ASOF_DAY = "2026-03-05"


def main() -> int:  # noqa: C901 —— 冒烟脚本，线性罗列各场景
    _install(_recording_tool)

    # **整个脚本跑在临时缓存目录里**。不这么做的话：第一次运行会往真实的
    # `data/bars_cache/` 写文件，第二次运行就命中上一次留下的文件——而这个脚本的行情
    # 全部来自 `_recording_tool` 那份假 K 线。结果是"第二次运行通过、第三次不一定"，
    # 而且真实缓存目录里躺着假数据（起跑一次真实回测就会读到它们）。
    tmp_cache = Path(tempfile.mkdtemp(prefix="bt_bars_smoke_"))
    saved_cache_dir = _bd._CACHE_DIR
    _bd._CACHE_DIR = tmp_cache

    # ---------------------------------------------------------------- 1. 实时不变
    section("1. 实时路径：请求参数逐字节不变（零回归门）")
    _reset()
    _wind.get_wind_ohlcv("600519.SH", "2026-01-01", "2026-03-03")
    _, _, p = CALLS[0]
    check("不传 aftype 时请求里没有 aftype", "aftype" not in p, str(p))
    check("end_date 未被改写", p["end_date"] == "2026-03-03", str(p))
    check("begin_date 未被改写", p["begin_date"] == "2026-01-01", str(p))
    check("period 保持默认 10", p["period"] == "10", str(p))
    check("windcode 已归一", p["windcode"] == "600519.SH", str(p))

    _reset()
    _wind.get_stock_data("600519.SH", "2026-01-01", "2026-03-03")
    _, tool, p = CALLS[0]
    check("实时 get_stock_data 也不带 aftype", "aftype" not in p, str(p))
    check("实时走 get_stock_kline", tool == "get_stock_kline", tool)

    # ---------------------------------------------------------------- 2. as-of 封顶
    section("2. as-of 作用域：强制后复权 + end_date 封顶")
    _reset()
    with asof_scope(ASOF_DAY):
        df = _wind.get_wind_ohlcv("600519.SH", "2026-01-01", "2026-03-31")
    _, _, p = CALLS[0]
    # ⚠️ 断言的是**字符串** ``"1"``，不是整数 1。老断言写的是 int，而 Wind 的 schema 里
    # ``aftype`` 是 ``type='string', default='0'``——传整数被服务端当场拒：
    # ``参数格式不正确:aftype``。**这条曾经全绿，而回测一个标的都取不到数。**
    check("回测下**漏传 aftype 也默认后复权**（fail-safe）", p.get("aftype") == "1", str(p))
    check("★ aftype 以字符串发送（Wind 的 period/aftype 都是 string 类型，传 int 会被拒）",
          isinstance(p.get("aftype"), str), f"{p.get('aftype')!r} ({type(p.get('aftype')).__name__})")
    check("end_date 被封顶到 asof", p["end_date"] == ASOF_DAY, str(p))
    check("begin_date 不动", p["begin_date"] == "2026-01-01", str(p))
    check("封顶不影响返回值解析", len(df) == 2 and df["Close"].iloc[-1] == 10.80, str(df.to_dict()))

    _reset()
    with asof_scope(ASOF_DAY):
        # end_date 早于 asof：不该被"提前"，只封顶不拉伸
        _wind.get_wind_ohlcv("600519.SH", "2026-01-01", "2026-02-01")
    check("end_date 早于 asof 时不被拉伸", CALLS[0][2]["end_date"] == "2026-02-01", str(CALLS[0][2]))

    _reset()
    with asof_scope(ASOF_DAY):
        _wind.get_stock_data("600519.SH", "2026-01-01", "2026-03-31")
    check("get_stock_data 在回测下也强制后复权", CALLS[0][2].get("aftype") == "1", str(CALLS[0][2]))
    check("get_stock_data 的 end_date 也被封顶", CALLS[0][2]["end_date"] == ASOF_DAY, str(CALLS[0][2]))

    # ---------------------------------------------------------------- 3. 禁用清单
    section("3. 日期盲入口在回测中全部降级，且一次网都不触")
    _install(_forbidden_tool)
    with asof_scope(ASOF_DAY):
        for label, fn in DISABLED_ENTRIES:
            _reset()
            out = fn()
            check(f"{label} 返回哨兵", out == _wind.ASOF_UNAVAILABLE, repr(out)[:80])
            check(f"{label} 未触网", CALLS == [], str(CALLS))

    # ---------------------------------------------------------------- 4. 快照 / 名称 / 时间戳
    section("4. 时点快照归空、名称归 None、时间戳走模拟日")
    with asof_scope(ASOF_DAY):
        _reset()
        check("回测中 get_price_snapshots 返回空", _wind.get_price_snapshots(["600519.SH"]) == {})
        check("且未触网", CALLS == [], str(CALLS))
        _reset()
        check("回测中 get_company_name 返回 None", _wind.get_company_name("600519.SH") is None)
        check("且未触网", CALLS == [], str(CALLS))
        # get_stock_data 是**日期可寻址**的，回测里照常可用（这正是价格唯一来源）——
        # 它需要网，所以换回记录桩再调。
        _install(_recording_tool)
        _reset()
        body = _wind.get_stock_data("600519.SH", "2026-01-01", "2026-02-01")
        check(
            "返回头里的取数时间是模拟日（不泄漏真实今天）",
            f"# Data retrieved on: {ASOF_DAY} 15:05:00" in body,
            [ln for ln in body.splitlines() if "retrieved" in ln][:1],
        )

    _install(_recording_tool)
    _reset()
    out = _wind.get_price_snapshots([])
    check("实时路径下 get_price_snapshots 不受影响（空入参仍返回空）", out == {})

    # ---------------------------------------------------------------- 5. 作用域语义
    section("5. 作用域：可嵌套、退出恢复、不污染实时")
    check("未设作用域时 current_asof() 为 None", current_asof() is None)
    with asof_scope("2026-01-05"):
        check("作用域内为设定值", current_asof() == "2026-01-05")
        with asof_scope("2026-02-10"):
            check("内层覆盖外层", current_asof() == "2026-02-10")
        check("内层退出后回到外层值", current_asof() == "2026-01-05")
    check("整体退出后恢复 None", current_asof() is None)
    check("ASOF 是 contextvar 而非全局 dict", hasattr(ASOF, "get") and hasattr(ASOF, "set"))

    _reset()
    _wind.get_wind_ohlcv("600519.SH", "2026-01-01", "2026-03-31")
    check("退出作用域后请求不再带 aftype", "aftype" not in CALLS[0][2], str(CALLS[0][2]))

    # ---------------------------------------------------------------- 6. bar 构造
    _load_bars_section()

    # ---------------------------------------------------------------- 7. 基准
    _benchmark_section()

    # ---------------------------------------------------------------- 8. 复权探针
    section("8. 复权探针（需网络，无网自动跳过）")
    if os.environ.get("SKIP_NETWORK_PROBE"):
        print("  ⏭  SKIP_NETWORK_PROBE 已设，跳过")
    else:
        _probe_adjustment()

    # ---------------------------------------------------------------- 9. 落盘缓存
    _cache_section()

    # ---------------------------------------------------------------- 10. 退避重试
    _retry_section()

    _bd._CACHE_DIR = saved_cache_dir
    shutil.rmtree(tmp_cache, ignore_errors=True)

    print(f"\n{'=' * 60}")
    if _FAILS:
        print(f"❌ {len(_FAILS)}/{_COUNT} 项未通过：")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


def _df(rows: list[list]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])


def _row(d, o, h, low, c, v) -> list:
    return [d, o, h, low, c, v]


def _retry_section() -> None:
    """退避重试的**判据**：重试有没有可能成功。

    这一节的来历是实测：24 只标的里 1 只返回「服务暂时不可用，请稍后重试」，
    而 ``_with_retry`` 当时只认 ``VendorRateLimitError``，于是当场放弃——那只票在回测里
    就成了"全程无行情"。**同一次实测里还有别的错也被错判**：额度耗尽的原文含"次数超限"，
    看着像限流，但它次日才重置，退避 2s/4s/8s 全是白等。

    所以判据必须是**语义**（服务端说它自己暂时不行 vs. 说你的额度用光了），
    不能是"错误文本里有没有限流字样"。
    """
    section("10. 取数退避：只重试「再试一次有可能成功」的失败")

    saved_sleep = _bd.time.sleep
    _bd.time.sleep = lambda _s: None  # 冒烟不真等
    try:
        cases = [
            ("限流（VendorRateLimitError）", VendorRateLimitError("触发限流"), True),
            ("服务暂时不可用 → 值得重试",
             VendorRejectedError("Wind stock_data/get_stock_kline 返回：服务暂时不可用，请稍后重试"), True),
            ("★ 每日额度耗尽 → 不重试（次日才重置，退避是白等）",
             VendorRejectedError("Wind stock_data/get_stock_kline 返回：单日请求次数超限"), False),
            ("参数格式不对 → 不重试（重试多少次都一样）",
             VendorRejectedError("参数格式不正确:aftype"), False),
            ("鉴权失败 → 不重试", VendorRejectedError("鉴权失败：API key 无效"), False),
            ("这只票没行情 → 不重试", NoMarketDataError("X"), False),
        ]
        for label, exc, want in cases:
            check(label, _bd._is_retryable(exc) is want, f"want={want}")

        # 行为层：可重试的失败真的被重试了、最终成功；不该重试的**一次都不多试**。
        calls = {"n": 0}

        def _flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise VendorRejectedError("服务暂时不可用，请稍后重试")
            return "ok"

        check("★ 前两次抖动、第三次成功 → 拿到结果",
              _bd._with_retry(_flaky, "试") == "ok" and calls["n"] == 3, str(calls))

        calls["n"] = 0

        def _quota():
            calls["n"] += 1
            raise VendorRejectedError("单日请求次数超限")

        try:
            _bd._with_retry(_quota, "试")
            check("★ 额度耗尽 → 立刻抛出，不做无谓退避", False, "没有抛")
        except VendorRejectedError:
            check("★ 额度耗尽 → 立刻抛出，不做无谓退避（只调了 1 次）",
                  calls["n"] == 1, f"调用 {calls['n']} 次")
    finally:
        _bd.time.sleep = saved_sleep


def _load_bars_section() -> None:
    """`build_bars_from_frame` 是纯函数——三个口径都能用可手算的数列钉死。"""
    section("6. bar 构造：代理量比 / 停牌顺延 / 上市前（纯函数）")

    # --- 代理量比：成交量取 100..700，均量恰好是整数，可以口算 ---
    days = [f"2026-03-{d:02d}" for d in range(2, 9)]  # 7 个交易日
    vols = [100, 200, 300, 400, 500, 600, 700]
    frame = _df([_row(d, 10.0, 10.5, 9.5, 10.0 + i * 0.1, v)
                 for i, (d, v) in enumerate(zip(days, vols))])
    bars = _bd.build_bars_from_frame(frame, days)

    check("7 个交易日都生成了 bar", len(bars) == 7, str(len(bars)))
    check("前 5 日无代理量比（窗口不足 → None，不猜）",
          all(bars[d].volume_ratio is None for d in days[:5]),
          str([bars[d].volume_ratio for d in days[:5]]))
    check("第 6 日 == 600 / mean(100..500) = 2.0",
          bars[days[5]].volume_ratio == 2.0, str(bars[days[5]].volume_ratio))
    check("第 7 日 == 700 / mean(200..600) = 1.75（窗口滑动，不是固定基期）",
          abs(bars[days[6]].volume_ratio - 1.75) < 1e-9, str(bars[days[6]].volume_ratio))

    # --- 停牌顺延：省略会让持仓变"无价"，归零会把浮亏算成 -100%，两者都会误导减仓决策 ---
    days2 = ["2026-04-01", "2026-04-02", "2026-04-03"]
    bars2 = _bd.build_bars_from_frame(_df([
        _row("2026-04-01", 10.0, 10.5, 9.5, 10.2, 1000),
        _row("2026-04-03", 10.4, 10.8, 10.1, 10.6, 1200),
    ]), days2)
    check("停牌日仍生成 bar（不省略）", "2026-04-02" in bars2, str(list(bars2)))
    halted = bars2["2026-04-02"]
    check("停牌 bar 标记为不可交易", halted.tradable is False, str(halted.tradable))
    check("停牌 bar 四价顺延最后有效收盘",
          (halted.open, halted.high, halted.low, halted.close) == (10.2, 10.2, 10.2, 10.2),
          str(halted))
    check("停牌 bar 无代理量比（当天根本没有成交量）", halted.volume_ratio is None)
    check("复牌日 prev_close 取停牌前的收盘（不是日历上的前一天）",
          bars2["2026-04-03"].prev_close == 10.2, str(bars2["2026-04-03"].prev_close))

    # --- 上市前：不生成 bar，调用方 get 到 None 自然跳过 ---
    days3 = ["2026-05-01", "2026-05-02", "2026-05-03"]
    bars3 = _bd.build_bars_from_frame(
        _df([_row("2026-05-02", 10.0, 11.0, 9.9, 10.5, 500)]), days3)
    check("上市前不生成 bar（不凭空顺延出一个价格）", "2026-05-01" not in bars3, str(list(bars3)))
    check("上市日起有 bar", "2026-05-02" in bars3)
    check("上市后的缺失日继续顺延",
          bars3.get("2026-05-03") is not None and bars3["2026-05-03"].tradable is False,
          str(list(bars3)))

    # --- 脏行：收盘价缺失的那行整行丢弃，退化成顺延 bar，而不是生成一个假价格 ---
    days4 = ["2026-06-01", "2026-06-02", "2026-06-03"]
    bars4 = _bd.build_bars_from_frame(_df([
        _row("2026-06-01", 10.0, 10.5, 9.5, 10.2, 100),
        _row("2026-06-02", 10.2, float("nan"), 10.0, float("nan"), 200),
        _row("2026-06-03", 10.3, 10.6, 10.2, 10.4, 300),
    ]), days4)
    check("收盘价缺失的行被丢弃（不生成假 bar）",
          bars4["2026-06-02"].tradable is False and bars4["2026-06-02"].close == 10.2,
          str(bars4.get("2026-06-02")))
    check("脏行不影响后续有效日", bars4["2026-06-03"].tradable is True, str(bars4.get("2026-06-03")))

    check("空 DataFrame → 空结果（不抛）",
          _bd.build_bars_from_frame(_df([]), ["2026-01-01"]) == {})

    # --- 转置 ---
    check("bars_on 把逐票的日期索引转成当日的逐票 bar",
          set(_bd.bars_on({"A": bars2, "B": {}}, "2026-04-01")) == {"A"},
          str(list(_bd.bars_on({"A": bars2, "B": {}}, "2026-04-01"))))
    check("bars_on 对无该日 bar 的票不硬塞",
          _bd.bars_on({"A": bars2}, "2026-09-09") == {}, str(_bd.bars_on({"A": bars2}, "2026-09-09")))


def _benchmark_section() -> None:
    """基准 = 沪深300：既是超额收益的对照，也是回测的**交易日历**。"""
    section("7. 基准与交易日历（走替身，仍离线）")
    _install(_recording_tool)
    # 这一整节断的都是"请求长什么样"，缓存命中时根本没有请求可断言。
    _clear_cache()

    _reset()
    days = _bd.trading_days("2026-03-01", "2026-03-05")
    server, tool, p = CALLS[0]
    check("交易日历走指数 K 线接口（不是个股接口）",
          (server, tool) == ("index_data", "get_index_kline"), f"{server}/{tool}")
    check("指数代码归一为 000300.SH", p["windcode"] == "000300.SH", str(p))
    # ⚠️ 这两条一度写反了：老断言是 ``period == "1d"`` 且 ``aftype == "0"``，而它们是照着
    # 一段**错的 docstring** 写出来的，从没跟真实 Wind 对过。真实 schema 以 ``index_data`` 的
    # ``tools/list`` 为准：period 与个股同一套数字口径，而且**这个工具没有 aftype 参数**。
    # 打真接口时服务端当场就拒：``Invalid value '1d' for field 'period'``。
    # 教训写在这：**冒烟脚本断的是"请求长什么样"，它会把错的口径也一起钉死。**
    check("指数 period 与个股同一套数字口径（10=日K）", p["period"] == "10", str(p))
    check("★ 指数接口根本没有 aftype 参数（它不是复权价的对照物）",
          "aftype" not in p, str(p))
    check("begin/end 原样透传",
          (p["begin_date"], p["end_date"]) == ("2026-03-01", "2026-03-05"), str(p))
    check("交易日取自指数 K 线的日期", days == ["2026-03-02", "2026-03-03"], str(days))

    _reset()
    bench = _bd.load_benchmark("2026-03-01", "2026-03-05")
    check("基准收盘序列按日期索引",
          bench == {"2026-03-02": 10.5, "2026-03-03": 10.8}, str(bench))

    _reset()
    with asof_scope(ASOF_DAY):
        _bd.trading_days("2026-01-01", "2026-03-31")
    p = CALLS[0][2]
    check("回测下基准请求里也不带 aftype（指数本就没有复权口径）",
          "aftype" not in p, str(p))
    check("回测下基准 end_date 被封顶到 asof", p["end_date"] == ASOF_DAY, str(p))
    # 缓存目录是给人排查用的：指数线没有复权口径，文件名上就不许写 hfq。
    idx_files = sorted(f.name for f in _bd._CACHE_DIR.glob("idx_*.json"))
    check("★ 基准的缓存文件名标 na 而不是 hfq（不许在文件名上撒谎）",
          len(idx_files) == 1 and idx_files[0].startswith("idx_na_000300.SH_"), str(idx_files))

    # 指数取不到时**必须抛**：降级成空日历会让回测跑完却一天都没交易，且不报错。
    _install(lambda *a, **k: {"data": {"columns": _COLS, "rows": []}})
    try:
        _bd.trading_days("2026-03-01", "2026-03-05")
        check("指数无数据时抛错（不降级为空日历）", False, "没有抛")
    except NoMarketDataError:
        check("指数无数据时抛错（不降级为空日历）", True)
    except Exception as e:  # noqa: BLE001
        check("指数无数据时抛错（不降级为空日历）", False, f"{type(e).__name__}: {e}")

    _install(_recording_tool)


def _cache_section() -> None:
    """落盘缓存：**只在 as-of 作用域内启用**，且"命中"必须与"重取"逐位等价。

    缓存是纯加速层，但它有把错误**固化下来**的能力：一份错价被缓存之后，后面每一轮回测
    都会安静地用它。所以这里的断言分两类——一类证明它真省了请求，一类证明它没有改变
    任何一个字节的结果，以及证明它**碰不到实时路径**。
    """
    section("9. 行情落盘缓存（离线，用临时目录）")
    # 临时目录是整个脚本共用的（见 ``main``），这里只需清空后从"零缓存"开始断言。
    _clear_cache()
    saved_ttl = _bd._CACHE_TTL_DAYS
    _bd._CACHE_TTL_DAYS = 30
    _install(_recording_tool)
    try:
        _cache_asserts(_bd._CACHE_DIR)
    finally:
        _bd._CACHE_TTL_DAYS = saved_ttl


def _cache_asserts(tmp: Path) -> None:  # noqa: C901 —— 冒烟脚本，线性罗列
    files = lambda: sorted(p.name for p in tmp.glob("*.json"))  # noqa: E731

    # --- 实时路径：连缓存目录都不建 ---
    _reset()
    live, _ = _bd.load_bars(["600519.SH"], ["2026-03-02"], "2026-03-02", "2026-03-05")
    check("实时取数不落盘（缓存目录整空）", files() == [], str(files()))
    check("实时取数请求里仍然没有 aftype（缓存没把实时路径改了口径）",
          "aftype" not in CALLS[0][2], str(CALLS[0][2]))
    check("实时取数照常返回 bar", bool(live.get("600519.SH")), str(list(live)))

    # --- as-of 下：第一次取、第二次命中，且结果逐位相同 ---
    _reset()
    with asof_scope(ASOF_DAY):
        b1, w1 = _bd.load_bars(["600519.SH"], ["2026-03-02", "2026-03-03"], "2026-03-02", ASOF_DAY)
    n_after_first = len(CALLS)
    check("as-of 下第一次取数真的打了接口", n_after_first == 1, str(n_after_first))
    check("取到之后落了盘", len(files()) == 1, str(files()))
    check("★ 文件名带复权口径与版本（打开目录一眼看懂，不靠反推哈希）",
          files()[0].startswith(f"stk_hfq_{'600519.SH'}_")
          and _bd._CACHE_VERSION in files()[0], files()[0])
    check("★ 前复权与后复权落在不同文件上（混用不会报错，只会静默算错收益）",
          _bd._cache_file("600519.SH", "2026-02-18", ASOF_DAY, "stk", "hfq")
          != _bd._cache_file("600519.SH", "2026-02-18", ASOF_DAY, "stk", "raw"))

    _reset()
    with asof_scope(ASOF_DAY):
        b2, w2 = _bd.load_bars(["600519.SH"], ["2026-03-02", "2026-03-03"], "2026-03-02", ASOF_DAY)
    check("★ 第二次同样的区间**零网络调用**（这是缓存的全部意义）", CALLS == [], str(CALLS))
    check("★ 命中缓存与重新取的 bar **逐位相同**", b1 == b2, f"{b1} != {b2}")
    check("命中缓存时 warnings 也一致（不该凭空多出/少掉告警）", w1 == w2, f"{w1} != {w2}")

    # --- 键：区间不同 → 不同文件；end_date 早于 asof 时，asof 不同仍应共用 ---
    with asof_scope("2026-03-06"):
        _bd.load_bars(["600519.SH"], [], "2026-03-02", "2026-03-06")
    check("换一个区间 → 另写一份缓存（不会拿旧区间顶替）", len(files()) == 2, str(files()))

    n_before = len(files())
    _reset()
    with asof_scope("2026-03-06"):
        # end_date 依旧钉在 03-05：有效窗口与上面那次完全相同，**应该**命中
        _bd.load_bars(["600519.SH"], [], "2026-03-02", ASOF_DAY)
    check("★ 键用 min(end_date, asof) 而不是 asof：有效窗口相同的两次查询共用一份",
          len(files()) == n_before and CALLS == [], f"files={files()} calls={CALLS}")

    check("个股线与指数线不共用文件（形状相同、语义不同）",
          _bd._cache_file("A", "s", "e", "stk", "hfq")
          != _bd._cache_file("A", "s", "e", "idx", "hfq"))

    # --- 坏缓存不许让回测挂掉，也不许被当成"这只票没行情" ---
    victim = sorted(tmp.glob("*.json"))[0]
    saved = victim.read_text()
    victim.write_text("{ 这不是 JSON")
    _reset()
    with asof_scope(ASOF_DAY):
        b3, _ = _bd.load_bars(["600519.SH"], ["2026-03-02", "2026-03-03"], "2026-03-02", ASOF_DAY)
    check("★ 缓存文件损坏 → 按未命中重取，不抛异常",
          len(CALLS) == 1 and b3 == b1, f"calls={len(CALLS)} eq={b3 == b1}")
    victim.write_text(saved)

    # --- TTL ---
    _reset()
    old = time.time() - 40 * 86400
    for p in tmp.glob("*.json"):
        os.utime(p, (old, old))
    with asof_scope(ASOF_DAY):
        _bd.load_bars(["600519.SH"], ["2026-03-02", "2026-03-03"], "2026-03-02", ASOF_DAY)
    check("超过 TTL 的缓存会自动重取（兜住 Wind 侧的数据修错）", len(CALLS) == 1, str(len(CALLS)))

    # --- 写入侧的两条底线 ---
    empty = tmp / "empty.json"
    _bd._write_cache(empty, pd.DataFrame())
    check("★ 空 frame 绝不落盘（否则一次瞬时故障会让这只票在 TTL 内被判成无行情）",
          not empty.exists())
    check("读一个不存在的路径返回 None（按未命中）", _bd._read_cache(tmp / "nope.json") is None)

    noClose = tmp / "noclose.json"
    noClose.write_text('{"columns":["Date"],"index":[],"data":[]}')
    check("缺 Close 列的缓存按未命中处理", _bd._read_cache(noClose) is None)

    # --- 读回来的形状必须与 get_wind_ohlcv 一致（Date 是 datetime64、升序） ---
    df = _wind.get_wind_ohlcv("600519.SH", "2026-03-01", "2026-03-05")
    path = _bd._cache_file("600519.SH", "2026-03-01", ASOF_DAY, "stk", "hfq")
    _bd._write_cache(path, df)
    back = _bd._read_cache(path)
    check("★ 往返之后 Date 仍是 datetime64（下游按日期切片凭这个）",
          back is not None and str(back["Date"].dtype).startswith("datetime64"),
          str(None if back is None else back["Date"].dtype))
    check("往返之后列与行与原 frame 一致",
          back is not None and list(back.columns) == list(df.columns) and len(back) == len(df),
          str(None if back is None else list(back.columns)))
    check("★ 往返之后 build_bars_from_frame 的结果逐位相同",
          back is not None
          and _bd.build_bars_from_frame(back, ["2026-03-02"]) == _bd.build_bars_from_frame(df, ["2026-03-02"]))


def _probe_adjustment() -> None:
    """真连一次 Wind，验证后复权序列**不随查询时刻变化**（前复权则会）。

    这是 §4.4 要求的落地前探针：后复权之所以能用，全靠"复权因子只依赖 t 之前的除权事件"。
    用真实数据确认一次，比在注释里相信它可靠。
    """
    _install(_REAL_CALL_TOOL)  # 真连一次；下面的断言只关乎价格稳定性，无关替身
    try:
        with asof_scope("2026-03-05"):
            a = _wind.get_wind_ohlcv("601398.SH", "2025-01-01", "2026-03-05", aftype=1)
            b = _wind.get_wind_ohlcv("601398.SH", "2025-01-01", "2026-03-05", aftype=1)
            c = _wind.get_wind_ohlcv("601398.SH", "2025-01-01", "2026-03-05", aftype=0)
    except Exception as e:  # noqa: BLE001 —— 无网/无 key/限流都算跳过，不算失败
        print(f"  ⏭  跳过（取数不可用：{type(e).__name__}: {e}）")
        _install(_recording_tool)
        return
    _install(_recording_tool)
    if a.empty or b.empty:
        print("  ⏭  跳过（返回空）")
        return
    check("后复权：两次查询结果完全一致", a.equals(b))
    check("后复权：序列非空且含收盘价", "Close" in a.columns and len(a) > 0)
    if not c.empty:
        # 有分红送转的票上前复权会与后复权不同；相同也无妨，只作观察不作断言。
        same = a["Close"].round(4).equals(c["Close"].round(4))
        print(f"  ℹ️  前复权与后复权同长：{len(a)} vs {len(c)}；收盘价相同：{same}")


if __name__ == "__main__":
    sys.exit(main())
