"""自选股（分组）+ 个股详情（K 线）+ 检索。

分组模型：
- 分组名册在 watchlist_groups（空组也能持久）；自选行在 watchlist（user_id, group_name, stock_code 唯一）。
- 每个用户有「当前激活分组」（watchlist_meta.active_group）：新加自选 / 删除 / 粘贴同步都落在当前组。
- 粘贴/截图同步一次只替换一组（目标 = 当前激活分组），避免误冲多组。
- 托管「仅自选股」范围（stock_scope=1）取该用户全部分组下的自选（见 trust.get_managed_symbols）。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from sqlalchemy import func

from app import account as account_mod
from app import db
from app.errors import ServiceError
from tradingagents.dataflows import wind as _wind
from tradingagents.dataflows.errors import NoMarketDataError, VendorError

DEFAULT_GROUP = "默认"
_SEARCH_LIMIT = 8


def _norm_group(name: str | None) -> str:
    s = (name or "").strip()
    if not s:
        raise ServiceError("分组名不能为空")
    if len(s) > 50:
        s = s[:50].strip() or "默认"
    return s


# ---------- 会话内小工具（不单独开 session） ----------

def _meta_row(session, user_id: int):
    m = (
        session.query(db.WatchlistMeta)
        .filter(db.WatchlistMeta.user_id == user_id)
        .first()
    )
    if m is None:
        m = db.WatchlistMeta(user_id=user_id, active_group=DEFAULT_GROUP, created_at=time.time())
        session.add(m)
        session.flush()
    return m


def _has_group(session, user_id: int, name: str) -> bool:
    return (
        session.query(db.WatchlistGroup)
        .filter(db.WatchlistGroup.user_id == user_id, db.WatchlistGroup.name == name)
        .first()
        is not None
    )


def _ensure_group(session, user_id: int, name: str) -> None:
    """确保分组名册里有该组（没有则建，空组可持久）。"""
    if not _has_group(session, user_id, name):
        session.add(db.WatchlistGroup(user_id=user_id, name=name, created_at=time.time()))
        session.flush()


def _active_group(session, user_id: int) -> str:
    """当前激活分组；若已被删/不存在则回退「默认」。"""
    m = _meta_row(session, user_id)
    active = m.active_group or DEFAULT_GROUP
    _ensure_group(session, user_id, active)
    if not _has_group(session, user_id, active):
        active = DEFAULT_GROUP
        _ensure_group(session, user_id, active)
        m.active_group = active
        session.flush()
    return active


def _group_names(session, user_id: int) -> list[str]:
    rows = (
        session.query(db.WatchlistGroup.name)
        .filter(db.WatchlistGroup.user_id == user_id)
        .order_by(db.WatchlistGroup.id.asc())
        .all()
    )
    return [r[0] for r in rows]


def _counts(session, user_id: int) -> dict[str, int]:
    rows = (
        session.query(db.Watchlist.group_name, func.count(db.Watchlist.id))
        .filter(db.Watchlist.user_id == user_id)
        .group_by(db.Watchlist.group_name)
        .all()
    )
    return {name: n for name, n in rows}


def _resolve_by_name(name: str) -> list[dict[str, str]]:
    """名称 → 候选代码（Wind 名称检索）；失败返回空。"""
    try:
        from app import intent as _intent

        rows = [r for r in _intent._basicinfo_rows(name) if not _intent._is_bond(r)]
        seen, out = set(), []
        for r in rows:
            code = r.get("code")
            if code and code not in seen:
                seen.add(code)
                out.append({"code": code, "name": r.get("name") or name})
        return out
    except Exception:  # noqa: BLE001 —— 检索不可用也不阻塞同步
        return []


# ---------- 分组 CRUD ----------

def list_groups(user_id: int) -> dict:
    """读取分组结构 + 当前激活分组的明细。"""
    session = db.get_session()
    try:
        active = _active_group(session, user_id)
        counts = _counts(session, user_id)
        groups = [
            {"name": n, "count": counts.get(n, 0), "active": n == active}
            for n in _group_names(session, user_id)
        ]
        rows = (
            session.query(db.Watchlist)
            .filter(db.Watchlist.user_id == user_id, db.Watchlist.group_name == active)
            .order_by(db.Watchlist.id.asc())
            .all()
        )
        items = [
            {"stock_code": r.stock_code, "stock_name": r.stock_name, "source": r.source}
            for r in rows
        ]
        session.commit()  # _active_group 可能补建 meta/默认组
    finally:
        session.close()
    # 只在当前分组拉行情（避免每次把所有组都打一遍 Wind）
    # 只在当前分组拉行情（批量一次补齐，避免每次把所有组/每只都单独打 Wind）
    quotes = account_mod.get_quotes([it["stock_code"] for it in items])
    for it in items:
        q = quotes.get(it["stock_code"])
        if q:
            it["price"] = q["price"]
            if q["prev_close"] is not None:
                chg = round(q["price"] - q["prev_close"], 2)
                it["change"] = chg
                it["change_pct"] = round(chg / q["prev_close"] * 100, 2) if q["prev_close"] else 0.0
            else:
                it["change"] = None
                it["change_pct"] = None
        else:
            it["price"] = None
            it["change"] = None
            it["change_pct"] = None
    return {"active_group": active, "groups": groups, "watchlist": items}


def groups_meta(user_id: int) -> dict:
    """仅分组名册 + 当前激活组（不含自选明细），供托管「仅自选某组」下拉使用。"""
    session = db.get_session()
    try:
        active = _active_group(session, user_id)
        counts = _counts(session, user_id)
        groups = [
            {"name": n, "count": counts.get(n, 0), "active": n == active}
            for n in _group_names(session, user_id)
        ]
        session.commit()  # _active_group 可能补建 meta/默认组
        return {"active_group": active, "groups": groups}
    finally:
        session.close()


def create_group(user_id: int, name: str) -> dict:
    name = _norm_group(name)
    session = db.get_session()
    try:
        if _has_group(session, user_id, name):
            raise ServiceError(f"分组「{name}」已存在")
        session.add(db.WatchlistGroup(user_id=user_id, name=name, created_at=time.time()))
        session.commit()
        return {"name": name, "ok": True}
    finally:
        session.close()


def rename_group(user_id: int, old_name: str, new_name: str) -> dict:
    old_name = _norm_group(old_name)
    new_name = _norm_group(new_name)
    if old_name == new_name:
        return {"name": new_name, "ok": True}
    session = db.get_session()
    try:
        if not _has_group(session, user_id, old_name):
            raise ServiceError(f"分组「{old_name}」不存在")
        if _has_group(session, user_id, new_name):
            raise ServiceError(f"分组「{new_name}」已存在")
        session.query(db.WatchlistGroup).filter(
            db.WatchlistGroup.user_id == user_id, db.WatchlistGroup.name == old_name
        ).update({"name": new_name})
        session.query(db.Watchlist).filter(
            db.Watchlist.user_id == user_id, db.Watchlist.group_name == old_name
        ).update({"group_name": new_name})
        m = _meta_row(session, user_id)
        if m.active_group == old_name:
            m.active_group = new_name
        session.commit()
        return {"name": new_name, "ok": True}
    finally:
        session.close()


def delete_group(user_id: int, name: str) -> dict:
    name = _norm_group(name)
    session = db.get_session()
    try:
        if not _has_group(session, user_id, name):
            raise ServiceError(f"分组「{name}」不存在")
        remaining = _group_names(session, user_id)
        if len(remaining) <= 1:
            raise ServiceError("至少保留一个分组")
        session.query(db.WatchlistGroup).filter(
            db.WatchlistGroup.user_id == user_id, db.WatchlistGroup.name == name
        ).delete()
        session.query(db.Watchlist).filter(
            db.Watchlist.user_id == user_id, db.Watchlist.group_name == name
        ).delete()
        m = _meta_row(session, user_id)
        if m.active_group == name:
            nxt = [n for n in remaining if n != name]
            m.active_group = nxt[0] if nxt else DEFAULT_GROUP
        session.commit()
        return {"ok": True}
    finally:
        session.close()


def set_active_group(user_id: int, name: str) -> dict:
    name = _norm_group(name)
    session = db.get_session()
    try:
        if not _has_group(session, user_id, name):
            raise ServiceError(f"分组「{name}」不存在")
        m = _meta_row(session, user_id)
        m.active_group = name
        session.commit()
        return {"name": name, "ok": True}
    finally:
        session.close()


# ---------- 单只自选添加 / 删除（落在当前分组） ----------

def add_watchlist(user_id: int, stock_code: str, stock_name: str = "", group: str | None = None) -> dict:
    code = _wind.to_wind_code(stock_code or "")
    if not code:
        raise ServiceError("请输入代码或从检索结果选择")
    session = db.get_session()
    try:
        target = _norm_group(group) if group else _active_group(session, user_id)
        if not _has_group(session, user_id, target):
            _ensure_group(session, user_id, target)
        # 名称：优先显式传入；否则查 Wind 简称；再否则沿用同组已有名称，最后代码兜底
        name = (stock_name or "").strip()
        if not name:
            try:
                name = _wind.get_company_name(code) or ""
            except Exception:  # noqa: BLE001
                name = ""
        if not name:
            prev = (
                session.query(db.Watchlist)
                .filter(db.Watchlist.user_id == user_id, db.Watchlist.stock_code == code)
                .first()
            )
            name = (prev.stock_name if prev else "") or code
        exists = (
            session.query(db.Watchlist)
            .filter(
                db.Watchlist.user_id == user_id,
                db.Watchlist.group_name == target,
                db.Watchlist.stock_code == code,
            )
            .first()
        )
        if not exists:
            session.add(
                db.Watchlist(
                    user_id=user_id,
                    group_name=target,
                    stock_code=code,
                    stock_name=name,
                    source=0,
                    created_at=time.time(),
                )
            )
            session.commit()
            return {"stock_code": code, "stock_name": name, "group": target, "added": True}
        return {"stock_code": code, "stock_name": name or code, "group": target, "added": False}
    finally:
        session.close()


def remove_watchlist(user_id: int, stock_code: str, group: str | None = None) -> None:
    code = _wind.to_wind_code(stock_code or "")
    session = db.get_session()
    try:
        target = _norm_group(group) if group else _active_group(session, user_id)
        session.query(db.Watchlist).filter(
            db.Watchlist.user_id == user_id,
            db.Watchlist.group_name == target,
            db.Watchlist.stock_code == code,
        ).delete()
        session.commit()
    finally:
        session.close()


# ---------- 粘贴 / 截图同步（一次替换一组 = 当前分组） ----------

def sync_watchlist(user_id: int, stocks: list[dict], group: str | None = None) -> dict:
    """用解析出的自选列表替换目标分组（默认当前分组）的内容。

    stocks: [{"code": ..., "name": ...}]；缺 code 但有 name 时尽力用 Wind 名称检索解析，
    解析不出则跳过（计入 skipped）。code+name 都有时不额外联网。
    """
    session = db.get_session()
    try:
        target = _norm_group(group) if group else _active_group(session, user_id)
        if not _has_group(session, user_id, target):
            _ensure_group(session, user_id, target)
        # 名称反查缓存：避免同组多个同名反复打 Wind
        name_cache: dict[str, dict] = {}

        def _resolve(row: dict) -> dict | None:
            code = (row.get("code") or "").strip()
            name = (row.get("name") or "").strip()
            if code:
                code = _wind.to_wind_code(code)
            if name:
                if name in name_cache:  # 已查过该名称：沿用命中代码或回到行内 code
                    hit = name_cache[name]
                    final_code = hit.get("code") or code
                    return {"code": final_code, "name": name} if final_code else None
                if not code:  # 只有名称 → Wind 名称检索取第一个命中
                    cands = _resolve_by_name(name)
                    if cands:
                        name_cache[name] = cands[0]
                        return {"code": cands[0]["code"], "name": name}
                    name_cache[name] = {"code": ""}
                    return None
            return {"code": code, "name": name} if code else None

        session.query(db.Watchlist).filter(
            db.Watchlist.user_id == user_id, db.Watchlist.group_name == target
        ).delete()
        seen: set[str] = set()
        added = 0
        skipped = 0
        for row in stocks:
            resolved = _resolve(row)
            if not resolved:
                skipped += 1
                continue
            code = resolved["code"]
            if code in seen:
                continue
            seen.add(code)
            name = resolved["name"]
            if not name or name == code:  # 缺名称 → 反查 Wind 简称（仅未去重过的码联网一次）
                try:
                    got = _wind.get_company_name(code)
                    if got:
                        name = got
                except Exception:  # noqa: BLE001
                    name = ""
            session.add(
                db.Watchlist(
                    user_id=user_id,
                    group_name=target,
                    stock_code=code,
                    stock_name=name or code,
                    source=1,  # 截图/文本解析同步
                    created_at=time.time(),
                )
            )
            added += 1
        session.commit()
        return {"group": target, "added": added, "skipped": skipped}
    finally:
        session.close()


# ---------- 检索 ----------

def search_stocks(user_id: int, q: str) -> list[dict]:
    """检索股票/ETF（Wind 名称 NL + 本地代码归一），返回候选 [{code,name}]。"""
    term = (q or "").strip()
    if not term:
        return []
    from app import intent as _intent

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        rows = [r for r in _intent._basicinfo_rows(term) if not _intent._is_bond(r)]
        for r in rows:
            code = r.get("code")
            if not code or code in seen:
                continue
            seen.add(code)
            results.append({"code": code, "name": r.get("name") or code})
            if len(results) >= _SEARCH_LIMIT:
                break
    except Exception:  # noqa: BLE001
        pass
    # 纯 6 位数字/带后缀代码但 Wind NL 没返回 → 本地前缀兜底
    if not results:
        code = _wind.to_wind_code(term)
        if code and (term.isdigit() or "." in term):
            name = ""
            try:
                name = _wind.get_company_name(code) or ""
            except Exception:  # noqa: BLE001
                name = ""
            results.append({"code": code, "name": name or code})
    return results


# ---------- 个股详情 ----------

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
