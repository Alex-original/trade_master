"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. At invocation, run the structured call and render the result back to
   markdown. If the structured call itself fails for any reason
   (malformed JSON from a weak model, transient provider issue), fall
   back to a plain ``llm.invoke`` so the pipeline never blocks.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

#: 账户级失败：**重试没有意义**。401 = key 失效，402 = 余额耗尽
#: （DeepSeek 用 402 + ``Insufficient Balance``）。
#:
#: 刻意**不含 429**：限流是瞬时的，把它当致命会让一次抖动废掉几小时的回测进度——那正是
#: 这里要避免的反面。也**不含 403**：同一个 provider 上它常常只是某个端点的权限。
_ACCOUNT_LEVEL_STATUS = frozenset({401, 402})
_ACCOUNT_LEVEL_HINTS = (
    "insufficient balance",
    "invalid api key",
    "authentication fails",
    "no credit",
)


class AccountLevelLLMError(RuntimeError):
    """LLM 账户不可用（鉴权失效 / 余额耗尽）。

    **为什么单独一个异常类型、而不是继续返回 None**：这类失败不是"这一次没拿到结构化结果"，
    而是"后面每一次调用都会失败"。``invoke_structured`` 的既有契约是失败返回 None 让调用方
    回落——对偶发的解析失败那是对的，对账户级失败是错的：回测会照着「连错 3 天」慢慢走完，
    白烧掉几小时墙钟，最后抛出的还是一句读起来像管线 bug 的「未产出有效行动方案」
    （run 5 就是这么被误诊成"API 接口中断"的）。所以这里**打破 None 契约向上抛**，
    由 ``backtest._drive`` 立刻中止并把真实原因写进用户可见的消息。
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def account_level_reason(exc: BaseException) -> str | None:
    """账户级失败返回可读原因，否则 None。**只认死错，不认慢错**（见上）。"""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status in _ACCOUNT_LEVEL_STATUS or any(h in text for h in _ACCOUNT_LEVEL_HINTS):
        code = f"HTTP {status}" if status else "LLM 账户错误"
        return f"{code}: {exc}"
    return None

# Schema-only structured output binds exactly one tool (the schema itself), so a
# model that reaches for a search tool emits an unknown tool call and the whole
# structured attempt is discarded for a free-text retry. Agents on this path
# state the constraint explicitly rather than relying on the binding alone
# (#1130).
NO_EXTERNAL_TOOLS = (
    "Use only the evidence provided in this prompt. Do not call external tools "
    "or search the web; if something is missing, say so explicitly."
)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Any | None:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.
    """
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run the structured call and render to markdown; fall back to free-text on any failure.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape). The same value is forwarded to the free-text path so the
    fallback sees the same input the structured call did.
    """
    if structured_llm is not None:
        try:
            result = structured_llm.invoke(prompt)
            if result is None:
                # A thinking model can answer in plain text instead of calling
                # the tool, leaving the parser with nothing to return. Treat it
                # as a structured miss and fall back, with a clear reason.
                raise ValueError("structured output returned no parsed result")
            return render(result)
        except Exception as exc:
            # 账户级失败不回落 free-text：它会以同样的方式失败，白多一次调用，而且把
            # "账户没了" 伪装成 "这个 agent 走了降级路径"。向上抛给编排层。
            fatal = account_level_reason(exc)
            if fatal:
                logger.error("%s: LLM 账户不可用，不再回落 free-text：%s", agent_name, fatal)
                raise AccountLevelLLMError(fatal) from exc
            logger.warning(
                "%s: structured-output invocation failed (%s); retrying once as free text",
                agent_name, exc,
            )

    response = plain_llm.invoke(prompt)
    return response.content


def invoke_structured(
    structured_llm: Any | None,
    prompt: Any,
    agent_name: str,
) -> T | None:
    """Run the structured call and return the raw Pydantic object (or None on failure).

    Unlike :func:`invoke_structured_or_freetext`, this does NOT render to markdown:
    it hands back the typed object so callers can persist structured fields (e.g.
    the portfolio plan's conditional actions) rather than only prose. Returns None
    when structured output is unsupported or the call fails, so callers can fall
    back to a safe default instead of blocking the pipeline.

    **失败会重试一次再放弃。** 理由：DeepSeek 全系是 thinking 模型，拒 ``tool_choice``
    （见 ``llm_clients/capabilities.py``），schema 只能当工具发出去、模型**可以不调**，
    于是偶发地纯文本作答、解析器拿到 None。实测这与并发/限流强相关——回测里 23 路研究
    并行时必现，而单发同上下文 8/8 成功——隔一次再问通常就拿到工具调用了。
    只重试**一次**：再失败说明是系统性问题（配额耗尽/鉴权/模型确实不调工具），
    继续重试只是把同一个错误写得更慢，应当让调用方看见并据此判定失败日。

    **例外：账户级失败（401/402）连一次都不重试，直接抛 ``AccountLevelLLMError``。**
    它是唯一"重试必错"的一类，而返回 None 会把「账户没了」伪装成「这个 agent 这次没拿到
    结构化结果」，上层按普通失败日处理——回测会照着连错三天慢慢走完（见该异常类的 docstring）。
    """
    if structured_llm is None:
        return None
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            result = structured_llm.invoke(prompt)
            if result is not None:
                return result
            last_exc = ValueError("structured output returned no parsed result")
        except Exception as exc:
            last_exc = exc
        fatal = account_level_reason(last_exc)
        if fatal:
            logger.error("%s: LLM 账户不可用，停止重试：%s", agent_name, fatal)
            raise AccountLevelLLMError(fatal) from last_exc
        logger.warning(
            "%s: structured-output invocation failed on attempt %d/2 (%s); %s",
            agent_name, attempt, last_exc,
            "retrying once" if attempt == 1 else "returning None for fallback",
        )
    return None
