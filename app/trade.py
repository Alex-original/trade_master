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
from app.market_clock import limit_pct
from tradingagents.dataflows import wind as _wind
from tradingagents.dataflows.errors import NoMarketDataError, VendorError


def _is_a_share(code: str) -> bool:
    return (code or "").upper().endswith((".SH", ".SZ", ".BJ"))


def _get_trust_config(session, user_id):
    cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
    if not cfg:
        raise ServiceError("托管配置不存在")
    return cfg


def _check_limit(
    wind_code: str, direction: int, fill_price: float | None = None, clock=None,
    name: str = "",
) -> None:
    """A股涨跌停/停牌判断。direction: 0 买 / 1 卖。非 A股、以及无涨跌停的板块跳过。

    判据是 **``clock is None`` ⟺ 实时**：

    - 不传 ``clock``（生产）→ 实时逻辑（``datetime.now()`` + 实时 K 线 + 传入的 ``fill_price``）。
    - 传 ``clock``（回测）→ 委托 ``clock.check_limit``，按**当日 bar** 判定。

    **两条路现在共用同一份板块规则**（``market_clock.limit_pct``）与同一句判据——
    「**成交价**是否已到/超过板价」：

    +--------+------------------------------+------------------------------+
    | 板块   | 旧（实时分支写死 ±9.5%）     | 现（按板块）                 |
    +========+==============================+==============================+
    | 主板   | 9.5% 起拦（不到板就拦）      | 10% 板价                     |
    | 创业板 | 9.5% 起拦（**误拦** 9.6% 涨）| 20% 板价                     |
    | 科创板 | 同上                         | 20% 板价                     |
    | 北交所 | 同上                         | 30% 板价                     |
    | ST     | 同上                         | 5% 板价                      |
    | 港/美  | 不判                         | 不判                         |
    +--------+------------------------------+------------------------------+

    也就是实时行为**会与改动前不同**：``9.5% < 涨幅 < 板价`` 的窗口原先买不进，现在可成交。
    这是修正（原先创业板 9.6% 的误拦是实打实的 bug），但要知道它变了。

    ``name`` 只用来识别 ST——判定顺序上 ST 优先于板块（与 ``limit_pct`` 一致）。
    """
    if clock is not None:
        reason = clock.check_limit(wind_code, direction, fill_price)
        if reason:
            raise ServiceError(reason)
        return
    if not _is_a_share(wind_code):
        return
    pct = limit_pct(wind_code, name)
    if not pct:
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
    if not prev_close:
        return
    # 判「成交价是否到板」而不是「收盘相对昨收涨了多少」：后者与回测口径不同，
    # 且盘中拿到的"最新收盘"未必是当日价。fill_price 恒由 place_order 传入。
    cur = float(fill_price) if fill_price is not None else float(df.iloc[-1]["Close"])
    if direction == 0 and cur >= round(prev_close * (1 + pct), 2):
        raise ServiceError("该股已涨停，无法买入")
    if direction == 1 and cur <= round(prev_close * (1 - pct), 2):
        raise ServiceError("该股已跌停，无法卖出")


def _calc_fee(cfg, wind_code: str, direction: int, amount: float) -> float:
    commission = amount * cfg.fee_commission_rate
    if not cfg.fee_waive_min and commission < 5.0:
        commission = 5.0
    fee = commission
    if direction == 1 and _is_a_share(wind_code):
        fee += amount * cfg.fee_stamp_duty_rate
    return round(fee, 2)


def place_order(
    user_id, stock_code, stock_name, direction, quantity, source=1, ai_reason="",
    price: float | None = None, ts: float | None = None, clock=None,
):
    """下单并即时撮合。返回成交记录 dict。direction: 0 买 / 1 卖。

    ``price`` / ``ts`` / ``clock`` 由执行层显式传入（**回测必须传**）：

    - ``price``：执行内核已定的成交价。不传时才回退实时取价——这个回退是隐式日期依赖
      （``get_latest_price`` 拿的是"此刻"的价），回测里绝不能被触发。
    - ``ts``：成交时间戳（记账用）。回测里是模拟日，不传取 ``time.time()``。
    - ``clock``：非空 = 回测，涨跌停/停牌由 ``_check_limit`` 转交时钟判定。

    显式传 ``price`` 还顺手修掉一个既有隐性不一致：``_execute_plan`` 用快照价定仓、
    这里又自己取一次价，两者靠 60s 快照缓存偶然一致。传进来之后定仓价与成交价**强制同源**。
    """
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
        try:
            stock_name = _wind.get_company_name(wind_code) or wind_code
        except Exception:  # noqa: BLE001 —— Wind 查无此标的等，回退用代码作名称
            stock_name = wind_code

    if price is None:
        price = account_mod.get_latest_price(wind_code)
    if price is None:
        raise ServiceError(f"无法获取 {wind_code} 行情，可能停牌或代码错误")
    _check_limit(wind_code, direction, fill_price=price, clock=clock, name=stock_name)

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

        # 回测里 now 是模拟日（同一天多笔共享同一秒），唯一性由 token_hex(4) 保证。
        now = ts if ts is not None else time.time()
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
            # 本簿已实现盈亏：**在改动 hold_qty 之前**用当时的成本价结算。
            # 口径与 backtest_calc.replay_realized 逐字一致（价差 − 本笔费用；成本价不含费用，
            # 费用是单独的资金流出），因此空仓起步的簿上两条路必须给出同一个数——见
            # scripts/smoke_backtest_calc.py 的交叉核对用例。
            # ``or 0.0``：本列对老库是 NULL（口径升级前的簿），首次卖出从 0 起算。
            cfg.realized_pnl = round(
                (cfg.realized_pnl or 0.0) + (price - pos.cost_price) * quantity - fee, 2
            )
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
    """日结：把托管簿当日买入的冻结量释放为可用（A股 T+1）。

    **⚠️ 调用时刻决定它是否合规**：本函数无脑解冻，不看日期。T+1 要求"当日买入的股票
    次一交易日才可卖"，所以它只能在**次日开盘前**调用。当前调度器排在**同一交易日 15:10**
    （``trust._sched_release``）——当天买入当天解冻，不合规，只因 15:10 之后没有执行 job
    才没造成实际成交。改动调度时刻或新增盘后执行 job 前，先读 ``_sched_release`` 的说明。
    """
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
