"""仓位变动通知：飞书 / 企微 webhook（邮件 SMTP 后续接）。"""
from __future__ import annotations

from app import db


def _get_configs(user_id: int) -> list[tuple[str, str]]:
    session = db.get_session()
    try:
        rows = (
            session.query(db.NotificationConfig)
            .filter(
                db.NotificationConfig.user_id == user_id,
                db.NotificationConfig.is_enabled == True,  # noqa: E712
            )
            .all()
        )
        return [(r.channel, r.webhook_url) for r in rows if r.webhook_url]
    finally:
        session.close()


def _post(url: str, payload: dict) -> bool:
    try:
        import requests

        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code in (200, 204)
    except Exception:  # noqa: BLE001
        return False


def send_trade_notification(user_id: int, trade_dict: dict) -> None:
    """成交后通知。飞书/企微机器人 webhook，失败重试 3 次，仍失败静默。"""
    configs = _get_configs(user_id)
    if not configs:
        return
    direction = "买入" if trade_dict["direction"] == 0 else "卖出"
    reason = trade_dict.get("ai_reason") or ""
    text = (
        f"[AI托管] {direction} {trade_dict['stock_name']}（{trade_dict['stock_code']}）\n"
        f"{trade_dict['quantity']} 股 @ {trade_dict['price']}\n"
        f"理由：{reason}" if reason else ""
    )

    for channel, url in configs:
        if channel == "feishu":
            payload = {"msg_type": "text", "content": {"text": text}}
        elif channel == "wecom":
            payload = {"msgtype": "text", "text": {"content": text}}
        else:
            continue  # email 等后续 SMTP 实现
        for _ in range(3):
            if _post(url, payload):
                break


def set_notification_config(user_id: int, channel: str, webhook_url: str, is_enabled: bool) -> None:
    session = db.get_session()
    try:
        row = (
            session.query(db.NotificationConfig)
            .filter(
                db.NotificationConfig.user_id == user_id,
                db.NotificationConfig.channel == channel,
            )
            .first()
        )
        if row:
            row.webhook_url = webhook_url
            row.is_enabled = is_enabled
        else:
            session.add(
                db.NotificationConfig(
                    user_id=user_id,
                    channel=channel,
                    webhook_url=webhook_url,
                    is_enabled=is_enabled,
                )
            )
        session.commit()
    finally:
        session.close()
