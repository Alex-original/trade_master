"""财团托管：托管簿管理 + 两级调度（计划层深度引擎出评级 + 执行层分钟级调仓）。

- 托管簿（book=1）：托管页粘贴快照建立；现金=粘贴的券商可用资金（可空仓以 50 万默认起步，见 DEFAULT_TRUST_CASH）。
- 托管全面代替个人：买入用托管簿现金加仓、卖出现金回流再分配。
- 计划层：收盘后对在管标的并行跑深度分析，评级落 engine_run。
- 执行层：盘中每分钟扫评级 + 行情 + 风控，产出调仓。
- 日结：释放 A股 T+1 冻结。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta

from app import account as account_mod
from app import db, notify, trade
from app.errors import PlanCancelled, PlanNeedsConfirm, ServiceError
# 计划动作归一与分档：落库时（写时）与卡片/执行读取时（读时）必须是**同一口径**，
# 否则线上历史计划里同一只票的「hold 基准行 + 条件动作行」会重复出现。
# build_ladders 把同一只票的同向多档触发排成由浅到深的梯子，执行层据此逐档推进。
from app.market_clock import LiveClock
from app.plan_actions import build_ladders, fill_price
from app.risk import next_trading_day
from tradingagents.agents.utils.structured import AccountLevelLLMError
from tradingagents.dataflows import wind as _wind

# 托管簿"空仓起步"默认资金（无粘贴持仓时由前端触发空仓启动建簿）
DEFAULT_TRUST_CASH = 500000.0

# A股收盘时刻：15:00 前算「盘中」（当日计划当日可执行），之后算「盘后」（生成次日计划）
MARKET_CLOSE_HOUR = 15

#: 当前调度器实例。由 ``start_scheduler`` 赋值，供 ``scheduler_status()`` 观测
#: （原先返回值被调用方丢弃，"下次什么时候跑"在进程外不可见）。未启动时为 None。
_SCHEDULER = None


# ---------- 托管簿 CRUD ----------

def _clear_book_runtime(session, user_id: int, *, plans: bool = True) -> dict:
    """清空托管簿的「运行痕迹」：委托 / 成交 / 次日行动计划 / 已实现盈亏，返回各自条数。

    「监控条件」不是独立表，而是 ``TrustPlan`` 的派生视图（``get_monitor_conditions`` 读最新
    一条计划），所以清掉计划即同时清空监控卡片——不需要也不可能单独删。

    **已实现盈亏也在这里归零**（``TrustConfig.realized_pnl``，语义是「本簿累计」）：本函数是
    清空账簿运行痕迹的**唯一收敛点**，重贴快照与重置托管都必经此处，所以归零不需要散落两处。
    历史教训正相反——清空逻辑曾在 ``reset_trust`` 里抄了一份并漏了 TrustPlan，见下。

    历史的坑：这条清空逻辑原先在 ``reset_trust`` 里写了一遍，**漏了 TrustPlan**，于是「重置
    托管」之后次日行动计划与监控条件仍挂在界面上显示已作废的旧计划。现在两处共用一个实现。

    ## ``plans``：真实账簿与影子账户在这里**故意不同**

    真实簿走默认的 ``plans=True``：重贴快照 / 重置托管就是「这本簿重新开始」，旧计划必须
    跟着消失，否则监控卡片会展示一份已作废的旧计划（上面那个坑）。

    回测的影子簿走 ``plans=False``（``backtest.reset_shadow_book``）。原因是影子账户被
    **刻意跨 run 复用**（见 ``backtest.ensure_shadow_user``：``EngineRun`` 研究缓存是最贵的
    成本项），于是「run A 的计划落在共享的影子上」是设计的必然后果；若再按运行痕迹去删，
    run B 的开始就会把 run A 唯一的历史记录抹掉。计划是那次回测自己的产出，与「研究报告
    留着」是同一个立场。跨 run 串味由 ``backtest.list_plan_reports`` 的**日期窗口收口**解决，
    而不是靠删行。

    不 commit——由调用方在同一事务里收尾。
    """
    n_orders = session.query(db.Order).filter(db.Order.user_id == user_id).delete()
    n_trades = session.query(db.Trade).filter(db.Trade.user_id == user_id).delete()
    n_plans = 0
    if plans:
        n_plans = session.query(db.TrustPlan).filter(db.TrustPlan.user_id == user_id).delete()
    # 归零而非置 NULL：簿刚被清空，它的累计已实现盈亏是**确实可知**的 0。
    # （NULL 的语义留给「建于本列存在之前、累计值不可知」的老簿，两者不要混。）
    cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
    if cfg is not None:
        cfg.realized_pnl = 0.0
    return {
        "orders": int(n_orders or 0),
        "trades": int(n_trades or 0),
        "plans": int(n_plans or 0),
    }


def snapshot_trust_book(
    user_id: int,
    rows: list[dict],
    cash: float | None = None,
    mv: float | None = None,
    pnl: float | None = None,
    reset_snapshot: bool = False,
) -> dict:
    """粘贴快照建立托管簿——**重贴即重建**。rows 同 account.sync_positions。

    重建语义（2026-09 改）：粘贴快照是「以这份快照为准重建托管簿」，因此本函数会先在**同一
    事务**里清空该用户的委托 / 成交 / 次日行动计划（监控条件随之清空，见
    ``_clear_book_runtime``），再用新快照覆盖托管簿持仓与可用资金。原先是「已有成交记录就报错、
    要求用户先去点一次『清空成交记录（重置托管）』」，既绕又只清了一半。

    调用方必须先把覆盖影响告知用户（前端是面板内常驻横幅 + 提交时二次确认）。

    cash：该簿可用资金 = 粘贴的券商可用资金；不传/None 默认 0（保持旧行为）。
    mv/pnl：券商快照口径的总市值 / 浮动盈亏，用户在解析确认页手动校准过才传；
            reset_snapshot=True 时清空（回到按持仓×实时价实时计算，见 account.sync_positions）。
    空仓启动（positions 为空 + cash=50 万）同样落库：book_created=True、现金=50 万，
    由执行层在自选/全市场内自由建仓。

    **同时把 ``is_active`` 关掉**（需用户回「策略设置」手动重新开启）。这不是顺手的清理：
    计划被清空后 ``run_execution`` 的 ``has_plan=False``，执行层会回落到旧的评级兜底路径
    ``_execute_legacy``——它读 ``engine_runs`` 里**旧仓位**留下的评级，下一个 cron tick 就会
    按旧评级对**刚贴进来的新仓位**下单，且那条路径不校验开市时间。关开关是唯一干净的堵法。

    返回被清空的条数 ``{"orders": n, "trades": n, "plans": n}``，供接口回执与前端提示。
    """
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        if not cfg:
            raise ServiceError("托管配置不存在")
        cleared = _clear_book_runtime(session, user_id)
        now = time.time()
        session.query(db.Position).filter(
            db.Position.user_id == user_id, db.Position.book == 1
        ).delete()
        seen: set[str] = set()
        for row in rows:
            qty = int(row.get("qty") or 0)
            if qty <= 0:
                continue
            code = _wind.to_wind_code(row["code"])
            if code in seen:
                continue  # 同一标的重复出现 → 去重，避免唯一键冲突
            seen.add(code)
            session.add(
                db.Position(
                    user_id=user_id,
                    book=1,
                    stock_code=code,
                    stock_name=row.get("name") or "",
                    hold_qty=qty,
                    available_qty=qty,  # 快照为已有持仓，全部可用
                    frozen_qty=0,
                    cost_price=float(row.get("cost_price") or 0.0),
                    updated_at=now,
                )
            )
        cfg.book_created = True
        cfg.is_active = False  # 见 docstring：防执行层按旧评级对新仓位下单
        cfg.available_cash = round(float(cash), 2) if cash is not None else 0.0
        if reset_snapshot:
            cfg.broker_mv = None
            cfg.broker_pnl = None
        else:
            if mv is not None:
                cfg.broker_mv = round(float(mv), 2)
            if pnl is not None:
                cfg.broker_pnl = round(float(pnl), 2)
        cfg.updated_at = now
        session.commit()
        return cleared
    finally:
        session.close()


def _is_backtest_user(user_id: int) -> bool:
    """是否回测影子账户。**读列，不解析手机号前缀**（前缀只是给人看的标记）。

    列不存在时（``_ensure_schema`` 还没跑）返回 False：调用方 ``toggle_trust`` 只可能被
    真实登录用户触达，而影子账户没有会话令牌（``phone`` 是 ``bt-`` 开头，短信登录不可达），
    所以这个失效方向不会把影子账户放进来。
    """
    session = db.get_session()
    try:
        row = session.query(db.User.is_backtest).filter(db.User.id == user_id).first()
        return bool(row and row[0])
    except Exception:  # noqa: BLE001 —— 迁移未落地（列不存在）
        return False
    finally:
        session.close()


def toggle_trust(user_id: int, active: bool) -> None:
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        if not cfg:
            raise ServiceError("托管配置不存在")
        # 回测影子账户**永远不能**被激活：is_active=True 会让实盘调度器（每周一到五
        # 9:00-14:59 每分钟一次）拿实时行情去交易回测账簿。这是唯一的激活入口，卡这里就够。
        if _is_backtest_user(user_id):
            raise ServiceError("回测账户不能开启实盘托管")
        if active and not cfg.book_created:
            raise ServiceError("请先粘贴持仓截图创建托管起始仓位")
        cfg.is_active = active
        cfg.updated_at = time.time()
        session.commit()
    finally:
        session.close()


def reset_trust(user_id: int) -> dict:
    """清空成交记录 + 次日行动计划 + 托管簿，回到未建簿干净态（二次确认在前端）。

    返回被清空的条数，同 ``snapshot_trust_book``。
    """
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        if not cfg:
            raise ServiceError("托管配置不存在")
        cleared = _clear_book_runtime(session, user_id)
        session.query(db.Position).filter(
            db.Position.user_id == user_id, db.Position.book == 1
        ).delete()
        cfg.book_created = False
        cfg.available_cash = 0.0
        cfg.is_active = False
        # 快照口径一并清掉：book_created=False 后总览回落实时计算，留着旧口径会显示错数
        cfg.broker_mv = None
        cfg.broker_pnl = None
        cfg.updated_at = time.time()
        session.commit()
        return cleared
    finally:
        session.close()


def update_trust_config(user_id: int, cfg_dict: dict) -> dict:
    """更新托管配置。可改：stock_scope/style/风控三参数/费用三项/通知。"""
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        if not cfg:
            raise ServiceError("托管配置不存在")
        for k, v in cfg_dict.items():
            if hasattr(cfg, k):
                # v 可为 None，表示显式清空该可选参数（如风险三项）
                setattr(cfg, k, v)
        cfg.updated_at = time.time()
        session.commit()
        return _cfg_to_dict(cfg)
    finally:
        session.close()


def _cfg_to_dict(cfg) -> dict:
    return {
        "is_active": cfg.is_active,
        "book_created": cfg.book_created,
        "available_cash": cfg.available_cash,
        "broker_mv": cfg.broker_mv,
        "broker_pnl": cfg.broker_pnl,
        "stock_scope": cfg.stock_scope,
        "stock_scope_group": cfg.stock_scope_group,
        "style": cfg.style,
        "risk_max_trades_day": cfg.risk_max_trades_day,
        "risk_max_position_pct": cfg.risk_max_position_pct,
        "risk_stop_loss_pct": cfg.risk_stop_loss_pct,
        "fee_commission_rate": cfg.fee_commission_rate,
        "fee_waive_min": cfg.fee_waive_min,
        "fee_stamp_duty_rate": cfg.fee_stamp_duty_rate,
        "agent_id": cfg.agent_id,
    }


def get_trust(user_id: int) -> dict:
    """托管配置 + 运行痕迹计数。

    三个计数（``trade_count`` / ``order_count`` / ``plan_count``）是给前端在**提交重贴之前**
    算「将清空多少」用的：贴快照会清空这三张表，用户得先看到后果。只被 ``main.py`` 的四个
    路由调用、不在任何循环里，三条 COUNT 的代价可忽略。
    """
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        if not cfg:
            raise ServiceError("托管配置不存在")
        d = _cfg_to_dict(cfg)
        d["trade_count"] = session.query(db.Trade).filter(db.Trade.user_id == user_id).count()
        d["order_count"] = session.query(db.Order).filter(db.Order.user_id == user_id).count()
        d["plan_count"] = session.query(db.TrustPlan).filter(db.TrustPlan.user_id == user_id).count()
        return d
    finally:
        session.close()


# ---------- 委托 / 成交 / 收益（只读） ----------
#
# 三段查询原本内联在 ``main.py`` 的路由里，抽出来是为了让对话助手的工具层能复用
# （``app/chat_tools.py``），顺带让路由保持薄包装。

def list_orders(user_id: int, limit: int = 100) -> list[dict]:
    """最近的委托流水，最新在前。``limit`` 由调用方 clamp。"""
    session = db.get_session()
    try:
        rows = (
            session.query(db.Order)
            .filter(db.Order.user_id == user_id)
            .order_by(db.Order.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "order_id": r.order_id, "stock_code": r.stock_code, "stock_name": r.stock_name,
                "direction": r.direction, "price": r.price, "quantity": r.quantity,
                "status": r.status, "source": r.source, "fail_reason": r.fail_reason,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    finally:
        session.close()


def list_trades(user_id: int, limit: int = 100) -> list[dict]:
    """最近的成交流水，最新在前。``limit`` 由调用方 clamp。"""
    session = db.get_session()
    try:
        rows = (
            session.query(db.Trade)
            .filter(db.Trade.user_id == user_id)
            .order_by(db.Trade.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "trade_id": r.trade_id, "stock_code": r.stock_code, "stock_name": r.stock_name,
                "direction": r.direction, "price": r.price, "quantity": r.quantity,
                "amount": r.amount, "fee": r.fee, "ai_reason": r.ai_reason, "traded_at": r.traded_at,
            }
            for r in rows
        ]
    finally:
        session.close()


def get_analytics(user_id: int) -> dict:
    """成交归因：流水 + 托管簿浮动盈亏 + 本簿累计已实现盈亏。

    ``realized_pnl`` 取自 ``TrustConfig.realized_pnl``：卖出时按加权平均成本增量累计
    （见 ``trade.place_order`` 卖出分支），口径与 ``backtest_calc.replay_realized`` 一致。
    语义是**本簿累计**——建簿 / 重贴快照 / 重置托管时由 ``_clear_book_runtime`` 归零。

    **老簿（本列上线前建的）如实返回 ``None`` + ``realized_pnl_note``，不是 0。** 原先这里
    硬编码 ``0.0``，会让「已实现盈亏 0 元」看起来像一个真实结论。``None`` 的语义是**不可知**：
    那些累计发生在口径升级之前，无从追溯；重贴一次快照即可从头启用。调用方（尤其是对话助手）
    拿到 ``None`` 时应如实转述 note，**不要**用成交流水自行倒推一个数。

    注意 ``None`` 与 ``0.0`` 在此**都合法且含义不同**，前端与工具层必须都保留这个区分。
    """
    acc = account_mod.get_account(user_id)
    session = db.get_session()
    try:
        n_trades = session.query(db.Trade).filter(db.Trade.user_id == user_id).count()
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        realized = None if cfg is None else cfg.realized_pnl
    finally:
        session.close()
    return {
        "realized_pnl": realized,
        "realized_pnl_note": "" if realized is not None else "暂未统计：本簿建于口径升级前，重贴一次快照即可启用",
        "trade_count": n_trades,
        "trust": acc["trust"],
        "trades": list_trades(user_id),
    }


# ---------- 在管标的 ----------

def get_managed_symbols(user_id: int) -> list[dict]:
    """按 stock_scope 返回在管标的 [{code,name}]。"""
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        if not cfg:
            return []
        scope = cfg.stock_scope
        out = []
        if scope in (0, 2):  # 仅持仓 / 全市场 含持仓
            for p in session.query(db.Position).filter(
                db.Position.user_id == user_id, db.Position.book == 1
            ).all():
                out.append({"code": p.stock_code, "name": p.stock_name})
        if scope in (1, 2):  # 仅自选 / 全市场 含自选
            wq = session.query(db.Watchlist).filter(db.Watchlist.user_id == user_id)
            # scope=1 指定了某组 → 只取该组；组被删/不存在时回退全部分组（不让托管池意外清空）
            if scope == 1 and cfg.stock_scope_group:
                g = (cfg.stock_scope_group or "").strip()
                if g:
                    exists = (
                        session.query(db.WatchlistGroup)
                        .filter(db.WatchlistGroup.user_id == user_id, db.WatchlistGroup.name == g)
                        .first()
                        is not None
                    )
                    if exists:
                        wq = wq.filter(db.Watchlist.group_name == g)
            for w in wq.all():
                out.append({"code": w.stock_code, "name": w.stock_name})
    finally:
        session.close()
    # 去重
    seen, uniq = set(), []
    for s in out:
        if s["code"] in seen:
            continue
        seen.add(s["code"])
        uniq.append(s)
    return uniq


def get_analysis_universe(user_id: int) -> list[dict]:
    """研究层（Stage1）应在管的全集 = 托管簿持仓 ∪ 按 stock_scope 选出的标的池。

    持仓是已经存在的真实敞口，必须被研究覆盖——否则计划会对它盲飞；
    stock_scope 只决定**候选**池（自选/全市场），不该把持仓排除在研究之外。
    """
    session = db.get_session()
    try:
        held = [
            {"code": p.stock_code, "name": p.stock_name}
            for p in session.query(db.Position)
            .filter(db.Position.user_id == user_id, db.Position.book == 1)
            .all()
        ]
    finally:
        session.close()
    out, seen = [], set()
    for s in held + get_managed_symbols(user_id):
        if not s["code"] or s["code"] in seen:
            continue
        seen.add(s["code"])
        out.append(s)
    return out


# ---------- 评级辅助 ----------

def _get_latest_rating(user_id: int, code: str) -> str | None:
    session = db.get_session()
    try:
        row = (
            session.query(db.EngineRun)
            .filter(db.EngineRun.user_id == user_id, db.EngineRun.ticker == code)
            .order_by(db.EngineRun.id.desc())
            .first()
        )
        return row.rating if row else None
    finally:
        session.close()


def _rating_to_action(rating: str | None) -> str:
    return {
        "Buy": "buy",
        "Overweight": "overweight",
        "Hold": "hold",
        "Underweight": "underweight",
        "Sell": "sell",
    }.get(rating, "hold")


# ---------- 执行层 ----------

def _today_trade_count(user_id: int, clock=None) -> int:
    """今日已成交笔数。``clock`` 非空时按**回测当日**划界（子 tick 共享同一天，额度累积）。"""
    now = clock.now if clock is not None else datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    session = db.get_session()
    try:
        return (
            session.query(db.Trade)
            .filter(db.Trade.user_id == user_id, db.Trade.traded_at >= today0)
            .count()
        )
    finally:
        session.close()


def _load_plan_json(row) -> dict | None:
    try:
        return json.loads(row.plan_json or "{}")
    except Exception:
        return None


def _plan_row_meta(row) -> dict:
    """``TrustPlan`` ORM 行 → ``{trade_date, created_at, plan}``。"""
    return {
        "trade_date": row.trade_date or "",
        "created_at": row.created_at or 0.0,
        "plan": _load_plan_json(row) or {},
    }


def _get_latest_plan_row(user_id: int) -> dict | None:
    """最新一份计划的元信息 + 正文：``{trade_date, created_at, plan}``，无计划返回 None。

    比 ``_get_latest_plan`` 多带回 row 上的元信息——「监控条件」卡片要显示**监控日期**
    （= 计划的执行日），光有 plan 正文不够。
    """
    session = db.get_session()
    try:
        row = (
            session.query(db.TrustPlan)
            .filter(db.TrustPlan.user_id == user_id)
            .order_by(db.TrustPlan.id.desc())
            .first()
        )
        return _plan_row_meta(row) if row is not None else None
    finally:
        session.close()


def _get_plan_row_for_date(user_id: int, date: str) -> dict | None:
    """指定**执行日**的那份计划，形状同 ``_get_latest_plan_row``；没有返回 None。

    回测专用：``_get_latest_plan_row`` 取的是"最新一份"，在回测里那可能是另一个 run 写的、
    或者比模拟日更晚的计划——两者都会让卡片说谎。``date`` 就是模拟日。
    """
    session = db.get_session()
    try:
        row = (
            session.query(db.TrustPlan)
            .filter(db.TrustPlan.user_id == user_id, db.TrustPlan.trade_date == date)
            .order_by(db.TrustPlan.id.desc())
            .first()
        )
        return _plan_row_meta(row) if row is not None else None
    finally:
        session.close()


def _get_latest_plan(user_id: int) -> dict | None:
    """最新一份计划正文（按 id 倒序，含明天/今天）。"""
    row = _get_latest_plan_row(user_id)
    return row["plan"] if row else None


def _get_today_plan(user_id: int, date: str | None = None) -> dict | None:
    """「今天」应执行的计划（trade_date == 今日），供执行层用。

    次日语义：昨晚/盘中生成、今日执行；盘中生成的「明天」计划不提前用。

    ``date`` 只为回测注入（回测要按**模拟日**取计划，不是真实今天）；生产不传。
    """
    today = date or datetime.now().strftime("%Y-%m-%d")
    session = db.get_session()
    try:
        row = (
            session.query(db.TrustPlan)
            .filter(db.TrustPlan.user_id == user_id, db.TrustPlan.trade_date == today)
            .order_by(db.TrustPlan.id.desc())
            .first()
        )
        return _load_plan_json(row) if row else None
    finally:
        session.close()


def get_plan_download_data(user_id: int) -> dict | None:
    """取下载报告所需数据：最新计划（含决策过程）+ 各标的 Stage1 研究报告 + 账户。

    研究报告**优先用计划生成时冻结的快照**（``process.research``）——它才是团队
    当时真正看到的东西；只有旧计划没有快照时才回落到实时查 EngineRun。
    """
    from app.analysis_service import collect_research_snapshot

    plan = _get_latest_plan(user_id)
    if not plan:
        return None
    positions = account_mod.get_positions(user_id, 1)
    codes = list({a.get("code") for a in plan.get("actions", []) if a.get("code")})
    for p in positions:
        if p["stock_code"] not in codes:
            codes.append(p["stock_code"])

    process = plan.get("process") or {}
    research: dict = process.get("research") or {}
    missing = list(process.get("research_missing") or [])
    frozen = bool(research)  # 是否来自冻结快照（决定报告里的措辞）
    if not research:
        research = collect_research_snapshot(user_id, codes)
        missing = [c for c in codes if c not in research]

    session = db.get_session()
    try:
        trow = (
            session.query(db.TrustPlan)
            .filter(db.TrustPlan.user_id == user_id)
            .order_by(db.TrustPlan.id.desc())
            .first()
        )
        trade_date = trow.trade_date if trow else ""
        created_at = trow.created_at if trow else 0.0
    finally:
        session.close()
    cash = account_mod.get_account(user_id)["trust"]["cash"]
    return {
        "plan": plan,
        "positions": positions,
        "research": research,
        "research_missing": missing,
        "research_frozen": frozen,
        "cash": cash,
        "trade_date": trade_date,
        "created_at": created_at,
    }


# A股连续竞价开始时刻。集合竞价（09:15–09:25）与 09:25–09:30 的撮合停滞期都不算：
# 量比在此刻之前没有意义，那个价位也只是一次撮合结果，未必可成交。
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30


def _market_open(now: datetime | None = None) -> bool:
    """是否已进入连续竞价（≥09:30）。``now`` 只为测试可注入，生产不传。"""
    now = now or datetime.now()
    return now.hour > MARKET_OPEN_HOUR or (
        now.hour == MARKET_OPEN_HOUR and now.minute >= MARKET_OPEN_MINUTE
    )


def _trigger_satisfied(
    action: dict,
    price: float | None,
    monitor_date: str | None = None,
    volume_ratio: float | None = None,
    now: datetime | None = None,
) -> bool:
    """按动作的触发条件判断本 tick 是否允许执行（价格条件 **且** 量能条件）。

    **统一时间闸门（对所有触发类型）**：未进入连续竞价（≥09:30）一律不触发。两条理由：
    - ``量比`` 在集合竞价阶段没有意义，用它判「放量」会误触发或漏触发；
    - 调度器 09:00 起就在跑（mon-fri 9-14 每分钟），而集合竞价刚结束那一瞬间的价位
      未必可成交。此前只有 ``open`` 类型有此检查，价格型档位 09:26 就照单执行过
      （2026-09-11 早上 518880 那笔卖出即是）。

    **唯一例外**：``monitor_date`` 晚于今天时不加闸门——「监控条件」卡片展示的是**下一
    交易日的计划**，今天根本不会执行，此时卡片是「下一日的预演」，应按当前价如实显示条件
    是否已满足。这不是漏判；改动前请先读这段注释。

    ``volume_ratio``（量比）为 None 时，带 ``volume_ratio_min`` 的档**视为不满足**：
    宁可不执行，也不能在最需要量能确认的时刻悄悄退化成纯价格触发。

    ``now`` 只为回测注入：回测的「现在」是模拟日盘中，不是真实墙钟。生产不传，
    走 ``datetime.now()`` —— 行为与改造前逐位相同。
    """
    t = action.get("trigger_type") or "none"
    ref = now if now is not None else datetime.now()
    future = bool(monitor_date) and monitor_date > ref.strftime("%Y-%m-%d")
    if future and t in ("none", "intraday", "open"):
        return False
    if not future and not _market_open(now):
        return False

    if t in ("none", "intraday", "open"):
        ok = True
    else:
        tp = action.get("trigger_price")
        if tp is None or price is None:
            return False
        if t == "price_below":
            ok = price <= tp
        elif t == "price_above":
            ok = price >= tp
        else:
            ok = True
    if not ok:
        return False

    vr_min = action.get("volume_ratio_min")
    if vr_min is not None and (volume_ratio is None or volume_ratio < vr_min):
        return False
    return True


# 目标占比容差：A股整手取整会让实际占比与目标差零点几个百分点，留一点余量，免得
# 「其实已经到了」被反复判成未达到而重复下单。
_WEIGHT_TOL = 0.002


def _pick_tier(
    tiers: list[dict],
    kind: str,
    price: float | None,
    volume_ratio: float | None,
    current_weight: float,
    now: datetime | None = None,
) -> dict | None:
    """从一条梯子里挑本 tick 该执行的那一档：**已满足、且目标尚未达到的最浅档**。

    「逐档分批」就靠这条规则实现，且**不需要持久化「执行到第几档」**——当前持仓占比
    本身就是进度：

        价格停在 8.70（穿越三档）时
          tick1 占比 18.4% → 第1档(13.8%) 未达到 → 卖到 13.8%
          tick2 占比 13.8% → 第1档已达、第2档(9%)  未达到 → 卖到 9%
          tick3 占比  9.0% → 第2档已达、第3档(0%)  未达到 → 清仓

    每个 tick 每只票最多一笔委托；价格回抽后不会重复卖（目标已达）。

    ``kind`` 只影响「达到」的方向：``exit`` 是占比降到目标以下，``entry`` 是升到目标以上。

    ``now`` 只为回测注入（透传给 ``_trigger_satisfied``）；生产不传。
    """
    for a in tiers:
        if not _trigger_satisfied(a, price, None, volume_ratio, now):
            continue
        tw = a.get("target_weight")
        if tw is None:
            continue
        reached = (
            current_weight <= tw + _WEIGHT_TOL
            if kind == "exit"
            else current_weight >= tw - _WEIGHT_TOL
        )
        if not reached:
            return a
    return None


def get_monitor_conditions(
    user_id: int,
    *,
    plan_row: dict | None = None,
    bars: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """「监控条件」卡片数据：次日行动计划的监控条件 + 价 + 触发状态。

    取数口径是**最近一次生成的计划**（不是「今天的计划」）——计划生成时执行日是
    下一个交易日，用「今天」去比会永远查不到，卡片恒空。计划自己的执行日作为
    「监控日期」展示。触发状态与执行层共用 ``_trigger_satisfied``，口径不分裂。

    另外**读时也做一次动作归一与分档**：归一最初只加在写时，于是此前落库的历史计划
    （同一只票一条 hold 基准行 + 一条条件动作行）在卡片上重复显示、两条的目标占比
    还互相矛盾。执行层与报告早就折叠掉了，这里补齐。

    **逐档一行**：同一只票的多档触发展开成多行（``tier_index`` / ``tier_count`` /
    ``is_first``），每档有自己的 ``triggered``——这张卡片的用途正是「现在走到哪一档了」。
    ``code_count`` 是**标的**数（≠ 行数），前端副标题用它，免得「监控 6 项」其实是 3 只票。
    单档计划仍是一行，字段与展开前一致。

    ## 三个注入缝（**默认值 = 实盘行为，逐字节不变**）

    回测要按**模拟日**问同一张卡片，而实盘那三个来源在回测里全都够不到：

    - ``plan_row``：缺省走 ``_get_latest_plan_row``（最新一份）。回测必须传**模拟日那天**
      的那份——"最新"可能是另一个 run 写的、或比模拟日更晚的计划。
    - ``bars``：``{code: Bar}``；缺省走 ``account_mod.get_quotes``（实时快照）。回测不能走
      实时：``asof_scope`` 内 ``get_price_snapshots`` 直接返回空，照搬会得到一张
      「价格全是 None、一档都不触发」的空卡片。
    - ``now``：缺省不传（``_trigger_satisfied`` 用真实墙钟）。回测必须传模拟时刻，否则
      09:30 的时间闸门按真实墙钟判，回测卡片会在盘前被整片判成"未触发"。

    ## 传了 ``bars`` 就按**执行口径**取价

    ``kind == "exit"`` 用当日 ``low``、``"entry"`` 用当日 ``high``、其余用 ``close``——
    与撮合完全同一判据（执行层就是拿 ``bar.low`` 探卖、``bar.high`` 探买）。这样卡片才能
    回答「这天为什么成交/为什么没成交」，而不是给一个和成交对不上的数。每行同时带回
    开/高/低/收，前端据此把判据写在脸上。

    判据本身**只走** ``_trigger_satisfied`` / ``_pick_tier``，这里不许另写一份。
    """
    row = plan_row if plan_row is not None else _get_latest_plan_row(user_id)
    plan = (row or {}).get("plan") or {}
    monitor_date = (row or {}).get("trade_date") or ""
    ladders, _warnings = build_ladders(plan.get("actions") or [])
    if not ladders:
        return {"trade_date": monitor_date, "created_at": 0.0, "code_count": 0, "actions": []}

    quotes = account_mod.get_quotes(list(ladders.keys())) if bars is None else {}
    out: list[dict] = []
    for code, by_kind in ladders.items():
        q = quotes.get(code) or {}
        bar = (bars or {}).get(code)
        if bar is not None:
            ohlc = {"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close}
            vr, prev_close = bar.volume_ratio, bar.prev_close
        else:
            ohlc = {"open": None, "high": None, "low": None, "close": None}
            vr, prev_close = q.get("volume_ratio"), q.get("prev_close")
        tiers = [(k, a) for k in ("hold", "exit", "entry") for a in by_kind.get(k) or []]
        total = len(tiers)
        for i, (kind, a) in enumerate(tiers):
            if bar is not None:
                # 执行口径：卖/减用当日最低、买/建用当日最高、其余用收盘。
                price = {
                    "exit": bar.low, "entry": bar.high,
                }.get(kind, bar.close)
            else:
                price = q.get("price")
            out.append({
                "code": code,
                "name": a.get("name"),
                "action": a.get("action"),
                "target_weight": a.get("target_weight"),
                "trigger_type": a.get("trigger_type") or "none",
                "trigger_price": a.get("trigger_price"),
                "volume_ratio_min": a.get("volume_ratio_min"),
                "reason": a.get("reason"),
                "kind": kind,
                "tier_index": i + 1,
                "tier_count": total,
                "is_first": i == 0,
                "price": price,
                "prev_close": prev_close,
                "volume_ratio": vr,
                "open": ohlc["open"],
                "high": ohlc["high"],
                "low": ohlc["low"],
                "close": ohlc["close"],
                # ``now`` 只在注入时透传；生产不传 ⇒ ``_trigger_satisfied`` 用真实墙钟，
                # 与改造前逐位相同。
                "triggered": _trigger_satisfied(a, price, monitor_date, vr, now),
            })
    return {
        "trade_date": monitor_date,
        "created_at": (row or {}).get("created_at") or 0.0,
        "code_count": len(ladders),
        "actions": out,
    }


def _target_qty(target_weight, total_assets: float, price: float, code: str) -> int | None:
    """目标占比 → 目标股数（A股整手）。target_weight 为空或价不可得返回 None。"""
    if target_weight is None or price is None or price <= 0:
        return None
    qty = int(target_weight * total_assets / price)
    if trade._is_a_share(code):
        qty = qty // 100 * 100
    return qty


def run_execution(user_id: int, clock=None) -> dict:
    """执行层**入口**：跑一次执行，成交则派发通知。

    刻意做成壳而不是把通知塞进 ``trade.place_order``（那里 ``trade.py`` 的 TODO 位置）：

    - ``_execute_plan`` 对每处 ``place_order`` **只捕 ``ServiceError``**。若通知在
      ``place_order`` 内部抛出别的异常（``_get_configs`` 的 DB 异常、webhook 库的
      ImportError……），它会贯穿整个循环，**静默丢掉该用户本次 tick 的剩余全部档位**。
      通知是附属品，绝不能有能力改成交。放在壳里，最坏也只是这条通知没发出去。
    - 一个 tick 常出 3~5 笔，逐笔发会刷屏；在壳里拿得到完整列表，可以合并成一条。

    ``clock is not None`` = 回测：**绝不发通知**——否则跑一次历史回测会拿模拟成交去刷真
    webhook，而回测里每个模拟日都会走到这里。
    """
    res = _run_execution(user_id, clock)
    if clock is None and res.get("trades"):
        # 再包一层 try：成交**已经 commit**，此刻任何异常都不该改变执行结果，更不该让它
        # 看起来像"执行失败"。``notify_trades`` 内部已经只管起线程并吞线程内的错，这里
        # 兜的是起线程本身失败这类边界（线程数上限、解释器正在关闭）。
        try:
            notify.notify_trades(user_id, res["trades"])
        except Exception as e:  # noqa: BLE001
            print(f"[trust] 用户 {user_id} 成交通知派发失败（成交不受影响）：{e}", flush=True)
    return res


def _run_execution(user_id: int, clock=None) -> dict:
    """执行层：优先按组合级次日计划（触发门控 + 再平衡），无计划回落旧评级逻辑。

    ``clock`` 只为回测注入（模拟日 + 模拟盘中 bar）；生产不传 = 实时路径，
    每一步都走原来那套 ``datetime.now()`` / ``get_quotes`` / ``get_latest_price``。

    由 ``run_execution`` 包壳调用（壳负责成交通知），一般不要直接调它。
    """
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
    finally:
        session.close()
    # ``is_active`` 是**实盘开关**，而影子账户的它必须硬性为 False（``app/backtest.py`` 里
    # 写死）——否则实盘调度器会在真实盘中拿实时价交易回测账簿。回测走显式 ``clock``，
    # 那本身就等价于"本次执行已被明确授权"，所以 clock 非空时不受这个开关约束。
    if not cfg or not cfg.book_created or (clock is None and not cfg.is_active):
        return {"trades": [], "skipped": "not_active"}

    positions = account_mod.get_positions(user_id, 1, clock=clock)
    scope = cfg.stock_scope
    # 空仓托管：托管簿无持仓但选池覆盖自选/全市场且有现金 → 允许托管自由建仓
    empty_start = (not positions) and scope in (1, 2) and (cfg.available_cash or 0.0) > 0
    if not positions and not empty_start:
        return {"trades": [], "skipped": "no_positions"}

    plan = _get_today_plan(user_id, date=clock.date if clock is not None else None)
    has_plan = bool(plan and plan.get("actions"))
    # 当日已成交笔数作为单日笔数额度的**起点**（旧的评级兜底路径没有分档概念，额度用尽
    # 就整体停手；计划路径则把额度交给 _execute_plan，让它只放行清仓档——详见那里的说明）。
    used = _today_trade_count(user_id, clock=clock) if cfg.risk_max_trades_day else 0
    if cfg.risk_max_trades_day and used >= cfg.risk_max_trades_day and not has_plan:
        return {"trades": [], "skipped": "max_trades"}

    if has_plan:
        return _execute_plan(user_id, cfg, positions, plan, budget_used=used, clock=clock)
    if clock is not None:
        # 回测**绝不**回落评级兜底路径：``_execute_legacy`` 读 ``_get_latest_rating``，
        # 那个查询没有任何日期维度，会串到别的日期上去。宁可当日不交易，也不能拿
        # 错日期的信号下单——那正是回测唯一要验的东西。
        return {"trades": [], "skipped": "no_plan_backtest"}
    return _execute_legacy(user_id, cfg, positions, empty_start)


def _execute_plan(
    user_id: int, cfg, positions: list[dict], plan: dict, budget_used: int = 0, clock=None
) -> dict:
    """按组合级次日计划机械执行：止损最高优先 + 分档触发 + 目标仓位再平衡。

    同一只票的同向多档触发排成一条**梯子**（``app.plan_actions.build_ladders``），本 tick
    只推进**一档**：价格一次穿越三档时不会一笔到底，而是每分钟走一步（见 ``_pick_tier``）。
    「走到第几档」不落库——当前持仓占比本身就是进度。

    ## 行情走 ``clock.bars()``，四个语义位各取所需

    改造前这里只有一个标量 ``price``，却承担了四件事。换成 ``Bar`` 后逐个归位：

    | 语义位 | 取法 | 理由 |
    |---|---|---|
    | 可交易性 | ``bar`` 是否存在 + ``bar.tradable`` | 停牌/无行情 → 跳过，持仓保留 |
    | 止损比较 | ``bar.low`` | 日内最低曾跌破止损线即触发 |
    | 选档标量 | exit→``bar.low`` / entry→``bar.high`` | 「当日 K 线是否穿越触发价」 |
    | 定仓与成交 | ``fill_price(action, bar)`` | 日内触发，跳空按开盘 |

    **实时路径逐位不变**：``LiveClock`` 给的是退化 bar（``open == high == low == close == 现价``），
    上表四行取到的都是现价，与改造前的标量路径完全一致。所以执行内核仍然只有一份，
    计划卡片用的 ``_pick_tier`` / ``_trigger_satisfied`` 也不可能与这里口径分裂。
    """
    # 归一 + 分档：此前靠 ``{code: a}`` 的后写覆盖，等于把结果押在计划里动作的排列顺序上
    # （hold 在后就会盖掉条件动作）；而且同 code 的多档会被压成一条，第 2 档之后永不生效。
    ladders, _warnings = build_ladders(plan.get("actions") or [])
    cash = cfg.available_cash
    total_mv = sum((p.get("market_value") or 0.0) for p in positions)
    total_assets = cash + total_mv
    # 单票上限只在用户**显式设置**时生效。未设置（None）= 不限制：买多少完全由团队通过
    # 计划的 target_weight 决定，执行层不再用风格档的默认上限去截断。
    single_cap = cfg.risk_max_position_pct

    codes = [p["stock_code"] for p in positions]
    if not codes:
        # 空仓起步：候选来自计划里的加仓梯子，行情要连它们一起取
        codes = [c for c, bk in ladders.items() if bk.get("entry")]
    clock = clock or LiveClock()
    bars = clock.bars(codes) if codes else {}

    trades: list[dict] = []
    buy_targets: list[dict] = []  # {code, name, target_weight, reason}

    # 单日笔数额度：**只有把仓位调到非零目标的成交才消耗**。清仓档（目标占比 0）与显式
    # 止损不占额度——否则一次跳空穿越三档就用光额度，最深那档（往往正是清仓/硬止损）
    # 永远不执行，仓位卡在半途。budget_used 由 run_execution 用当日已成交笔数播种，
    # 之后只累加非清仓成交，所以「额度用尽」也不会挡住清仓。
    cap = cfg.risk_max_trades_day or 0
    budget_used = int(budget_used or 0)
    budget_exhausted = bool(cap) and budget_used >= cap

    def _spend() -> None:
        """记一笔消耗额度的成交（非清仓）。买入与减仓档都走这里。"""
        nonlocal budget_used, budget_exhausted
        if not cap:
            return
        budget_used += 1
        if budget_used >= cap:
            budget_exhausted = True

    # 1) 卖出/减仓 + 止损
    for p in positions:
        code, name = p["stock_code"], p["stock_name"]
        bar = bars.get(code)
        # 可交易性：当日无 bar（停牌/未上市/代码错）→ 跳过。持仓保留，不按 0 盯市。
        # 这是改造前 `price is None: continue` 的同一个语义位。
        if bar is None or not bar.tradable:
            continue
        sell_qty, reason, closing, px = 0, "", False, None
        if (
            cfg.risk_stop_loss_pct
            and p.get("cost_price")
            and bar.low < p["cost_price"] * (1 - cfg.risk_stop_loss_pct)
        ):
            # 显式止损：用户的硬风控，优先级高于计划，也不占单日笔数额度。
            # 比较用 bar.low（日内最低曾破线即触发）；实时下 bar.low == 现价，与改造前一致。
            sell_qty = p["available_qty"]
            reason = f"止损触发：价格 {bar.low:.2f} 低于成本 {p['cost_price']:.2f}"
            closing = True
            # 硬止损本质是「跌破止损线」这一档：借用同一个 fill_price 口径成交，
            # 跳空低开时按开盘价（更差的一端），不假设能按止损线成交。
            stop_line = p["cost_price"] * (1 - cfg.risk_stop_loss_pct)
            px = fill_price({"trigger_type": "price_below", "trigger_price": stop_line}, bar)
        else:
            tiers = ladders.get(code, {}).get("exit") or []
            if budget_exhausted:
                # 额度用尽后只剩清仓档能走。**不能跳过这只票了事**——否则跳空当天最深
                # 的清仓档会被前面几档连累，永远不执行，仓位卡在半途。
                tiers = [a for a in tiers if not a.get("target_weight")]
            weight = (p.get("market_value") or 0.0) / total_assets if total_assets > 0 else 0.0
            # 选档标量用 bar.low：减仓梯子都是 price_below 档，「当日最低是否穿越触发价」
            # 正是 low <= tp —— 谓词一个字都不用改。
            act = _pick_tier(tiers, "exit", bar.low, bar.volume_ratio, weight, clock.now)
            if act is not None:
                # 定仓价用 fill_price 而非触发价：跳空时按开盘成交，数量才不会虚增/虚减。
                fill = fill_price(act, bar)
                px = fill
                tq = _target_qty(act.get("target_weight"), total_assets, fill, code)
                if tq is not None:
                    delta = tq - p["hold_qty"]
                    if delta < 0:
                        sell_qty = min(-delta, p["available_qty"])
                        reason = f"计划 {act.get('action')}：目标占比 {act.get('target_weight')}"
                        closing = not act.get("target_weight")
        if sell_qty > 0 and p["available_qty"] > 0:
            # 成交价显式传入，不再让 place_order 自己去取一次价——定仓价与成交价强制同源
            # （旧路径靠 60s 快照缓存"偶然一致"，回测里没有这层运气）。px 为 None 只可能
            # 出现在"有卖量却没走上面任一条分支"这种不该发生的组合上，兜底用开盘价。
            px = px if px is not None else bar.open
            try:
                r = trade.place_order(
                    user_id, code, name, 1, sell_qty, source=1, ai_reason=reason,
                    price=px, ts=clock.ts, clock=clock,
                )
                trades.append(r)
                if not closing:
                    _spend()
            except ServiceError:
                pass

    # 2) 买入候选：空仓起步时按 entry 梯子建仓；有持仓时只给**已有持仓**的票加仓
    #    （不为计划里提到的新标的开仓——沿用原行为，避免一句提及就凭空买入）。
    pos_by_code = {p["stock_code"]: p for p in positions}
    if not budget_exhausted:  # 买入永远不是清仓，额度用尽直接不做
        for code, by_kind in ladders.items():
            entry = by_kind.get("entry") or []
            if not entry:
                continue
            p = pos_by_code.get(code)
            if positions and p is None:
                continue
            bar = bars.get(code)
            if bar is None or not bar.tradable:
                continue
            cur_mv = (p.get("market_value") or 0.0) if p else 0.0
            weight = cur_mv / total_assets if total_assets > 0 else 0.0
            # 加仓梯子都是 price_above 档：「当日最高是否穿越触发价」正是 high >= tp。
            act = _pick_tier(entry, "entry", bar.high, bar.volume_ratio, weight, clock.now)
            if act is None or not act.get("target_weight"):
                continue
            buy_targets.append({
                "code": code,
                "name": (p["stock_name"] if p else (act.get("name") or code)),
                "target_weight": act.get("target_weight"),
                "reason": act.get("reason", ""),
                # 定仓价与成交价必须同源，在选档这一刻就定死，后面不再二次取价。
                "fill": fill_price(act, bar),
            })

    # 3) 买入：目标占比换股数，现金协调 + 单票上限，按 delta 降序（先加最欠配的）
    if buy_targets:
        priced = []
        for bt in buy_targets:
            # 用选档时定下的 fill，**不再二次取价**：旧路径在这里又调一次
            # ``get_latest_price``，靠 60s 快照缓存与上面 _pick_tier 偶然一致；
            # 回测里没有这层运气，且那是当日收盘价，会构成未来函数。
            price = bt["fill"]
            if not price:
                continue
            tq = _target_qty(bt["target_weight"], total_assets, price, bt["code"])
            cur = next((p["hold_qty"] for p in positions if p["stock_code"] == bt["code"]), 0)
            delta = tq - cur if tq else 0
            if delta > 0:
                priced.append({**bt, "price": price, "delta": delta})
        priced.sort(key=lambda c: c["delta"], reverse=True)

        for c in priced:
            if cash <= 0:
                break
            code, price = c["code"], c["price"]
            qty = c["delta"]
            cur_mv = next((p["market_value"] for p in positions if p["stock_code"] == code), 0.0)
            if single_cap and price:
                cap_qty = int((single_cap * total_assets - cur_mv) / price)
                qty = min(qty, max(cap_qty, 0))
            if trade._is_a_share(code):
                qty = qty // 100 * 100
            affordable = int(cash / (price * (1 + cfg.fee_commission_rate))) if price else 0
            if trade._is_a_share(code):
                affordable = affordable // 100 * 100
            qty = min(qty, affordable)
            if qty <= 0:
                continue
            try:
                r = trade.place_order(
                    user_id, code, c["name"], 0, qty, source=1,
                    ai_reason=f"计划加仓/建仓 目标占比 {c['target_weight']}：{c['reason']}",
                    price=price, ts=clock.ts, clock=clock,
                )
                trades.append(r)
                cash -= r["amount"] + r["fee"]
            except ServiceError:
                pass
            _spend()
            if budget_exhausted:
                break

    return {"trades": trades, "skipped": None}


def _execute_legacy(user_id: int, cfg, positions: list[dict], empty_start: bool) -> dict:
    """旧评级→动作逻辑（无组合计划时兜底）：Sell清仓/Underweight减半/Buy加仓。"""
    trades: list[dict] = []
    buy_candidates: list[tuple[str, str, int]] = []  # (code, name, weight)

    for p in positions:
        code, name = p["stock_code"], p["stock_name"]
        price = p.get("price")
        if price is None:
            continue
        rating = _get_latest_rating(user_id, code)
        action = _rating_to_action(rating)

        sell_qty, reason = 0, ""
        if (
            cfg.risk_stop_loss_pct
            and p.get("cost_price")
            and price < p["cost_price"] * (1 - cfg.risk_stop_loss_pct)
        ):
            sell_qty = p["available_qty"]
            reason = f"止损触发：现价 {price:.2f} 低于成本 {p['cost_price']:.2f}"
        elif action == "sell":
            sell_qty = p["available_qty"]
            reason = f"评级 {rating}（卖出），清仓"
        elif action == "underweight":
            sell_qty = p["available_qty"] // 2
            reason = f"评级 {rating}（减持），减半"
        elif action == "buy":
            buy_candidates.append((code, name, 2))
        elif action == "overweight":
            buy_candidates.append((code, name, 1))

        if sell_qty > 0 and p["available_qty"] > 0:
            try:
                r = trade.place_order(user_id, code, name, 1, sell_qty, source=1, ai_reason=reason)
                trades.append(r)
            except ServiceError:
                pass
        if cfg.risk_max_trades_day and len(trades) >= cfg.risk_max_trades_day:
            return {"trades": trades, "skipped": "max_trades"}

    if empty_start:
        for s in get_managed_symbols(user_id):
            rating = _get_latest_rating(user_id, s["code"])
            action = _rating_to_action(rating)
            if action == "buy":
                buy_candidates.append((s["code"], s["name"], 2))
            elif action == "overweight":
                buy_candidates.append((s["code"], s["name"], 1))
        if not buy_candidates:
            return {"trades": [], "skipped": "no_buy_rating"}

    if buy_candidates:
        session = db.get_session()
        try:
            cash = session.query(db.TrustConfig).filter(
                db.TrustConfig.user_id == user_id
            ).first().available_cash
        finally:
            session.close()
        total_weight = sum(w for _, _, w in buy_candidates)
        for code, name, weight in buy_candidates:
            if cash <= 0 or total_weight <= 0:
                break
            alloc = cash * weight / total_weight
            price = account_mod.get_latest_price(code)
            if price is None or price <= 0:
                continue
            qty = int(alloc / price)
            if trade._is_a_share(code):
                qty = qty // 100 * 100
            if qty <= 0:
                continue
            try:
                r = trade.place_order(
                    user_id, code, name, 0, qty, source=1,
                    ai_reason=f"评级 {_get_latest_rating(user_id, code)} "
                              f"{'建仓' if not positions else '加仓'}",
                )
                trades.append(r)
                cash -= r["amount"] + r["fee"]
            except ServiceError:
                pass
            if cfg.risk_max_trades_day and len(trades) >= cfg.risk_max_trades_day:
                break

    return {"trades": trades, "skipped": None}


# ---------- 计划层 / 日结 / 调度 ----------

def run_plan_for_user(user_id: int, date: str | None = None, on_progress=None,
                      should_cancel=None, clock=None) -> list[dict]:
    """计划层 Stage1：对在管全集（持仓 ∪ 选池）并行跑深度分析。date 默认最近交易日。

    ``on_progress(done, total, result)`` 可选，透传给 run_plan 上报逐只进展。
    ``should_cancel()`` 可选：返回 True 时中止（抛 ``PlanCancelled``），未开始的分析被取消，
    已经在跑的 LLM 调用无法打断，会跑完但结果被丢弃。

    ``clock`` 只为回测注入：它同时承载「as-of 作用域」（在 ``run_analysis_cached`` 的
    worker 线程里开启）与「按模拟日盯市的账户现状」两件事。生产不传 = 实时。
    """
    from app.analysis_service import run_plan

    if not date:
        from app.intent import latest_trading_day

        date = latest_trading_day()
    symbols = [s["code"] for s in get_analysis_universe(user_id)]
    if not symbols:
        return []
    return run_plan(
        user_id, symbols, date, on_progress=on_progress,
        should_cancel=should_cancel, clock=clock,
    )


# ---------- 手动触发计划生成（盘中/盘后均可） ----------

_plan_tasks: dict[str, dict] = {}
_plan_lock = threading.Lock()


def check_plan_preconditions(user_id: int) -> None:
    """生成计划的前置条件：托管配置存在且已建簿。

    手动入口与定时入口共用同一套前置条件——此前手动入口不校验 TrustConfig，
    导致「没有托管配置 → 在管池为空 → Stage1 静默空转 → 计划照出」的盲飞。
    """
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
    finally:
        session.close()
    if not cfg:
        raise ServiceError("尚未开通财团托管：请先在托管页开启托管并导入持仓")
    if not cfg.book_created:
        raise ServiceError("托管簿尚未建簿：请先「粘贴持仓快照」或「空仓启动」")


def validate_portfolio_data(user_id: int) -> tuple[bool, list[str]]:
    """生成次日计划前的数据完整性校验：持仓现价必须可得，否则计划无意义。

    返回 (是否通过, 错误列表)。空仓起步无持仓视为通过（计划将全为建仓候选）。
    """
    positions = account_mod.get_positions(user_id, 1)
    if not positions:
        return True, []
    errors: list[str] = []
    for p in positions:
        if p.get("price") is None:
            errors.append(f"{p['stock_name']}（{p['stock_code']}）现价缺失")
    return (not errors, errors)


def validate_research_coverage(
    user_id: int, results: list[dict]
) -> tuple[bool, list[str], list[str], list[str]]:
    """Stage1 研究层覆盖硬校验。

    返回 ``(是否通过, 阻断错误, 非阻断警告, 已覆盖标的)``。

    阻断条件（任一命中即不出计划）：
      - 在管全集为空（托管配置/选池问题）；
      - 未产出任何有效研究结论（Stage1 整体失败）；
      - 任意一只**托管簿持仓**没有研究结论 —— 持仓是真实敞口，
        没有研究依据就不该由执行层自动买卖。

    候选标的（自选池里非持仓的部分）研究失败只记为警告，不阻断。
    """
    resolved: list[str] = []
    failed: dict[str, str] = {}
    for r in results or []:
        code = r.get("ticker") or "?"
        if r.get("error") or not r.get("report"):
            failed[code] = str(r.get("error") or "无研究结论")
        else:
            resolved.append(code)

    universe = [s["code"] for s in get_analysis_universe(user_id)]
    held = [p["stock_code"] for p in account_mod.get_positions(user_id, 1)]
    resolved_set = set(resolved)

    errors: list[str] = []
    warnings: list[str] = []
    if not universe:
        errors.append("在管标的池为空（请检查「操作股票范围」与自选分组设置）")
    if not resolved_set:
        errors.append("研究层未产出任何研究结论，无法生成有依据的计划")
    missing_held = [c for c in held if c not in resolved_set]
    if missing_held:
        errors.append("以下持仓未获得研究结论，不会在无依据的情况下自动交易：" + "、".join(missing_held))
    missing_candidate = [c for c in universe if c not in resolved_set and c not in set(held)]
    if missing_candidate:
        warnings.append("以下候选标的研究失败，本次计划不覆盖：" + "、".join(missing_candidate))
    if not errors and failed and not missing_candidate:
        warnings.append("研究失败的标的：" + "、".join(sorted(failed)))
    return (not errors, errors, warnings, resolved)


def plan_target_date(now: datetime | None = None) -> str:
    """本次计划针对哪个交易日执行：交易日 15:00 前 → 今天；收盘后/周末 → 下一工作日。

    盘中生成必须针对**今天**，否则刚生成的计划当天执行层取不到（执行层只认
    ``trade_date == 今天``），「按最新计划执行」就落不了地。
    """
    now = now or datetime.now()
    d = now.date()
    if d.weekday() < 5 and now.hour < MARKET_CLOSE_HOUR:
        return d.isoformat()
    return next_trading_day(d)


def check_plan_generation_allowed(user_id: int, target_date: str | None = None) -> dict:
    """生成前闸门，返回 ``{mode, target_date, message, existing}``。

    - ``ok``      ：该执行日还没有计划，可生成。
    - ``confirm`` ：盘中且当日计划已存在 → 重跑会覆盖旧计划、执行层改按最新计划执行，须用户确认。
    - ``blocked`` ：盘后且该执行日的计划已生成 → 当日盘后只生成一次，不再重复生产。

    盘后重复生成没有任何收益：同一天收盘后的行情/账户/研究输入完全一致，只会白烧一次
    研究层深析（每只 10–15 分钟），还会因为采样随机性把同一只票的结论改掉，
    让「昨天定的计划」和「今天看到的计划」对不上。
    """
    target = target_date or plan_target_date()
    today = datetime.now().strftime("%Y-%m-%d")
    intraday = target == today  # 执行日就是今天 ⇒ 处于盘中（或开盘前）
    session = db.get_session()
    try:
        row = (
            session.query(db.TrustPlan)
            .filter(db.TrustPlan.user_id == user_id, db.TrustPlan.trade_date == target)
            .order_by(db.TrustPlan.id.desc())
            .first()
        )
        existing = bool(row)
    finally:
        session.close()
    if not existing:
        return {"mode": "ok", "target_date": target, "message": "", "existing": False}
    if intraday:
        return {
            "mode": "confirm",
            "target_date": target,
            "existing": True,
            "message": (
                f"今日（{target}）已有行动计划。重新生成将产出一份**最新**的当日计划并覆盖旧计划，"
                f"执行层从现在起按最新计划执行（旧计划作废）。是否确认重新生成？"
            ),
        }
    return {
        "mode": "blocked",
        "target_date": target,
        "existing": True,
        "message": (
            f"当日盘后计划已生成（执行日 {target}），无需重复生成。"
            f"重复生成只会白跑一次研究层深析（每只 10–15 分钟），且因模型采样随机性，"
            f"同一只票的结论可能被改掉，反而与已定的计划冲突。"
        ),
    }


def submit_plan_generation(user_id: int, force: bool = False) -> str:
    """手动触发次日行动计划生成（Stage1 逐标的深析 + Stage2 组合决策），异步返回 task_id。

    盘中/盘后皆可调用；生成结果落 TrustPlan，执行层按计划执行（盘中生成的计划当日生效）。
    若该用户已有运行中的生成任务，复用其 task_id（避免重复跑）。
    ``force=True`` 表示用户已在弹窗里确认覆盖（盘中已有当日计划的情形）。
    """
    with _plan_lock:
        for tid, t in _plan_tasks.items():
            if t.get("user_id") == user_id and t.get("status") == "running":
                return tid
        task_id = uuid.uuid4().hex
        _plan_tasks[task_id] = {
            "user_id": user_id, "status": "running", "result": None, "error": None,
            "stage": "queued", "message": "任务已入队，等待开始", "done": 0, "total": 0,
            "warnings": [], "cancel": False, "force": bool(force),
        }
    threading.Thread(target=_plan_worker, args=(task_id, user_id), daemon=True).start()
    return task_id


def cancel_plan_generation(task_id: str, user_id: int) -> dict:
    """暂停计划生成：置取消标志，由生成线程在下一个检查点收手。

    已经发出的 LLM 调用无法中断，会跑完并**丢弃结果**（不落库、不出计划）。
    """
    with _plan_lock:
        t = _plan_tasks.get(task_id)
        if not t or t.get("user_id") != user_id:
            return {"ok": False, "message": "任务不存在或不属于当前用户"}
        if t.get("status") != "running":
            return {"ok": False, "status": t.get("status"), "message": "任务已结束，无需暂停"}
        t["cancel"] = True
        if t.get("stage") == "queued":
            t["stage"] = "cancelling"
            t["message"] = "正在暂停…"
    return {"ok": True, "message": "已请求暂停（已开始的分析会跑完再丢弃）"}


def _plan_progress(task_id: str, stage: str, message: str, done: int = 0, total: int = 0) -> None:
    """更新任务进度（供 web 端轮询实时展示）。不改变 status/result。"""
    with _plan_lock:
        t = _plan_tasks.get(task_id)
        if t is None:
            return
        t["stage"] = stage
        t["message"] = message
        t["done"] = done
        t["total"] = total


def _plan_finish(task_id: str, **fields) -> None:
    """收尾写入任务状态，保留已上报的进度字段。"""
    with _plan_lock:
        t = _plan_tasks.get(task_id) or {}
        t.update(fields)
        _plan_tasks[task_id] = t


def generate_plan_for_user(user_id: int, on_progress=None, force: bool = False,
                           should_cancel=None) -> tuple[dict, list[str]]:
    """完整的次日行动计划生成：前置校验 → 生成闸门 → Stage1 研究层 → 覆盖校验 → Stage2 组合决策。

    手动入口（``_plan_worker``）与 15:05 定时入口（``_sched_plan``）共用本函数，
    保证两条路径的前置条件、生成闸门与逐层校验完全一致。

    ``on_progress(stage, message, done, total)`` 可选，用于实时上报进度。
    ``force=True`` 跳过「盘中已有当日计划」的确认要求（用户已在弹窗确认覆盖）。
    ``should_cancel()`` 可选：返回 True 时抛 ``PlanCancelled``，不落库、不出计划。
    返回 ``(plan, warnings)``；硬校验不通过抛 ``ServiceError``，需确认时抛 ``PlanNeedsConfirm``。
    """
    def _p(stage: str, message: str, done: int = 0, total: int = 0) -> None:
        if on_progress is not None:
            on_progress(stage, message, done, total)

    def _cancelled() -> bool:
        return bool(should_cancel is not None and should_cancel())

    _p("check", "校验托管配置与持仓数据完整性")
    check_plan_preconditions(user_id)
    ok, errors = validate_portfolio_data(user_id)
    if not ok:
        raise ServiceError("数据校验不通过：" + "；".join(errors))

    # 生成闸门：盘后当日只生成一次；盘中已有当日计划须显式确认覆盖
    target_date = plan_target_date()
    gate = check_plan_generation_allowed(user_id, target_date)
    if gate["mode"] == "blocked" and not force:
        raise ServiceError(gate["message"])
    if gate["mode"] == "confirm" and not force:
        raise PlanNeedsConfirm(gate["message"], trade_date=target_date)

    # Stage1 研究层：逐标的深析（组合感知），每完成一只上报一次
    # 进度文案不再重复层名（阶段名由前端 chip 展示），只描述本层正在做什么
    def _on_research(done: int, total: int, res: dict) -> None:
        code = res.get("ticker") or "?"
        if res.get("error"):
            # 带上失败原因：静默失败正是上一版"计划照出但毫无研究依据"的根因
            msg = f"{done}/{total} · {code} 分析失败（{str(res['error'])[:60]}）"
        else:
            rating = res.get("decision_zh") or res.get("decision") or "—"
            msg = f"{done}/{total} 已完成 · {code} 评级 {rating}"
        _p("research", msg, done, total)

    _p("research", "准备逐标的深度分析（每只 10–15 分钟）")
    results = run_plan_for_user(user_id, on_progress=_on_research, should_cancel=should_cancel)
    if _cancelled():
        raise PlanCancelled()

    # 研究层覆盖硬校验：宁可停在这里，也不让计划在无研究依据下自动交易
    ok, errors, warnings, resolved = validate_research_coverage(user_id, results)
    if not ok:
        raise ServiceError("研究层校验不通过：" + "；".join(errors))
    _p("research", f"完成：{len(resolved)} 只标的已产出研究结论", len(resolved), len(resolved))

    # Stage2 组合决策层：组合分析师 → 风控官 → 组合经理（三个节点，用于进度推进）
    team_order = {"portfolio_analyst": 1, "risk_officer": 2, "portfolio_manager": 3}

    def _on_team(node: str, label: str) -> None:
        _p("portfolio", label, team_order.get(node, 0), len(team_order))

    if _cancelled():
        raise PlanCancelled()
    _p("portfolio", "准备组合决策会议")
    from app.analysis_service import run_portfolio_plan_for_user

    try:
        plan = run_portfolio_plan_for_user(user_id, target_date, on_progress=_on_team)
    except AccountLevelLLMError as fatal:
        # 账户级失败（余额耗尽 / key 失效）与"这次没拿到结构化结果"是两回事：前者重试无用，
        # 后者才是「稍后重试」。混成一句话会让用户以为等一会儿就好。
        raise ServiceError(f"LLM 账户不可用（{fatal}）：请检查 API key 与账户余额") from fatal
    if _cancelled():
        raise PlanCancelled()
    if not plan or not plan.get("actions"):
        raise ServiceError("组合决策层未产出有效行动方案，请稍后重试")
    return plan, warnings


def _plan_worker(task_id: str, user_id: int) -> None:
    """后台跑完整计划生成，并把每一层的进展实时写进任务状态供 web 端轮询。"""
    def _report(stage: str, message: str, done: int = 0, total: int = 0) -> None:
        _plan_progress(task_id, stage, message, done, total)

    def _should_cancel() -> bool:
        with _plan_lock:
            t = _plan_tasks.get(task_id) or {}
            return bool(t.get("cancel")) or t.get("status") != "running"

    with _plan_lock:
        force = bool((_plan_tasks.get(task_id) or {}).get("force"))

    try:
        plan, warnings = generate_plan_for_user(
            user_id, on_progress=_report, force=force, should_cancel=_should_cancel
        )
        _plan_progress(task_id, "done", f"已生成 {len(plan['actions'])} 项行动决策")
        _plan_finish(task_id, status="done", result=plan, error=None, warnings=warnings)
    except PlanCancelled:
        _plan_finish(task_id, status="cancelled", result=None,
                     error="已暂停：本次生成作废，未产出新计划", warnings=[])
    except PlanNeedsConfirm as e:
        # 理论上到不了这里（手动入口在提交前已问过、定时入口不会传 force），兜底成可读错误
        _plan_finish(task_id, status="error", result=None, error=e.message, warnings=[])
    except Exception as e:  # noqa: BLE001
        _plan_finish(task_id, status="error", result=None, error=str(e))


def get_plan_generation_status(task_id: str) -> dict:
    """任务进度快照（不含计划正文——正文含研究快照，体积大，不该每 5 秒轮询传一遍）。"""
    with _plan_lock:
        t = _plan_tasks.get(task_id)
        if not t:
            return {"status": "unknown"}
        return {
            "status": t.get("status"),
            "stage": t.get("stage"),
            "message": t.get("message"),
            "done": t.get("done", 0),
            "total": t.get("total", 0),
            "error": t.get("error"),
            "warnings": t.get("warnings") or [],
            "cancelling": bool(t.get("cancel")) and t.get("status") == "running",
        }


#: 迁移未落地时 ``_iter_active_users`` 会退回旧查询。只告警**一次**——调度器每分钟跑一次，
#: 否则日志会被刷爆。
_SCHEMA_FALLBACK_WARNED = False


def _iter_active_users():
    """实盘调度器的用户选择。**必须把回测影子账户排除掉。**

    影子账户的 ``TrustConfig.is_active`` 恒为 False（``app/backtest.py`` 里硬性写死，且
    ``toggle_trust`` 拒绝激活），所以即使下面的兜底分支退回旧查询也不会选中它——这里的
    join 是**第二道防线**，不是唯一防线。两道都留着，是因为任何一道单独失效都不该造成
    "用实时价交易历史回测账簿"这种后果。
    """
    global _SCHEMA_FALLBACK_WARNED
    session = db.get_session()
    try:
        try:
            rows = (
                session.query(db.TrustConfig)
                .join(db.User, db.User.id == db.TrustConfig.user_id)
                .filter(
                    db.TrustConfig.is_active == True,  # noqa: E712
                    db.User.is_backtest == False,  # noqa: E712
                )
                .all()
            )
        except Exception as e:  # noqa: BLE001
            # users.is_backtest 列还没加上。**不能让整个调度器瘫痪**：退回旧查询并告警。
            session.rollback()
            if not _SCHEMA_FALLBACK_WARNED:
                _SCHEMA_FALLBACK_WARNED = True
                # 只打异常类型不打全文：SQLAlchemy 的报错会把整条 SQL 和参数铺开几十行，
                # 而这条日志在运维视角里只需要回答"迁移落没落地"。
                print(
                    f"[trust] users.is_backtest 不可用（{type(e).__name__}），"
                    "调度器退回旧查询；影子账户由 is_active=False 隔离",
                    flush=True,
                )
            rows = (
                session.query(db.TrustConfig)
                .filter(db.TrustConfig.is_active == True)  # noqa: E712
                .all()
            )
        return [r.user_id for r in rows]
    finally:
        session.close()


def _sched_execution():
    """交易日盘中每分钟一次。**每个 tick 无条件打一行心跳**（见下）。"""
    users = _iter_active_users()
    # 心跳。这是"调度器真的在按时间自己跑"最便宜的凭据——执行 job 工作日 9-14 点每分钟
    # 都跑，不必等到 15:05 的计划 job 才知道 job 活着。同时它顺带证明**容器时区正确**：
    # 若 TZ 没生效（UTC），这行的时间戳会比北京时间早 8 小时，一眼就能看出来。
    # ``flush=True`` 不可省：容器里 stdout 是块缓冲，不 flush 会攒着看不到。
    print(
        f"[trust] {datetime.now():%Y-%m-%d %H:%M:%S} execution tick，活跃用户 {len(users)}",
        flush=True,
    )
    for uid in users:
        try:
            run_execution(uid)
        except Exception as e:  # noqa: BLE001
            print(f"[trust] 执行层用户 {uid} 失败：{e}", flush=True)


def _sched_plan():
    """交易日 15:05：对每个在托管用户跑完整计划生成（与手动入口同一套校验）。

    15:05 已收盘，执行日取下一工作日；若用户自己已经在盘后生成过，闸门会拦下，
    这里按「跳过」处理而不是报失败——重复生成本就是多余动作。
    """
    users = _iter_active_users()
    print(
        f"[trust] {datetime.now():%Y-%m-%d %H:%M:%S} plan tick，活跃用户 {len(users)}",
        flush=True,
    )
    for uid in users:
        try:
            gate = check_plan_generation_allowed(uid)
            if gate["mode"] == "blocked":
                print(f"[trust] 计划层用户 {uid} 跳过：{gate['message']}", flush=True)
                continue
            plan, warnings = generate_plan_for_user(uid)
            # 成功也要出声：这是**唯一**能证明「15:05 的计划 job 真的跑完了」的日志。
            # 此前成功时静默，验证时只能去 trust_plans 表里数行，看不出是「跑了但没动作」
            # 还是「压根没跑」。动作数为 0 也照打——那本身就是要看见的异常。
            acts = (plan or {}).get("actions") or []
            print(
                f"[trust] 计划层用户 {uid} 完成：执行日 {gate['target_date']}、"
                f"{len(acts)} 条动作、{len(warnings)} 条提示",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[trust] 计划层用户 {uid} 失败：{e}", flush=True)


def _sched_release():
    """交易日 15:10：释放 A股 T+1 冻结。

    **⚠️ 这个时刻本身有隐患，动之前先读这条**：15:10 是**同一交易日收盘后**，所以当日
    买入的冻结股当天就解冻了——按 A股 T+1，它们本该到**次日**才可卖。今天没出事只是因为
    15:10 之后不再有执行 job；一旦有人加个 15:30 的执行 job，当天买的票就能被当天的计划
    卖掉，静默违反 T+1。

    正确做法是把日结挪到**次日开盘前**（如 09:15）。本轮不改（会动到线上既有账务节奏），
    但 ``/api/internal/run-execution`` 端点在 15:00 后拒绝触发，就是为了不在这条隐患上
    再开一个新的"收盘后还能下单"的口子。
    """
    users = _iter_active_users()
    print(
        f"[trust] {datetime.now():%Y-%m-%d %H:%M:%S} release tick，活跃用户 {len(users)}",
        flush=True,
    )
    for uid in users:
        try:
            trade.release_t1(uid)
        except Exception as e:  # noqa: BLE001
            print(f"[trust] 日结用户 {uid} 失败：{e}", flush=True)


def scheduler_status() -> dict:
    """调度器与各 job 的现状，供 ``/api/internal/scheduler`` 观测。未启动时 ``running=False``。

    **顺带是容器时区的证明**：``now`` 与 ``next_run_time`` 都是本地时间。若容器的
    ``TZ=Asia/Shanghai`` 没生效（跑在 UTC），两者会一起偏 8 小时——那正是"计划表看起来
    排好了、到点却没跑"的头号原因，而这个接口一眼就能看出来。
    """
    sched = _SCHEDULER
    # 用 **aware** 的本地时间：APScheduler 的 next_run_time 带 tzinfo，naive 的 now 没法
    # 直接与它相减，而且把 "+08:00" 直接印在响应里本身就是时区正确最直观的证据。
    now = datetime.now().astimezone()
    if sched is None:
        return {"running": False, "now": now.isoformat(sep=" ", timespec="seconds"),
                "tz": str(now.tzinfo), "jobs": []}
    jobs = []
    for j in sched.get_jobs():
        nrt = getattr(j, "next_run_time", None)   # 未 start 时该属性不存在
        jobs.append({
            "id": j.id,
            "next_run_time": nrt.isoformat(sep=" ", timespec="seconds") if nrt else None,
            "trigger": str(j.trigger),
        })
    return {
        "running": bool(getattr(sched, "running", False)),
        "now": now.isoformat(sep=" ", timespec="seconds"),
        "tz": str(now.tzinfo),
        "jobs": jobs,
    }


def start_scheduler():
    """启动托管调度（盘中执行 + 收盘计划 + 日结）。APScheduler 未装则返回 None。

    实例存进模块级 ``_SCHEDULER``——原先返回值被调用方丢掉，于是"job 下次什么时候跑"
    在进程外完全不可观测（只能等日志）。``scheduler_status()`` 靠这个引用工作。
    """
    global _SCHEDULER
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        print("[trust] APScheduler 未安装，调度器未启动（可手动触发 run_execution/run_plan_for_user）", flush=True)
        return None

    sched = BackgroundScheduler()
    sched.add_job(
        _sched_execution,
        CronTrigger(day_of_week="mon-fri", hour="9-14", minute="*"),
        id="execution",
    )
    sched.add_job(
        _sched_execution, CronTrigger(day_of_week="mon-fri", hour="15", minute="0"), id="execution-15"
    )
    sched.add_job(
        _sched_plan, CronTrigger(day_of_week="mon-fri", hour="15", minute="5"), id="plan"
    )
    sched.add_job(
        _sched_release, CronTrigger(day_of_week="mon-fri", hour="15", minute="10"), id="release"
    )
    sched.start()
    _SCHEDULER = sched
    return sched
