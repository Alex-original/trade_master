"""深度分析异步封装：后台任务 + engine_run 决策缓存。

run_analysis 同步阻塞 10-15 分钟，这里用线程后台跑，任务状态存内存 dict，
结果落 engine_run 表（同日同标的命中缓存不重跑）。
"""
from __future__ import annotations

import json
import threading
import time
import uuid

from app import db
from app.analysis import RATING_ZH, run_analysis
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


def run_analysis_cached(user_id: int, ticker: str, date: str) -> dict:
    """检查 engine_run 缓存，命中返回；未命中跑 run_analysis 并落库。"""
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

    result = run_analysis(ticker, date)
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


def run_plan(user_id: int, symbols: list[str], date: str) -> list[dict]:
    """计划层：对一批标的并行跑深度分析（命中缓存则不重跑），返回结果列表。"""
    from concurrent.futures import ThreadPoolExecutor

    results = []

    def _one(sym):
        return run_analysis_cached(user_id, sym, date)

    with ThreadPoolExecutor(max_workers=min(4, len(symbols) or 1)) as ex:
        futures = [ex.submit(_one, s) for s in symbols]
        for f in futures:
            try:
                results.append(f.result())
            except Exception as e:  # noqa: BLE001
                results.append({"ticker": "?", "error": str(e)})
    return results
