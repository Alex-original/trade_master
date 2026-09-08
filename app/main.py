"""Trade Master · 模拟交易 + AI 托管 FastAPI 入口。

启动：.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8010
前端：app_frontend/（同源托管，无需 CORS）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app import (
    account,
    analysis_service,
    auth,
    chat,
    db,
    intent as intent_mod,
    notify,
    ocr,
    trade,
    trust,
    watchlist,
)
from app.errors import ServiceError
from tradingagents.dataflows.errors import NoMarketDataError

logger = logging.getLogger("trade_master")

app = FastAPI(title="Trade Master · 模拟交易 + AI 托管", version="0.3.0")

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app_frontend"
SAMPLE_REPORT = ROOT / "data" / "sample_report.json"


def _startup() -> None:
    try:
        db.init_db()
        print("[startup] 数据库初始化完成", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[startup] 数据库初始化失败（检查 DATABASE_URL / PG 是否就绪）：{e}", flush=True)
    trust.start_scheduler()


_startup()


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


# ---------- 请求模型 ----------

class SendCodeRequest(BaseModel):
    phone: str


class LoginRequest(BaseModel):
    phone: str
    code: str


class SyncParseRequest(BaseModel):
    input_type: Literal["text", "image"]
    content: str


class SyncConfirmRequest(BaseModel):
    positions: list[dict]


class SnapshotRequest(BaseModel):
    positions: list[dict]


class ToggleRequest(BaseModel):
    active: bool


class TrustConfigRequest(BaseModel):
    stock_scope: int | None = None
    style: int | None = None
    risk_max_trades_day: int | None = None
    risk_max_position_pct: float | None = None
    risk_stop_loss_pct: float | None = None
    fee_commission_rate: float | None = None
    fee_waive_min: bool | None = None
    fee_stamp_duty_rate: float | None = None


class WatchlistRequest(BaseModel):
    stock_code: str
    stock_name: str = ""


class NotifyConfigRequest(BaseModel):
    channel: str
    webhook_url: str = ""
    is_enabled: bool = False


class AccountQuestionRequest(BaseModel):
    question: str


class AnalysisRequest(BaseModel):
    ticker: str = Field(..., description="股票代码，A股如 600519.SH")
    date: str = Field(..., description="分析日期 YYYY-MM-DD")


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatTurn]


# ---------- 基础路由 ----------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "Trade Master · 模拟交易 + AI 托管"}


@app.get("/analysis/demo")
def analysis_demo() -> JSONResponse:
    if SAMPLE_REPORT.exists():
        return JSONResponse(json.loads(SAMPLE_REPORT.read_text(encoding="utf-8")))
    return JSONResponse({"note": "演示数据未生成"})


# ---------- 鉴权 ----------

@app.post("/api/auth/send-code")
def api_send_code(req: SendCodeRequest):
    return {"message": auth.send_code(req.phone)}


@app.post("/api/auth/login")
def api_login(req: LoginRequest):
    return auth.login(req.phone, req.code)


@app.post("/api/auth/logout")
def api_logout(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    auth.delete_session(token)
    return {"ok": True}


# ---------- 账户 / 持仓 ----------

@app.get("/api/account")
def api_account(user_id: int = Depends(auth.get_current_user)):
    return account.get_account(user_id)


@app.get("/api/positions")
def api_positions(user_id: int = Depends(auth.get_current_user)):
    return {"positions": account.get_positions(user_id, 0)}


@app.post("/api/positions/parse")
def api_positions_parse(req: SyncParseRequest, user_id: int = Depends(auth.get_current_user)):
    """解析持仓（不落库），返回预览供前端确认。"""
    return ocr.parse_holdings(req.input_type, req.content)


@app.post("/api/positions/sync")
def api_positions_sync(req: SyncConfirmRequest, user_id: int = Depends(auth.get_current_user)):
    """确认同步镜像簿持仓。"""
    account.sync_positions(user_id, req.positions)
    return account.get_account(user_id)


# ---------- 自选 / 个股详情 ----------

@app.get("/api/watchlist")
def api_watchlist(user_id: int = Depends(auth.get_current_user)):
    return {"watchlist": watchlist.get_watchlist(user_id)}


@app.post("/api/watchlist")
def api_watchlist_add(req: WatchlistRequest, user_id: int = Depends(auth.get_current_user)):
    return watchlist.add_watchlist(user_id, req.stock_code, req.stock_name)


@app.delete("/api/watchlist/{stock_code}")
def api_watchlist_del(stock_code: str, user_id: int = Depends(auth.get_current_user)):
    watchlist.remove_watchlist(user_id, stock_code)
    return {"ok": True}


@app.get("/api/stock/{code}")
def api_stock_detail(code: str, user_id: int = Depends(auth.get_current_user)):
    return watchlist.get_stock_detail(code)


# ---------- 托管 ----------

@app.get("/api/trust")
def api_trust(user_id: int = Depends(auth.get_current_user)):
    cfg = trust.get_trust(user_id)
    cfg["positions"] = account.get_positions(user_id, 1)
    return cfg


@app.post("/api/trust/snapshot")
def api_trust_snapshot(req: SnapshotRequest, user_id: int = Depends(auth.get_current_user)):
    """粘贴持仓快照建立托管簿。"""
    trust.snapshot_trust_book(user_id, req.positions)
    return trust.get_trust(user_id)


@app.post("/api/trust/toggle")
def api_trust_toggle(req: ToggleRequest, user_id: int = Depends(auth.get_current_user)):
    trust.toggle_trust(user_id, req.active)
    return trust.get_trust(user_id)


@app.get("/api/trust/config")
def api_trust_get_config(user_id: int = Depends(auth.get_current_user)):
    return trust.get_trust(user_id)


@app.put("/api/trust/config")
def api_trust_put_config(req: TrustConfigRequest, user_id: int = Depends(auth.get_current_user)):
    return trust.update_trust_config(user_id, req.model_dump(exclude_none=True))


@app.post("/api/trust/reset")
def api_trust_reset(user_id: int = Depends(auth.get_current_user)):
    trust.reset_trust(user_id)
    return {"ok": True}


@app.get("/api/trust/orders")
def api_trust_orders(user_id: int = Depends(auth.get_current_user)):
    session = db.get_session()
    try:
        rows = session.query(db.Order).filter(db.Order.user_id == user_id).order_by(db.Order.id.desc()).limit(100).all()
        return {"orders": [
            {"order_id": r.order_id, "stock_code": r.stock_code, "stock_name": r.stock_name,
             "direction": r.direction, "price": r.price, "quantity": r.quantity,
             "status": r.status, "source": r.source, "fail_reason": r.fail_reason,
             "created_at": r.created_at}
            for r in rows
        ]}
    finally:
        session.close()


@app.get("/api/trust/trades")
def api_trust_trades(user_id: int = Depends(auth.get_current_user)):
    session = db.get_session()
    try:
        rows = session.query(db.Trade).filter(db.Trade.user_id == user_id).order_by(db.Trade.id.desc()).limit(100).all()
        return {"trades": [
            {"trade_id": r.trade_id, "stock_code": r.stock_code, "stock_name": r.stock_name,
             "direction": r.direction, "price": r.price, "quantity": r.quantity,
             "amount": r.amount, "fee": r.fee, "ai_reason": r.ai_reason, "traded_at": r.traded_at}
            for r in rows
        ]}
    finally:
        session.close()


@app.get("/api/trust/analytics")
def api_trust_analytics(user_id: int = Depends(auth.get_current_user)):
    session = db.get_session()
    try:
        trades = session.query(db.Trade).filter(db.Trade.user_id == user_id).order_by(db.Trade.id).all()
    finally:
        session.close()
    realized = 0.0
    items = []
    for t in trades:
        # 简化归因：卖出计入已实现盈亏（相对成本价由上层算），这里仅列出流水
        items.append({
            "traded_at": t.traded_at, "stock_name": t.stock_name, "stock_code": t.stock_code,
            "direction": t.direction, "price": t.price, "quantity": t.quantity,
            "amount": t.amount, "ai_reason": t.ai_reason,
        })
    acc = account.get_account(user_id)
    return {"realized_pnl": round(realized, 2), "trade_count": len(items),
            "trust": acc["trust"], "trades": items}


@app.get("/api/records")
def api_records(user_id: int = Depends(auth.get_current_user)):
    """当日委托/成交（托管产生的全部流水）。"""
    return api_trust_trades(user_id)


# ---------- 通知 ----------

@app.put("/api/notify/config")
def api_notify_config(req: NotifyConfigRequest, user_id: int = Depends(auth.get_current_user)):
    notify.set_notification_config(user_id, req.channel, req.webhook_url, req.is_enabled)
    return {"ok": True}


# ---------- AI 对话 ----------

@app.post("/chat")
def chat_endpoint(req: ChatRequest):
    """深度分析意图（自然语言 → plan/ask），无鉴权（意图拆解无需账户）。"""
    messages = [{"role": t.role, "content": t.content} for t in req.messages]
    return intent_mod.handle_chat(messages)


@app.post("/api/chat/account")
def api_chat_account(req: AccountQuestionRequest, user_id: int = Depends(auth.get_current_user)):
    """账户问答（持仓诊断/托管问答/风险检查/策略），注入账户上下文。"""
    return {"answer": chat.answer_account_question(user_id, req.question)}


# ---------- 深度分析（异步） ----------

@app.post("/analysis")
def analysis_submit(req: AnalysisRequest, user_id: int = Depends(auth.get_current_user)):
    """提交异步深度分析，返回 task_id。"""
    task_id = analysis_service.submit_analysis(user_id, req.ticker, req.date)
    return {"task_id": task_id}


@app.get("/analysis/{task_id}")
def analysis_status(task_id: str, user_id: int = Depends(auth.get_current_user)):
    return analysis_service.get_analysis_status(task_id)
