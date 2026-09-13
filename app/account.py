"""双簿账务：同步持仓、资金、市值与盈亏计算。

镜像簿（book=0）：同步真实券商持仓快照；现金在 Account.available_cash（初始 50 万，同步后 0）。
托管簿（book=1）：托管页粘贴快照建立；现金在 TrustConfig.available_cash（固定 0，换仓式）。
首页总资产 = 镜像簿总资产 + 托管簿总资产（展示层求和）。
"""
from __future__ import annotations

import time

from app import db
from app.errors import ServiceError
from tradingagents.dataflows import wind as _wind


# ---- 行情短缓存：快照接口批量取价 + 进程内 TTL，避免列表/账户每刷一次就逐只打 Wind ----
_QUOTE_TTL = 60.0  # 秒：60s 内同标的只真正拉一次；收盘后价格不变，TTL 不损失新鲜度
# code -> (expires_at, price, prev_close, volume_ratio)
_quote_cache: dict[str, tuple[float, float | None, float | None, float | None]] = {}


def get_quotes(stock_codes: list[str]) -> dict[str, dict]:
    """批量取最新行情 → {code: {"price", "prev_close", "volume_ratio"}}。

    命中缓存直接返回；未命中部分合并成一次 Wind 快照调用补齐（一只或 N 只同价，
    走 get_stock_price_indicators，不再逐只拉 K 线）。查不到的标的（停牌无价/代码错）
    不在返回里，调用方按无行情降级。

    ``volume_ratio``（量比）供执行层判定「放量跌破」这类复合触发条件；取不到时为
    None，调用方按条件不满足处理。它是**执行层、监控卡片、账户页共用的唯一行情入口**，
    所以量比在这一个函数里补齐，不要再引第二条取数路径。
    """
    codes = [c for c in (stock_codes or []) if c]
    if not codes:
        return {}
    now = time.time()
    out: dict[str, dict] = {}
    miss: list[str] = []
    for c in codes:
        ent = _quote_cache.get(c)
        if ent and ent[0] > now:
            out[c] = {"price": ent[1], "prev_close": ent[2], "volume_ratio": ent[3]}
        else:
            miss.append(c)
    if miss:
        try:
            snaps = _wind.get_price_snapshots(miss)
        except Exception:  # noqa: BLE001 —— 快照拿不到按无行情降级，不影响页面
            snaps = {}
        for c, q in snaps.items():
            if c in codes:
                vr = q.get("volume_ratio")
                _quote_cache[c] = (now + _QUOTE_TTL, q["price"], q["prev_close"], vr)
                out[c] = {"price": q["price"], "prev_close": q["prev_close"], "volume_ratio": vr}
    return out


def get_quote(stock_code: str) -> dict | None:
    """取单只最新行情（快照接口 + 短缓存）：最新价 + 前收。失败返回 None。"""
    return get_quotes([stock_code]).get(stock_code)


def resolve_quote(user_code: str) -> dict:
    """按用户输入（6 位或带后缀代码）解析行情快照，供确认页填充现价。

    返回 {"code": wind代码, "price": 最新价或 None, "prev_close": 昨收或 None}；
    代码非法/无行情时 price/prev_close 为 None（由前端回退成本价），不抛错。
    """
    user_code = (user_code or "").strip()
    try:
        wc = _wind.to_wind_code(user_code)
        q = get_quote(wc)
    except Exception:  # noqa: BLE001 —— 任意解析/行情异常统一降级为无行情
        return {"code": user_code, "price": None, "prev_close": None}
    return {
        "code": wc,
        "price": q["price"] if q else None,
        "prev_close": q["prev_close"] if q else None,
    }


def get_latest_price(stock_code: str) -> float | None:
    """取标的最新价（Wind 日线最新收盘，作为模拟撮合/盈亏的现价近似）。失败返回 None。"""
    q = get_quote(stock_code)
    return q["price"] if q else None


def sync_positions(
    user_id: int,
    rows: list[dict],
    cash: float | None = None,
    mv: float | None = None,
    pnl: float | None = None,
    reset_snapshot: bool = False,
) -> None:
    """同步镜像簿持仓（覆盖式）。镜像簿=真实账户的只读展示，现金=粘贴的券商可用资金。

    rows 形如 [{"code": "600519.SH", "name": "贵州茅台", "qty": 100, "cost_price": 1750.0}, ...]
    cash：可用资金（元），随每次同步刷新为最新粘贴值；不传/None 时保留旧行为（首次同步清空种子现金）。
    mv/pnl：券商快照口径的总市值 / 浮动盈亏，用户在解析确认页手动校准过才传（见确认页）；
            传 None 时保留既有值；reset_snapshot=True 时清空（回到按持仓×实时价实时计算）。
    """
    session = db.get_session()
    try:
        account = session.query(db.Account).filter(db.Account.user_id == user_id).first()
        if not account:
            raise ServiceError("账户不存在")
        now = time.time()
        if not account.has_synced:
            account.has_synced = True
            if cash is None:
                account.available_cash = 0.0  # 首次同步未显式带现金：清掉 50 万种子，避免当真实现金展示
        if cash is not None:
            account.available_cash = round(float(cash), 2)
        if reset_snapshot:
            account.broker_mv = None
            account.broker_pnl = None
        else:
            if mv is not None:
                account.broker_mv = round(float(mv), 2)
            if pnl is not None:
                account.broker_pnl = round(float(pnl), 2)
        account.last_sync_at = now

        session.query(db.Position).filter(
            db.Position.user_id == user_id, db.Position.book == 0
        ).delete()
        seen: set[str] = set()
        for row in rows:
            qty = int(row.get("qty") or 0)
            if qty <= 0:
                continue
            code = _wind.to_wind_code(row["code"])
            if code in seen:
                continue  # 同一标的重复出现（OCR 偶发）→ 去重，避免唯一键冲突
            seen.add(code)
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


def _book_cash(user_id: int, book: int) -> float:
    """某簿的现金口径：镜像簿→Account.available_cash；托管簿→TrustConfig.available_cash。"""
    session = db.get_session()
    try:
        if book == 0:
            row = session.query(db.Account).filter(db.Account.user_id == user_id).first()
            return float(row.available_cash) if row else 0.0
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == user_id).first()
        return float(cfg.available_cash) if cfg else 0.0
    finally:
        session.close()


def get_positions(user_id: int, book: int, clock=None) -> list[dict]:
    """读某簿持仓，附现价/市值/浮动盈亏/仓位占比。行情失败时 price=None。

    position_ratio：市值/总市值（组内相对占比）。
    assets_ratio：市值/总资产（= 该簿现金 + 市值），即"占整个账户（含现金）的比例"。

    ``clock`` 只为回测注入：非空时用 ``clock.bars()`` 给的历史收盘价盯市，而不是实时快照。
    回测**必须**走这条路——否则团队会拿"今天的价"去算半年前的仓位占比，再据此定仓。

    ⚠️ ``tradable=False`` 的 bar **照样用来盯市**：它的 ``close`` 是停牌前最后有效收盘，
    这正是「停牌不假装归零」的实现。归零会把浮亏算成 -100%、占比算成 0，进而触发错误的
    减仓。是否可交易由执行层看 ``tradable`` 决定，盯市与否和它无关。
    """
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

    codes = [it["stock_code"] for it in items]
    if clock is not None:
        # 历史盯市：形状对齐 get_quotes 的返回项，下面整段逻辑一行不用改。
        quotes = {
            c: {"price": b.close, "prev_close": b.prev_close, "volume_ratio": b.volume_ratio}
            for c, b in clock.bars(codes).items()
        }
    else:
        quotes = get_quotes(codes)
    for it in items:
        quote = quotes.get(it["stock_code"])
        price = quote["price"] if quote else None
        prev_close = quote["prev_close"] if quote else None
        it["price"] = price
        it["prev_close"] = prev_close
        if price is not None and it["hold_qty"] > 0:
            it["market_value"] = round(price * it["hold_qty"], 2)
            it["pnl"] = round((price - it["cost_price"]) * it["hold_qty"], 2)
            it["pnl_pct"] = (
                round((price - it["cost_price"]) / it["cost_price"], 4)
                if it["cost_price"]
                else 0.0
            )
            # 当日盈亏 = (现价 - 昨收) * 数量；缺昨收（新上市/停牌数据不足）时为 None
            it["day_pnl"] = (
                round((price - prev_close) * it["hold_qty"], 2)
                if prev_close is not None
                else None
            )
            it["day_pnl_pct"] = (
                round((price - prev_close) / prev_close, 4)
                if prev_close
                else None
            )
        else:
            it["market_value"] = 0.0
            it["pnl"] = None
            it["pnl_pct"] = None
            it["day_pnl"] = None
            it["day_pnl_pct"] = None

    total_mv = sum(it["market_value"] for it in items)
    total_assets = _book_cash(user_id, book) + total_mv
    for it in items:
        it["position_ratio"] = round(it["market_value"] / total_mv, 4) if total_mv else 0.0
        it["assets_ratio"] = round(it["market_value"] / total_assets, 4) if total_assets else 0.0
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
        mirror_broker_mv = account.broker_mv if account else None
        mirror_broker_pnl = account.broker_pnl if account else None
        trust_broker_mv = trust.broker_mv if trust else None
        trust_broker_pnl = trust.broker_pnl if trust else None
    finally:
        session.close()

    mirror_pos = get_positions(user_id, 0)
    trust_pos = get_positions(user_id, 1)
    mirror_mv_live = round(sum(p["market_value"] for p in mirror_pos), 2)
    trust_mv_live = round(sum(p["market_value"] for p in trust_pos), 2)
    mirror_pnl_live = round(sum(p["pnl"] or 0.0 for p in mirror_pos), 2)
    trust_pnl_live = round(sum(p["pnl"] or 0.0 for p in trust_pos), 2)
    mirror_day_pnl = round(sum(p["day_pnl"] or 0.0 for p in mirror_pos), 2)
    trust_day_pnl = round(sum(p["day_pnl"] or 0.0 for p in trust_pos), 2)

    # 券商快照口径优先：用户解析确认时手动校准过「总市值 / 浮动盈亏」，则作为总览权威值展示；
    # 总资产恒为 现金 + 市值。未校准时回落持仓×实时价。
    mirror_mv = mirror_broker_mv if mirror_broker_mv is not None else mirror_mv_live
    mirror_pnl = mirror_broker_pnl if mirror_broker_pnl is not None else mirror_pnl_live
    trust_mv = trust_broker_mv if trust_broker_mv is not None else trust_mv_live
    trust_pnl = trust_broker_pnl if trust_broker_pnl is not None else trust_pnl_live
    mirror_mv = round(float(mirror_mv), 2)
    trust_mv = round(float(trust_mv), 2)
    mirror_pnl = round(float(mirror_pnl), 2)
    trust_pnl = round(float(trust_pnl), 2)

    mirror_assets = round(mirror_cash + mirror_mv, 2)
    trust_assets = round(trust_cash + trust_mv, 2)
    return {
        "mirror": {
            "cash": mirror_cash,
            "market_value": mirror_mv,
            "pnl": mirror_pnl,
            "day_pnl": mirror_day_pnl,
            "total_assets": mirror_assets,
            "has_synced": has_synced,
            "snapshot_overridden": mirror_broker_mv is not None,
        },
        "trust": {
            "cash": trust_cash,
            "market_value": trust_mv,
            "pnl": trust_pnl,
            "day_pnl": trust_day_pnl,
            "total_assets": trust_assets,
            "is_active": bool(trust.is_active) if trust else False,
            "book_created": bool(trust.book_created) if trust else False,
            "snapshot_overridden": trust_broker_mv is not None,
        },
        "total_assets": round(mirror_assets + trust_assets, 2),
    }
