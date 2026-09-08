"""AI 对话账户问答：持仓诊断 / 托管问答 / 风险检查 / 策略建议。

把账户、持仓、成交、评级上下文注入 prompt，走 DeepSeek 返回文本回答。
深度分析（单标的 10-15 分钟）仍走 intent.handle_chat → /analysis 链路。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from app import account as account_mod
from app import db
from app.errors import ServiceError

DEEPSEEK_BASE = "https://api.deepseek.com/chat/completions"


def _llm_text(system: str, messages: list[dict], max_tokens: int = 2000) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise ServiceError("未找到 DEEPSEEK_API_KEY")
    model = os.getenv("DEEPSEEK_QUICK_MODEL", "deepseek-v4-flash")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *messages],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        DEEPSEEK_BASE,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ServiceError(f"LLM HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    return (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def account_context(user_id: int) -> str:
    """组装账户上下文文本（账户汇总 + 双簿持仓 + 最近成交）。"""
    acc = account_mod.get_account(user_id)
    mirror_pos = account_mod.get_positions(user_id, 0)
    trust_pos = account_mod.get_positions(user_id, 1)

    lines = [
        f"【账户】总资产 {acc['total_assets']:.2f} 元",
        "镜像簿：现金 {:.2f}，市值 {:.2f}，浮动盈亏 {:.2f}".format(
            acc["mirror"]["cash"], acc["mirror"]["market_value"], acc["mirror"]["pnl"]
        ),
        "托管簿：现金 {:.2f}，市值 {:.2f}，浮动盈亏 {:.2f}，托管{}".format(
            acc["trust"]["cash"],
            acc["trust"]["market_value"],
            acc["trust"]["pnl"],
            "运行中" if acc["trust"]["is_active"] else "未开启",
        ),
        "【镜像簿持仓】",
    ]
    for p in mirror_pos:
        lines.append(
            f"  {p['stock_name']}（{p['stock_code']}）{p['hold_qty']}股 成本{p['cost_price']} 现价{p['price']} 盈亏{p['pnl']}"
        )
    if not mirror_pos:
        lines.append("  （空）")

    lines.append("【托管簿持仓】")
    for p in trust_pos:
        lines.append(
            f"  {p['stock_name']}（{p['stock_code']}）{p['hold_qty']}股 成本{p['cost_price']} 现价{p['price']} 盈亏{p['pnl']}"
        )
    if not trust_pos:
        lines.append("  （空）")

    session = db.get_session()
    try:
        trades = (
            session.query(db.Trade)
            .filter(db.Trade.user_id == user_id)
            .order_by(db.Trade.id.desc())
            .limit(10)
            .all()
        )
        if trades:
            lines.append("【最近成交】")
            for t in trades:
                d = "买入" if t.direction == 0 else "卖出"
                lines.append(
                    f"  {d} {t.stock_name}（{t.stock_code}）{t.quantity}股 @ {t.price} 理由：{t.ai_reason or '无'}"
                )
    finally:
        session.close()

    return "\n".join(lines)


_SYSTEM = (
    "你是 A股模拟交易 + AI 托管的金融助手。根据用户账户上下文回答用户问题"
    "（持仓诊断 / 托管调仓逻辑 / 风险检查 / 策略建议）。"
    "回答简洁、专业、中文，用 Markdown。只基于提供的上下文，不要编造数据。"
)


def answer_account_question(user_id: int, question: str) -> str:
    ctx = account_context(user_id)
    messages = [{"role": "user", "content": f"我的账户上下文：\n{ctx}\n\n我的问题：{question}"}]
    return _llm_text(_SYSTEM, messages)
