"""涨跌停板块规则离线冒烟脚本（G5）。

确定性、离线：**不跑 LLM、不连数据库、不碰 Wind**——行情用假 DataFrame 顶替
``trade._wind.get_wind_ohlcv``，因此可以反复运行、秒级出结果。

覆盖：
  1. ``market_clock.limit_pct`` 的板块全表（ST / .BJ / 300 / 301 / 688 / 689 / .SH / .SZ / .HK / 空）
  2. ``trade._check_limit`` **实时分支**（``clock is None``）——判据是「成交价是否到板价」
  3. **口径变化本身**：旧代码在 9.5% 一刀切，新代码按板块。这里专门断言
     ``9.5% < 涨幅 < 板价`` 的窗口由「拦」变「放」，创业板 9.6% 的误拦消失。
  4. ``trade._check_limit`` 回测分支（传 clock）仍原样委托 ``clock.check_limit``
  5. 停牌 / 行情不足 / ``prev_close == 0`` 等边界

用法：
    .venv/bin/python scripts/smoke_limit_rules.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from app import market_clock, trade  # noqa: E402
from app.errors import ServiceError  # noqa: E402
from app.market_clock import Bar  # noqa: E402

# ---------------------------------------------------------------- 断言脚手架

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


# ---------------------------------------------------------------- 假 Wind

class FakeWind:
    """顶替 ``trade._wind``。只实现 ``_check_limit`` 用到的 ``get_wind_ohlcv``。

    ``rows`` 是 Close 序列：倒数第二根 = 昨收，倒数最后一根 = 当日价。
    ``calls`` 记账用——用来断言「港股/美股压根不去查行情」。
    """

    def __init__(self, closes=None, raises=None):
        self._closes = closes
        self._raises = raises
        self.calls = []

    def get_wind_ohlcv(self, code, start, end, period="10"):
        self.calls.append((code, period))
        if self._raises is not None:
            raise self._raises
        if self._closes is None:
            return None
        return pd.DataFrame({"Close": list(self._closes)})


_REAL_WIND = trade._wind
_FW = None


def fake_wind(closes=None, raises=None) -> FakeWind:
    """装好假 Wind 并返回它（供断言 calls）。"""
    global _FW
    _FW = FakeWind(closes, raises)
    trade._wind = _FW
    return _FW


def limit_of(code, direction, fill=None, name="", closes=(100.0, 100.0), raises=None):
    """跑一次 ``_check_limit``，返回拦截原因；放行返回 None。

    用 ``raises=""`` 传不了——异常类要真对象，所以单独留给下面的停牌用例。
    """
    fake_wind(closes, raises)
    try:
        trade._check_limit(code, direction, fill_price=fill, clock=None, name=name)
    except ServiceError as e:
        return str(e)
    return None


# ---------------------------------------------------------------- 1. 板块全表

def test_limit_pct_table() -> None:
    section("limit_pct 板块全表")
    L = market_clock.limit_pct
    cases = [
        ("600519.SH", "", 0.10, "主板 沪"),
        ("000001.SZ", "", 0.10, "主板 深"),
        ("300750.SZ", "", 0.20, "创业板 300"),
        ("301029.SZ", "", 0.20, "创业板 301"),
        ("688981.SH", "", 0.20, "科创板 688"),
        ("689009.SH", "", 0.20, "科创板 689"),
        ("830799.BJ", "", 0.30, "北交所"),
        ("600519.SH", "ST中免", 0.05, "ST（沪主板）"),
        ("000001.SZ", "*ST平安", 0.05, "ST（深主板）"),
        ("830799.BJ", "ST某某", 0.05, "ST 压过北交所板块"),
        ("00700.HK", "", 0.0, "港股不判"),
        ("AAPL", "", 0.0, "美股不判"),
        ("", "", 0.0, "空代码"),
        # 空代码恒 0：「板块」是代码的属性，没有代码就无从判板。ST 优先指的是
        # **优先于板块规则**（上一行 .BJ 已覆盖），不是优先于「有没有代码」。
        # 生产中空/非 A股 由 `_is_a_share` 在更前面就挡掉了，到不了这里。
        ("", "ST某某", 0.0, "空代码但有 ST 名（仍 0）"),
    ]
    for code, name, want, desc in cases:
        got = L(code, name)
        check(f"{desc:<16} limit_pct({code!r}, {name!r}) == {want}", got == want, f"got={got}")

    # 大小写不敏感（Wind 代码偶有小写，后端不该因此判成"无涨跌停"）
    check("小写代码 .sz 仍按板块判", L("300750.sz", "") == 0.20)
    check("小写 st 仍按 ST 判", L("600519.SH", "st中免") == 0.05)
    check("旧名 _limit_pct 别名仍可用", market_clock._limit_pct is market_clock.limit_pct)


# ---------------------------------------------------------------- 2. 实时分支

def test_live_board_rules() -> None:
    section("实时分支：按板块判板价（fill_price 参与判定）")

    # ---- 主板 10%：板价 110.00 ----
    check("主板 买 涨 10.00% → 涨停拦截",
          limit_of("600519.SH", 0, 110.00) == "该股已涨停，无法买入")
    check("主板 买 涨 9.99% → 放行",
          limit_of("600519.SH", 0, 109.99) is None)
    check("主板 卖 跌 10.00% → 跌停拦截",
          limit_of("600519.SH", 1, 90.00) == "该股已跌停，无法卖出")
    check("主板 卖 跌 9.99% → 放行",
          limit_of("600519.SH", 1, 90.01) is None)

    # ---- 创业板 20%：旧代码 9.5% 一刀切 → 9.6% 的票买不进（实打实的 bug）----
    check("创业板 买 涨 9.60% → 放行（旧代码会误拦）",
          limit_of("300750.SZ", 0, 109.60) is None)
    check("创业板 买 涨 15.00% → 仍放行（未到 20% 板）",
          limit_of("300750.SZ", 0, 115.00) is None)
    check("创业板 买 涨 20.00% → 涨停拦截",
          limit_of("300750.SZ", 0, 120.00) == "该股已涨停，无法买入")
    check("创业板 卖 跌 20.00% → 跌停拦截",
          limit_of("300750.SZ", 1, 80.00) == "该股已跌停，无法卖出")

    # ---- 科创板 20% ----
    check("科创板 买 涨 20.00% → 涨停拦截",
          limit_of("688981.SH", 0, 120.00) == "该股已涨停，无法买入")
    check("科创板 买 涨 19.99% → 放行",
          limit_of("688981.SH", 0, 119.99) is None)

    # ---- 北交所 30% ----
    check("北交所 买 涨 30.00% → 涨停拦截",
          limit_of("830799.BJ", 0, 130.00) == "该股已涨停，无法买入")
    check("北交所 买 涨 29.99% → 放行",
          limit_of("830799.BJ", 0, 129.99) is None)
    check("北交所 是 A股（受 T+1 与涨跌停约束）", trade._is_a_share("830799.BJ"))

    # ---- ST 5% ----
    check("ST 买 涨 5.00% → 涨停拦截",
          limit_of("600519.SH", 0, 105.00, name="ST中免") == "该股已涨停，无法买入")
    check("ST 买 涨 4.99% → 放行",
          limit_of("600519.SH", 0, 104.99, name="ST中免") is None)
    check("ST 名压过创业板 20%（买 涨 5% → 拦）",
          limit_of("300750.SZ", 0, 105.00, name="*ST某某") == "该股已涨停，无法买入")


def test_live_no_board() -> None:
    section("实时分支：无涨跌停的板块连行情都不查")
    for code in ("00700.HK", "AAPL", ""):
        fw = fake_wind(closes=(100.0, 200.0))
        r = None
        try:
            trade._check_limit(code, 0, fill_price=200.0, clock=None, name="")
        except ServiceError as e:
            r = str(e)
        check(f"{code or '(空)':<8} 直接放行", r is None, str(r))
        check(f"{code or '(空)':<8} 未调用 get_wind_ohlcv", fw.calls == [], str(fw.calls))


def test_live_price_source() -> None:
    section("实时分支：取价与四舍五入边界")
    # 不传 fill_price 时回退当日收盘（生产恒传，这里只验回退不炸）
    check("不传 fill_price → 回退最后一根收盘（100 → 涨停板 110 拦）",
          limit_of("600519.SH", 0, None, closes=(100.0, 110.0)) == "该股已涨停，无法买入")
    check("不传 fill_price 且当日未到板 → 放行",
          limit_of("600519.SH", 0, None, closes=(100.0, 109.99)) is None)

    # 四舍五入：昨收 10.03 → 板价 round(11.033, 2) = 11.03
    check("昨收 10.03 买 11.03 → 拦（板价取整到分）",
          limit_of("600519.SH", 0, 11.03, closes=(10.03, 11.03)) == "该股已涨停，无法买入")
    check("昨收 10.03 买 11.02 → 放行",
          limit_of("600519.SH", 0, 11.02, closes=(10.03, 11.02)) is None)
    # 昨收 100 → 跌停价 round(90.0, 2) = 90.0
    check("昨收 100 卖 90.00 → 拦（边界含等号）",
          limit_of("600519.SH", 1, 90.00) == "该股已跌停，无法卖出")


def test_live_edges() -> None:
    section("实时分支：停牌 / 行情不足 / 昨收为 0")
    r = limit_of("600519.SH", 0, 100.0, raises=trade.NoMarketDataError("no data"))
    check("停牌（NoMarketDataError）→ 提示不可交易", r == "该标的停牌或无行情，暂不可交易", str(r))
    r = limit_of("600519.SH", 0, 100.0, raises=trade.VendorError("boom"))
    check("Wind 故障（VendorError）→ 提示不可交易", r == "该标的停牌或无行情，暂不可交易", str(r))

    check("返回 None → 行情数据不足", limit_of("600519.SH", 0, 100.0, closes=None) == "行情数据不足，暂不可交易")
    check("返回空表 → 行情数据不足", limit_of("600519.SH", 0, 100.0, closes=[]) == "行情数据不足，暂不可交易")
    check("只有 1 根 K 线 → 行情数据不足",
          limit_of("600519.SH", 0, 100.0, closes=[100.0]) == "行情数据不足，暂不可交易")
    check("昨收为 0 → 不判、放行（不除零）", limit_of("600519.SH", 0, 999.0, closes=(0.0, 999.0)) is None)


# ---------------------------------------------------------------- 3. 回测分支

class SpyClock:
    """假的回测时钟：只记下被怎么调用，用来证明实时分支没有抢跑。"""

    def __init__(self, verdict=None):
        self.verdict = verdict
        self.calls = []

    def check_limit(self, code, direction, fill):
        self.calls.append((code, direction, fill))
        return self.verdict


def test_clock_delegation() -> None:
    section("回测分支：传 clock 时原样委托 clock.check_limit")
    fw = fake_wind(closes=(100.0, 999.0))
    c = SpyClock(verdict=None)
    trade._check_limit("600519.SH", 0, fill_price=110.0, clock=c, name="")
    check("委托了 clock，且参数原样透传", c.calls == [("600519.SH", 0, 110.0)], str(c.calls))
    check("委托路径不查 Wind", fw.calls == [], str(fw.calls))

    c = SpyClock(verdict="该股已涨停，无法买入")
    r = None
    try:
        trade._check_limit("600519.SH", 0, fill_price=110.0, clock=c, name="")
    except ServiceError as e:
        r = str(e)
    check("clock 返回原因 → 抛 ServiceError", r == "该股已涨停，无法买入", str(r))

    # 回测侧（HistoryClock）行为不该被这次改动碰到
    bar = Bar(open=11.0, high=11.0, low=11.0, close=11.0, prev_close=10.0)
    hc = market_clock.HistoryClock("2026-09-14", {"600519.SH": bar}, {"600519.SH": "贵州茅台"})
    check("HistoryClock 主板 10%：fill 11.00 == 板价 → 拦",
          hc.check_limit("600519.SH", 0, 11.0) == "该股已涨停，无法买入")
    check("HistoryClock 主板：fill 10.99 → 放行", hc.check_limit("600519.SH", 0, 10.99) is None)

    bar_st = Bar(open=10.5, high=10.5, low=10.5, close=10.5, prev_close=10.0)
    hc_st = market_clock.HistoryClock("2026-09-14", {"600519.SH": bar_st}, {"600519.SH": "ST中免"})
    check("HistoryClock 走 limit_pct：ST 名 → 5% 板价拦",
          hc_st.check_limit("600519.SH", 0, 10.5) == "该股已涨停，无法买入")

    # 停牌
    bar_off = Bar(open=0.0, high=0.0, low=0.0, close=0.0, prev_close=10.0, tradable=False)
    hc_off = market_clock.HistoryClock("2026-09-14", {"600519.SH": bar_off})
    check("HistoryClock 停牌 bar → 提示不可交易",
          hc_off.check_limit("600519.SH", 0, 10.0) == "该标的停牌或无行情，暂不可交易")
    hc_miss = market_clock.HistoryClock("2026-09-14", {})
    check("HistoryClock 无该票 bar → 提示不可交易",
          hc_miss.check_limit("600519.SH", 0, 10.0) == "该标的停牌或无行情，暂不可交易")


# ---------------------------------------------------------------- 4. 口径变化

def test_behavior_change_is_intentional() -> None:
    """把「改动到底动了什么」钉成断言，防止后人以为这是回归。"""
    section("口径变化：9.5% 窗口由「拦」变「放」")
    old_would_block = lambda q: q >= 0.095 * 100  # noqa: E731  旧代码：收盘/昨收 ≥ +9.5% 即拦

    for fill in (109.60, 110.00):
        blocked_now = limit_of("300750.SZ", 0, fill) is not None
        check(f"创业板 {fill} (涨 {fill - 100:.2f}%)：旧拦={old_would_block(fill)}，现拦={blocked_now}",
              blocked_now == (fill >= 120.0), f"blocked_now={blocked_now}")

    # 主板 9.5%~10% 窗口：旧代码放行，新代码也只在该价 ≥ 板价时才拦——两版在 109.99 都放行
    check("主板 109.99：新旧都放行（改的是板块取值，不是收紧主板）",
          limit_of("600519.SH", 0, 109.99) is None)
    check("主板 110.00：新旧都拦",
          limit_of("600519.SH", 0, 110.00) == "该股已涨停，无法买入")


def main() -> int:
    try:
        test_limit_pct_table()
        test_live_board_rules()
        test_live_no_board()
        test_live_price_source()
        test_live_edges()
        test_clock_delegation()
        test_behavior_change_is_intentional()
    finally:
        trade._wind = _REAL_WIND   # 还原，免得同进程后续用例受影响

    print(f"\n{'=' * 60}")
    if _FAILS:
        print(f"❌ {len(_FAILS)}/{_COUNT} 项未通过：")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
