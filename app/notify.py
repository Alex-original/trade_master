"""仓位变动通知：飞书 / 企微 webhook（邮件 SMTP 后续接）。

**入口有两个，别用错**：

- ``notify_trades(user_id, trades)``：执行层用的**异步**派发，起守护线程后立即返回。
- ``_send_trades(user_id, trades)``：**同步**实现，测试与需要确认送达的场合直接调它。

**合并粒度 = 用户 × 一次执行**：一次再平衡常出 3~5 笔，逐笔发会把群刷屏，所以一个
tick 内的成交拼成一条消息。``_format_text`` 负责这件事，两条入口共用同一份文案。
"""
from __future__ import annotations

import threading

from app import db

#: 单次 webhook 的超时与尝试次数。**刻意压得比"能成功"更保守**：
#: 执行 job 每分钟顺序遍历全部活跃用户，``_post`` 的耗时是同步累加的，
#: 一个挂掉的 webhook 会卡住排在它后面的所有用户。宁可少重试一次也不要拖住 tick。
_TIMEOUT = 5
_RETRIES = 2


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

        resp = requests.post(url, json=payload, timeout=_TIMEOUT)
        return resp.status_code in (200, 204)
    except Exception:  # noqa: BLE001
        return False


def _format_text(trades: list[dict]) -> str:
    """1..N 笔成交 → 一条消息文本。单笔走原文案，多笔走「本次调仓 N 笔」+ 逐行明细。"""
    if len(trades) == 1:
        t = trades[0]
        direction = "买入" if t["direction"] == 0 else "卖出"
        lines = [
            f"[AI托管] {direction} {t['stock_name']}（{t['stock_code']}）",
            f"{t['quantity']} 股 @ {t['price']}",
        ]
        reason = (t.get("ai_reason") or "").strip()
        # 没理由就**不出现这一行**。原先是
        #     f"...{t['price']}\n" f"理由：{reason}" if reason else ""
        # ——Python 把三元表达式的优先级解析在**隐式字符串拼接之下**，等价于
        # ``(前两行 + "理由：...") if reason else ""``，于是 reason 为空时**整条消息
        # 变成空串**，推出去一个空白气泡。改成追加式而不是再写一次行内三元：只要还留着
        # 隐式拼接，下次加字段同样会踩。
        if reason:
            lines.append(f"理由：{reason}")
        return "\n".join(lines)
    lines = [f"[AI托管] 本次调仓 {len(trades)} 笔"]
    for t in trades:
        direction = "买入" if t["direction"] == 0 else "卖出"
        lines.append(
            f"· {direction} {t['stock_name']}（{t['stock_code']}）{t['quantity']} 股 @ {t['price']}"
        )
    return "\n".join(lines)


def _send_trades(user_id: int, trades: list[dict]) -> int:
    """**同步**发送，返回成功送达的渠道数（0 = 没配置 / 全失败 / 空入参）。

    同步是为了可测、也是为了"真的送达了吗"能被明确观测到；执行层要走 ``notify_trades``。
    """
    trades = [t for t in (trades or []) if t]
    if not trades:
        return 0
    configs = _get_configs(user_id)
    if not configs:
        return 0
    text = _format_text(trades)
    sent = 0
    for channel, url in configs:
        if channel == "feishu":
            payload = {"msg_type": "text", "content": {"text": text}}
        elif channel == "wecom":
            payload = {"msgtype": "text", "text": {"content": text}}
        else:
            continue  # email 等后续 SMTP 实现
        for _ in range(_RETRIES):
            if _post(url, payload):
                sent += 1
                break
    return sent


def notify_trades(user_id: int, trades: list[dict]) -> None:
    """成交后**异步**派发通知（守护线程，立即返回）。

    为什么必须异步：``trust._sched_execution`` 每分钟顺序遍历全部活跃用户，而 ``_post``
    最坏 ``_TIMEOUT × _RETRIES`` 秒。同步发的话，一个挂掉的 webhook 会把该 tick 里排在
    它后面的所有用户一起卡住——通知是附属品，不该有能力拖垮执行主链路。

    线程体整个包在 try/except 里：在守护线程里抛出去的异常没人接，会静默消失。

    已知债：守护线程 + fire-and-forget ⇒ 进程重启会丢掉在途通知。生产级答案是 outbox 表
    （先落库、再由单独的发送器投递与重试），本轮不做，但别把它当成"已经可靠"。
    """
    trades = [t for t in (trades or []) if t]
    if not trades:
        return

    def _worker() -> None:
        try:
            _send_trades(user_id, trades)
        except Exception as e:  # noqa: BLE001
            print(f"[notify] 用户 {user_id} 通知发送异常：{e}", flush=True)

    threading.Thread(target=_worker, daemon=True, name=f"notify-{user_id}").start()


def send_trade_notification(user_id: int, trade_dict: dict) -> None:
    """单笔成交通知（同步，保留入口）。转发给 ``_send_trades``，文案只有一份。

    ``_execute_plan`` 不走这里——它一个 tick 可能出多笔，走 ``notify_trades`` 合并发送。
    """
    _send_trades(user_id, [trade_dict])


def get_notification_configs(user_id: int) -> list[dict]:
    """读取用户全部通知渠道配置（含未启用的）。"""
    session = db.get_session()
    try:
        rows = (
            session.query(db.NotificationConfig)
            .filter(db.NotificationConfig.user_id == user_id)
            .all()
        )
        return [
            {"channel": r.channel, "webhook_url": r.webhook_url, "is_enabled": r.is_enabled}
            for r in rows
        ]
    finally:
        session.close()


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
