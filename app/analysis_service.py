"""深度分析异步封装：后台任务 + engine_run 决策缓存。

run_analysis 同步阻塞 10-15 分钟，这里用线程后台跑，任务状态存内存 dict，
结果落 engine_run 表（同日同标的命中缓存不重跑）。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid

from app import account as account_mod
from app import db
from app.analysis import RATING_ZH, run_analysis, run_portfolio_plan
from app.plan_actions import sanitize_ladder_actions
from app.risk import style_label
from tradingagents.dataflows import wind as _wind

# 内存任务状态（单进程够用；多进程部署需换 Redis/DB）
_tasks: dict[str, dict] = {}
_tasks_lock = threading.Lock()


def submit_analysis(user_id: int, ticker: str, date: str) -> str:
    """提交异步分析任务，返回 task_id。"""
    task_id = uuid.uuid4().hex
    with _tasks_lock:
        _tasks[task_id] = {"status": "running", "result": None, "error": None}
    t = threading.Thread(
        target=_worker, args=(task_id, user_id, ticker, date), daemon=True
    )
    t.start()
    return task_id


def _worker(task_id: str, user_id: int, ticker: str, date: str) -> None:
    try:
        result = run_analysis_cached(user_id, ticker, date)
        with _tasks_lock:
            _tasks[task_id] = {"status": "done", "result": result, "error": None}
    except Exception as e:  # noqa: BLE001
        with _tasks_lock:
            _tasks[task_id] = {"status": "error", "result": None, "error": str(e)}


def get_analysis_status(task_id: str) -> dict:
    with _tasks_lock:
        return _tasks.get(task_id, {"status": "unknown"})


def build_portfolio_context(user_id: int, clock=None) -> str:
    """组装「账户现状」文本：全仓 + 现金 + 总资产 + 每票成本/现价/占比/盈亏。

    供引擎（Stage1 逐标的深析）注入，让研究/决策结合组合现状而非孤立分析。

    ``clock`` 只为回测注入，**不能省**：回测里这段文本必须是「模拟日盘中执行完、按当日
    收盘价计的持仓」，否则团队会拿着今天的仓位去分析半年前——这是最隐蔽的一类未来函数，
    因为它不出现在行情里，而是藏在中性的"账户现状"描述中。
    """
    positions = account_mod.get_positions(user_id, 1, clock=clock)
    trust = account_mod.get_account(user_id)["trust"]
    cash = trust["cash"]
    total_assets = trust["total_assets"]
    lines = [f"账户总资产 {total_assets:.2f} 元，可用现金 {cash:.2f} 元。"]
    if not positions:
        lines.append("当前托管簿无持仓（空仓起步）。")
    else:
        lines.append("当前持仓：")
        for p in positions:
            weight = (p.get("assets_ratio") or 0.0) * 100
            pnl = p.get("pnl")
            pnl_s = f"{pnl:+.2f}" if pnl is not None else "N/A"
            price_s = f"{p['price']:.2f}" if p.get("price") is not None else "N/A"
            lines.append(
                f"  {p['stock_code']} {p['stock_name']}：持有 {p['hold_qty']} 股"
                f"（可用 {p['available_qty']}），成本 {p['cost_price']}，现价 {price_s}，"
                f"市值 {p.get('market_value', 0.0):.2f}，占总资产 {weight:.1f}%，浮动盈亏 {pnl_s}"
            )
    return "\n".join(lines)


def run_analysis_cached(user_id: int, ticker: str, date: str, clock=None) -> dict:
    """检查 engine_run 缓存，命中返回；未命中跑 run_analysis 并落库。

    ``clock`` 非空 = 回测。它在**本函数内**（而非调用方）开启 as-of 作用域，是因为
    ``run_plan`` 用的是裸 ``ThreadPoolExecutor``——contextvar 必须在 worker 自己身上设，
    在外层设会被丢掉（见 ``tradingagents/asof.py``）。
    """
    wind_code = _wind.to_wind_code(ticker)
    market = wind_code.split(".")[-1] if "." in wind_code else ""

    session = db.get_session()
    try:
        cached = (
            session.query(db.EngineRun)
            .filter(
                db.EngineRun.user_id == user_id,
                db.EngineRun.ticker == wind_code,
                db.EngineRun.trade_date == date,
            )
            .first()
        )
        if cached:
            return {
                "cached": True,
                "ticker": wind_code,
                "date": date,
                "decision": cached.rating,
                "decision_zh": RATING_ZH.get(cached.rating, cached.rating),
                "report": json.loads(cached.report_json or "{}"),
            }
    finally:
        session.close()

    if clock is not None:
        from tradingagents.asof import asof_scope

        with asof_scope(date):
            result = run_analysis(
                ticker, date, portfolio_context=build_portfolio_context(user_id, clock)
            )
    else:
        result = run_analysis(ticker, date, portfolio_context=build_portfolio_context(user_id))
    session = db.get_session()
    try:
        session.add(
            db.EngineRun(
                user_id=user_id,
                ticker=wind_code,
                market=market,
                trade_date=date,
                rating=result.get("decision", ""),
                report_json=json.dumps(result.get("report", {}), ensure_ascii=False),
                created_at=time.time(),
            )
        )
        session.commit()
    finally:
        session.close()
    result["cached"] = False
    result["ticker"] = wind_code
    return result


def run_plan(user_id: int, symbols: list[str], date: str, on_progress=None,
             should_cancel=None, clock=None, max_workers: int | None = None) -> list[dict]:
    """计划层：对一批标的并行跑深度分析（命中缓存则不重跑），返回结果列表。

    ``on_progress(done, total, result)`` 可选：每完成一只（含失败）回调一次。
    ``should_cancel()`` 可选：返回 True 时抛 ``PlanCancelled``——**还没开始**的分析被取消，
    已经在跑的 LLM 调用无法中断（会跑完但结果被丢弃，不返回给调用方）。
    返回项要么是 ``run_analysis_cached`` 的结果（含 decision/report），
    要么是 ``{"ticker": code, "error": ...}``——**失败不抛出**，由调用方校验覆盖率。

    ``clock`` / ``max_workers`` 只为回测注入。并发度**不要盲目调高**（默认仍是
    ``min(4, total)``）：会撞 Wind 429（``VendorRateLimitError`` 无重试）与 DeepSeek 限流；
    回测真正的加速来自 ``engine_run`` 缓存，不是把并发拉满。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from app.errors import PlanCancelled

    total = len(symbols)
    if not total:
        return []
    results = []
    done = 0
    ex = ThreadPoolExecutor(max_workers=max_workers or min(4, total))
    try:
        futures = {
            ex.submit(run_analysis_cached, user_id, s, date, clock): s for s in symbols
        }
        for f in as_completed(futures):
            if should_cancel is not None and should_cancel():
                raise PlanCancelled()
            sym = futures[f]
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001
                res = {"ticker": _wind.to_wind_code(sym), "error": str(e)}
            results.append(res)
            done += 1
            if on_progress is not None:
                try:
                    on_progress(done, total, res)
                except Exception:  # noqa: BLE001 — 进度上报失败不影响分析
                    pass
            if should_cancel is not None and should_cancel():
                raise PlanCancelled()
    finally:
        # 取消时立即返回（丢弃排队中的标的），不等在跑的线程 —— 暂停按钮才有响应
        ex.shutdown(wait=False, cancel_futures=True)
    return results


def _research_summary(row) -> str:
    """从一条 EngineRun 提取紧凑研究摘要（评级 + 执行摘要 + 目标价 + 止损/仓位）。"""
    try:
        report = json.loads(row.report_json or "{}")
    except Exception:
        report = {}
    pm = report.get("portfolio_manager", "") or ""
    trader = report.get("trader", "") or ""

    def grab(text: str, key: str) -> str:
        m = re.search(rf"\*\*{re.escape(key)}\*\*[：:]\s*(.+)", text)
        return m.group(1).strip() if m else ""

    parts = [f"  {row.ticker}（评级 {row.rating or 'N/A'}）"]
    exec_sum = grab(pm, "Executive Summary")
    if exec_sum:
        parts.append(f"    执行摘要：{exec_sum[:200]}")
    price_target = grab(pm, "Price Target")
    if price_target:
        parts.append(f"    目标价：{price_target}")
    stop_loss = grab(trader, "Stop Loss")
    if stop_loss:
        parts.append(f"    止损价：{stop_loss}")
    sizing = grab(trader, "Position Sizing")
    if sizing:
        parts.append(f"    仓位建议：{sizing}")
    return "\n".join(parts)


#: 报告「各标的深度研究报告」渲染所需的 Stage1 角色字段
RESEARCH_FIELDS = (
    "market", "sentiment", "news", "fundamentals",
    "bull", "bear", "research_manager", "trader",
    "aggressive", "conservative", "neutral", "portfolio_manager",
)


def collect_research_snapshot(user_id: int, codes: list[str], trade_date: str | None = None) -> dict:
    """按 code 取该用户最新一次 Stage1 研究结论（评级 + 各角色报告原文）。

    用于两处：①生成计划时**冻结快照**落进 plan_json；②下载报告时作兜底查库。
    只取 ``RESEARCH_FIELDS`` 里的角色字段，避免把无关内容塞进计划记录。

    ``trade_date`` 非空时只认该研究日的结果（回测必传）。**默认必须是 None**：
    若默认成"下个交易日"，实盘会过滤到一个还没有 ``EngineRun`` 的未来日期 → 快照变空 →
    计划照出但**静默失去依据**。那是不会报错的错，最难发现。
    """
    out: dict = {}
    if not codes:
        return out
    session = db.get_session()
    try:
        for code in codes:
            if not code or code in out:
                continue
            q = session.query(db.EngineRun).filter(
                db.EngineRun.user_id == user_id, db.EngineRun.ticker == code
            )
            if trade_date is not None:
                # 回测：只认当天研究日的结论。不加这个过滤会取到"最近写入"的那条——
                # 回测里那可能是**更晚的日期**（断点续跑时先跑了后一天），即未来函数。
                q = q.filter(db.EngineRun.trade_date == trade_date)
            row = q.order_by(db.EngineRun.id.desc()).first()
            if not row:
                continue
            try:
                rep = json.loads(row.report_json or "{}")
            except Exception:
                rep = {}
            out[code] = {
                "rating": row.rating,
                "trade_date": row.trade_date,
                "report": {k: rep[k] for k in RESEARCH_FIELDS if rep.get(k)},
            }
    finally:
        session.close()
    return out


def build_portfolio_plan_context(user_id: int, clock=None, research_date: str | None = None) -> str:
    """组装 Stage2 组合决策图输入：账户现状 + **可操作范围** + 各票研究摘要 + 显式风控参数。

    ``clock`` / ``research_date`` 只为回测注入，两者必须**成对**传：账户现状要按模拟日
    盯市（``clock``），研究摘要也要按同一天取（``research_date``）。只传一个就会让 Stage2
    看到「半年前的持仓 + 最新的研究」这种现实中不存在的组合。

    ## 标的集是**在管全集**，不只是持仓

    这里曾经只取**持仓**的研究摘要。后果是：Stage1 花钱把 ``stock_scope`` 选出的自选股
    研究了一遍，报告落进 ``EngineRun``，然后**没有任何环节把它交给决策团队**——团队在一个
    只看得见持仓的世界里决策，自然永远产不出新标的的建仓动作。实盘里「选股范围＝自选股」
    因此几乎不生效（空仓起步的兜底路径除外），而设置页照常显示着「自选股」，界面上看不出
    任何异常。这是设计文档 §10.1 点名的那类**静默失效**，且花的是真钱。

    现在取 ``trust.get_analysis_universe``（持仓 ∪ 按 ``stock_scope`` 选出的在管池），
    分【持仓】【候选】两段呈现，并在文案里明确要求把动作限定在这个范围内。

    **回测为什么不需要额外参数**：回测的影子账户在起跑时已被镜像成 ``stock_scope=1`` 且
    自选股 = 冻结标的池（``backtest._materialize_universe``），所以同一段代码在回测里读到的
    就是冻结池。一处镜像，两边自动对齐。

    ⚠️ 范围约束是**提示词约束，不是代码硬保证**。回测里越界的动作会因为 ``clock.bars()``
    没有该标的的行情而 ``tradable=False``、成交不了；实盘没有这层天然保护，止血手段是把
    托管设置的选股范围改回「仅持仓股」——那时【候选】段自然为空，行为立刻回到改动前。
    """
    ctx = build_portfolio_context(user_id, clock)

    # 在**开 session 之前**调用：它自己会开 session（同进程嵌套 session 在测试的
    # StaticPool 下会互相回滚，见 scripts/smoke_backtest_isolation.py 的说明）。
    from app import trust as trust_mod

    universe = trust_mod.get_analysis_universe(user_id)
    research_date_filter = research_date

    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        held_rows = (
            session.query(db.Position)
            .filter(db.Position.user_id == user_id, db.Position.book == 1)
            .all()
        )
        held_codes = [r.stock_code for r in held_rows]
        held_set = set(held_codes)
        # 名字优先取在管池里的；影子账户物化时若没带名字，用托管簿持仓的名字兜底。
        name_of = {s["code"]: (s.get("name") or "") for s in universe}
        for r in held_rows:
            if not name_of.get(r.stock_code):
                name_of[r.stock_code] = r.stock_name or ""
        # 保持"持仓在前、候选在后"的稳定顺序
        ordered = [c for c in held_codes if c]
        ordered += [s["code"] for s in universe if s["code"] and s["code"] not in held_set]

        research_held: list[str] = []
        research_new: list[str] = []
        missing: list[str] = []
        for code in ordered:
            q = session.query(db.EngineRun).filter(
                db.EngineRun.user_id == user_id, db.EngineRun.ticker == code
            )
            if research_date_filter is not None:
                q = q.filter(db.EngineRun.trade_date == research_date_filter)
            row = q.order_by(db.EngineRun.id.desc()).first()
            if not row:
                missing.append(code)
                continue
            (research_held if code in held_set else research_new).append(_research_summary(row))
    finally:
        session.close()

    style = cfg.style if cfg else 1
    single_cap = cfg.risk_max_position_pct if cfg else None
    stop_loss = cfg.risk_stop_loss_pct if cfg and cfg.risk_stop_loss_pct else None
    max_trades = cfg.risk_max_trades_day if cfg and cfg.risk_max_trades_day else None
    scope = cfg.stock_scope if cfg else 0

    lines = [
        ctx,
        "",
        "风控参数：",
        f"  投资风格：{style_label(style)}（仅代表进取/保守倾向，不构成硬性仓位上限）",
    ]
    # 硬性风控**只在用户显式设置后**才存在；留空即「不限制」，全部交由团队自主决定。
    # 文案必须与执行层一致：执行层不再做任何默认上限，上下文里也不能暗示有。
    hard: list[str] = []
    if single_cap:
        hard.append(f"单票市值占比不得超过 {single_cap:.0%}")
    if stop_loss:
        hard.append(f"单票浮亏超过 {stop_loss:.0%} 必须止损")
    if max_trades:
        hard.append(f"单日成交笔数不得超过 {max_trades} 笔")
    if hard:
        lines.append("  用户显式设定的硬性风控（必须遵守）：" + "；".join(hard))
    else:
        lines.append(
            "  用户未设定任何硬性限制——总仓位高低、单票占多少、预留多少现金、"
            "是否设止损位，全部由团队自主决定，请按账户实际情况与风险收益自行把握。"
        )
    lines.append(f"  选股范围 stock_scope={scope}（0仅持仓/1仅自选/2全市场）")

    # 「可操作范围」必须显式写出来：团队此前只看得见持仓，于是永远不会考虑新标的。
    # 这一段是本次改动的**全部意义**所在。
    def _fmt(codes: list[str]) -> str:
        return "、".join(f"{c} {name_of.get(c)}".strip() for c in codes) if codes else "（无）"

    new_codes = [c for c in ordered if c not in held_set]
    held_in_order = [c for c in ordered if c in held_set]
    lines.append("")
    lines.append("本次可操作的标的范围（**范围外的标的一律不要提交任何动作**）：")
    lines.append(f"  【持仓】{_fmt(held_in_order)}")
    lines.append(f"  【候选】{_fmt(new_codes)}")
    lines.append(
        "  本次的建仓与调仓应限定在上面列出的标的范围内。"
        "【候选】里的是尚未持有、但已做过研究、可以新建仓的标的。"
    )

    if research_held or research_new:
        lines.append("各标的深度研究结论（Stage1 摘要）：")
        if research_held:
            lines.append("【持仓】")
            lines.extend(research_held)
        if research_new:
            lines.append("【候选】")
            lines.extend(research_new)
        if missing:
            lines.append(
                "  （以下标的在范围内，但本次没有研究结论："
                + "、".join(missing)
                + "——请谨慎对待，不要仅凭名称下注）"
            )
    else:
        lines.append("（暂无各标的深度研究结论——计划仅基于账户现状与通用判断）")
    return "\n".join(lines)


# 动作归一 + 分档校验的实现已抽到 app/plan_actions（纯函数、无依赖），供写时 / 读时 /
# 执行层 / 报告渲染四处共用（见 plan_actions.sanitize_ladder_actions）。


def run_portfolio_plan_for_user(
    user_id: int, next_date: str, on_progress=None, clock=None, research_date: str | None = None
) -> dict | None:
    """Stage 2：组装上下文 → 跑组合决策图 → 落 TrustPlan（含完整决策过程 + 研究快照）。

    ``on_progress(node_name, label)`` 可选：透传给 PortfolioTeamGraph 上报三节点进展。

    ``clock`` / ``research_date`` 只为回测注入（研究日 = 执行日的前一交易日）。
    **``research_date`` 默认必须是 ``None``**——默认成 ``next_date`` 会让实盘去过滤一个
    还没有 ``EngineRun`` 的未来日期，研究快照变空、计划照出但静默失去依据（见
    ``collect_research_snapshot``）。
    """
    ctx = build_portfolio_plan_context(user_id, clock, research_date)
    result = run_portfolio_plan(ctx, on_progress=on_progress)
    plan = (result or {}).get("plan") or {}
    if not plan.get("actions"):
        return None
    # 落库前先归一 + 分档校验：同 code 多行**保留**（那是多档梯子，不是重复），
    # 但排好序、剔掉非法档，下游（监控条件卡片 / 报告 / 执行层）统一按此口径。
    plan["actions"], ladder_warnings = sanitize_ladder_actions(plan["actions"])
    if not plan["actions"]:
        return None
    # 冻结研究快照：报告里的「各标的深度研究报告」以**计划生成时刻**为准，
    # 而不是下载时刻的 EngineRun（否则事后重跑/失败会让报告与决策脱节）。
    codes: list[str] = [a.get("code") for a in plan.get("actions", []) if a.get("code")]
    for p in account_mod.get_positions(user_id, 1, clock=clock):
        if p["stock_code"] not in codes:
            codes.append(p["stock_code"])
    research = collect_research_snapshot(user_id, codes, research_date)
    missing = [c for c in codes if c not in research]
    # 完整决策过程：账户快照 + 组合分析师草稿 + 风控官意见 + 研究快照 + 最终计划
    payload = {
        **plan,
        "process": {
            "portfolio_context": ctx,
            "analyst_draft": (result or {}).get("draft") or {},
            "risk_review": (result or {}).get("risk_review") or {},
            "research": research,
            "research_missing": missing,
            # 分档校验里被截断/丢弃的档，报告的口径说明里如实列出（正常为空）
            "ladder_warnings": ladder_warnings,
            "generated_at": time.time(),
        },
    }
    session = db.get_session()
    try:
        existing = (
            session.query(db.TrustPlan)
            .filter(db.TrustPlan.user_id == user_id, db.TrustPlan.trade_date == next_date)
            .first()
        )
        now = time.time()
        blob = json.dumps(payload, ensure_ascii=False)
        if existing:
            existing.plan_json = blob
            existing.created_at = now
        else:
            session.add(
                db.TrustPlan(
                    user_id=user_id, trade_date=next_date, plan_json=blob, created_at=now
                )
            )
        session.commit()
    finally:
        session.close()
    return plan
