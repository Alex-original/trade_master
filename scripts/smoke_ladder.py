"""多档触发（价格阶梯 + 量能条件）离线冒烟脚本。

确定性、离线：**不跑 LLM、不连数据库、不碰 Wind**——行情与成交都是假的，直接驱动
``app/trust._execute_plan``，因此可以反复运行、秒级出结果。

覆盖：
  1. ``app/plan_actions`` 的分档归组/排序/校验/截断
  2. ``app/trust._market_open`` 与 ``_trigger_satisfied`` 的统一时间闸门 + 量能门槛
  3. ``app/trust._pick_tier`` 的「最浅已满足且目标未达到」
  4. ``app/trust._execute_plan`` 的逐档分批推进、单日笔数额度的清仓豁免

用法：
    .venv/bin/python scripts/smoke_ladder.py
"""
import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import trade, trust  # noqa: E402
from app.plan_actions import build_ladders, sanitize_ladder_actions  # noqa: E402

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

STATE = {
    "cash": 0.0,
    "hold": {},      # code -> 股数
    "cost": {},      # code -> 成本价
    "price": {},     # code -> 现价
    "vr": {},        # code -> 量比
    "orders": [],
}


def reset(cash: float, **codes) -> None:
    """codes: code=dict(hold=, price=, cost=, vr=)"""
    STATE["cash"] = cash
    STATE["hold"] = {c: v.get("hold", 0) for c, v in codes.items()}
    STATE["cost"] = {c: v.get("cost") for c, v in codes.items()}
    STATE["price"] = {c: v.get("price") for c, v in codes.items()}
    STATE["vr"] = {c: v.get("vr") for c, v in codes.items()}
    STATE["orders"] = []


def _fake_quotes(codes):
    out = {}
    for c in codes:
        p = STATE["price"].get(c)
        if p is None:
            continue
        out[c] = {"price": p, "prev_close": None, "volume_ratio": STATE["vr"].get(c)}
    return out


def _fake_place_order(user_id, code, name, direction, qty, source=0, ai_reason="",
                      price=None, ts=None, clock=None):
    """``trade.place_order`` 的替身。**签名必须与生产同步**，否则 pytest 之外最有效的
    那道回归网会直接报 TypeError。

    它同时是一根探针：执行内核现在把成交价**显式传进来**（由 ``clock.bars()`` 的退化
    bar 经 ``fill_price`` 算出），而 ``STATE["price"]`` 是改造前标量路径的价。两者若不等，
    说明"把实时表达成历史退化情形"这条主线出了偏差——那就是零回归门要拦的东西。
    """
    ref = STATE["price"][code]
    if price is None:
        price = ref
    elif price != ref:
        _FAILS.append(f"成交价分歧 {code}: bar路径={price} != 旧标量路径={ref}")
        print(f"  ❌ 成交价分歧 {code}: bar路径={price} != 旧标量路径={ref}")
    amount = price * qty
    fee = round(amount * 0.0005, 2)
    if direction == 1:
        STATE["hold"][code] = STATE["hold"].get(code, 0) - qty
        STATE["cash"] += amount - fee
    else:
        STATE["hold"][code] = STATE["hold"].get(code, 0) + qty
        STATE["cash"] -= amount + fee
    STATE["orders"].append(
        {"code": code, "direction": direction, "qty": qty, "price": price, "reason": ai_reason}
    )
    return {
        "trade_id": "t", "order_id": "o", "stock_code": code, "stock_name": name,
        "direction": direction, "price": price, "quantity": qty, "amount": amount, "fee": fee,
    }


def _positions(codes):
    out = []
    for c in codes:
        qty = STATE["hold"].get(c, 0)
        price = STATE["price"].get(c)
        if price is None or qty <= 0:
            continue
        out.append({
            "stock_code": c, "stock_name": c, "price": price,
            "cost_price": STATE["cost"].get(c), "hold_qty": qty,
            "available_qty": qty,  # T+1 场景由测试自行覆写
            "market_value": qty * price,
        })
    return out


def make_cfg(**kw) -> SimpleNamespace:
    """托管配置的最小替身。默认值对应生产现状：风控三项都没设（NULL）。"""
    base = dict(
        available_cash=STATE["cash"],
        risk_stop_loss_pct=None,
        risk_max_position_pct=None,
        risk_max_trades_day=None,
        fee_commission_rate=0.0005,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def tick(codes, plan, cfg=None, budget_used=0):
    cfg = cfg or make_cfg()
    cfg.available_cash = STATE["cash"]  # 每次 tick 从假账户取最新现金
    before = len(STATE["orders"])
    trust._execute_plan(1, cfg, _positions(codes), plan, budget_used=budget_used)
    return STATE["orders"][before:]


# 生产真实的梯子（2026-09-11 盘前计划里的那一套）
GOLD_LADDER = [
    {"code": "518880.SH", "name": "黄金ETF华安", "action": "sell",
     "trigger_type": "price_below", "trigger_price": 8.75, "target_weight": 0.02,
     "reason": "硬止损"},
    {"code": "518880.SH", "name": "黄金ETF华安", "action": "reduce",
     "trigger_type": "price_below", "trigger_price": 8.90, "target_weight": 0.138,
     "reason": "放量跌破8.90先减约1/4"},
    {"code": "518880.SH", "name": "黄金ETF华安", "action": "reduce",
     "trigger_type": "price_below", "trigger_price": 8.76, "target_weight": 0.09,
     "reason": "有效跌破8.76–8.82再减至半仓以下"},
]
GOLD_PLAN = {"actions": GOLD_LADDER}


def main() -> int:  # noqa: C901 —— 冒烟脚本，线性罗列各场景
    trust.account_mod.get_quotes = _fake_quotes   # type: ignore[assignment]
    trade.place_order = _fake_place_order         # type: ignore[assignment]

    # ---------------------------------------------------------------- 1. 分档
    section("1. 分档归组与校验（app/plan_actions）")
    ladders, warns = build_ladders(GOLD_LADDER)
    tiers = ladders["518880.SH"]["exit"]
    check("乱序输入排成由浅到深", [t["trigger_price"] for t in tiers] == [8.90, 8.76, 8.75],
          str([t["trigger_price"] for t in tiers]))
    check("sell 的目标占比被强制为 0", tiers[2]["target_weight"] == 0.0)
    check("三档全部保留", len(tiers) == 3)
    check("合法梯子无告警", warns == [], str(warns))

    flat, _ = sanitize_ladder_actions(GOLD_LADDER)
    check("扁平输出保持由浅到深", [a["trigger_price"] for a in flat] == [8.90, 8.76, 8.75])

    old = [{"code": "600519.SH", "action": "reduce", "trigger_type": "price_below",
            "trigger_price": 1700, "target_weight": 0.05}]
    lo, wo = build_ladders(old)
    check("单档旧计划 → 1 档（向后兼容）", len(lo["600519.SH"]["exit"]) == 1 and wo == [])

    bad = [
        {"code": "X", "action": "reduce", "trigger_type": "price_below",
         "trigger_price": 10, "target_weight": 0.05},
        {"code": "X", "action": "reduce", "trigger_type": "price_below",
         "trigger_price": 9, "target_weight": 0.15},
        {"code": "X", "action": "reduce", "trigger_type": "price_below",
         "trigger_price": 8, "target_weight": 0.01},
    ]
    lb, wb = build_ladders(bad)
    check("非单调梯子截断到合法前缀", len(lb["X"]["exit"]) == 1, str(len(lb["X"]["exit"])))
    check("截断有告警", any("更激进" in w for w in wb), str(wb))

    dead = [{"code": "Y", "action": "reduce", "trigger_type": "price_below",
             "trigger_price": None, "target_weight": 0.05}]
    ld, wd = build_ladders(dead)
    check("缺触发价的死档被剔除", ld["Y"]["exit"] == [] and any("缺少触发价" in w for w in wd))

    dup = [
        {"code": "Z", "action": "reduce", "trigger_type": "price_below",
         "trigger_price": 9, "target_weight": 0.10},
        {"code": "Z", "action": "reduce", "trigger_type": "price_below",
         "trigger_price": 9, "target_weight": 0.05},
    ]
    lz, wz = build_ladders(dup)
    check("重复触发价只告警、不截断", len(lz["Z"]["exit"]) == 2 and any("重复触发价" in w for w in wz))

    both = [
        {"code": "W", "action": "reduce", "trigger_type": "price_below",
         "trigger_price": 10, "target_weight": 0.05},
        {"code": "W", "action": "buy", "trigger_type": "price_above",
         "trigger_price": 9.5, "target_weight": 0.15},
    ]
    lw, ww = build_ladders(both)
    check("双向阈值重叠 → 丢弃加仓梯子", lw["W"]["entry"] == [] and any("双向梯子冲突" in w for w in ww))
    check("冲突时保留减仓梯子", len(lw["W"]["exit"]) == 1)

    ok_both = [
        {"code": "V", "action": "reduce", "trigger_type": "price_below",
         "trigger_price": 9.0, "target_weight": 0.05},
        {"code": "V", "action": "add", "trigger_type": "price_above",
         "trigger_price": 9.5, "target_weight": 0.15},
    ]
    lv, wv = build_ladders(ok_both)
    check("双向阈值不重叠 → 两条梯子都保留",
          len(lv["V"]["exit"]) == 1 and len(lv["V"]["entry"]) == 1 and wv == [])

    again, _ = sanitize_ladder_actions(flat)
    check("幂等：再跑一次结果不变", [a["trigger_price"] for a in again] == [8.90, 8.76, 8.75])

    hold_only = [{"code": "H", "action": "hold", "trigger_type": "none", "target_weight": 0.1}]
    lh, _ = build_ladders(hold_only)
    check("纯 hold 行进 hold 桶（不进梯子）", lh["H"]["hold"] and not lh["H"]["exit"])

    # ---------------------------------------------------------------- 2. 闸门
    section("2. 统一时间闸门与量能门槛（app/trust._trigger_satisfied）")
    for hh, mm, want in [(9, 10, False), (9, 26, False), (9, 29, False), (9, 30, True), (10, 0, True)]:
        got = trust._market_open(datetime(2026, 9, 11, hh, mm))
        check(f"_market_open {hh:02d}:{mm:02d} → {want}", got is want)

    t1 = {"trigger_type": "price_below", "trigger_price": 8.90}
    saved = trust._market_open
    trust._market_open = lambda now=None: False  # type: ignore[assignment]
    check("09:30 前价格型档不触发", trust._trigger_satisfied(t1, 8.80) is False)
    trust._market_open = lambda now=None: True   # type: ignore[assignment]
    check("09:30 后价格型档触发", trust._trigger_satisfied(t1, 8.80) is True)
    check("价格未到不触发", trust._trigger_satisfied(t1, 9.20) is False)

    fut = "2099-01-01"
    trust._market_open = lambda now=None: False  # type: ignore[assignment]
    check("监控日期在未来时不加闸门（预演）", trust._trigger_satisfied(t1, 8.80, fut) is True)
    trust._market_open = saved  # type: ignore[assignment]

    t2 = {"trigger_type": "price_below", "trigger_price": 8.90, "volume_ratio_min": 1.5}
    # 量能档同样受统一时间闸门约束（闸门对**所有**触发类型生效），所以这里也要挡掉，
    # 否则本段变成「只在 09:30–15:00 跑才过」——夜里跑必红，而它验的是量比逻辑。
    trust._market_open = lambda now=None: True  # type: ignore[assignment]
    check("量能不足 → 不触发", trust._trigger_satisfied(t2, 8.80, None, 1.2) is False)
    check("量能满足 → 触发", trust._trigger_satisfied(t2, 8.80, None, 1.8) is True)
    check("量比缺失 → 不触发（宁可不做）", trust._trigger_satisfied(t2, 8.80, None, None) is False)
    trust._market_open = saved  # type: ignore[assignment]

    # ---------------------------------------------------------------- 3. 挑档
    section("3. 挑档规则（app/trust._pick_tier）")
    tiers = build_ladders(GOLD_LADDER)[0]["518880.SH"]["exit"]
    trust._market_open = lambda now=None: True  # type: ignore[assignment]
    pick = lambda w, p=8.70: trust._pick_tier(tiers, "exit", p, None, w)  # noqa: E731
    check("18.4% 时挑第 1 档", pick(0.184)["trigger_price"] == 8.90)
    check("13.8% 时第 1 档已达 → 挑第 2 档", pick(0.138)["trigger_price"] == 8.76)
    check("9.0% 时挑第 3 档（清仓）", pick(0.09)["trigger_price"] == 8.75)
    check("已清仓 → 无档可挑", pick(0.0) is None)
    check("价格未跌破 → 无档可挑", pick(0.184, 9.20) is None)

    # ---------------------------------------------------------------- 4. 执行
    section("4. 逐档分批执行（app/trust._execute_plan）")
    reset(381280.0, **{"518880.SH": {"hold": 9500, "price": 8.85, "cost": 9.0, "vr": 1.8}})
    codes = ["518880.SH"]

    o = tick(codes, GOLD_PLAN)
    check("tick1 只有一笔", len(o) == 1, str(o))
    check("tick1 卖到第 1 档约 13.8%", o and o[0]["qty"] == 2300 and o[0]["direction"] == 1,
          str(o[0] if o else None))

    STATE["price"]["518880.SH"] = 8.70
    o = tick(codes, GOLD_PLAN)
    check("tick2（价格停 8.70）再推进一档", len(o) == 1 and o[0]["qty"] == 2400, str(o))

    o = tick(codes, GOLD_PLAN)
    check("tick3 推进到清仓档", len(o) == 1 and o[0]["qty"] == 4800, str(o))
    check("清仓后持仓归零", STATE["hold"]["518880.SH"] == 0)

    o = tick(codes, GOLD_PLAN)
    check("tick4 无持仓 → 不再动作", o == [], str(o))

    # 价格回抽：目标已达，不该重复卖
    reset(381280.0, **{"518880.SH": {"hold": 7000, "price": 8.85, "cost": 9.0, "vr": 1.8}})
    o = tick(["518880.SH"], GOLD_PLAN)
    check("已在 13.8% 附近时第 1 档不再重复卖", len(o) <= 1, str(o))
    before = STATE["hold"]["518880.SH"]
    STATE["price"]["518880.SH"] = 8.95
    o = tick(["518880.SH"], GOLD_PLAN)
    check("价格回抽到 8.95 → 不追涨回来", o == [], str(o))
    check("持仓不变", STATE["hold"]["518880.SH"] == before)

    # 量能门槛：同一份计划加 volume_ratio_min
    vr_plan = {"actions": [dict(GOLD_LADDER[1], volume_ratio_min=1.5)]}
    reset(381280.0, **{"518880.SH": {"hold": 9500, "price": 8.85, "cost": 9.0, "vr": 1.2}})
    o = tick(["518880.SH"], vr_plan)
    check("放量门槛未达 → 不下单", o == [], str(o))
    STATE["vr"]["518880.SH"] = 1.8
    o = tick(["518880.SH"], vr_plan)
    check("放量门槛达成 → 下单", len(o) == 1, str(o))
    reset(381280.0, **{"518880.SH": {"hold": 9500, "price": 8.85, "cost": 9.0}})
    o = tick(["518880.SH"], vr_plan)
    check("量比取不到 → 不下单", o == [], str(o))

    # 时间闸门：闸门关闭时整条计划不动
    reset(381280.0, **{"518880.SH": {"hold": 9500, "price": 8.85, "cost": 9.0}})
    trust._market_open = lambda now=None: False  # type: ignore[assignment]
    o = tick(["518880.SH"], GOLD_PLAN)
    check("09:30 前整条梯子都不动", o == [], str(o))
    trust._market_open = lambda now=None: True   # type: ignore[assignment]

    # ---------------------------------------------------------------- 5. 额度
    section("5. 单日笔数额度：清仓档不占额度")
    A = {"code": "518880.SH", "action": "reduce", "trigger_type": "price_below",
         "trigger_price": 8.90, "target_weight": 0.138}          # 非清仓
    B_close = {"code": "600519.SH", "action": "reduce", "trigger_type": "price_below",
               "trigger_price": 105, "target_weight": 0.0}       # 清仓
    B_open = dict(B_close, target_weight=0.05)                   # 非清仓

    def two_holds():
        reset(400000.0,
              **{"518880.SH": {"hold": 9500, "price": 8.85, "cost": 9.0},
                 "600519.SH": {"hold": 1000, "price": 100.0, "cost": 100.0}})
        return ["518880.SH", "600519.SH"]

    cfg = make_cfg(risk_max_trades_day=1)
    o = tick(two_holds(), {"actions": [A, B_close]}, cfg)
    check("额度用尽后清仓档仍然成交", len(o) == 2 and o[1]["code"] == "600519.SH", str(o))
    check("清仓档确实清空", STATE["hold"]["600519.SH"] == 0)

    o = tick(two_holds(), {"actions": [A, B_open]}, cfg)
    check("额度用尽后非清仓档被拦", len(o) == 1 and o[0]["code"] == "518880.SH", str(o))

    cfg = make_cfg(risk_max_trades_day=2)
    o = tick(two_holds(), {"actions": [A, B_open]}, cfg)
    check("额度 2 时两笔非清仓都放行", len(o) == 2, str(o))

    o = tick(two_holds(), {"actions": [A, B_close]}, cfg, budget_used=2)
    check("入场即额度用尽：只放行清仓档", len(o) == 1 and o[0]["code"] == "600519.SH", str(o))

    # 止损不占额度：止损与清仓档同 tick 并存
    reset(400000.0,
          **{"518880.SH": {"hold": 9500, "price": 8.85, "cost": 9.0},
             "600519.SH": {"hold": 1000, "price": 100.0, "cost": 100.0}})
    cfg = make_cfg(risk_max_trades_day=1, risk_stop_loss_pct=0.05)
    STATE["price"]["600519.SH"] = 90.0  # 跌破成本 5% → 止损
    o = tick(["518880.SH", "600519.SH"], {"actions": [A]}, cfg)
    check("止损触发", any(x["code"] == "600519.SH" for x in o), str(o))
    check("止损不占额度，减仓档仍放行", len(o) == 2, str(o))

    # ---------------------------------------------------------------- 6. 边界
    section("6. 边界")
    # T+1：可用不足时只卖可用部分，下一 tick 继续
    reset(381280.0, **{"518880.SH": {"hold": 9500, "price": 8.85, "cost": 9.0}})
    cfg = make_cfg()
    pos = _positions(["518880.SH"])
    pos[0]["available_qty"] = 500  # 只有 500 股可卖
    trust._execute_plan(1, cfg, pos, GOLD_PLAN)
    check("T+1 可用不足时只卖可用部分", STATE["hold"]["518880.SH"] == 9000, str(STATE["hold"]))
    o = tick(["518880.SH"], GOLD_PLAN)
    check("下一 tick 继续卖（目标未达到）", len(o) == 1, str(o))

    # 止损优先于计划
    reset(400000.0, **{"518880.SH": {"hold": 9500, "price": 8.60, "cost": 9.5}})
    cfg = make_cfg(risk_stop_loss_pct=0.05)
    o = tick(["518880.SH"], GOLD_PLAN, cfg)
    check("止损优先，清仓而非只减到 13.8%", len(o) == 1 and o[0]["qty"] == 9500, str(o))
    check("理由标记为止损", "止损" in (o[0]["reason"] if o else ""), str(o))

    # 空仓起步走 entry 梯子（用 4 元的宽基 ETF：50 万的 10% 能落在一手以上）
    reset(500000.0, **{"510300.SH": {"hold": 0, "price": 4.0, "cost": None}})
    entry_plan = {"actions": [
        {"code": "510300.SH", "name": "沪深300ETF", "action": "buy",
         "trigger_type": "none", "target_weight": 0.10, "reason": "建仓"},
    ]}
    o = tick([], entry_plan)
    check("空仓起步按 entry 梯子建仓", len(o) == 1 and o[0]["direction"] == 0, str(o))
    check("建仓量约 10%", STATE["hold"]["510300.SH"] == 12500, str(STATE["hold"]))

    # 单票上限仍生效
    reset(500000.0, **{"510300.SH": {"hold": 0, "price": 4.0, "cost": None}})
    cfg = make_cfg(risk_max_position_pct=0.05)
    tick([], entry_plan, cfg)
    check("单票上限截断建仓量",
          STATE["hold"]["510300.SH"] * 4.0 <= 0.05 * 500000 + 1, str(STATE["hold"]))

    # 有持仓时不为计划外的新标的开仓（沿用原行为）
    reset(400000.0, **{"518880.SH": {"hold": 9500, "price": 8.85, "cost": 9.0}})
    o = tick(["518880.SH"], {"actions": [dict(entry_plan["actions"][0])]})
    check("有持仓时不为计划外新标的开仓", o == [], str(o))

    # ---------------------------------------------------------------- 7. 报告
    section("7. 报告渲染（app/plan_report）")
    from app.plan_report import render_plan_report  # noqa: PLC0415

    pos = [{"stock_code": "518880.SH", "stock_name": "黄金ETF华安", "price": 8.85,
            "cost_price": 9.0, "hold_qty": 9500, "available_qty": 9500,
            "market_value": 84075.0, "assets_ratio": 0.184}]
    html = render_plan_report(
        {"actions": GOLD_LADDER, "cash_target": 0.3, "summary": "测试", "risk_notes": ""},
        pos, {}, 373000.0, "2026-09-14", 1789000000.0,
    )
    check("报告里三档按由浅到深编号",
          "1.</span> 跌破 8.9 → 13.8%" in html and "3.</span> 跌破 8.75 → 0.0%" in html,
          "")
    check("目标占比列显示档位序列", "13.8% → 9.0% → 0.0%" in html)
    check("标了共几档", "共 3 档" in html)

    # 向后兼容：单档计划的渲染与历史逐字一致（无编号、无档数徽标）
    one = [dict(GOLD_LADDER[1])]
    html1 = render_plan_report({"actions": one}, pos, {}, 373000.0, "2026-09-14", 1.0)
    check("单档计划不加编号", "1.</span>" not in html1)
    check("单档计划不标档数", "共 1 档" not in html1)
    check("单档触发条件逐字保留", "跌破 8.9" in html1)

    # 量能门槛写进触发条件列
    vr = [dict(GOLD_LADDER[1], volume_ratio_min=1.5)]
    html_vr = render_plan_report({"actions": vr}, pos, {}, 373000.0, "2026-09-14", 1.0)
    check("量能门槛渲染进触发条件", "跌破 8.9（量比≥1.5）" in html_vr)

    # 校验告警如实列出（写库时落在 process.ladder_warnings，报告也要读）
    bad = [dict(GOLD_LADDER[1]), dict(GOLD_LADDER[1], trigger_price=8.95, target_weight=0.2)]
    html_w = render_plan_report(
        {"actions": bad, "process": {"ladder_warnings": ["518880.SH：第 2 档目标占比高于前档，已截断"]}},
        pos, {}, 373000.0, "2026-09-14", 1.0,
    )
    check("被截断的档在报告里显式列出", "已按由浅到深截断" in html_w and "第 2 档" in html_w)

    # ---------------------------------------------------------------- 汇总
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
