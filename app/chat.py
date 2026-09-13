"""AI 对话助手：只读工具调用循环。

助手通过 ``app/chat_tools.py`` 提供的只读工具按需查询后端数据（账户/持仓/收益/次日计划/
监控条件/研究评级/自选/行情），再基于查到的真实数据回答。工具是**手写白名单**且用户身份由
闭包捕获，助手没有任何写权限。

深度分析（单标的 10-15 分钟）**不由本模块发起**：``request_deep_analysis`` 工具只产出确认请求，
前端拿到 ``kind="analysis_confirm"`` 后弹确认，用户点了才走 ``/analysis`` 异步链路。
"""
from __future__ import annotations

import json
import os
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app import chat_tools
from app.errors import ServiceError
from tradingagents.dataflows import wind as _wind
from tradingagents.llm_clients import create_llm_client

#: 工具循环的轮次上限。典型问题 2-4 轮；上限只是防跑飞。
MAX_TOOL_ROUNDS = 8

#: 整轮对话的耗时预算（秒）。超了就剥掉工具逼模型用已有信息作答，避免请求无限挂起
#: （前端 ``api()`` 是裸 fetch，没有超时）。
TOOL_BUDGET_SECONDS = 180

#: 送进 LLM 的历史条数上限。前端每次都把整份会话带上，不裁会无限增长。
MAX_HISTORY_TURNS = 12

# DeepSeek 思考型模型需要足够的输出长度，否则长回答（逐项分析持仓）会被 max_tokens 截断
_ANSWER_MAX_TOKENS = 8000

_SYSTEM_TOOLS = (
    "你是 A股模拟交易 + AI 托管应用的只读数据助手。你可以调用工具查询当前登录用户的真实数据，"
    "并据此回答。回答简洁、专业、中文；可换行分点，关键数字用 **加粗**。\n"
    "\n"
    "【权限边界 —— 只读】\n"
    "你没有任何写权限：不能开关托管、不能修改策略/风控/费率、不能下单、不能触发深度分析、"
    "不能生成或撤销计划、不能清空记录。任何改动都必须由用户自己在界面上点击完成；"
    "你只能告知「去哪里点什么」，绝不声称自己已完成某项改动。\n"
    "\n"
    "【账户口径 —— 最重要的红线】\n"
    "账户有「镜像簿（真实券商账户）」和「AI 托管簿」两本。\n"
    "- 总资产一律以【镜像簿】为准。\n"
    "- 若托管簿的起始仓是从镜像簿复制建立的，两簿持仓是同一笔资产的两种视图，"
    "严禁把两簿总资产相加（会翻倍）。\n"
    "- 只有在托管簿是「空仓独立建仓」（默认 50 万现金）时，托管金才独立于真实账户。\n"
    "get_account_overview 的输出里已带口径说明，回答总资产时必须沿用该口径。\n"
    "\n"
    "【空态表达】\n"
    "工具返回的「尚未建簿 / 未开启托管 / 暂无计划 / 无行情 / 尚未研究」都是空态，不是 0。"
    "必须如实告知空态，绝不把空态当成 0 或编造数字。\n"
    "\n"
    "【日期与交易日】\n"
    "涉及「次日计划 / 监控条件」时，执行日以工具返回的 trade_date 为准，不要自行推算交易日；"
    "不确定就先调 get_plan_status。\n"
    "\n"
    "【数据纪律】\n"
    "- 只使用工具返回的数据，绝不编造价格、持仓、评级、计划。工具没查到的就说不确定，"
    "并建议用户如何获取。\n"
    "- 引用数字时说明来源与时点（实时快照 / 最近一次计划 / 最新研究报告）。\n"
    "- 已实现盈亏后端暂未统计，只有浮动盈亏。被问到就说「暂未统计」，"
    "不要用成交流水倒推一个数字。\n"
    "\n"
    "【深度分析 —— 必须先确认】\n"
    "深度分析耗时约 10-15 分钟，你**不能直接发起**。当用户要求深度分析时，调用 "
    "request_deep_analysis，然后把它返回的确认请求**原样**交给用户，等用户在界面点确认。"
    "绝不声称已开始分析。\n"
    "\n"
    "【回答要求】\n"
    "若问题要求逐项分析持仓/成交，请逐只完整列出并给结论，不要省略、不要中途截断。"
)

#: 轮次/预算耗尽后的收尾提示：剥掉工具，逼模型用手上已有的数据作答。
_FINAL_NUDGE = "请基于以上已获取的数据直接作答，不要再请求工具。"
_BUDGET_EXHAUSTED = "查询步骤太多了，我先就目前掌握的信息回答；如果需要更细的数据，可以把问题拆小一点再问。"


def _build_llm():
    """用引擎的 DeepSeek 客户端。

    不手写 HTTP 请求是有原因的：``deepseek-flash`` 是 thinking 模型，多轮工具调用必须回传
    ``reasoning_content``，而 ``DeepSeekChatOpenAI`` 已实现这个往返。手搓版本会在第二轮 400。
    """
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise ServiceError("未找到 DEEPSEEK_API_KEY")
    client = create_llm_client(
        "deepseek",
        os.getenv("DEEPSEEK_QUICK_MODEL", "deepseek-flash"),
        timeout=120,
        max_retries=1,
        max_tokens=_ANSWER_MAX_TOKENS,
    )
    return client.get_llm()


_ROLE_CLS = {"user": HumanMessage, "assistant": AIMessage}


def _to_lc_messages(messages: list[dict]) -> list:
    """前端来的 ``[{role, content}]``（纯文本）→ LangChain 消息，只保留最近若干条。"""
    out: list = []
    for m in (messages or [])[-MAX_HISTORY_TURNS:]:
        cls = _ROLE_CLS.get((m.get("role") or "").lower())
        content = m.get("content")
        if cls is None or not content:
            continue
        out.append(cls(content=content))
    if not out:
        out.append(HumanMessage(content="请介绍一下我的账户情况。"))
    return out


def _run_tool(tools_by_name: dict, call: dict) -> str:
    """执行一次工具调用，**任何情况下都返回文本**。

    ``_guard``（chat_tools）已经兜住了工具函数体内的异常，这里再兜一层是因为 ``invoke``
    还可能因**入参校验**（模型给错类型）而抛——那种情况下把错误回给模型，它自己会改参数重试，
    整轮对话不该因此崩掉。
    """
    name = call.get("name") or ""
    tool_obj = tools_by_name.get(name)
    if tool_obj is None:
        return f"【工具出错】没有名为「{name}」的工具。可用工具：{'、'.join(sorted(tools_by_name))}"
    try:
        out = tool_obj.invoke(call.get("args") or {})
    except Exception as e:  # noqa: BLE001 —— 见 docstring
        return f"【工具出错】{name}: {type(e).__name__}"
    return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


def _parse_confirm(result: str) -> dict | None:
    """识别深度分析确认哨兵。不是确认请求（或载荷不完整）返回 None。"""
    if not result.startswith(chat_tools.CONFIRM_PREFIX):
        return None
    raw = result[len(chat_tools.CONFIRM_PREFIX):]
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict) or not payload.get("ticker"):
        return None
    return payload


def answer_with_tools(user_id: int, messages: list[dict]) -> dict:
    """跑工具循环，返回前端契约字典。

    返回 ``{"kind": "account", "answer": str}`` 或
    ``{"kind": "analysis_confirm", "ticker", "name", "date", "question"}``。
    """
    tools = chat_tools.build_registry(user_id)
    tools_by_name = {t.name: t for t in tools}
    llm = _build_llm()
    # 绝不传 tool_choice —— deepseek-flash 是 thinking 模型，任何 tool_choice 都会 400
    # 「Thinking mode does not support this tool_choice」。bind_tools 不带 tool_choice
    # 正是 DeepSeek 支持的形式，模型自主决定调不调、调哪个。
    bound = llm.bind_tools(tools)

    history: list = [SystemMessage(content=_SYSTEM_TOOLS), *_to_lc_messages(messages)]
    deadline = time.monotonic() + TOOL_BUDGET_SECONDS

    for _ in range(MAX_TOOL_ROUNDS):
        if time.monotonic() > deadline:
            break
        ai = bound.invoke(history)
        # ★ 必须原样 append AIMessage 对象，不能拍平成 dict：
        #   DeepSeekChatOpenAI 靠 additional_kwargs["reasoning_content"] 做往返，
        #   重建消息会丢掉它，下一轮直接 400。
        history.append(ai)
        calls = getattr(ai, "tool_calls", None) or []
        if not calls:
            return {"kind": "account", "answer": ai.content or ""}
        for call in calls:
            result = _run_tool(tools_by_name, call)
            confirm = _parse_confirm(result)
            if confirm:
                # 确认请求不落进 history：这一轮到此为止，等用户在界面上点确认。
                return {"kind": "analysis_confirm", **confirm}
            # 一次 AIMessage 可能带多个 tool_calls，每个 id 都必须有对应的 ToolMessage，
            # 少一条下一轮协议校验就失败。
            history.append(ToolMessage(content=result, tool_call_id=call["id"]))

    # 轮次或预算耗尽：剥掉工具再问一轮，保证有回答而不是空手而归。
    try:
        final = llm.invoke(history + [HumanMessage(content=_FINAL_NUDGE)])
        return {"kind": "account", "answer": final.content or _BUDGET_EXHAUSTED}
    except Exception:  # noqa: BLE001 —— 收尾也失败就退回固定文案，不再往上抛
        return {"kind": "account", "answer": _BUDGET_EXHAUSTED}


def answer_account_question(user_id: int, question: str) -> str:
    """账户问答的文本入口（``/api/chat/account`` 用）。"""
    result = answer_with_tools(user_id, [{"role": "user", "content": question}])
    return result.get("answer") or result.get("question") or ""


def account_context(user_id: int) -> str:
    """账户上下文文本。实现在 chat_tools（工具层复用），此处保留转出以兼容既有引用。"""
    return chat_tools.account_context(user_id)


def market_quote(ticker: str, name: str = "") -> dict:
    """查某只证券的最新行情快照。ticker 可为 6 位代码或带后缀代码。

    返回 {"code","name","price","prev_close","change_pct"}；行情不可得时抛 ServiceError。
    """
    from app import account as account_mod

    wind_code = _wind.to_wind_code(ticker)
    quote = account_mod.get_quote(wind_code)
    if not quote:
        raise ServiceError(f"无法获取 {name or wind_code} 行情，可能停牌或代码错误")
    price = quote["price"]
    prev_close = quote["prev_close"]
    change_pct = round((price - prev_close) / prev_close, 4) if prev_close else 0.0
    return {
        "code": wind_code,
        "name": name or wind_code,
        "price": price,
        "prev_close": prev_close,
        "change_pct": change_pct,
    }
