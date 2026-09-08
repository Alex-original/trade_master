"""双簿账务：同步持仓、资金、市值与盈亏计算。

镜像簿（book=0）：同步真实券商持仓快照；现金在 Account.available_cash（初始 50 万，同步后 0）。
托管簿（book=1）：托管页粘贴快照建立；现金在 TrustConfig.available_cash（固定 0，换仓式）。
首页总资产 = 镜像簿总资产 + 托管簿总资产（展示层求和）。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from app import db
from app.errors import ServiceError
from tradingagents.dataflows import wind as _wind
from tradingagents.dataflows.errors import NoMarketDataError, VendorError


def get_latest_price(stock_code: str) -> float | None:
    """取标的最新价（Wind 日线最新收盘，作为模拟撮合/盈亏的现价近似）。失败返回 None。"""
    end = datetime.now()
    start = end - timedelta(days=30)
    try:
        df = _wind.get_wind_ohlcv(
            stock_code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), period="10"
        )
    except (NoMarketDataError, VendorError):
        return None
    if df is None or df.empty:
        return None
    return float(df.iloc[-1]["Close"])


def sync_positions(user_id: int, rows: list[dict]) -> None:
    """同步镜像簿持仓（覆盖式）。首次同步清 50 万种子现金。

    rows 形如 [{"code": "600519.SH", "name": "贵州茅台", "qty": 100, "cost_price": 1750.0}, ...]
    """
    session = db.get_session()
    try:
        account = session.query(db.Account).filter(db.Account.user_id == user_id).first()
        if not account:
            raise ServiceError("账户不存在")
        now = time.time()
        if not account.has_synced:
            account.available_cash = 0.0
            account.has_synced = True
        account.last_sync_at = now

        session.query(db.Position).filter(
            db.Position.user_id == user_id, db.Position.book == 0
        ).delete()
        for row in rows:
            qty = int(row.get("qty") or 0)
            if qty <= 0:
                continue
            code = _wind.to_wind_code(row["code"])
            session.add(
                db.Position(
                    user_id=user_id,
                    book=0,
                    stock_code=code,
                    stock_name=row.get("name") or "",
                    hold_qty=qty,
                    available_qty=qty,  # 同步的真实持仓视为全部可用
                    cost_price=float(row.get("cost_price") or 0.0),
                    updated_at=now,
                )
            )
        session.commit()
    finally:
        session.close()


def get_positions(user_id: int, book: int) -> list[dict]:
    """读某簿持仓，附现价/市值/浮动盈亏/仓位占比。行情失败时 price=None。"""
    session = db.get_session()
    try:
        rows = (
            session.query(db.Position)
            .filter(db.Position.user_id == user_id, db.Position.book == book)
            .all()
        )
        items = [
            {
                "stock_code": r.stock_code,
                "stock_name": r.stock_name,
                "hold_qty": r.hold_qty,
                "available_qty": r.available_qty,
                "cost_price": r.cost_price,
            }
            for r in rows
        ]
    finally:
        session.close()

    for it in items:
        price = get_latest_price(it["stock_code"])
        it["price"] = price
        if price is not None and it["hold_qty"] > 0:
            it["market_value"] = round(price * it["hold_qty"], 2)
            it["pnl"] = round((price - it["cost_price"]) * it["hold_qty"], 2)
            it["pnl_pct"] = (
                round((price - it["cost_price"]) / it["cost_price"], 4)
                if it["cost_price"]
                else 0.0
            )
        else:
            it["market_value"] = 0.0
            it["pnl"] = None
            it["pnl_pct"] = None

    total_mv = sum(it["market_value"] for it in items)
    for it in items:
        it["position_ratio"] = round(it["market_value"] / total_mv, 4) if total_mv else 0.0
    return items


def get_account(user_id: int) -> dict:
    """双簿汇总：镜像簿（现金+市值）、托管簿（现金+市值）、合并口径。"""
    session = db.get_session()
    try:
        account = session.query(db.Account).filter(db.Account.user_id == user_id).first()
        trust = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        has_synced = account.has_synced if account else False
        mirror_cash = account.available_cash if account else 0.0
        trust_cash = trust.available_cash if trust else 0.0
    finally:
        session.close()

    mirror_pos = get_positions(user_id, 0)
    trust_pos = get_positions(user_id, 1)
    mirror_mv = sum(p["market_value"] for p in mirror_pos)
    trust_mv = sum(p["market_value"] for p in trust_pos)
    mirror_pnl = sum(p["pnl"] or 0.0 for p in mirror_pos)
    trust_pnl = sum(p["pnl"] or 0.0 for p in trust_pos)

    mirror_assets = round(mirror_cash + mirror_mv, 2)
    trust_assets = round(trust_cash + trust_mv, 2)
    return {
        "mirror": {
            "cash": mirror_cash,
            "market_value": round(mirror_mv, 2),
            "pnl": round(mirror_pnl, 2),
            "total_assets": mirror_assets,
            "has_synced": has_synced,
        },
        "trust": {
            "cash": trust_cash,
            "market_value": round(trust_mv, 2),
            "pnl": round(trust_pnl, 2),
            "total_assets": trust_assets,
            "is_active": bool(trust.is_active) if trust else False,
            "book_created": bool(trust.book_created) if trust else False,
        },
        "total_assets": round(mirror_assets + trust_assets, 2),
    }
