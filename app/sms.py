"""短信验证码：发送 + 校验。

复用 video-note 的实现，去掉 B站 cookie 告警等视频业务专属逻辑。
阿里云短信未配置时（缺 AccessKey/签名/模板），验证码打印到服务端日志（开发/内测兜底）；
配置齐全后走真实短信。校验通过即标记已用，防止重复登录。
"""
from __future__ import annotations

import os
import random
import time

from app import db

CODE_EXPIRE_SECONDS = 300  # 5 分钟有效
CODE_LENGTH = 6
SEND_INTERVAL_SECONDS = 60  # 同号发送间隔，防短信费被刷

# 阿里云短信配置（环境变量）
ACCESS_KEY_ID = os.getenv("ALIYUN_ACCESS_KEY_ID", "")
ACCESS_KEY_SECRET = os.getenv("ALIYUN_ACCESS_KEY_SECRET", "")
SIGN_NAME = os.getenv("SMS_SIGN_NAME", "")
TEMPLATE_CODE = os.getenv("SMS_TEMPLATE_CODE", "")

SMS_CONFIGURED = all([ACCESS_KEY_ID, ACCESS_KEY_SECRET, SIGN_NAME, TEMPLATE_CODE])


def _gen_code():
    return f"{random.randint(0, 10 ** CODE_LENGTH - 1):0{CODE_LENGTH}d}"


def send_code(phone):
    """发送验证码到指定手机号。返回 (ok, message)。"""
    phone = (phone or "").strip()
    if not (len(phone) == 11 and phone.isdigit()):
        return False, "手机号格式不正确"

    code = _gen_code()
    now = time.time()
    expires_at = now + CODE_EXPIRE_SECONDS

    session = db.get_session()
    try:
        latest = (
            session.query(db.SmsCode)
            .filter(db.SmsCode.phone == phone)
            .order_by(db.SmsCode.id.desc())
            .first()
        )
        if latest:
            elapsed = now - (latest.expires_at - CODE_EXPIRE_SECONDS)
            if elapsed < SEND_INTERVAL_SECONDS:
                wait = int(SEND_INTERVAL_SECONDS - elapsed) + 1
                return False, f"发送太频繁，请 {wait} 秒后再试"
        session.query(db.SmsCode).filter(db.SmsCode.phone == phone).delete()
        session.add(db.SmsCode(phone=phone, code=code, expires_at=expires_at, used=False))
        session.commit()
    finally:
        session.close()

    if SMS_CONFIGURED:
        ok, msg = _send_via_aliyun(phone, TEMPLATE_CODE, f'{{"code":"{code}"}}')
        if not ok:
            return False, f"短信发送失败：{msg}"
        return True, "验证码已发送，请查收短信"
    # 开发兜底：打印到服务端日志
    print(f"[验证码] 手机号 {phone} 验证码 {code}（{CODE_EXPIRE_SECONDS // 60} 分钟内有效）", flush=True)
    return True, f"开发模式验证码：{code}（{CODE_EXPIRE_SECONDS // 60} 分钟内有效）"


def verify_code(phone, code):
    """校验验证码，通过则标记已用。返回 bool。"""
    phone = (phone or "").strip()
    code = (code or "").strip()
    if not code:
        return False
    session = db.get_session()
    try:
        row = (
            session.query(db.SmsCode)
            .filter(
                db.SmsCode.phone == phone,
                db.SmsCode.code == code,
                db.SmsCode.used == False,  # noqa: E712
            )
            .order_by(db.SmsCode.id.desc())
            .first()
        )
        if not row:
            return False
        if row.expires_at < time.time():
            return False
        row.used = True
        session.commit()
        return True
    finally:
        session.close()


def _send_via_aliyun(phone, template_code, template_param=""):
    """调用阿里云短信 SDK 发送。返回 (ok, message)。"""
    try:
        from alibabacloud_dysmsapi20170525.client import Client
        from alibabacloud_dysmsapi20170525 import models as dysmsapi_models
        from alibabacloud_tea_openapi import models as open_api_models
    except ImportError:
        return False, "未安装阿里云短信 SDK（alibabacloud_dysmsapi20170525）"

    try:
        config = open_api_models.Config(
            access_key_id=ACCESS_KEY_ID,
            access_key_secret=ACCESS_KEY_SECRET,
            endpoint="dysmsapi.aliyuncs.com",
        )
        client = Client(config)
        req = dysmsapi_models.SendSmsRequest(
            phone_numbers=phone,
            sign_name=SIGN_NAME,
            template_code=template_code,
            template_param=template_param,
        )
        resp = client.send_sms(req)
        if resp.body.code == "OK":
            return True, ""
        return False, resp.body.message or "未知错误"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
