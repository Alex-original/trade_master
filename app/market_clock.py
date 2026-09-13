"""行情时钟：把「实时」与「历史某日」统一成同一种输入，让执行层只有一份代码。

**这个模块的存在理由**：历史回测最容易走歪的路是「给回测另写一套执行逻辑」。那样一写，
``_pick_tier`` / ``_trigger_satisfied`` 就会分裂成两套口径——而仓库已经明确定了规矩：
这两个函数与「监控条件」卡片同源，复制一份必然导致口径漂移（见 ``trust._trigger_satisfied``
与 ``trust._pick_tier`` 的 docstring）。

所以这里换个思路：**把「实时」表达成「历史的退化情形」**。

    实时行情 → ``Bar(open=high=low=close=现价)``

对这样一个**退化 bar**：
- 选档时喂给谓词的标量（``bar.low`` / ``bar.high``）= 现价；
- 成交价 ``fill_price(action, bar)`` 在谓词成立时也 = 现价（因为 ``min(现价, tp) == 现价``
  当且仅当 ``现价 <= tp``，而那正是 ``price_below`` 触发的前提）。

于是实时路径与今天**逐位相同**，而历史路径自动获得日内的四个价位。
执行内核因此一行都不用复制。

**两条通道的分工**（回测时）：
- 执行层（本模块的 ``MarketClock``）走**显式参数**——因为同一根 bar 要按档位取不同的价位，
  一个标量表达不了。
- 引擎数据层（``wind.py`` 的日期盲工具）走 ``contextvars``——那些是模块级 ``@tool``，
  拿不到对象图。见 ``tradingagents/asof.py``。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime

# ---------------------------------------------------------------- Bar

@dataclass(frozen=True)
class Bar:
    """一个撮合周期内的行情。实时是退化 bar（四价相同），历史是真实日线。

    ``tradable=False`` 表示该标的当日无行情（停牌/代码错/未上市）——执行层据此跳过，
    持仓保留、盯市沿用最后有效收盘。**不要**用 ``close=0`` 之类去表达停牌，
    那会让占比计算除零并把浮亏算成 -100%。
    """
    open: float
    high: float
    low: float
    close: float
    prev_close: float | None = None
    volume_ratio: float | None = None
    tradable: bool = True

    @classmethod
    def from_quote(cls, q: dict) -> Bar:
        """实时行情快照 → 退化 bar。

        ``q`` 的形状就是 ``account.get_quotes`` 的返回项：``{price, prev_close, volume_ratio}``。
        四价相同不是近似——实时路径本来就只有「现价」这一个价位可用，而现行的标量路径
        也正是拿它同时做选档与成交价。
        """
        p = float(q["price"])
        return cls(
            open=p, high=p, low=p, close=p,
            prev_close=q.get("prev_close"),
            volume_ratio=q.get("volume_ratio"),
            tradable=True,
        )


# ---------------------------------------------------------------- 实时

class LiveClock:
    """生产时钟：行情走 ``account.get_quotes``（快照 + 60s 缓存），时间取真实当前时刻。

    **刻意不做任何事**：``bars()`` 只把 ``get_quotes`` 的结果退化成 bar，``check_limit``
    返回 None 表示"不参与涨跌停判定"——实时的涨跌停/停牌判定留在 ``trade._check_limit``
    里原样执行（那里用 ``datetime.now()`` 与实时 K 线，是经过生产验证的路径）。
    这样"实时"这条路上没有任何新代码参与决策。

    判据是 **``clock is None`` ⟺ 实时**：``trade.place_order`` 只在显式传了 clock 时才走
    时钟的涨跌停判定，所以传 ``LiveClock()`` 与不传是**不同**的（前者会跳过 ``_check_limit``）。
    回测路径永远传 ``HistoryClock``，实时路径永远不传——不会混。
    """

    #: 回测编排读它来做日期归属；实时路径不读（用 datetime.now()）
    date: str | None = None

    @property
    def now(self) -> datetime:
        return datetime.now()

    @property
    def ts(self) -> float:
        return time.time()

    def bars(self, codes: list[str]) -> dict[str, Bar]:
        from app import account as account_mod

        return {c: Bar.from_quote(q) for c, q in account_mod.get_quotes(codes or []).items()}

    def check_limit(self, code: str, direction: int, fill: float | None) -> str | None:
        """返回 None = 不由本时钟判定（实时走 ``trade._check_limit`` 的原逻辑）。"""
        return None

    def with_offset(self, k: int) -> LiveClock:
        """子 tick 偏移。实时路径没有「日内多次」的概念，原样返回。"""
        return self


# ---------------------------------------------------------------- 历史

#: A股各板块涨跌幅上限。历史涨跌停价 Wind 取不到（只有无日期的时点快照接口），
#: 所以按板块静态规则近似——**这是近似，不是精确值**，结果页必须声明。
_LIMIT_ST = 0.05
_LIMIT_BJ = 0.30
_LIMIT_STAR = 0.20      # 创业板 300/301、科创板 688/689
_LIMIT_MAIN = 0.10


def limit_pct(code: str, name: str = "") -> float:
    """按板块给涨跌幅上限。0 = 无涨跌停（港股/美股/未知）。

    **回测与实时共用这一份**（``HistoryClock.check_limit`` 与 ``trade._check_limit``）：
    两边判的都是同一句话——「**成交价**是否已到/超过板价」——只是取价来源不同
    （回测取当日 bar、实时取 ``place_order`` 传入的 ``fill_price``）。

    曾经实时那边写的是「±9.5% 一刀切」，于是创业板票涨 9.6% 会被误拦、主板票
    9.5%~10% 的窗口又拦不住。改成按板块取值后这两处偏差一起消失。
    """
    c = (code or "").upper()
    if not c:
        return 0.0
    if "ST" in (name or "").upper():
        return _LIMIT_ST
    if c.endswith(".BJ"):
        return _LIMIT_BJ
    body = c.split(".")[0]
    if body.startswith(("300", "301", "688", "689")):
        return _LIMIT_STAR
    if c.endswith((".SH", ".SZ")):
        return _LIMIT_MAIN
    return 0.0


#: 旧名兼容别名——本模块内部与历史调用点都还写着 ``_limit_pct``。新代码请用公开名。
_limit_pct = limit_pct


class HistoryClock:
    """回测时钟：bar 来自一次性硬取的日线（后复权），时间固定在当日盘中。

    **时间取 10:00 而不是 09:30**：``_market_open`` 只要求 ≥09:30；取 10:00 是为了给
    「子 tick」留出偏移空间（``with_offset(k)`` 每次 +1 分钟），且避开开盘瞬间的
    撮合停滞期——与 ``trust._trigger_satisfied`` 里对集合竞价的顾虑一致。
    """

    def __init__(
        self,
        date: str,
        bars: dict[str, Bar],
        names: dict[str, str] | None = None,
        offset_minutes: int = 0,
    ) -> None:
        if not date:
            raise ValueError("HistoryClock 需要明确的交易日")
        self.date = date
        self._bars = dict(bars or {})
        self._names = dict(names or {})
        self._offset = int(offset_minutes)

    # ---- 时间 ----
    @property
    def now(self) -> datetime:
        y, m, d = (int(x) for x in self.date.split("-"))
        base = datetime.combine(datetime(y, m, d).date(), dtime(10, 0))
        return base + timedelta(minutes=self._offset)

    @property
    def ts(self) -> float:
        return self.now.timestamp()

    def with_offset(self, k: int) -> HistoryClock:
        """日内第 k 个子 tick。**只改时间，不改 date 与 bars。**

        这一点是子 tick 能工作的前提：``trust._get_today_plan(date=...)`` 与
        ``_today_trade_count(clock=...)`` 都按 ``date`` 归属，所以同一天内多次调用
        ``run_execution`` 会被算作同一天，单日笔数额度跨子 tick 累积——与实盘
        「盘中每分钟一个 tick」的语义一致。
        """
        return HistoryClock(self.date, self._bars, self._names, self._offset + int(k))

    # ---- 行情 ----
    def bars(self, codes: list[str]) -> dict[str, Bar]:
        """只返回当日有行情的标的；缺失即"当日不可交易"，执行层据此跳过。"""
        return {c: self._bars[c] for c in (codes or []) if c in self._bars}

    def all_bars(self) -> dict[str, Bar]:
        return dict(self._bars)

    # ---- 涨跌停 / 停牌 ----
    def check_limit(self, code: str, direction: int, fill: float | None) -> str | None:
        """返回拦截原因；None = 放行。

        判定口径是**「成交价是否已到/超过板价」**——与「不利方向假设」一致：
        涨停板上买不到、跌停板上卖不出。一字板天然被覆盖（此时 ``fill == open`` 即板价）。

        **已知不精确处**（结果页声明）：新股上市首日无涨跌幅限制、盘中临时停牌、
        退市整理期的特殊比例、以及四舍五入规则，都按静态规则近似。
        """
        bar = self._bars.get(code)
        if bar is None or not bar.tradable:
            return "该标的停牌或无行情，暂不可交易"
        pct = _limit_pct(code, self._names.get(code, ""))
        if not pct or fill is None or not bar.prev_close:
            return None
        prev = float(bar.prev_close)
        up = round(prev * (1 + pct), 2)
        dn = round(prev * (1 - pct), 2)
        if direction == 0 and fill >= up:
            return "该股已涨停，无法买入"
        if direction == 1 and fill <= dn:
            return "该股已跌停，无法卖出"
        return None
