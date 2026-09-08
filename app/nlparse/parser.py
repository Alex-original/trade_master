"""NL → StrategyConfig 解析编排器。

链路（对齐 PRD v1.0 §4.1 / v2.0 §4.1）：
    本地规则匹配 → 命中直接返回（source="local"）
    → 未命中 → LLM 结构化解析（source="llm"，本阶段接入中）
    → 仍未成功 → source="miss"（前端提示换种说法 / 补充示例）

LLM 层强约束由 app.schemas.STRATEGY_JSON_SCHEMA 提供（结构化输出），
产出 dict 经 StrategyConfig.from_dict 确定性校验兜底，防幻觉。
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import config
from ..strategy import StrategyConfig, StrategyValidationError
from .rules import match_local


@dataclass
class ParseResult:
    source: str                       # "local" | "llm" | "miss"
    strategy: StrategyConfig | None
    rule_name: str = ""               # 本地命中模板名
    message: str = ""                 # 给前端/日志的可读说明
    raw: str = ""


def parse_strategy(text: str, allow_llm: bool = True) -> ParseResult:
    """解析自然语言为策略。

    allow_llm=False 时只走本地规则（离线调试/测试用）。
    """
    t = (text or "").strip()
    if not t:
        return ParseResult("miss", None, message="输入为空", raw=t)

    hit = match_local(t)
    if hit is not None:
        return ParseResult(
            source="local",
            strategy=hit.strategy,
            rule_name=hit.name,
            message=f"本地模板「{hit.name}」命中",
            raw=t,
        )

    if allow_llm:
        try:
            strat = _parse_with_llm(t)
            if strat is not None:
                return ParseResult(
                    source="llm", strategy=strat, message="LLM 结构化解析", raw=t
                )
        except LLMNotReady as e:
            return ParseResult(
                source="miss",
                strategy=None,
                message=str(e),
                raw=t,
            )

    return ParseResult(
        source="miss",
        strategy=None,
        message="本地规则未命中，当前处于无 LLM 兜底状态（Sprint 1 接入 DeepSeek）",
        raw=t,
    )


class LLMNotReady(RuntimeError):
    """LLM 兜底层尚未就绪（未配置 key / 未实现）。"""


def _parse_with_llm(text: str) -> StrategyConfig | None:
    """[Sprint 1] DeepSeek 结构化解析兜底。

    接入点说明：
    - 用 openai SDK + base_url=config.DEEPSEEK_BASE_URL，
      model=config.DEEPSEEK_MODEL，max_tokens=config.DEEPSEEK_MAX_TOKENS；
    - 把 app.schemas.STRATEGY_JSON_SCHEMA 注入 system prompt，强制 JSON 输出；
    - 返回结果先 json.loads → StrategyConfig.from_dict 校验，
      非法格式自动重试一次（对齐 AC-1.1 容错）；
    - 参考 video-note 的标准 DeepSeek 配置（memory：推理模型 max_tokens≥8000）。
    """
    if not config.DEEPSEEK_API_KEY:
        raise LLMNotReady("未配置 OPENAI_API_KEY/DEEPSEEK_API_KEY，LLM 兜底不可用")

    # TODO(Sprint 1)：实现 DeepSeek 结构化输出调用。届时需要装依赖
    # （openai）——为避免本阶段污染环境，此函数暂不发起网络请求。
    raise LLMNotReady("LLM 解析将在 Sprint 1 接入 DeepSeek 后启用")


def validate_dict(raw: dict) -> StrategyConfig:
    """外部（如未来 API 层）传入 dict 的确定性校验入口。"""
    try:
        return StrategyConfig.from_dict(raw)
    except StrategyValidationError:
        raise
