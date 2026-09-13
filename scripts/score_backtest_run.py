"""托管团队**能力**打分：跑完一轮后回答"这个团队值不值得用"。

与 ``verify_backtest_run.py`` 的分工：那个查"记录自不自洽"（管线可信否），这个查"决策好不好"
（能力行不行）。两者不可互相替代——一轮自洽到完美的记录，完全可能是团队每天写满 38 条
「不主动建仓」。

**数据来源只用 step 快照**（``plan_json`` / ``positions_json`` / ``trades_json``），不读
``trust_plans`` 表：影子账户跨 run 复用，后起的 run 会覆盖计划行，读表会把"上一轮的决策"
当成这一轮的来打分。快照是 run 自己的，跑完就不会变。

六条判据（方案 §4 阶段 D）：

  1. **覆盖率**：每个在管标的是否都有结论；``process.research_missing`` 是否为空。
  2. **依据性**：每条动作的 ``reason`` 是否真的写了理由；动作代码是否都能在研究里找到；
     ``risk_notes`` 这类字段有没有退化成噪声（实测出现过 ``【错因"].`` 这种纯垃圾）。
  3. **梯子合理性**：触发价相对当日收盘的距离。**触发价站在反侧**（``price_below`` 却高于
     收盘 = 开盘即触发）与 **不可达**（>30%）都要点名。
  4. **风控纪律**：冻结在 config 里的 ``risk_*`` 三条是否真被遵守。
  5. **前后一致**：同一标的连续两天是否无理由反手（今天买、明天卖）。
  6. **现金纪律**：是不是"永远不敢下手"——``cash_target`` 长期贴 100% 且买单全部不可达。

用法（在 product/trade_master 下）：
    .venv/bin/python scripts/score_backtest_run.py --run-id 2 --owner-user-id 1
    .venv/bin/python scripts/score_backtest_run.py --run-id 2 --owner-user-id 1 --no-price
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402

ENTER = ("buy", "add")
EXIT = ("sell", "reduce")
_FAR = 0.30  # 触发价离收盘超过 30% 视为不可达档


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def score_run(run_id: int, owner: int, with_price: bool = True) -> dict:
    session = db.get_session()
    try:
        r = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        if r is None:
            raise SystemExit(f"run {run_id} 不存在")
        cfg = json.loads(r.config_json or "{}")
        universe = json.loads(r.universe_json or "[]")
        run_start, run_end = r.start_date, r.end_date
        steps = (
            session.query(db.BacktestStep)
            .filter(db.BacktestStep.run_id == run_id)
            .order_by(db.BacktestStep.trade_date)
            .all()
        )
        rows = [
            {
                "date": s.trade_date,
                "cash": float(s.cash or 0.0),
                "mv": float(s.market_value or 0.0),
                "total": float(s.total_assets or 0.0),
                "positions": json.loads(s.positions_json or "[]"),
                "trades": json.loads(s.trades_json or "[]"),
                "plan": json.loads(s.plan_json or "{}") or {},
                "status": s.status,
            }
            for s in steps
        ]
    finally:
        session.close()

    out: dict = {"run_id": run_id, "days": len(rows), "universe": len(universe),
                 "criteria": {}, "notes": []}

    plan_rows = [x for x in rows if (x["plan"].get("actions") or [])]
    first_day = rows[0]["date"] if rows else ""

    # ------------------------------------------------------------ 1 覆盖率
    # **判据是「当天有没有对这只票表过态」，不是「研究快照里有几条」**。``process.research``
    # 是 ``collect_research_snapshot`` 按**计划动作涉及到的 code ∪ 持仓**截取的快照，
    # 天生等于「涉及标的」数（实测逐日恒等），拿它比标的池会把正常情况报成覆盖不足。
    # 真正会漏的是另一件事：某天的计划**根本没给剩下的票任何结论**——连 ``hold`` 都没有。
    # 那才是"团队漏看了一半仓位"，所以按当天动作覆盖到的**去重标的数**比。
    cov_missing: list[str] = []
    cov_short: list[str] = []
    cov_detail: list[str] = []
    for x in plan_rows:
        proc = x["plan"].get("process") or {}
        miss = list(proc.get("research_missing") or [])
        codes = {a.get("code") for a in (x["plan"].get("actions") or []) if a.get("code")}
        if miss:
            cov_missing.append(f"{x['date']}:{len(miss)}")
        if universe and len(codes) < len(universe):
            cov_short.append(f"{x['date']}:{len(codes)}/{len(universe)}")
        cov_detail.append(
            f"{x['date']} 表态 {len(codes)}/{len(universe)} 只，研究缺口 {len(miss)} 只"
        )
    cov = 100.0
    if plan_rows:
        short_days = {d.split(":")[0] for d in cov_short}
        miss_days = {d.split(":")[0] for d in cov_missing}
        cov = max(0.0, 100.0 * (1 - len(short_days | miss_days) / len(plan_rows)))
    out["criteria"]["覆盖率"] = {"score": cov, "detail": cov_detail,
                                 "missing": cov_missing, "short": cov_short}

    # ------------------------------------------------------------ 2 依据性
    acts = [a for x in plan_rows for a in (x["plan"].get("actions") or [])]
    thin = [a for a in acts if len((a.get("reason") or "").strip()) < 10]
    no_research: list[str] = []
    for x in plan_rows:
        res = (x["plan"].get("process") or {}).get("research") or {}
        for a in (x["plan"].get("actions") or []):
            if a.get("code") not in res:
                no_research.append(f"{x['date']}/{a.get('code')}")
    junk_risk = [
        f"{x['date']}: {x['plan'].get('risk_notes')!r}"
        for x in plan_rows
        if x["plan"].get("risk_notes") and len(str(x["plan"]["risk_notes"])) < 20
    ]
    # ⚠️ "研究里找不到对应标的"这条**没有鉴别力**，要如实标出：``process.research`` 是按
    # 「计划动作涉及到的 code ∪ 持仓」截取的快照，所以它**恒等**于动作涉及的标的集（覆盖率
    # 那一段记着同一个坑）。留着它只作"计划里带了研究快照"的存在性检查，**不参与扣分**。
    # 真正有鉴别力的是下面这条：**理由里有没有一个具体的东西**——价位（带小数的数字）或
    # 指标名。两者都没有 = "凭感觉/凭名称下注"，那才是要抓的（计划书里明令不接受）。
    _LEVEL_RE = re.compile(
        r"布林|均线|日线|周线|月线|年线|ma\d+|ATR|atr|新高|新低|缺口|放量|缩量|"
        r"支撑|压力|前高|前低|颈线|下轨|中轨|上轨|金叉|死叉|背离"
    )
    _NUM_RE = re.compile(r"\d+\.\d+")

    def _is_abstract(a: dict) -> bool:
        # ⚠️ **只对"要动手"的动作判**（buy/add/sell/reduce）。``hold`` 不需要引价位——
        # 尤其"维持 0% 仓"这类理由，引用的是**数据闸门**（"跟踪指数/IOPV/规模未核实"），
        # 那正是它**不该下注**的依据，把它们判成"凭感觉"是彻头彻尾的误报（实测 4 条全是这类）。
        #
        # ⚠️ 方向**必须从 ``action`` 字段判**，不能用 ``kind``：``kind`` 是监控接口由
        # ``build_ladders`` **现算**出来的，**并不存在于落库的 plan_json 里**（实测落库的
        # 动作只有 action/code/name/reason/target_weight/trigger_price/trigger_type/volume_ratio_min）。
        # 用 ``a.get("kind")`` 会永远得到 None，判据静默失效——本文件第 303/305 行的
        # 「前后一致」一直是用 ``action in ENTER/EXIT`` 的，那才是对的。
        if a.get("action") not in ENTER + EXIT:
            return False
        r = (a.get("reason") or "").strip()
        return not (_NUM_RE.search(r) or _LEVEL_RE.search(r))

    abstract = [a for a in acts if _is_abstract(a)]
    reas = [len((a.get("reason") or "").strip()) for a in acts]
    evid = 100.0
    if acts:
        evid = 100.0 * (len(acts) - len(thin)) / len(acts)
        # 抽象理由（既没有价位也没有指标名）扣分，每条 2 分、封顶 40
        evid -= min(40.0, 2.0 * len(abstract))
        evid = max(0.0, evid)
    out["criteria"]["依据性"] = {
        "score": evid,
        "detail": [f"动作 {len(acts)} 条，理由中位长度 {int(statistics.median(reas)) if reas else 0} 字",
                   f"理由过短(<10字) {len(thin)} 条",
                   f"★ 抽象理由（既无价位也无指标名）{len(abstract)} 条"
                   + ("（= 凭感觉下注，计划书明令不接受）" if abstract else ""),
                   f"疑似退化的 risk_notes {len(junk_risk)} 天",
                   f"（参考）研究快照里找不到对应标的的动作 {len(no_research)} 条"
                   "——此项无鉴别力，仅供存在性检查，不参与扣分"],
        "thin": [f"{a.get('code')}: {a.get('reason')!r}" for a in thin[:5]],
        "abstract": [f"{a.get('code')}: {a.get('reason')!r}" for a in abstract[:8]],
        "no_research": no_research[:5],
        "junk_risk": junk_risk[:5],
    }

    # ------------------------------------------------------------ 3 梯子合理性
    ladder_detail: list[str] = []
    wrong_side: list[str] = []
    unreachable: list[str] = []
    dists: list[float] = []
    ladder_score = None
    if with_price and plan_rows:
        # **参照价必须是「研究日收盘」，即执行日的前一交易日**。计划是在研究日日终用那天的
        # 收盘价做出来的（``run_portfolio_plan_for_user(next_date, research_date=d)``），
        # 所以档位是相对**d 日收盘**埋的。拿执行日 d+1 自己的收盘去比，就把"隔夜/当日涨跌
        # 把触发价甩到另一侧"误报成"梯子埋反了"——那是行情走出来的，不是团队写错的。
        from tradingagents.asof import asof_scope

        from app import backtest_data as bd
        try:
            with asof_scope(run_end):
                all_days = bd.trading_days(run_start, run_end)
                all_bars, _w = bd.load_bars(
                    universe or sorted({a.get("code") for x in plan_rows
                                        for a in (x["plan"].get("actions") or [])}),
                    all_days, run_start, run_end,
                )
        except Exception as e:  # noqa: BLE001
            all_days, all_bars = [], {}
            ladder_detail.append(f"取行情失败，梯子一条不给分：{type(e).__name__}: {e}")

        for x in plan_rows:
            if x["date"] not in all_days:
                continue
            i = all_days.index(x["date"])
            if i == 0:
                ladder_detail.append(f"{x['date']} 是区间首日，没有研究日，跳过")
                continue
            ref_date = all_days[i - 1]
            ref_bars = bd.bars_on(all_bars, ref_date)
            n_priced = 0
            for a in (x["plan"].get("actions") or []):
                tp = a.get("trigger_price")
                tt = a.get("trigger_type") or ""
                if not tp or tt not in ("price_below", "price_above"):
                    continue
                bar = ref_bars.get(a.get("code") or "")
                if bar is None or not bar.close:
                    continue
                close = float(bar.close)
                tp = float(tp)
                n_priced += 1
                dist = (tp - close) / close
                dists.append(abs(dist))
                if tt == "price_below" and dist > 0:
                    wrong_side.append(
                        f"{x['date']}/{a['code']} 跌破档 {tp} 高于研究日({ref_date})收盘 {close}"
                        "（开盘即已满足 = 实质是市价了结，不是价格档）"
                    )
                if tt == "price_above" and dist < 0:
                    wrong_side.append(
                        f"{x['date']}/{a['code']} 突破档 {tp} 低于研究日({ref_date})收盘 {close}"
                        "（开盘即已满足 = 实质是市价建仓）"
                    )
                if abs(dist) > _FAR:
                    unreachable.append(
                        f"{x['date']}/{a['code']} 触发价距研究日收盘 {_pct(dist)}"
                    )
            ladder_detail.append(f"{x['date']} 带价档 {n_priced} 条（参照 {ref_date} 收盘）")
        n = len(dists)
        if n:
            ladder_score = 100.0 * (n - len(wrong_side) - len(unreachable)) / n
            ladder_score = max(0.0, ladder_score)
            ladder_detail.append(
                f"距离中位 {_pct(statistics.median(dists))}，"
                f"最大 {_pct(max(dists))}，>30% 的 {len(unreachable)} 条，共 {n} 条带价档"
            )
    out["criteria"]["梯子合理性"] = {
        "score": ladder_score,
        "detail": ladder_detail or ["（未取价）"],
        "wrong_side": wrong_side[:8],
        "unreachable": unreachable[:8],
        "n_priced": len(dists),
    }

    # ------------------------------------------------------------ 4 风控纪律
    # **首日的持仓是「继承来的」，不是团队做出来的**。回测首日没有前置研究日、因此没有计划、
    # 因此结构上无法成交（``_run_execution`` 的 ``no_plan_backtest`` 早退），团队那天**没有
    # 任何手段**去把一只超配的票减下来。把首日的继承状态记成团队的违例，等于用起跑线的位置
    # 扣分——实测 run 4 的三条违例全部落在首日（两条超配 + 一条由 F1 复权口径造成的假浮亏），
    # 于是"风控纪律 40 分"读起来像团队失职，实际那天团队一句话都没说。
    #
    # 所以首日的**持仓类**违例单列（照 ``梯子合理性`` 里"首日没有研究日，跳过"的同一先例），
    # 不进扣分；成交笔数上限照旧全量判——它在首日天然为 0，豁免与否都不改变结果，留着更省心。
    viol: list[str] = []
    inherited: list[str] = []
    mx_pos = cfg.get("risk_max_position_pct")
    mx_trd = cfg.get("risk_max_trades_day")
    stop = cfg.get("risk_stop_loss_pct")
    for x in rows:
        is_first = x["date"] == first_day
        if mx_trd and len(x["trades"]) > float(mx_trd):
            viol.append(f"{x['date']} 当日成交 {len(x['trades'])} 笔 > 上限 {mx_trd}")
        if mx_pos and x["total"] > 0:
            for p in x["positions"]:
                w = float(p.get("market_value") or 0.0) / x["total"]
                if w > float(mx_pos) + 1e-6:
                    msg = (f"{x['date']} {p.get('stock_code')} 权重 {_pct(w)} "
                           f"> 上限 {_pct(float(mx_pos))}")
                    (inherited if is_first else viol).append(
                        f"{msg}（首日·起始状态，当天无可执行计划）" if is_first else msg
                    )
        if stop:
            for p in x["positions"]:
                cp, px = p.get("cost_price"), p.get("price")
                if cp and px and float(cp) > 0:
                    loss = (float(px) - float(cp)) / float(cp)
                    if loss < -abs(float(stop)):
                        msg = (f"{x['date']} {p.get('stock_code')} 浮亏 {_pct(loss)} 超过止损 "
                               f"{_pct(-abs(float(stop)))} 仍持仓")
                        (inherited if is_first else viol).append(
                            f"{msg}（首日·起始状态，当天无可执行计划）" if is_first else msg
                        )
    # **三条限值一条都没配时不给分**。给 100 是"没配限值 = 没违规"的假绿灯——而"用户没设
    # 风控"恰恰是这一条最该说的话。宁可显式地"无法评估"，也不要一个看起来满分的空结论。
    if not any(v is not None for v in (mx_pos, mx_trd, stop)):
        risk_score = None
        risk_detail = ["config 里 risk_max_trades_day / risk_max_position_pct / "
                       "risk_stop_loss_pct **三条都是空的**——这一轮无从评估风控纪律",
                       "（不是本轮团队的失误，是发起回测时托管配置里没有限值）"]
    else:
        risk_score = max(0.0, 100.0 - 20.0 * len(viol))
        risk_detail = [f"config: 单日最大成交 {mx_trd}、单票最大权重 {mx_pos}、止损 {stop}",
                       f"违例 {len(viol)} 条"]
        if inherited:
            risk_detail.append(
                f"另有 {len(inherited)} 条落在首日起始状态（不计分：那天没有可执行计划）"
            )
    out["criteria"]["风控纪律"] = {
        "score": risk_score, "detail": risk_detail, "violations": viol[:8],
        "inherited_violations": inherited[:8],
    }

    # ------------------------------------------------------------ 5 前后一致
    # **比的是「净倾向」，不是「有没有两侧档」**。梯子天然双面：一个 code 当天既有 entry 档
    # 又有 exit 档是设计（``build_ladders`` 的 exit/entry/hold 三组），所以"看有没有两侧"
    # 会把几乎每天都判成双向（实测 24/25 天如此），这一条就退化成 0 组对比的满分——正是
    # "看起来通过、其实没测"的假绿灯。真正的倾向在**力度**上：把当天的 entry 档目标权重
    # 与 exit 档目标权重各自求和再相减，符号就是这天对这只票的净倾向。
    seq: dict[str, list[tuple[str, str]]] = {}
    for x in plan_rows:
        net: dict[str, float] = {}
        for a in (x["plan"].get("actions") or []):
            act = a.get("action") or ""
            w = float(a.get("target_weight") or 0.0)
            if act in ENTER:
                net[a.get("code") or ""] = net.get(a.get("code") or "", 0.0) + w
            elif act in EXIT:
                net[a.get("code") or ""] = net.get(a.get("code") or "", 0.0) - w
        for code, w in net.items():
            if abs(w) < 1e-9:
                continue  # 两侧力度恰好相等：这天对这只票没有倾向，不参与对比
            seq.setdefault(code, []).append((x["date"], "加" if w > 0 else "减"))
    flips: list[str] = []
    for code, s in seq.items():
        s.sort()
        for i in range(1, len(s)):
            if s[i][1] != s[i - 1][1]:
                flips.append(
                    f"{code}: {s[i-1][0]} {s[i-1][1]}仓 → {s[i][0]} {s[i][1]}仓"
                )
    n_pairs = sum(max(0, len(s) - 1) for s in seq.values())
    if not n_pairs:
        cons_score = None
        cons_detail = [f"可比对（净倾向非零）的相邻日对为 0，无法判定前后一致",
                       f"标的 {len(seq)} 只"]
    else:
        cons_score = max(0.0, 100.0 * (1 - len(flips) / n_pairs))
        cons_detail = [f"标的 {len(seq)} 只，可比对相邻日对 {n_pairs} 组，反手 {len(flips)} 组"
                       f"（{_pct(len(flips) / n_pairs)}）"]
    out["criteria"]["前后一致"] = {
        "score": cons_score, "detail": cons_detail, "flips": flips[:8],
    }

    # ------------------------------------------------------------ 6 现金纪律
    cash_targets = [float(x["plan"].get("cash_target") or 0.0) for x in plan_rows]
    cash_ratio = [x["cash"] / x["total"] for x in rows if x["total"] > 0]
    mix = Counter(a.get("action") for a in acts)
    n_enter = sum(mix.get(k, 0) for k in ENTER)
    always_high = bool(cash_targets) and all(c >= 0.9 for c in cash_targets)
    no_deploy = n_enter == 0

    # ★ "说了要建仓、实际一动没动" —— 这是本项**唯一有鉴别力**的判据，也是"永远不敢下手"
    # 的直接证据。``cash_target`` 是计划**自己宣布**的目标仓位；把 d 日的计划与 d+1 日
    # **实际**跑出来的现金占比对照：宣称要大幅建仓（target < 0.7）且**确实给了进场档**，
    # 结果次日现金仍 ≥ 90% —— 说明那些进场档要么不可达、要么被别处抵掉了。
    # 这**不是**"没违规"，是**计划与梯子互相不自洽**：目标仓位根本靠自己的档位实现不了。
    idle_planned: list[str] = []
    for x in plan_rows:
        # ⚠️ **同日对照**：``BacktestStep.d`` 的 ``plan_json`` 是**在 d 日执行的那份计划**
        # （计划 d-1 日终产出、d 日执行），所以要和 **d 日自己的**实际现金占比比。
        # 拿 d+1 日比会错开一天（写这个判据时踩过）。
        tgt = float(x["plan"].get("cash_target") or 0.0)
        if tgt >= 0.7:
            continue                                   # 本来就没打算动手
        ent = [a for a in (x["plan"].get("actions") or []) if a.get("action") in ENTER]
        if not ent:
            continue                                   # 说要留现金、也没有进场档 = 自洽
        if x["total"] <= 0:
            continue
        ratio = x["cash"] / x["total"]
        ent_w = sum(float(a.get("target_weight") or 0.0) for a in ent)
        if ratio >= 0.9:
            idle_planned.append(
                f"{x['date']}：计划 cash_target {tgt:.0%}（= 要投 {1 - tgt:.0%}）且给了 "
                f"{len(ent)} 条进场档（合计目标权重 {ent_w:.0%}），实际现金仍 {_pct(ratio)}"
                f"——目标仓位靠自己的档位够不着"
            )

    cash_score = 100.0
    if always_high:
        cash_score -= 40.0
    if no_deploy:
        cash_score -= 30.0
    if idle_planned:
        # **扣分刻意温和**：宣布的目标仓位没达到，可能是"永远不敢下手"，也可能是
        # "档位没被行情穿越 = 有纪律地不追"。判据本身分不清这两者，所以只提示、不重罚——
        # 把"存疑"当"失误"打分会给出一个看起来精确、其实没有依据的数。
        cash_score -= min(30.0, 10.0 * len(idle_planned))
    if ladder_score is not None and ladder_score < 50:
        cash_score -= 10.0
    cash_score = max(0.0, cash_score)
    out["criteria"]["现金纪律"] = {
        "score": cash_score,
        "detail": [
            f"cash_target 逐日 {[round(c, 2) for c in cash_targets] or '—'}",
            f"实际现金占比 首/末 {_pct(cash_ratio[0]) if cash_ratio else '—'} / "
            f"{_pct(cash_ratio[-1]) if cash_ratio else '—'}",
            f"动作分布 {dict(mix)}",
            f"建仓侧动作 {n_enter} 条"
            + ("（一条都没有 = 完全没下手）" if no_deploy else ""),
            f"★ 宣布目标仓位没达到的天 {len(idle_planned)} 天（存疑项：可能是有纪律地不追，"
            "也可能是够不着——判据分不清，故只轻扣）",
        ],
        "idle_planned": idle_planned,
    }

    scores = [v["score"] for v in out["criteria"].values() if v["score"] is not None]
    out["total"] = round(sum(scores) / len(scores), 1) if scores else 0.0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--owner-user-id", type=int, required=True)
    ap.add_argument("--no-price", action="store_true", help="跳过取价（梯子合理性一条不给分）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out = score_run(a.run_id, a.owner_user_id, with_price=not a.no_price)
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("=" * 70)
    print(f"托管团队能力打分  run={out['run_id']}  {out['days']} 个交易日  "
          f"标的池 {out['universe']} 只")
    print("=" * 70)
    for name, v in out["criteria"].items():
        s = v["score"]
        bar = "—" if s is None else f"{s:5.1f}"
        print(f"\n【{name}】 {bar}")
        for d in v["detail"]:
            print(f"    · {d}")
        for key, label in (("missing", "有研究缺口的天"), ("short", "研究覆盖不足的天"),
                           ("thin", "理由过短"), ("abstract", "抽象理由（凭感觉下注）"),
                           ("no_research", "研究里找不到（参考，无鉴别力）"),
                           ("junk_risk", "退化的 risk_notes"),
                           ("wrong_side", "触发价站在反侧"), ("unreachable", "不可达档"),
                           ("violations", "风控违例"),
                           ("inherited_violations", "首日起始状态（不计分）"),
                           ("flips", "无理由反手"),
                           ("idle_planned", "说了要建仓、实际没动")):
            items = v.get(key) or []
            if items:
                print(f"    ⚠️ {label}（{len(items)}）：")
                for it in items:
                    print(f"        - {it}")
    print("\n" + "=" * 70)
    print(f"综合（等权六项）= {out['total']}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
