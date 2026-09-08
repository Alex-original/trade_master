"""自选股 + 个股详情（K 线）。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from app import account as account_mod
from app import db
from app.errors import ServiceError
from tradingagents.dataflows import wind as _wind
from tradingagents.dataflows.errors import NoMarketDataError, VendorError


def add_watchlist(user_id: int, stock_code: str, stock_name: str = "") -> dict:
    code = _wind.to_wind_code(stock_code)
    if not stock_name:
        stock_name = _wind.get_company_name(code) or code
    session = db.get_session()
    try:
        exists = (
            session.query(db.Watchlist)
            .filter(db.Watchlist.user_id == user_id, db.Watchlist.stock_code == code)
            .first()
        )
        if not exists:
            session.add(
                db.Watchlist(
                    user_id=user_id,
                    stock_code=code,
                    stock_name=stock_name,
                    source=0,
                    created_at=time.time(),
                )
            )
            session.commit()
    finally:
        session.close()
    return {"stock_code": code, "stock_name": stock_name}


def remove_watchlist(user_id: int, stock_code: str) -> None:
    code = _wind.to_wind_code(stock_code)
    session = db.get_session()
    try:
        session.query(db.Watchlist).filter(
            db.Watchlist.user_id == user_id, db.Watchlist.stock_code == code
        ).delete()
        session.commit()
    finally:
        session.close()


def get_watchlist(user_id: int) -> list[dict]:
    session = db.get_session()
    try:
        rows = session.query(db.Watchlist).filter(db.Watchlist.user_id == user_id).all()
        items = [
            {"stock_code": r.stock_code, "stock_name": r.stock_name, "source": r.source}
            for r in rows
        ]
    finally:
        session.close()
    for it in items:
        it["price"] = account_mod.get_latest_price(it["stock_code"])
    return items


def get_stock_detail(code: str, days: int = 60) -> dict:
    """个股详情：名称 + 现价 + 近 N 日 K 线。"""
    wind_code = _wind.to_wind_code(code)
    name = _wind.get_company_name(wind_code) or wind_code
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6) + 7)
    try:
        df = _wind.get_wind_ohlcv(
            wind_code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), period="10"
        )
    except (NoMarketDataError, VendorError):
        raise ServiceError(f"无法获取 {wind_code} 行情")
    if df is None or df.empty:
        raise ServiceError(f"无 {wind_code} 行情数据")

    kline = []
    for _, row in df.tail(days).iterrows():
        kline.append(
            {
                "date": row["Date"].strftime("%Y-%m-%d"),
                "open": round(float(row["Open"]), 4),
                "high": round(float(row["High"]), 4),
                "low": round(float(row["Low"]), 4),
                "close": round(float(row["Close"]), 4),
                "volume": float(row.get("Volume") or 0.0),
            }
        )
    latest = kline[-1]
    return {
        "code": wind_code,
        "name": name,
        "price": latest["close"],
        "kline": kline,
    }
