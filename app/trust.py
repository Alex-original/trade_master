"""财团托管：托管簿管理 + 两级调度（计划层深度引擎出评级 + 执行层分钟级调仓）。

- 托管簿（book=1）：托管页粘贴快照建立，现金恒 0（换仓式）。
- 计划层：收盘后对在管标的并行跑深度分析，评级落 engine_run。
- 执行层：盘中每分钟扫评级 + 行情 + 风控，产出调仓。
- 日结：释放 A股 T+1 冻结。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from app import account as account_mod
from app import db, trade
from app.errors import ServiceError
from tradingagents.dataflows import wind as _wind


# ---------- 托管簿 CRUD ----------

def snapshot_trust_book(user_id: int, rows: list[dict]) -> None:
    """粘贴快照建立托管簿。已有成交记录须先 reset。rows 同 account.sync_positions。"""
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        if not cfg:
            raise ServiceError("托管配置不存在")
        trade_count = session.query(db.Trade).filter(db.Trade.user_id == user_id).count()
        if trade_count > 0:
            raise ServiceError("已有成交记录，请先清空成交记录再重新同步")
        now = time.time()
        session.query(db.Position).filter(
            db.Position.user_id == user_id, db.Position.book == 1
        ).delete()
        for row in rows:
            qty = int(row.get("qty") or 0)
            if qty <= 0:
                continue
            code = _wind.to_wind_code(row["code"])
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
        cfg.available_cash = 0.0
        cfg.updated_at = now
        session.commit()
    finally:
        session.close()


def toggle_trust(user_id: int, active: bool) -> None:
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        if not cfg:
            raise ServiceError("托管配置不存在")
        if active and not cfg.book_created:
            raise ServiceError("请先粘贴持仓截图创建托管起始仓位")
        cfg.is_active = active
        cfg.updated_at = time.time()
        session.commit()
    finally:
        session.close()


def reset_trust(user_id: int) -> None:
    """清空成交记录 + 托管簿，回到未建簿干净态（二次确认在前端）。"""
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        if not cfg:
            raise ServiceError("托管配置不存在")
        session.query(db.Trade).filter(db.Trade.user_id == user_id).delete()
        session.query(db.Order).filter(db.Order.user_id == user_id).delete()
        session.query(db.Position).filter(
            db.Position.user_id == user_id, db.Position.book == 1
        ).delete()
        cfg.book_created = False
        cfg.available_cash = 0.0
        cfg.is_active = False
        cfg.updated_at = time.time()
        session.commit()
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
            if hasattr(cfg, k) and v is not None:
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
        "stock_scope": cfg.stock_scope,
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
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        if not cfg:
            raise ServiceError("托管配置不存在")
        return _cfg_to_dict(cfg)
    finally:
        session.close()


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
            for w in session.query(db.Watchlist).filter(db.Watchlist.user_id == user_id).all():
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

def _today_trade_count(user_id: int) -> int:
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    session = db.get_session()
    try:
        return (
            session.query(db.Trade)
            .filter(db.Trade.user_id == user_id, db.Trade.traded_at >= today0)
            .count()
        )
    finally:
        session.close()


def run_execution(user_id: int) -> dict:
    """执行层：扫托管簿持仓 + 最新评级 + 风控，产出调仓。"""
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
    finally:
        session.close()
    if not cfg or not cfg.is_active or not cfg.book_created:
        return {"trades": [], "skipped": "not_active"}

    positions = account_mod.get_positions(user_id, 1)
    if not positions:
        return {"trades": [], "skipped": "no_positions"}

    if cfg.risk_max_trades_day and _today_trade_count(user_id) >= cfg.risk_max_trades_day:
        return {"trades": [], "skipped": "max_trades"}

    trades: list[dict] = []
    buy_candidates: list[tuple[str, str, int]] = []  # (code, name, weight)

    # 1) 止损 + 卖出（清仓/减半）
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
                pass  # 卖出失败（如跌停/停牌）跳过
        if cfg.risk_max_trades_day and len(trades) >= cfg.risk_max_trades_day:
            return {"trades": trades, "skipped": "max_trades"}

    # 2) 买入（加仓，用卖出产生的现金按评级权重分配）
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
                    ai_reason=f"评级 {_get_latest_rating(user_id, code)} 加仓",
                )
                trades.append(r)
                cash -= r["amount"] + r["fee"]
            except ServiceError:
                pass
            if cfg.risk_max_trades_day and len(trades) >= cfg.risk_max_trades_day:
                break

    return {"trades": trades, "skipped": None}


# ---------- 计划层 / 日结 / 调度 ----------

def run_plan_for_user(user_id: int, date: str | None = None) -> list[dict]:
    """计划层：对在管标的并行跑深度分析。date 默认最近交易日。"""
    from app.analysis_service import run_plan

    if not date:
        from app.intent import latest_trading_day

        date = latest_trading_day()
    symbols = [s["code"] for s in get_managed_symbols(user_id)]
    if not symbols:
        return []
    return run_plan(user_id, symbols, date)


def _iter_active_users():
    session = db.get_session()
    try:
        rows = (
            session.query(db.TrustConfig)
            .filter(db.TrustConfig.is_active == True)  # noqa: E712
            .all()
        )
        return [r.user_id for r in rows]
    finally:
        session.close()


def _sched_execution():
    for uid in _iter_active_users():
        try:
            run_execution(uid)
        except Exception as e:  # noqa: BLE001
            print(f"[trust] 执行层用户 {uid} 失败：{e}", flush=True)


def _sched_plan():
    for uid in _iter_active_users():
        try:
            run_plan_for_user(uid)
        except Exception as e:  # noqa: BLE001
            print(f"[trust] 计划层用户 {uid} 失败：{e}", flush=True)


def _sched_release():
    for uid in _iter_active_users():
        try:
            trade.release_t1(uid)
        except Exception as e:  # noqa: BLE001
            print(f"[trust] 日结用户 {uid} 失败：{e}", flush=True)


def start_scheduler():
    """启动托管调度（盘中执行 + 收盘计划 + 日结）。APScheduler 未装则返回 None。"""
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
    return sched
