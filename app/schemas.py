"""策略 JSON Schema 协议（LLM 结构化输出 + 前端积木 + 回测 三方对齐）。

独立于 Python dataclass，作为传给大模型的强约束 schema 存在。
本地规则层 / LLM 层产出的 dict 都应通过 app.strategy.StrategyConfig.from_dict
反序列化并校验（两者枚举集合保持同步）。
"""
from __future__ import annotations

from typing import Any

from .strategy import (
    STATIC_INDICATORS,
    PERIODIC_INDICATORS,
    KNOWN_OPERATORS,
    EVENT_ONLY_OPERATORS,
)

# 供 LLM 候选的 operator 需与回测实现能力对齐，这里交给本地规则先落地 subset；
# schema 全量列出，回测引擎对尚未实现的组合返回明确 unsupported 而非静默错算。

_STATIC_PATTERN = "|".join(sorted(STATIC_INDICATORS))
_PERIODIC_PATTERN = "|".join(sorted(PERIODIC_INDICATORS))
# indicator 列表达式：静态列，或带周期派生列（ma20 / ema12 / volume_ma5）
INDICATOR_PATTERN = rf"^(?:{_STATIC_PATTERN}|(?:{_PERIODIC_PATTERN})[0-9]{{1,3}})$"
_OPERATOR_ENUM = sorted(KNOWN_OPERATORS | EVENT_ONLY_OPERATORS)

_CONDITION_JSON = {
    "type": "object",
    "properties": {
        "indicator": {
            "type": "string",
            "pattern": INDICATOR_PATTERN,
            "description": (
                "判断指标（列表达式）：静态列 close_price 收盘 / volume 成交量 / "
                "macd_dif / rsi 等；或带周期派生列，如 ma20=20 日均线、volume_ma5=5 日均量"
            ),
        },
        "operator": {
            "type": "string",
            "enum": _OPERATOR_ENUM,
            "description": "greater_than> / less_than< / cross_above 上穿 / cross_below 下穿 / is 事件类(如涨停)",
        },
        "target": {
            "type": "string",
            "description": "阈值、参照指标或周期。例：'MA20'、'20'、'5'。事件类条件(is)可为空串",
        },
        "note": {"type": "string", "description": "该条条件的白话解释，便于用户确认"},
    },
    "required": ["indicator", "operator"],
    "additionalProperties": False,
}

STRATEGY_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "StrategyConfig",
    "type": "object",
    "properties": {
        "strategy_name": {
            "type": "string",
            "description": "策略名，一句话概括（中文）",
        },
        "stock_pool": {
            "type": "string",
            "default": "A股全市场",
            "description": "标的范围，MVP 固定 'A股全市场'",
        },
        "timeframe": {
            "type": "string",
            "enum": ["1d"],
            "default": "1d",
            "description": "K 线周期，MVP 仅日线 1d",
        },
        "buy_conditions": {
            "type": "array",
            "items": _CONDITION_JSON,
            "description": "买入触发条件，默认 AND 连接（前端积木可再改 OR 分组）",
        },
        "sell_conditions": {
            "type": "array",
            "items": _CONDITION_JSON,
            "description": "卖出触发条件，可为空数组（配 holding_days）",
        },
        "holding_days": {
            "type": ["integer", "null"],
            "minimum": 1,
            "description": "备选卖出方式：买入后固定持有 N 天平仓。与 sell_conditions 二选一，都填则 sell_conditions 优先",
        },
        "position_control": {
            "type": "number",
            "minimum": 0.1,
            "maximum": 1.0,
            "default": 1.0,
            "description": "单笔仓位比例 0.1~1.0",
        },
        "max_positions": {
            "type": "integer",
            "minimum": 1,
            "default": 1,
            "description": "同时持有的股票数量，MVP 固定 1",
        },
        "note": {"type": "string", "description": "策略整体白话说明，供用户二次确认"},
    },
    "required": ["strategy_name", "buy_conditions"],
    "additionalProperties": False,
}
