"""登录鉴权：短信验证码登录 + opaque token 会话 + FastAPI 鉴权依赖。

复用 video-note 的 send_code/login/create_session/get_user_id_by_token/delete_session，
差异：新用户建号时初始化镜像簿账户（50 万现金）+ 托管配置（未建簿）。
"""
from __future__ import annotations

import secrets
import time

from fastapi import Header, HTTPException

from app import db, sms
from app.errors import ServiceError

SESSION_TTL_SECONDS = 7 * 24 * 3600  # 会话 7 天
INITIAL_CASH = 500000.0  # 新用户镜像簿初始资金（50 万）


def send_code(phone: str) -> str:
    ok, msg = sms.send_code(phone)
    if not ok:
        raise ServiceError(msg)
    return msg


def login(phone: str, code: str) -> dict:
    phone = (phone or "").strip()
    code = (code or "").strip()
    if not phone:
        raise ServiceError("请输入手机号")
    if not (len(phone) == 11 and phone.isdigit()):
        raise ServiceError("手机号格式不正确")
    if not code:
        raise ServiceError("请输入验证码")
    if not sms.verify_code(phone, code):
        raise ServiceError("验证码错误或已过期")

    session = db.get_session()
    try:
        user = session.query(db.User).filter(db.User.phone == phone).first()
        if not user:
            now = time.time()
            user = db.User(phone=phone, created_at=now)
            session.add(user)
            session.flush()  # 拿到 user.id
            session.add(
                db.Account(
                    user_id=user.id,
                    available_cash=INITIAL_CASH,
                    has_synced=False,
                    created_at=now,
                )
            )
            session.add(
                db.TrustConfig(
                    user_id=user.id,
                    is_active=False,
                    book_created=False,
                    available_cash=0.0,
                    updated_at=now,
                )
            )
            session.commit()
        user_id = user.id
    finally:
        session.close()

    token = create_session(user_id)
    return {"token": token, "phone": phone}


def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    now = time.time()
    session = db.get_session()
    try:
        session.add(
            db.Session(
                token=token,
                user_id=user_id,
                created_at=now,
                expires_at=now + SESSION_TTL_SECONDS,
            )
        )
        session.commit()
        return token
    finally:
        session.close()


def get_user_id_by_token(token: str | None) -> int | None:
    if not token:
        return None
    session = db.get_session()
    try:
        s = session.query(db.Session).filter(db.Session.token == token).first()
        if not s or s.expires_at < time.time():
            return None
        return s.user_id
    finally:
        session.close()


def delete_session(token: str | None) -> None:
    if not token:
        return
    session = db.get_session()
    try:
        session.query(db.Session).filter(db.Session.token == token).delete()
        session.commit()
    finally:
        session.close()


def get_current_user(authorization: str = Header(default="")) -> int:
    """FastAPI 依赖：从 Authorization: Bearer <token> 解析 user_id，失败抛 401。"""
    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    user_id = get_user_id_by_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return user_id
