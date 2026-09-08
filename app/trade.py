"""撮合引擎：下单 → 校验 → 现价即时成交 → 更新持仓/现金。

规则（修订版 PRD §7）：
- 成交价 = Wind 最新价（日线最新收盘近似）。
- A股：100 股/手取整、T+1（当日买入不可卖）、涨跌停/停牌不可成交。
- 港股/美股：按股撮合，无涨跌停。
- 费用：佣金（用户配费率，默认万 2.5 最低 5 元可免五）+ 印花税（仅 A股卖出）。
- 交易仅发生在托管簿（book=1；source 预留 0 手动）。
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta

from app import account as account_mod
from app import db
from app.errors import ServiceError
from tradingagents.dataflows import wind as _wind
from tradingagents.dataflows.errors import NoMarketDataError, VendorError


def _is_a_share(code: str) -> bool:
    return (code or "").upper().endswith((".SH", ".SZ", ".BJ"))


def _get_trust_config(session, user_id):
    cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
    if not cfg:
        raise ServiceError("托管配置不存在")
    return cfg


def _check_limit(wind_code: str, direction: int) -> None:
    """A股涨跌停/停牌判断。direction: 0 买 / 1 卖。非 A股跳过。"""
    if not _is_a_share(wind_code):
        return
    end = datetime.now()
    start = end - timedelta(days=10)
    try:
        df = _wind.get_wind_ohlcv(
            wind_code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), period="10"
        )
    except (NoMarketDataError, VendorError):
        raise ServiceError("该标的停牌或无行情，暂不可交易")
    if df is None or df.empty or len(df) < 2:
        raise ServiceError("行情数据不足，暂不可交易")
    prev_close = float(df.iloc[-2]["Close"])
    today_close = float(df.iloc[-1]["Close"])
    pct = (today_close - prev_close) / prev_close if prev_close else 0.0
    # 近似涨跌停阈值（主板 10%；创业板/科创板 20% 后续按代码精确化）
    if direction == 0 and pct >= 0.095:
        raise ServiceError("该股已涨停，无法买入")
    if direction == 1 and pct <= -0.095:
        raise ServiceError("该股已跌停，无法卖出")


def _calc_fee(cfg, wind_code: str, direction: int, amount: float) -> float:
    commission = amount * cfg.fee_commission_rate
    if not cfg.fee_waive_min and commission < 5.0:
        commission = 5.0
    fee = commission
    if direction == 1 and _is_a_share(wind_code):
        fee += amount * cfg.fee_stamp_duty_rate
    return round(fee, 2)


def place_order(user_id, stock_code, stock_name, direction, quantity, source=1, ai_reason=""):
    """下单并即时撮合。返回成交记录 dict。direction: 0 买 / 1 卖。"""
    wind_code = _wind.to_wind_code(stock_code)
    direction = int(direction)
    quantity = int(quantity)
    if direction not in (0, 1):
        raise ServiceError("买卖方向不合法")
    if quantity <= 0:
        raise ServiceError("数量必须大于 0")
    if _is_a_share(wind_code) and direction == 0 and quantity % 100 != 0:
        raise ServiceError("A股买入数量须为 100 股整数倍")
    if not stock_name:
        stock_name = _wind.get_company_name(wind_code) or wind_code

    price = account_mod.get_latest_price(wind_code)
    if price is None:
        raise ServiceError(f"无法获取 {wind_code} 行情，可能停牌或代码错误")
    _check_limit(wind_code, direction)

    amount = round(price * quantity, 2)

    session = db.get_session()
    try:
        cfg = _get_trust_config(session, user_id)
        fee = _calc_fee(cfg, wind_code, direction, amount)

        if direction == 0:  # 买入
            if cfg.available_cash < amount + fee:
                raise ServiceError(
                    f"托管簿可用资金不足（需 {round(amount + fee, 2):.2f}，可用 {cfg.available_cash:.2f}）"
                )
        else:  # 卖出
            pos = (
                session.query(db.Position)
                .filter(
                    db.Position.user_id == user_id,
                    db.Position.book == 1,
                    db.Position.stock_code == wind_code,
                )
                .first()
            )
            avail = pos.available_qty if pos else 0
            if avail < quantity:
                raise ServiceError(f"可用数量不足（需 {quantity}，可用 {avail}）")

        now = time.time()
        order_id = "O" + str(int(now)) + secrets.token_hex(4)
        trade_id = "T" + str(int(now)) + secrets.token_hex(4)

        session.add(
            db.Order(
                order_id=order_id,
                user_id=user_id,
                stock_code=wind_code,
                stock_name=stock_name,
                direction=direction,
                price=price,
                quantity=quantity,
                status=1,  # 已成
                source=source,
                created_at=now,
            )
        )
        session.add(
            db.Trade(
                trade_id=trade_id,
                order_id=order_id,
                user_id=user_id,
                stock_code=wind_code,
                stock_name=stock_name,
                direction=direction,
                price=price,
                quantity=quantity,
                amount=amount,
                fee=fee,
                ai_reason=ai_reason,
                traded_at=now,
            )
        )

        if direction == 0:  # 买入
            cfg.available_cash = round(cfg.available_cash - amount - fee, 2)
            pos = (
                session.query(db.Position)
                .filter(
                    db.Position.user_id == user_id,
                    db.Position.book == 1,
                    db.Position.stock_code == wind_code,
                )
                .first()
            )
            if not pos:
                pos = db.Position(
                    user_id=user_id,
                    book=1,
                    stock_code=wind_code,
                    stock_name=stock_name,
                    hold_qty=0,
                    available_qty=0,
                    frozen_qty=0,
                    cost_price=0.0,
                    updated_at=now,
                )
                session.add(pos)
                session.flush()
            old_qty = pos.hold_qty
            old_cost = pos.cost_price
            new_cost = (old_cost * old_qty + price * quantity) / (old_qty + quantity)
            pos.hold_qty = old_qty + quantity
            pos.frozen_qty = pos.frozen_qty + quantity  # T+1 冻结
            pos.cost_price = round(new_cost, 4)
            pos.stock_name = stock_name or pos.stock_name
            pos.updated_at = now
        else:  # 卖出
            pos = (
                session.query(db.Position)
                .filter(
                    db.Position.user_id == user_id,
                    db.Position.book == 1,
                    db.Position.stock_code == wind_code,
                )
                .first()
            )
            cfg.available_cash = round(cfg.available_cash + amount - fee, 2)
            pos.hold_qty -= quantity
            pos.available_qty -= quantity
            if pos.hold_qty <= 0:
                session.delete(pos)
            else:
                pos.updated_at = now
        cfg.updated_at = now
        session.commit()
    finally:
        session.close()

    # TODO(里程碑5): 成交后触发通知 notify.send_trade_notification(user_id, result)
    return {
        "trade_id": trade_id,
        "order_id": order_id,
        "stock_code": wind_code,
        "stock_name": stock_name,
        "direction": direction,
        "price": price,
        "quantity": quantity,
        "amount": amount,
        "fee": fee,
        "ai_reason": ai_reason,
    }


def release_t1(user_id):
    """日结：把托管簿当日买入的冻结量释放为可用（A股 T+1）。"""
    session = db.get_session()
    try:
        rows = (
            session.query(db.Position)
            .filter(
                db.Position.user_id == user_id,
                db.Position.book == 1,
                db.Position.frozen_qty > 0,
            )
            .all()
        )
        for pos in rows:
            pos.available_qty += pos.frozen_qty
            pos.frozen_qty = 0
            pos.updated_at = time.time()
        session.commit()
    finally:
        session.close()
