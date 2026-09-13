"""撮合引擎离线冒烟脚本：日线触发 + 跳空 + 涨跌停 + T+1 + 子 tick。

确定性、离线：**不跑 LLM、不连数据库、不碰 Wind**——成交由替身记账，行情来自手工构造的
``HistoryClock``，直接驱动 ``app/trust._execute_plan``（回测与实盘共用的那**同一个**内核）。

覆盖：
  A. 跳空 / 触价 / 未触发（``price_below`` 与 ``price_above`` 各三例，含止损档）
  B. 实时零回归：退化 bar 路径与旧标量路径成交价逐笔一致（时刻钉在 10:00，与运行时刻无关）
  C. 时间注入：决策只读 clock，不读墙钟
  D. 涨跌停 / 停牌（走生产的 ``trade._check_limit``，只把 DB 部分换掉）
  E. T+1 与子 tick
  F. 账务恒等式（独立交叉校验）

用法：
    .venv/bin/python scripts/smoke_backtest_match.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import account as account_mod  # noqa: E402
from app import trade, trust  # noqa: E402
from app.market_clock import Bar, HistoryClock, LiveClock  # noqa: E402

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


# ---------------------------------------------------------------- 假账户
# hold=总持仓，avail=可卖（T+1 冻结的那部分不在 avail 里）。release_t1 把 avail 拉平到 hold。

STATE: dict = {"cash": 0.0, "hold": {}, "avail": {}, "cost": {}, "orders": []}


def reset(cash: float, **codes) -> None:
    """codes: code=dict(hold=, avail=, cost=)"""
    STATE["cash"] = cash
    STATE["hold"] = {c: v.get("hold", 0) for c, v in codes.items()}
    STATE["avail"] = {c: v.get("avail", v.get("hold", 0)) for c, v in codes.items()}
    STATE["cost"] = {c: v.get("cost") for c, v in codes.items()}
    STATE["orders"] = []


def _positions(codes: list[str], clock) -> list[dict]:
    """镜像 ``account.get_positions(user_id, 1, clock=clock)`` 的形状。

    盯市价用**当日 bar 的收盘**——这正是回测与实盘唯一的分歧点，也是本脚本要验的东西。
    停牌 bar（``tradable=False``）照样有价（顺延的最后收盘），与生产一致。
    """
    bars = clock.bars(codes)
    out = []
    for c in codes:
        qty = STATE["hold"].get(c, 0)
        if qty <= 0:
            continue
        bar = bars.get(c)
        px = bar.close if bar is not None else None
        out.append({
            "stock_code": c, "stock_name": c, "price": px,
            "cost_price": STATE["cost"].get(c),
            "hold_qty": qty,
            "available_qty": min(STATE["avail"].get(c, qty), qty),
            "market_value": round(px * qty, 2) if px is not None else 0.0,
        })
    return out


def _fake_place_order(user_id, code, name, direction, qty, source=0, ai_reason="",
                      price=None, ts=None, clock=None):
    """``trade.place_order`` 的替身：换个清楚的说法，只把"写库"那一段替掉。

    第一道闸直接调生产的 ``trade._check_limit``——涨跌停/停牌那条路径必须是真的被走到的，
    否则 D 段测的就只是我自己写的一个 if。成交价必须由执行内核**显式传入**（回测里
    ``place_order`` 自己那次取价是未来函数），所以 ``price is None`` 一律记为缺陷。
    """
    if price is None:
        _FAILS.append(f"{code} 下单时未显式传价（回测里禁止隐式取价）")
        print(f"  ❌ {code} 下单时未显式传价")
    trade._check_limit(code, direction, fill_price=price, clock=clock)

    amount = round(price * qty, 2)
    fee = round(amount * 0.0005, 2)
    if direction == 1:
        STATE["hold"][code] = STATE["hold"].get(code, 0) - qty
        STATE["avail"][code] = STATE["avail"].get(code, 0) - qty
        STATE["cash"] += amount - fee
    else:
        STATE["hold"][code] = STATE["hold"].get(code, 0) + qty
        # T+1：买入当日**不进**可用，日终 release_t1 才解冻。
        STATE["cash"] -= amount + fee
    STATE["orders"].append(
        {"code": code, "direction": direction, "qty": qty, "price": price,
         "amount": amount, "fee": fee, "reason": ai_reason}
    )
    return {"trade_id": "t", "order_id": "o", "stock_code": code, "stock_name": name,
            "direction": direction, "price": price, "quantity": qty,
            "amount": amount, "fee": fee}


def make_cfg(**kw) -> SimpleNamespace:
    base = dict(available_cash=STATE["cash"], risk_stop_loss_pct=None,
                risk_max_position_pct=None, risk_max_trades_day=None,
                fee_commission_rate=0.0005)
    base.update(kw)
    return SimpleNamespace(**base)


def tick(codes, plan, clock, cfg=None, budget_used=0) -> list[dict]:
    """驱动一次 ``_execute_plan``（= 实盘里的一分钟），返回新产生的委托。"""
    cfg = cfg or make_cfg()
    cfg.available_cash = STATE["cash"]
    before = len(STATE["orders"])
    trust._execute_plan(1, cfg, _positions(codes, clock), plan, budget_used=budget_used, clock=clock)
    return STATE["orders"][before:]


def release_t1() -> None:
    for c, q in STATE["hold"].items():
        STATE["avail"][c] = q


# ---------------------------------------------------------------- 用例

DAY = "2026-03-05"
CODE = "600519.SH"


def bar(o, h, low, c, prev=None, vr=None, tradable=True) -> Bar:
    return Bar(o, h, low, c, prev_close=prev, volume_ratio=vr, tradable=tradable)


def clock_at(b: Bar, offset: int = 0, code: str = CODE, name: str = "") -> HistoryClock:
    return HistoryClock(DAY, {code: b}, names={code: name} if name else None,
                        offset_minutes=offset)


class _LiveAt(LiveClock):
    """``now`` 钉死在给定时刻的 ``LiveClock``：实时路径那套行为逐位不动。

    ``bars`` 仍由用例替换、``check_limit`` 仍返回 None、``date`` 仍为 None——**只把「现在
    几点」从墙钟换成定值**。B 段验的是退化 bar 的撮合口径，与运行时刻无关；而真实墙钟会让
    它变成「只在 09:30–15:00 跑才过」的用例（统一时间闸门对价格档同样生效），夜里跑必红。
    """

    def __init__(self, hour: int, minute: int) -> None:
        self._at = datetime(2026, 3, 5, hour, minute)

    @property
    def now(self) -> datetime:
        return self._at


def exit_plan(tp: float, target: float, action: str = "reduce") -> dict:
    return {"actions": [{"code": CODE, "name": CODE, "action": action,
                         "trigger_type": "price_below", "trigger_price": tp,
                         "target_weight": target, "reason": "测试档"}]}


def entry_plan(tp: float, target: float) -> dict:
    return {"actions": [{"code": CODE, "name": CODE, "action": "add",
                         "trigger_type": "price_above", "trigger_price": tp,
                         "target_weight": target, "reason": "测试档"}]}


def main() -> int:  # noqa: C901 —— 冒烟脚本，线性罗列各场景
    account_mod.get_quotes = lambda codes: {}  # 回测路径不该碰它（碰了就是走错路）
    trade.place_order = _fake_place_order  # type: ignore[assignment]

    _a_gap_and_touch()
    _b_live_zero_regression()
    _c_clock_not_wall()
    _d_limit_and_halt()
    _e_t1_and_subticks()
    _f_accounting_identity()

    print(f"\n{'=' * 60}")
    if _FAILS:
        print(f"❌ {len(_FAILS)}/{_COUNT} 项未通过：")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


# ---------------------------------------------------------------- A

def _a_gap_and_touch() -> None:
    section("A. 跳空 / 触价 / 未触发（日内触发 + 跳空按开盘）")
    # 数字是**挑过的**：持仓 1000 股、目标占比 0.5，使得「按开盘价算整手」与「按触发价算
    # 整手」的结果相差一手以上。否则两种口径都取整到同一个数，用例就没有鉴别力——
    # 下面的「用例有鉴别力」断言就是专门守这一点的。

    # A1 触价：开盘在触发价之上，日内 low 跌破 → 按**触发价**成交
    reset(0.0, **{CODE: {"hold": 1000, "cost": 9.0}})
    o = tick([CODE], exit_plan(10.00, 0.5), clock_at(bar(10.50, 10.60, 9.95, 10.20, prev=10.10)))
    check("A1 触价：触发并成交", len(o) == 1, str(o))
    check("A1 触价：成交价 == 触发价（不是开盘价）", o and o[0]["price"] == 10.00, str(o))
    check("A1 触价：方向为卖", o and o[0]["direction"] == 1, str(o))
    check("A1 触价：数量按触发价计", o and o[0]["qty"] == 500, str(o))

    # A2 跳空低开：开盘已在触发价之下 → 按**开盘价**成交（更差的一端）
    reset(0.0, **{CODE: {"hold": 1000, "cost": 9.0}})
    t_open = trust._target_qty(0.5, 1000 * 9.90, 9.50, CODE)
    t_tp = trust._target_qty(0.5, 1000 * 9.90, 10.00, CODE)
    o = tick([CODE], exit_plan(10.00, 0.5), clock_at(bar(9.50, 9.55, 9.45, 9.90, prev=10.10)))
    check("A2 跳空：触发并成交", len(o) == 1, str(o))
    check("A2 跳空：成交价 == 开盘价", o and o[0]["price"] == 9.50, str(o))
    check("A2 跳空：成交价 != 触发价（确实跳空了）", o and o[0]["price"] != 10.00, str(o))
    check("A2 用例有鉴别力（按 open 与按 tp 的整手数不同）", t_open != t_tp,
          f"open={t_open} tp={t_tp}")
    check("A2 跳空：卖出量按开盘价计", o and o[0]["qty"] == 1000 - t_open,
          f"qty={o[0]['qty'] if o else None} expect={1000 - t_open}")

    # A3 未触发：日内最低没跌破触发价
    o = tick([CODE], exit_plan(10.00, 0.5), clock_at(bar(10.50, 10.60, 10.05, 10.30, prev=10.10)))
    check("A3 未触发：一单不下", o == [], str(o))

    # A4 涨过档触价：开盘在触发价之下，日内 high 涨过 → 按触发价
    reset(50000.0, **{CODE: {"hold": 1000, "cost": 9.0}})
    o = tick([CODE], entry_plan(11.00, 0.5), clock_at(bar(10.80, 11.20, 10.70, 11.10, prev=10.90)))
    check("A4 涨过档触价：触发并买入", len(o) == 1 and o[0]["direction"] == 0, str(o))
    check("A4 涨过档触价：成交价 == 触发价", o and o[0]["price"] == 11.00, str(o))
    check("A4 涨过档触价：数量按触发价计", o and o[0]["qty"] == 1700, str(o))

    # A5 涨过档跳空高开：开盘已在触发价之上 → 按开盘价
    reset(50000.0, **{CODE: {"hold": 1000, "cost": 9.0}})
    t_open = trust._target_qty(0.5, 50000 + 1000 * 11.55, 11.50, CODE)
    t_tp = trust._target_qty(0.5, 50000 + 1000 * 11.55, 11.00, CODE)
    o = tick([CODE], entry_plan(11.00, 0.5), clock_at(bar(11.50, 11.60, 11.40, 11.55, prev=11.00)))
    check("A5 涨过档跳空：成交价 == 开盘价", o and o[0]["price"] == 11.50, str(o))
    check("A5 用例有鉴别力", t_open != t_tp, f"open={t_open} tp={t_tp}")
    check("A5 买入量按开盘价计", o and o[0]["qty"] == t_open - 1000,
          f"qty={o[0]['qty'] if o else None} expect={t_open - 1000}")

    # A6 涨过档未触发
    o = tick([CODE], entry_plan(11.00, 0.5), clock_at(bar(10.80, 10.95, 10.70, 10.90, prev=10.60)))
    check("A6 涨过档未触发：一单不下", o == [], str(o))

    # A7 止损：日内最低破线即触发（判据是 bar.low，不是收盘价）
    reset(0.0, **{CODE: {"hold": 1000, "cost": 10.0}})
    o = tick([CODE], {"actions": []}, clock_at(bar(10.10, 10.20, 9.40, 10.05, prev=10.10)),
             cfg=make_cfg(risk_stop_loss_pct=0.05))  # 止损线 9.50
    check("A7 止损：日内最低破线即触发（收盘并未破线）", len(o) == 1, str(o))
    check("A7 止损：按止损线成交（不是最低价 9.40）", o and o[0]["price"] == 9.50, str(o))

    # A8 止损跳空低开：按开盘价成交，不假设能按止损线成交。
    # 前收取 9.50（板价 8.55）而不是 10.10：后者会让 9.00 直接破跌停线，这笔单会被
    # 涨跌停闸门拦掉，测的就成了 D 段的规则而不是这里的跳空口径。
    reset(0.0, **{CODE: {"hold": 1000, "cost": 10.0}})
    o = tick([CODE], {"actions": []}, clock_at(bar(9.00, 9.10, 8.90, 9.05, prev=9.50)),
             cfg=make_cfg(risk_stop_loss_pct=0.05))
    check("A8 止损跳空：按开盘价 9.00 成交（不是止损线 9.50）",
          o and o[0]["price"] == 9.00, str(o))


# ---------------------------------------------------------------- B

def _b_live_zero_regression() -> None:
    section("B. 实时零回归：退化 bar 路径 == 旧标量路径")
    # 退化 bar（四价相同 = 现价）。取档与成交都应回到现价，与改造前的标量写法逐位相同。
    for px in (9.00, 10.00, 10.50):
        reset(0.0, **{CODE: {"hold": 3000, "cost": 8.0}})
        b = Bar.from_quote({"price": px, "prev_close": 8.80, "volume_ratio": None})
        lc = _LiveAt(10, 0)
        lc.bars = lambda codes, _b=b: {CODE: _b}  # type: ignore[assignment]
        o = tick([CODE], exit_plan(10.00, 0.5), lc)
        triggered = px <= 10.00
        check(f"B 现价 {px} vs 触发价 10.00：{'触发' if triggered else '不触发'}",
              bool(o) == triggered, str(o))
        if triggered:
            check(f"B 现价 {px}：成交价 == 现价（退化 bar 自洽）", o[0]["price"] == px, str(o))
            tq = trust._target_qty(0.5, 3000 * px, px, CODE)
            check(f"B 现价 {px}：定仓价与成交价同源（按现价算数量）",
                  o[0]["qty"] == 3000 - tq, f"qty={o[0]['qty']} expect={3000 - tq}")

    # 闸门本身也要有断言：同一份计划、同一根 bar，**只有时刻不同** → 09:00 不下单、09:30 下单。
    # 这条把「统一时间闸门对所有触发类型生效」钉住（改动前只有 open 类有闸门，价格型档位
    # 09:26 就照单成交过——2026-09-11 早盘 518880 那笔）。
    for (h, m), want in (((9, 0), False), ((9, 30), True)):
        reset(0.0, **{CODE: {"hold": 3000, "cost": 8.0}})
        b = Bar.from_quote({"price": 9.00, "prev_close": 8.80, "volume_ratio": None})
        lcx = _LiveAt(h, m)
        lcx.bars = lambda codes, _b=b: {CODE: _b}  # type: ignore[assignment]
        o = tick([CODE], exit_plan(10.00, 0.5), lcx)
        check(f"B {h:02d}:{m:02d} 现价 9.00 已触价 → {'下单' if want else '不下单'}"
              f"（统一时间闸门 ≥09:30）", bool(o) == want, str(o))

    # LiveClock 不接管实时的涨跌停判定——那条路留在 trade._check_limit 里原样执行。
    check("B LiveClock.check_limit 返回 None（不参与涨跌停判定）",
          LiveClock().check_limit(CODE, 0, 999.0) is None)
    check("B LiveClock 的 date 为 None（实时不做日期归属）", LiveClock().date is None)


# ---------------------------------------------------------------- C

def _c_clock_not_wall() -> None:
    section("C. 时间注入：决策只读时钟，不读墙钟")
    # 两次调用**只有 clock 不同**（同一根 bar、同一份计划、同一笔持仓）：
    # 若代码还在读墙钟，两次结果会相同（都取决于真实此刻）→ 断言必然失败。
    plan = exit_plan(10.00, 0.5)
    b = bar(10.50, 10.60, 9.95, 10.20, prev=10.10)

    reset(0.0, **{CODE: {"hold": 1000, "cost": 9.0}})
    o_open = tick([CODE], plan, clock_at(b))                 # 模拟日 10:00
    reset(0.0, **{CODE: {"hold": 1000, "cost": 9.0}})
    o_pre = tick([CODE], plan, clock_at(b, offset=-60))      # 模拟日 09:00（未进连续竞价）

    check("模拟日 10:00 → 触发（不再依赖墙钟）", len(o_open) == 1, str(o_open))
    check("模拟日 09:00 → 不触发（统一时间闸门生效）", o_pre == [], str(o_pre))
    check("两者结果不同 → 证明读的是 clock.now 而非 datetime.now()",
          bool(o_open) != bool(o_pre))

    # 该日期可以落在周末：闸门只看时刻，不看星期——回测的日期轴来自交易日历，不是墙钟。
    reset(0.0, **{CODE: {"hold": 1000, "cost": 9.0}})
    sat = HistoryClock("2026-03-07", {CODE: b})  # 2026-03-07 是周六
    o_sat = tick([CODE], plan, sat)
    check("模拟日落在周六照样触发（证明用的是注入的日期而非墙钟）", len(o_sat) == 1, str(o_sat))

    # 未来计划预演：monitor_date 晚于模拟日时，时间档不加闸门（与实盘口径一致）
    check("clock.now 同时喂给 _trigger_satisfied 的 future 判断",
          trust._trigger_satisfied({"trigger_type": "none"}, None, "2099-01-01", None,
                                   clock_at(b).now) is False)


# ---------------------------------------------------------------- D

def _d_limit_and_halt() -> None:
    section("D. 涨跌停 / 停牌（走生产的 trade._check_limit）")

    # D1 一字涨停买不进：主板 10%，前收 10.00 → 板价 11.00；全天 11.00
    reset(50000.0, **{CODE: {"hold": 1000, "cost": 9.0}})
    o = tick([CODE], entry_plan(10.50, 0.5), clock_at(bar(11.00, 11.00, 11.00, 11.00, prev=10.00)))
    check("D1 一字涨停：买不进", o == [], str(o))

    # D2 一字跌停卖不出：跌停价 9.00
    reset(0.0, **{CODE: {"hold": 3000, "cost": 10.0}})
    o = tick([CODE], exit_plan(9.50, 0.5), clock_at(bar(9.00, 9.00, 9.00, 9.00, prev=10.00)))
    check("D2 一字跌停：卖不出", o == [], str(o))

    # D3 同一天、同样的深度，但只是跌了而非跌停 → 能卖（证明 D2 拦的是板价，不是"跌了"）
    reset(0.0, **{CODE: {"hold": 3000, "cost": 10.0}})
    o = tick([CODE], exit_plan(9.35, 0.5), clock_at(bar(9.30, 9.35, 9.20, 9.25, prev=10.00)))
    check("D3 跌 7.5% 未到跌停：照常卖出", len(o) == 1, str(o))

    # D4/D5 创业板 20%：300 开头，前收 10.00 → 板价 12.00
    p300 = {"actions": [{"code": "300750.SZ", "name": "300750.SZ", "action": "add",
                         "trigger_type": "price_above", "trigger_price": 11.00,
                         "target_weight": 0.5, "reason": "x"}]}
    reset(50000.0, **{"300750.SZ": {"hold": 1000, "cost": 9.0}})
    o = tick(["300750.SZ"], p300,
             clock_at(bar(11.50, 11.50, 11.50, 11.50, prev=10.00), code="300750.SZ"))
    check("D4 创业板 +15% 不是涨停，可买", len(o) == 1, str(o))
    reset(50000.0, **{"300750.SZ": {"hold": 1000, "cost": 9.0}})
    o = tick(["300750.SZ"], p300,
             clock_at(bar(12.00, 12.00, 12.00, 12.00, prev=10.00), code="300750.SZ"))
    check("D5 创业板 +20% 是涨停，买不进", o == [], str(o))

    # D6/D7 *ST 按 5%：同一根跌停 bar，名称带 ST 才拦——这条才是 5% 规则的鉴别力所在
    b_st = bar(9.50, 9.50, 9.50, 9.50, prev=10.00)
    reset(0.0, **{CODE: {"hold": 3000, "cost": 10.0}})
    o = tick([CODE], exit_plan(9.60, 0.5), clock_at(b_st, name="*ST测试"))
    check("D6 *ST 跌 5% 即跌停：卖不出", o == [], str(o))
    reset(0.0, **{CODE: {"hold": 3000, "cost": 10.0}})
    o = tick([CODE], exit_plan(9.60, 0.5), clock_at(b_st))
    check("D7 同样的价位但非 ST（板价 9.00）：卖得出", len(o) == 1, str(o))

    # D8 停牌：tradable=False → 跳过，且持仓分毫不动
    reset(0.0, **{CODE: {"hold": 3000, "cost": 9.0}})
    halted = clock_at(bar(9.10, 9.10, 9.10, 9.10, prev=9.10, tradable=False))
    o = tick([CODE], exit_plan(9.50, 0.5), halted)
    check("D8 停牌：一单不下", o == [], str(o))
    check("D8 停牌：持仓不变", STATE["hold"][CODE] == 3000, str(STATE["hold"]))
    check("D8 停牌：持仓仍有市值（顺延收盘，不归零）",
          _positions([CODE], halted)[0]["market_value"] == round(9.10 * 3000, 2),
          str(_positions([CODE], halted)))

    # D9 当日无 bar（未上市/取数失败）→ 同样跳过
    reset(0.0, **{CODE: {"hold": 3000, "cost": 9.0}})
    o = tick([CODE], exit_plan(9.50, 0.5), HistoryClock(DAY, {}))
    check("D9 当日无 bar：跳过", o == [], str(o))


# ---------------------------------------------------------------- E

def _e_t1_and_subticks() -> None:
    section("E. T+1 与子 tick")

    # E1 T+1：当日买入不进可用，同日再想卖卖不掉
    reset(50000.0, **{CODE: {"hold": 0, "cost": None}})
    day = clock_at(bar(10.00, 10.10, 9.90, 10.00, prev=9.80))
    o1 = tick([CODE], entry_plan(9.50, 0.5), day)
    check("E1 先买入成功", len(o1) == 1 and STATE["hold"][CODE] > 0, str(o1))
    check("E1 买入后可用仍为 0（T+1 冻结）", STATE["avail"].get(CODE, 0) == 0,
          str(STATE["avail"]))
    o2 = tick([CODE], exit_plan(10.50, 0.0), day)
    check("E1 同日想卖：卖不掉", o2 == [], str(o2))
    release_t1()
    check("E1 release_t1 后可用解冻", STATE["avail"][CODE] == STATE["hold"][CODE],
          str(STATE["avail"]))
    held = STATE["hold"][CODE]
    o3 = tick([CODE], exit_plan(10.50, 0.0), day)
    check("E1 解冻后能卖", len(o3) == 1, str(o3))
    check("E1 清仓档卖光全部持仓", o3 and o3[0]["qty"] == held, str(o3))

    # E2 子 tick：三档梯子，一天内走完（等价于实盘跨分钟逐档推进）
    ladder = {"actions": [
        {"code": CODE, "name": CODE, "action": "reduce", "trigger_type": "price_below",
         "trigger_price": 9.50, "target_weight": 0.60, "reason": "第1档"},
        {"code": CODE, "name": CODE, "action": "reduce", "trigger_type": "price_below",
         "trigger_price": 9.30, "target_weight": 0.30, "reason": "第2档"},
        {"code": CODE, "name": CODE, "action": "sell", "trigger_type": "price_below",
         "trigger_price": 9.10, "target_weight": 0.0, "reason": "第3档"},
    ]}
    b = bar(9.20, 9.25, 9.00, 9.05, prev=9.40)  # low 9.00 一次穿越全部三档
    base = clock_at(b)
    reset(0.0, **{CODE: {"hold": 10000, "cost": 8.0}})
    counts = []
    for k in range(3):
        counts.append(len(tick([CODE], ladder, base.with_offset(k))))
    check("E2 子 tick=3：三档逐档走完", counts == [1, 1, 1], str(counts))
    check("E2 走完后已清仓", STATE["hold"][CODE] == 0, str(STATE["hold"]))
    qtys = [o["qty"] for o in STATE["orders"]]
    check("E2 每次卖出量递减（是逐档推进，不是一笔到底）", qtys == sorted(qtys, reverse=True),
          str(qtys))
    check("E2 三笔都成交了", len(qtys) == 3, str(qtys))

    # E3 子 tick=1：只走一档，仓位卡在半途
    reset(0.0, **{CODE: {"hold": 10000, "cost": 8.0}})
    o = tick([CODE], ladder, base.with_offset(0))
    check("E3 子 tick=1：只走一档", len(o) == 1, str(o))
    check("E3 子 tick=1：仓位未清空", STATE["hold"][CODE] > 0, str(STATE["hold"]))
    check("E3 子 tick 不改变日期归属（仍是同一天）", base.with_offset(2).date == DAY,
          base.with_offset(2).date)

    # E4 单日笔数额度跨子 tick 累积：额度用尽后只放行清仓档，仓位不会被卡在半途
    reset(0.0, **{CODE: {"hold": 10000, "cost": 8.0}})
    cfg = make_cfg(risk_max_trades_day=1)
    o1 = tick([CODE], ladder, base.with_offset(0), cfg=cfg, budget_used=0)
    check("E4 额度=1：第一个子 tick 走第一档", len(o1) == 1, str(o1))
    held_before = STATE["hold"][CODE]
    o2 = tick([CODE], ladder, base.with_offset(1), cfg=cfg, budget_used=1)
    check("E4 额度用尽后只剩清仓档可走（不会卡死）", len(o2) == 1, str(o2))
    check("E4 清仓档把剩余持仓一次卖光", o2 and o2[0]["qty"] == held_before, str(o2))
    check("E4 清仓档的目标占比为 0（不消耗额度）",
          o2 and "0.0" in (o2[0]["reason"] or ""), str(o2))


# ---------------------------------------------------------------- F

def _f_accounting_identity() -> None:
    section("F. 账务恒等式（独立交叉校验）")
    reset(100000.0, **{CODE: {"hold": 2000, "cost": 9.0}})
    cash0 = STATE["cash"]
    ladder = {"actions": [
        {"code": CODE, "name": CODE, "action": "reduce", "trigger_type": "price_below",
         "trigger_price": 9.50, "target_weight": 0.30, "reason": "减仓"},
        {"code": CODE, "name": CODE, "action": "add", "trigger_type": "price_above",
         "trigger_price": 10.00, "target_weight": 0.60, "reason": "加仓"},
    ]}
    tick([CODE], ladder, clock_at(bar(10.20, 10.40, 9.30, 10.30, prev=9.50)))

    check("F 期间至少发生了一笔", len(STATE["orders"]) >= 1, str(STATE["orders"]))
    buys = sum(o["amount"] + o["fee"] for o in STATE["orders"] if o["direction"] == 0)
    sells = sum(o["amount"] - o["fee"] for o in STATE["orders"] if o["direction"] == 1)
    fees = sum(o["fee"] for o in STATE["orders"])
    expect = cash0 - buys + sells
    check("F 现金 == 期初 − 买入含费 + 卖出净额",
          abs(STATE["cash"] - expect) < 1e-6,
          f"actual={STATE['cash']} expect={expect}")
    check("F 费用全为正", fees > 0, str(fees))
    # 给这条恒等式一点牙齿：若费用被漏算，现金就不是这个数——证明它真的在约束费用。
    check("F 漏算费用会破坏该恒等式（说明这条断言有约束力）",
          abs(STATE["cash"] - (expect + fees)) > 1e-9,
          f"cash={STATE['cash']} no_fee={expect + fees}")


if __name__ == "__main__":
    sys.exit(main())
