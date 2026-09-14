"""Trade Master · 模拟交易 + AI 托管 FastAPI 入口。

启动：.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8010
前端：app_frontend/（同源托管，无需 CORS）。
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app import (
    account,
    analysis_service,
    auth,
    backtest,
    chat,
    config,
    db,
    intent as intent_mod,
    notify,
    ocr,
    plan_report,
    trade,
    trust,
    watchlist,
)
from app.errors import PlanNeedsConfirm, ServiceError
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
    # 管理员白名单是 fail-closed 的：没配 = 谁都不是 ⇒ 回测功能整体关闭。那会让"忘了配"
    # 表现成"功能凭空消失"，所以这里把实际生效的数量打出来，排查时一眼看得见。
    print(f"[startup] 管理员白名单：{len(config.ADMIN_PHONES)} 个手机号", flush=True)
    trust.start_scheduler()
    # 回测动辄数小时，进程重启（发版 / OOM / 手动 kill）后必须能自己接着跑——否则一次
    # 重启就让一条已经烧掉几小时 LLM 的 run 永远卡在 running 上，而用户只会看到进度条不动。
    try:
        resumed = backtest.resume_orphan_runs()
        if resumed:
            print(f"[startup] 续跑中断的回测：{resumed}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[startup] 回测续跑检查失败（不影响其它功能）：{e}", flush=True)


_startup()


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(PlanNeedsConfirm)
async def plan_needs_confirm_handler(request: Request, exc: PlanNeedsConfirm):
    """盘中已有当日计划：返回 409 + need_confirm，前端据此弹确认框而不是直接报错。"""
    return JSONResponse(
        status_code=409,
        content={"detail": exc.message, "need_confirm": True, "trade_date": exc.trade_date},
    )


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
    cash: float | None = None  # 可用资金（元），随粘贴一并更新
    mv: float | None = None  # 券商快照总市值（确认页手动校准后传；None 沿用旧值）
    pnl: float | None = None  # 券商快照浮动盈亏（同上）
    reset_snapshot: bool = False  # True 时清空快照口径，回到按持仓×实时价计算


class SnapshotRequest(BaseModel):
    positions: list[dict]
    cash: float | None = None  # 可用资金（元）；空仓启动传 50 万
    mv: float | None = None  # 券商快照总市值（确认页手动校准后传；None 沿用旧值）
    pnl: float | None = None  # 券商快照浮动盈亏（同上）
    reset_snapshot: bool = False  # True 时清空快照口径


class ToggleRequest(BaseModel):
    active: bool


class TrustConfigRequest(BaseModel):
    stock_scope: int | None = None
    stock_scope_group: str | None = None  # scope=1 时：仅自选里指定的一组（None=全部分组）
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
    group: str | None = None  # 目标分组；缺省 = 当前激活分组


class WatchGroupRequest(BaseModel):
    name: str


class WatchGroupRenameRequest(BaseModel):
    old_name: str
    new_name: str


class WatchGroupActiveRequest(BaseModel):
    name: str


class WatchSyncParseRequest(BaseModel):
    input_type: Literal["text", "image"]
    content: str


class WatchSyncRequest(BaseModel):
    stocks: list[dict]
    group: str | None = None  # 缺省 = 当前激活分组（一次替换一组）


class NotifyConfigRequest(BaseModel):
    channel: str
    webhook_url: str = ""
    is_enabled: bool = False


class AccountQuestionRequest(BaseModel):
    question: str


class AnalysisRequest(BaseModel):
    ticker: str = Field(..., description="股票代码，A股如 600519.SH")
    date: str = Field(..., description="分析日期 YYYY-MM-DD")


class BacktestRunRequest(BaseModel):
    start_date: str = Field(..., description="起始日期 YYYY-MM-DD")
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD")
    init_mode: Literal["cash", "copy"] = "cash"  # 空仓起步 / 复制当前托管簿
    init_cash: float = 0.0
    universe: list[str] | None = None  # 缺省 = 发起人当前的托管标的池（预览页展示的那一份）
    fresh_research: bool = False  # 强制重跑深度研究（缓存键不含组合上下文，见结果页声明 8）
    sub_ticks: int | None = None  # 同一模拟日内最多推进几档梯子
    # 回测股票范围：0 持仓股 / 1 自选股（见 backtest.resolve_range）。None = 沿用旧行为
    # （在管全集），老调用方不受影响。给了 bt_scope 且 universe 为空时由服务端解析，
    # **保证预览与起跑同源**。
    bt_scope: int | None = None
    bt_groups: list[str] = []  # bt_scope=1 时选中的分组；空 = 全部分组


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
    # ``is_admin`` 挂在这里而不是 account.get_account 里：这是**请求级**的身份判断，
    # 不是账户数据。前端启动时的 Promise.all 里就有这一次调用，所以入口显隐不用多发请求。
    data = account.get_account(user_id)
    data["is_admin"] = auth.is_admin(user_id)
    return data


@app.get("/api/positions")
def api_positions(user_id: int = Depends(auth.get_current_user)):
    return {"positions": account.get_positions(user_id, 0)}


@app.post("/api/positions/parse")
def api_positions_parse(req: SyncParseRequest, user_id: int = Depends(auth.get_current_user)):
    """解析持仓（不落库），返回预览供前端确认。"""
    return ocr.parse_holdings(req.input_type, req.content)


@app.post("/api/positions/sync")
def api_positions_sync(req: SyncConfirmRequest, user_id: int = Depends(auth.get_current_user)):
    """确认同步镜像簿持仓（含可用资金 + 可选券商快照口径）。"""
    account.sync_positions(
        user_id,
        req.positions,
        cash=req.cash,
        mv=req.mv,
        pnl=req.pnl,
        reset_snapshot=req.reset_snapshot,
    )
    return account.get_account(user_id)


# ---------- 自选（分组） / 个股详情 ----------

@app.get("/api/watchlist")
def api_watchlist(user_id: int = Depends(auth.get_current_user)):
    """分组结构 + 当前激活分组的自选明细（含行情）。"""
    return watchlist.list_groups(user_id)


@app.get("/api/watchlist/groups")
def api_watchlist_groups(user_id: int = Depends(auth.get_current_user)):
    """仅分组名册 + 当前激活组（不含自选明细），供托管「仅自选某组」下拉使用。"""
    return watchlist.groups_meta(user_id)


@app.post("/api/watchlist")
def api_watchlist_add(req: WatchlistRequest, user_id: int = Depends(auth.get_current_user)):
    return watchlist.add_watchlist(user_id, req.stock_code, req.stock_name, group=req.group)


@app.delete("/api/watchlist/{stock_code}")
def api_watchlist_del(
    stock_code: str,
    group: str | None = None,
    user_id: int = Depends(auth.get_current_user),
):
    """从分组删除自选（缺省 = 当前激活分组）。"""
    watchlist.remove_watchlist(user_id, stock_code, group=group)
    return {"ok": True}


@app.get("/api/watchlist/search")
def api_watchlist_search(q: str = "", user_id: int = Depends(auth.get_current_user)):
    """检索股票/ETF 供添加（Wind 名称 NL + 代码归一）。"""
    return {"stocks": watchlist.search_stocks(user_id, q)}


@app.post("/api/watchlist/parse")
def api_watchlist_parse(req: WatchSyncParseRequest, user_id: int = Depends(auth.get_current_user)):
    """解析自选列表（文本或截图），不落库，返回预览。"""
    return ocr.parse_watchlist(req.input_type, req.content)


@app.post("/api/watchlist/sync")
def api_watchlist_sync(req: WatchSyncRequest, user_id: int = Depends(auth.get_current_user)):
    """用解析结果替换目标分组（缺省当前激活分组）的自选，一次一组。"""
    summary = watchlist.sync_watchlist(user_id, req.stocks, group=req.group)
    summary.update(watchlist.list_groups(user_id))
    return summary


@app.post("/api/watchlist/groups")
def api_watchlist_group_create(req: WatchGroupRequest, user_id: int = Depends(auth.get_current_user)):
    return watchlist.create_group(user_id, req.name)


@app.put("/api/watchlist/groups")
def api_watchlist_group_rename(req: WatchGroupRenameRequest, user_id: int = Depends(auth.get_current_user)):
    return watchlist.rename_group(user_id, req.old_name, req.new_name)


@app.delete("/api/watchlist/groups/{name}")
def api_watchlist_group_delete(name: str, user_id: int = Depends(auth.get_current_user)):
    return watchlist.delete_group(user_id, name)


@app.post("/api/watchlist/groups/active")
def api_watchlist_group_active(req: WatchGroupActiveRequest, user_id: int = Depends(auth.get_current_user)):
    return watchlist.set_active_group(user_id, req.name)


@app.get("/api/stock/{code}")
def api_stock_detail(code: str, user_id: int = Depends(auth.get_current_user)):
    return watchlist.get_stock_detail(code)


@app.get("/api/quote/{code}")
def api_quote(code: str, user_id: int = Depends(auth.get_current_user)):
    """轻量行情快照（确认页填现价用）：6 位或带后缀代码 → {code, price, prev_close}。"""
    return account.resolve_quote(code)


# ---------- 托管 ----------

@app.get("/api/trust")
def api_trust(user_id: int = Depends(auth.get_current_user)):
    cfg = trust.get_trust(user_id)
    cfg["positions"] = account.get_positions(user_id, 1)
    return cfg


@app.post("/api/trust/snapshot")
def api_trust_snapshot(req: SnapshotRequest, user_id: int = Depends(auth.get_current_user)):
    """粘贴持仓快照建立托管簿（cash=券商可用资金 + 可选券商快照口径）；positions 为空即空仓启动。

    **重贴即重建**：会先清空委托 / 成交 / 次日行动计划（监控条件随之清空）并关闭托管开关，
    再用本次快照覆盖持仓。前端负责在提交前把这份影响告知用户。返回体里的 ``cleared``
    是被清空的条数，供前端回执。
    """
    cleared = trust.snapshot_trust_book(
        user_id,
        req.positions,
        cash=req.cash,
        mv=req.mv,
        pnl=req.pnl,
        reset_snapshot=req.reset_snapshot,
    )
    cfg = trust.get_trust(user_id)
    cfg["cleared"] = cleared
    return cfg


@app.post("/api/trust/toggle")
def api_trust_toggle(req: ToggleRequest, user_id: int = Depends(auth.get_current_user)):
    trust.toggle_trust(user_id, req.active)
    return trust.get_trust(user_id)


@app.get("/api/trust/config")
def api_trust_get_config(user_id: int = Depends(auth.get_current_user)):
    return trust.get_trust(user_id)


@app.put("/api/trust/config")
def api_trust_put_config(req: TrustConfigRequest, user_id: int = Depends(auth.get_current_user)):
    # exclude_unset：保留"显式传 null"的字段，用于清空可选风控参数（如止损比例）
    return trust.update_trust_config(user_id, req.model_dump(exclude_unset=True))


@app.post("/api/trust/reset")
def api_trust_reset(user_id: int = Depends(auth.get_current_user)):
    """清空成交记录 + 次日行动计划 + 托管簿（监控条件随计划清空）。返回被清空的条数。"""
    return {"ok": True, "cleared": trust.reset_trust(user_id)}


@app.get("/api/trust/orders")
def api_trust_orders(user_id: int = Depends(auth.get_current_user)):
    return {"orders": trust.list_orders(user_id, limit=100)}


@app.get("/api/trust/trades")
def api_trust_trades(user_id: int = Depends(auth.get_current_user)):
    return {"trades": trust.list_trades(user_id, limit=100)}


@app.get("/api/trust/analytics")
def api_trust_analytics(user_id: int = Depends(auth.get_current_user)):
    """成交归因。``realized_pnl`` 是本簿累计已实现盈亏；**``null`` 与 ``0`` 含义不同**
    （null = 该簿建于口径升级前、无从追溯），详见 trust.get_analytics。"""
    return trust.get_analytics(user_id)


@app.get("/api/trust/plan")
def api_trust_plan(user_id: int = Depends(auth.get_current_user)):
    """最新次日行动计划（Stage2 组合决策层产出）。无计划返回空结构。"""
    plan = trust._get_latest_plan(user_id) or {}
    process = plan.get("process") or {}
    research = process.get("research") or {}
    missing = process.get("research_missing") or []
    return {
        "summary": plan.get("summary", ""),
        "cash_target": plan.get("cash_target"),
        "risk_notes": plan.get("risk_notes", ""),
        "actions": plan.get("actions", []),
        # 研究覆盖情况：计划背后的 Stage1 依据是否完整
        "research_count": len(research),
        "research_missing": missing,
    }


@app.get("/api/trust/plan/today")
def api_trust_plan_today(user_id: int = Depends(auth.get_current_user)):
    """监控条件：最近一次次日行动计划的监控条件 + 现价 + 触发状态（已触发/监控中）。"""
    return trust.get_monitor_conditions(user_id)


@app.get("/api/trust/plan/generate/check")
def api_trust_plan_generate_check(user_id: int = Depends(auth.get_current_user)):
    """生成前闸门：ok / confirm / blocked + 文案。

    盘中且当日计划已存在 → confirm，前端弹确认框后再带 confirm=true 重新提交；
    盘后且该执行日计划已生成 → blocked，直接拒绝（当日盘后只生成一次）。
    必须声明在 /generate/{task_id} 之前，否则会被那条通配路由吃掉。
    """
    return trust.check_plan_generation_allowed(user_id)


@app.post("/api/trust/plan/generate")
def api_trust_plan_generate(confirm: bool = False, user_id: int = Depends(auth.get_current_user)):
    """手动触发次日行动计划生成（Stage1 深析 + Stage2 组合决策），异步返回 task_id。

    与 15:05 定时入口共用同一套前置条件：托管配置存在 + 已建簿 + 持仓现价可得。
    不通过则直接返回错误提示，不启动任务。盘中已有当日计划时须 ``confirm=true`` 覆盖。
    """
    trust.check_plan_preconditions(user_id)
    ok, errors = trust.validate_portfolio_data(user_id)
    if not ok:
        raise ServiceError("数据校验不通过：" + "；".join(errors))
    return {"task_id": trust.submit_plan_generation(user_id, force=confirm)}


@app.post("/api/trust/plan/generate/{task_id}/cancel")
def api_trust_plan_cancel(task_id: str, user_id: int = Depends(auth.get_current_user)):
    """暂停计划生成：置取消标志，生成线程在下一个检查点收手（已在跑的分析跑完即丢弃）。"""
    return trust.cancel_plan_generation(task_id, user_id)


@app.get("/api/trust/plan/generate/{task_id}")
def api_trust_plan_generate_status(task_id: str, user_id: int = Depends(auth.get_current_user)):
    """轮询计划生成任务状态：running / done / error / cancelled。"""
    return trust.get_plan_generation_status(task_id)


@app.get("/api/trust/plan/download")
def api_trust_plan_download(user_id: int = Depends(auth.get_current_user)):
    """下载次日行动计划报告（HTML：行动决策总览 + 决策过程 + 各角色研究报告）。"""
    data = trust.get_plan_download_data(user_id)
    if not data:
        raise ServiceError("暂无次日行动计划，请先生成")
    html = plan_report.render_plan_report(
        data["plan"], data["positions"], data["research"], data["cash"],
        data["trade_date"], data["created_at"],
        research_missing=data.get("research_missing") or [],
        research_frozen=data.get("research_frozen", False),
    )
    filename = f"nextday_plan_{data['trade_date'] or 'latest'}.html"
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/records")
def api_records(user_id: int = Depends(auth.get_current_user)):
    """当日委托/成交（托管产生的全部流水）。"""
    return api_trust_trades(user_id)


# ---------- 历史回测 ----------
#
# ⚠️ 声明顺序很重要：``/api/backtest/run``、``/api/backtest/list``、``/api/backtest/preview``
# 必须写在 ``/api/backtest/{run_id}`` **之前**。Starlette 按声明顺序匹配，把通配的那条放前面
# 会让 ``list`` 被当成 run_id="list" 吃掉（``/api/trust/plan/download`` 就是踩过这个坑才
# 与 ``/api/trust/plan/{task_id}`` 分开声明的）。


@app.post("/api/backtest/run")
def api_backtest_run(req: BacktestRunRequest, user_id: int = Depends(auth.require_admin)):
    """发起一次回测：建 run → 立刻起跑（后台线程）→ 返回 run_id 供前端轮询。

    **先拿预览再建 run**：预览里已经算好了交易日数与默认标的池，顺带把日期合法性与
    "结束日期不能晚于今天"这类硬校验跑一遍——在烧掉十小时之前把不合格的请求挡掉。

    **范围解析放在服务端、且预览与建 run 用同一套**（``resolve_range``）：如果让前端
    自己算好 universe 传上来，预览说"8 只"而实际跑 12 只就成了必然——用户在结果页上
    看到的是假账，而且查不出来。``req.universe`` 依然更高优先级（兼容既有调用与测试）。
    """
    pv = backtest.preview(
        user_id, req.start_date, req.end_date, init_mode=req.init_mode,
        bt_scope=req.bt_scope, bt_groups=req.bt_groups,
    )
    if not pv["universe_size"]:
        raise ServiceError(
            "回测股票范围是空的：请改选自选股分组、或先在自选股里加票后再发起"
            if req.bt_scope is not None
            else "标的池为空：请先在自选股里加票，或把选股范围改成「全市场」"
        )
    universe = req.universe or pv["universe"]
    extra: dict = {"sub_ticks": req.sub_ticks} if req.sub_ticks else {}
    # 冻结本次的范围选择。``universe`` 被显式指定时不再解析——那时真正在跑的是 req.universe，
    # 把解析出来的 names 冻进去反而会让"冻结池"与"实际池"对不上（``_drive`` 取股票名会串）。
    if req.bt_scope is not None and not req.universe:
        rr = backtest.resolve_range(user_id, req.bt_scope, req.bt_groups, req.init_mode)
        extra.update({
            "bt_scope": req.bt_scope,
            "bt_groups": list(req.bt_groups or []),
            "bt_names": rr["names"],       # 供 _drive 取股票名（影子池里名字可能是空的）
            "bt_merged": rr["merged"],     # 供结果页如实声明"自动并入了哪几只"
            "bt_range_label": rr["label"],
        })
    run_id = backtest.create_run(
        user_id,
        start_date=req.start_date,
        end_date=req.end_date,
        init_mode=req.init_mode,
        init_cash=req.init_cash,
        universe=universe,
        total=pv["trading_days"],
        extra_config=extra or None,
    )
    backtest.start_run(run_id, fresh_research=req.fresh_research)
    return {
        "run_id": run_id,
        "trading_days": pv["trading_days"],
        "est_label": pv["est_label"],
        "range_label": pv["range_label"],
        "merged_holdings": pv["merged_holdings"],
    }


@app.get("/api/backtest/list")
def api_backtest_list(limit: int = 20, user_id: int = Depends(auth.require_admin)):
    """本用户发起过的回测（新的在前）。只给摘要，完整结果走 /result。"""
    return {"runs": backtest.list_runs(user_id, limit=limit)}


@app.get("/api/backtest/capability")
def api_backtest_capability(user_id: int = Depends(auth.require_admin)):
    """托管团队能力评估报告（静态单页），回测页顶部内嵌它。

    **为什么是接口而不是直接 <iframe src>**：应用用 ``Authorization: Bearer``
    （token 在 localStorage），而 iframe 发起的是浏览器裸请求、带不上这个头 ⇒ 必然 403。
    所以前端用带头 fetch 取回文本、写进 ``iframe.srcdoc``。

    **为什么文件在 ``app_frontend/``**：``docs/`` 被 .dockerignore 排除、deploy.sh 也不传它，
    放进镜像的只有 ``app_frontend/``。``docs/`` 那份是给人看的同一份，
    ``scripts/smoke_admin_gate.py`` 逐字节比对两者，防止改了文档页而应用内不更新。

    ⚠️ 声明顺序：本路由在 ``/{run_id}`` **之前**（见上方那段注释），否则会被当成 run_id="capability"。
    """
    path = STATIC / "capability.html"
    if not path.exists():  # 缺文件时给出可读的 404，而不是 500
        raise ServiceError(
            "能力评估报告尚未随镜像发布（app_frontend/capability.html 不存在）",
            status_code=404,
        )
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/api/backtest/preview")
def api_backtest_preview(
    start: str = "", end: str = "", probe: bool = False, init_mode: str = "cash",
    bt_scope: int | None = None, bt_groups: str = "",
    user_id: int = Depends(auth.require_admin),
):
    """发起前预览：交易日数、标的数、预计 LLM 调用次数与耗时、以及告警。

    **耗时是估算**（按第 0 步实测的单次 35 秒 / 19 次调用反推），文案里因此说的是"至少"——
    真实值只会更高（还要加 Stage2 与工具轮次）。

    ``probe=1`` 会**真去取一次数**（N 次 Wind 调用、几十秒），回报"实际有几只有行情"、
    缺失清单、以及会不会被起跑门禁拦下。它是一个显式选项而不是默认行为：预览按钮不该
    在用户没要求时突然变慢。取到的数据会落进磁盘缓存，所以探数同时也给起跑预热了。

    ``bt_scope`` / ``bt_groups`` 是回测股票范围（0 持仓股 / 1 自选股；``bt_groups`` 为
    逗号分隔的分组名，空 = 全部分组）。不传 ``bt_scope`` 时沿用旧行为（在管全集）。
    """
    groups = [g.strip() for g in (bt_groups or "").split(",") if g.strip()]
    return backtest.preview(user_id, start, end, probe=probe, init_mode=init_mode,
                            bt_scope=bt_scope, bt_groups=groups)


@app.get("/api/backtest/{run_id}")
def api_backtest_progress(run_id: int, user_id: int = Depends(auth.require_admin)):
    """进度：status / stage / message / done / total。前端轮询这一个口。"""
    return backtest.get_progress(run_id, user_id)


@app.post("/api/backtest/{run_id}/cancel")
def api_backtest_cancel(run_id: int, user_id: int = Depends(auth.require_admin)):
    """取消：只置标志位，跑着的分析收尾后线程自己退出（不硬杀，避免留下半截账务）。"""
    backtest.cancel_run(run_id, user_id)
    return {"ok": True}


@app.get("/api/backtest/{run_id}/result")
def api_backtest_result(run_id: int, user_id: int = Depends(auth.require_admin)):
    """聚合结果 + 逐日净值 + 声明。没跑完时 ``result`` 为空 dict。"""
    return backtest.get_result(run_id, user_id)


@app.get("/api/backtest/{run_id}/trades")
def api_backtest_trades(run_id: int, user_id: int = Depends(auth.require_admin)):
    """逐日成交流水（含 AI 决策理由）。"""
    return backtest.get_trades(run_id, user_id)


@app.get("/api/backtest/{run_id}/monitor")
def api_backtest_monitor(run_id: int, date: str,
                         user_id: int = Depends(auth.require_admin)):
    """某一天的监控条件（**执行口径**：卖/减看当日最低、买/建看当日最高）。

    与实盘那张卡片同一个渲染器、同一份判据，只把"实时价"换成"那天的真实行情"——
    这样卡片才能回答「这天为什么成交 / 为什么没成交」。行情只读预热好的磁盘缓存。
    该日没有计划时返回 ``has_plan=false`` + 原因，不返回假卡片。
    """
    return backtest.get_day_monitor_conditions(run_id, user_id, date)


@app.get("/api/backtest/{run_id}/reports")
def api_backtest_reports(run_id: int, user_id: int = Depends(auth.require_admin)):
    """可查看流程报告的日期清单（每个计划生效日一条，含是否真执行过）。"""
    return backtest.list_plan_reports(run_id, user_id)


@app.get("/api/backtest/{run_id}/report")
def api_backtest_report(run_id: int, date: str, inline: bool = False,
                        user_id: int = Depends(auth.require_admin)):
    """某一天的流程报告（HTML）。回测跨多日、每天一份，故用 ``date`` 选日。

    **与实盘「次日行动报告」同一个渲染器**，所以格式逐字一致；只多一条横幅标注
    "这是回测、不是实盘计划"。``inline=1`` 给前端内嵌预览用（不触发下载）。
    """
    data = backtest.get_plan_report(run_id, user_id, date)
    if not data:
        raise ServiceError(f"{date} 没有流程报告（该日没有生成计划）")
    html = plan_report.render_plan_report(
        data["plan"], data["positions"], data["research"], data["cash"],
        data["trade_date"], data["created_at"],
        research_missing=data.get("research_missing") or [],
        research_frozen=data.get("research_frozen", False),
        banner=f"本报告来自历史回测 run #{run_id}（模拟撮合，非实盘成交）。"
               f"账户快照取自 {data['as_of'] or '回测起始状态'}。",
    )
    filename = f"backtest_{run_id}_{data['trade_date'] or date}.html"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if inline:
        headers = {}
    return Response(content=html, media_type="text/html; charset=utf-8", headers=headers)


@app.delete("/api/backtest/{run_id}")
def api_backtest_delete(run_id: int, user_id: int = Depends(auth.require_admin)):
    """删除回测记录。**影子账户留着**——它承载研究缓存（十小时量级的 LLM 调用）。"""
    return backtest.delete_run(run_id, user_id)


# ---------- 通知 ----------

@app.get("/api/notify/config")
def api_notify_get_config(user_id: int = Depends(auth.get_current_user)):
    return {"channels": notify.get_notification_configs(user_id)}


@app.put("/api/notify/config")
def api_notify_config(req: NotifyConfigRequest, user_id: int = Depends(auth.get_current_user)):
    notify.set_notification_config(user_id, req.channel, req.webhook_url, req.is_enabled)
    return {"ok": True}


# ---------- 内部运维端点 ----------
# 用途：内测期手动触发一次执行层、观测调度器是否真的在按时间跑。
# 两道门：nginx ``location ^~ /api/internal/`` 直接 404（公网不可达，只有 127.0.0.1:8010
# 能到，compose 已只绑回环）；进到应用层还要过一个 token，且**未配置即拒绝**。

def _require_internal_token(token: str | None) -> None:
    """**fail-closed**：未配置 INTERNAL_TOKEN、或没带、或不匹配 → 一律 403。

    刻意不写"没配就放行"：一个默认放行的运维端点等于把下单能力挂在公网上。
    ``compare_digest`` 而不是 ``==``：避免按字节短路带来的时序侧信道。
    """
    want = config.INTERNAL_TOKEN
    if not want or not token or not secrets.compare_digest(token, want):
        raise HTTPException(status_code=403, detail="forbidden")


class InternalRunExecutionRequest(BaseModel):
    user_id: int = Field(..., description="要手动触发执行层的用户 id")
    # 强制显式确认：这个端点会**真实下单并发通知**，不该被顺手 curl 一下就跑了。
    confirm: bool = Field(False, description="必须显式传 true")


@app.post("/api/internal/run-execution")
def api_internal_run_execution(
    req: InternalRunExecutionRequest,
    x_internal_token: str | None = Header(default=None),
):
    """手动跑一次执行层（实时路径，**会真实下单、会成交通知**）。

    专门给内测验证用：不新造执行逻辑，就是 ``trust.run_execution``（不传 clock = 实时
    路径，因此会走完整的行情/风控/落库/通知链路）。拒绝四类：

    - token 缺失/错误/未配置 → 403（fail-closed）
    - ``confirm`` 不为 true → 400
    - 用户不存在 → 404；用户是回测影子账户 → 400（影子账户由回测编排，永不手动触发）
    - 非交易时段（周末或 15:00 后）→ 409（理由见 ``trust._sched_release`` 的 T+1 说明）
    """
    _require_internal_token(x_internal_token)
    if not req.confirm:
        raise HTTPException(status_code=400, detail="需要 confirm=true（本端点会真实下单）")

    now = datetime.now()
    if now.weekday() >= 5 or now.hour >= 15:
        raise HTTPException(
            status_code=409,
            detail="仅工作日 15:00 前可手动触发执行层（盘后下单会踩到 T+1 解冻时刻的隐患）",
        )

    session = db.get_session()
    try:
        u = session.query(db.User).filter(db.User.id == req.user_id).first()
        if u is None:
            raise HTTPException(status_code=404, detail=f"用户 {req.user_id} 不存在")
        if getattr(u, "is_backtest", False):
            raise HTTPException(status_code=400, detail="影子账户由回测编排，禁止手动触发")
    finally:
        session.close()

    res = trust.run_execution(req.user_id)
    trades = res.get("trades") or []
    return {
        "ok": True,
        "user_id": req.user_id,
        "trade_count": len(trades),
        "skipped": res.get("skipped"),
        "trades": trades,
    }


@app.get("/api/internal/scheduler")
def api_internal_scheduler(x_internal_token: str | None = Header(default=None)):
    """调度器各 job 的 ``next_run_time``（同时证明容器时区正确）。

    若容器 ``TZ=Asia/Shanghai`` 没生效，这里的 ``now`` 与 ``next_run_time`` 会整体偏
    8 小时——那正是"计划表排了、到点却没跑"的头号原因。
    """
    _require_internal_token(x_internal_token)
    return trust.scheduler_status()


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


@app.post("/api/chat/ask")
def api_chat_ask(req: ChatRequest, user_id: int = Depends(auth.get_current_user)):
    """AI 对话助手统一入口：按意图分流。

    输入 messages（含最新一条用户问题）。返回 {kind, ...}：
      kind=quote            → {quote:{code,name,price,prev_close,change_pct}}（行情快照）
      kind=analysis         → {ticker,name,date,summary}（前端据此提交 /analysis 异步跑 trade_agent）
      kind=analysis_confirm → {ticker,name,date,question}（助手请求做深度分析，**须用户确认**）
      kind=ask              → {question}（标的歧义/需澄清）
      kind=account          → {answer}（助手基于只读工具的回答）
    """
    messages = [{"role": t.role, "content": t.content} for t in req.messages]
    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

    try:
        intent = intent_mod.parse_intent(messages)
    except Exception:  # noqa: BLE001 —— 解析失败退回助手问答
        return chat.answer_with_tools(user_id, messages)

    mode = intent.get("mode") or "ask"
    has_target = bool((intent.get("ticker") or "").strip() or (intent.get("name") or "").strip())

    if mode in ("quote", "analyze") and has_target:
        try:
            plan = intent_mod.resolve_to_plan(intent)
        except ServiceError as e:
            return {"kind": "ask", "question": e.message}
        except Exception:  # noqa: BLE001
            return {"kind": "ask", "question": "查询标的时候出了点问题，稍后再试，或直接给 6 位代码（如 159779）。"}
        if plan.get("type") == "ask":
            return {"kind": "ask", "question": plan["question"]}
        if mode == "quote":
            try:
                return {"kind": "quote", "quote": chat.market_quote(plan["ticker"], plan["name"])}
            except ServiceError as e:
                return {"kind": "ask", "question": e.message}
        # analyze：返回计划，由前端提交 /analysis 异步执行（10-15 分钟）
        return {
            "kind": "analysis",
            "ticker": plan["ticker"],
            "name": plan["name"],
            "date": plan["date"],
            "summary": plan.get("summary", ""),
        }

    # mode=ask 且用户已给了具体标的（如中芯国际 A/H 多市场）→ 透出澄清问题
    if mode == "ask" and has_target:
        q = (intent.get("question") or "").strip()
        if q:
            return {"kind": "ask", "question": q}

    # 其余（无具体标的需求：持仓诊断/策略/托管/闲聊）交给助手：它自己调只读工具取数后再答
    return chat.answer_with_tools(user_id, messages)


# ---------- 深度分析（异步） ----------

@app.post("/analysis")
def analysis_submit(req: AnalysisRequest, user_id: int = Depends(auth.get_current_user)):
    """提交异步深度分析，返回 task_id。"""
    task_id = analysis_service.submit_analysis(user_id, req.ticker, req.date)
    return {"task_id": task_id}


@app.get("/analysis/{task_id}")
def analysis_status(task_id: str, user_id: int = Depends(auth.get_current_user)):
    return analysis_service.get_analysis_status(task_id)
