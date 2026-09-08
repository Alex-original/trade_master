"""本地规则匹配器（Local Rule Matcher）。

目的（对齐 PRD v1.0 §4.1 / v2.0 §4.1）：
- 高频、固定逻辑的中文策略描述优先在这里命中，直接产出标准 StrategyConfig，
  不调 LLM —— 省 token、降低延迟、消除幻觉；
- 命中不了的文本返回 None，由上层交给 LLM 结构化解析兜底。

原则：
- **只做「能完整回译」的模板**。无法 100% 还原语义的表述一律返回 None 交 LLM，
  绝不拼接出"看似命中实则错意"的策略。
- 本阶段落地模板：双均线交叉、价格突破单均线（支持放量修饰）、MACD 金叉死叉。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..strategy import Condition, StrategyConfig

# 均线别名 → 周期
_ALIAS_PERIOD = {"年线": 250, "半年线": 120, "季线": 60, "月线": 20}

# 周期提取：MA20 / ma 60 / 5日均线 / 20 日线 / 年线 等
_MA_NUM_RE = re.compile(r"ma\s*(\d{1,3})", re.IGNORECASE)
_MA_CN_RE = re.compile(r"(\d{1,3})\s*日(?:均)?线")


def _extract_ma_periods(text: str) -> set[int]:
    """从文本中提取出现的均线周期集合（去重）。"""
    periods: set[int] = set()
    periods.update(int(m) for m in _MA_NUM_RE.findall(text))
    for m in _MA_CN_RE.finditer(text):
        periods.add(int(m.group(1)))
    for alias, p in _ALIAS_PERIOD.items():
        if alias in text:
            periods.add(p)
    return periods


def _has_buy_signal(text: str) -> bool:
    return any(k in text for k in ("金叉", "上穿", "突破", "站上", "升破", "上叉"))


def _has_sell_signal(text: str) -> bool:
    return any(k in text for k in ("死叉", "下穿", "跌破", "失守", "击穿", "下叉"))


def _human(text: str, max_len: int = 26) -> str:
    """从原文截一段作策略名（中文可读）。"""
    t = re.sub(r"\s+", "", text)
    return t[:max_len] if len(t) > max_len else t


@dataclass
class RuleHit:
    name: str  # 命中的模板名
    strategy: StrategyConfig


RuleBuilder = Callable[[str], StrategyConfig | None]


def build_dual_ma_cross(text: str) -> StrategyConfig | None:
    """双均线交叉：短均线金叉/上穿长均线买入，死叉/下穿卖出。

    示例命中：『5日均线上穿20日线』『MA20 金叉 MA60』『20日线与5日线金叉买入，
    死叉卖出』。需恰好出现两条不同周期均线，并至少给买入方向；否则返回 None。
    """
    periods = _extract_ma_periods(text)
    if len(periods) != 2:
        return None
    short_p, long_p = min(periods), max(periods)
    short_col, long_col = f"ma{short_p}", f"ma{long_p}"

    if not _has_buy_signal(text):
        return None  # 只有卖出没有买入，不是完整策略，交 LLM

    buy = [
        Condition(
            indicator=short_col,
            operator="cross_above",
            target=long_col,
            note=f"{short_p}日均线上穿{long_p}日均线",
        )
    ]
    if _has_sell_signal(text):
        sell = [
            Condition(
                indicator=short_col,
                operator="cross_below",
                target=long_col,
                note=f"{short_p}日均线下穿{long_p}日均线",
            )
        ]
    else:
        # 模板默认卖出：短线下穿长线（经典双均线死叉），note 中明示是模板默认
        sell = [
            Condition(
                indicator=short_col,
                operator="cross_below",
                target=long_col,
                note=f"{short_p}日均线下穿{long_p}日均线（模板默认卖出）",
            )
        ]

    return StrategyConfig(
        strategy_name=f"双均线({short_p}/{long_p})交叉",
        buy_conditions=buy,
        sell_conditions=sell,
        note=f"经典双均线策略：{short_p}日线上穿{long_p}日线买入，下穿卖出。原文：{_human(text)}",
    )


def build_price_breakout_ma(text: str) -> StrategyConfig | None:
    """价格突破单均线（支持放量修饰）：收盘价上穿均线买入。

    示例命中：『股价突破年线』『放量站上20日均线』『突破 60 日线买入，跌破卖出』。
    周期来源：年线/半年线/季线/月线 或「N日均线 / MA N」。
    """
    # 买入方向：捕获其后紧跟的均线（容忍中间少量修饰词，如「放量站上」）
    buy_m = re.search(
        r"(突破|站上|上穿|升破)(?:[^一-龥A-Za-z\d]{0,4})?(MA\s*\d{1,3}|年线|半年线|季线|月线|\d{1,3}\s*日(?:均)?线)",
        text,
        re.IGNORECASE,
    )
    if not buy_m:
        return None
    n = _period_from_ma_expr(buy_m.group(2))
    if n is None:
        return None
    ma_col = f"ma{n}"
    buy_label = _ma_label(buy_m.group(2))

    # 可选卖出：跌破/下穿同一均线（只捕捉同一条线，避免误并别的均线）
    sell = None
    sell_m = re.search(
        r"(跌破|下穿|失守|击穿)(?:[^一-龥A-Za-z\d]{0,4})?(MA\s*\d{1,3}|年线|半年线|季线|月线|\d{1,3}\s*日(?:均)?线)",
        text,
        re.IGNORECASE,
    )
    if sell_m:
        sn = _period_from_ma_expr(sell_m.group(2))
        if sn == n:
            sell = [
                Condition(
                    indicator="close_price",
                    operator="cross_below",
                    target=ma_col,
                    note=f"收盘价跌破{buy_label}",
                )
            ]

    buy = [
        Condition(
            indicator="close_price",
            operator="cross_above",
            target=ma_col,
            note=f"收盘价上穿{buy_label}",
        )
    ]
    # 放量修饰：前置「成交量高于 5 日均量」（MVP 默认 1 倍，精确倍数后续精化）
    if "放量" in text:
        buy.insert(
            0,
            Condition(
                indicator="volume",
                operator="greater_than",
                target="volume_ma5",
                note="成交量放大（高于5日均量）",
            ),
        )

    return StrategyConfig(
        strategy_name=f"突破{buy_label}",
        buy_conditions=buy,
        sell_conditions=sell or [],
        holding_days=None,  # 触发式卖出由 sell_conditions 表达
        note=f"价格突破{buy_label}买入策略。原文：{_human(text)}",
    )


def build_macd_cross(text: str) -> StrategyConfig | None:
    """MACD 金叉死叉：DIF 上穿 DEA 买入，下穿卖出。

    示例命中：『MACD 金叉买入』『日线 MACD 死叉卖出（需含买入方向才成立）』。
    """
    if "macd" not in text.lower():
        return None
    if not _has_buy_signal(text):
        return None

    buy = [
        Condition(
            indicator="macd_dif",
            operator="cross_above",
            target="macd_dea",
            note="MACD DIF 上穿 DEA（金叉）",
        )
    ]
    sell = None
    if _has_sell_signal(text):
        sell = [
            Condition(
                indicator="macd_dif",
                operator="cross_below",
                target="macd_dea",
                note="MACD DIF 下穿 DEA（死叉）",
            )
        ]

    return StrategyConfig(
        strategy_name="MACD 金叉死叉",
        buy_conditions=buy,
        sell_conditions=sell or [],
        note=f"MACD 金叉买入、死叉卖出。原文：{_human(text)}",
    )


def _period_from_ma_expr(expr: str) -> int | None:
    """把『年线 / MA60 / 20日均线』解析成周期数。"""
    e = expr.strip()
    if e in _ALIAS_PERIOD:
        return _ALIAS_PERIOD[e]
    m = _MA_NUM_RE.search(e) or _MA_CN_RE.search(e)
    return int(m.group(1)) if m else None


def _ma_label(expr: str) -> str:
    """把均线表达（MA60 / 20日均线 / 年线）原样转成可读线名。

    例："年线" → "年线"；"MA60" → "60日均线"；"20日均线" → "20日均线"。
    """
    e = expr.strip()
    if e in _ALIAS_PERIOD:
        return e
    m = _MA_NUM_RE.search(e)
    if m:
        return f"{int(m.group(1))}日均线"
    m = _MA_CN_RE.search(e)
    if m:
        return f"{int(m.group(1))}日均线"
    return expr


# 模板注册表：按特异性从高到低尝试（双均线 > 单均线 > MACD）
BUILDERS: list[tuple[str, RuleBuilder]] = [
    ("双均线交叉", build_dual_ma_cross),
    ("价格突破均线", build_price_breakout_ma),
    ("MACD 金叉死叉", build_macd_cross),
]


def match_local(text: str) -> RuleHit | None:
    """遍历本地模板，返回首个完整命中的策略；未命中返回 None（交 LLM 兜底）。"""
    t = text.strip()
    if not t:
        return None
    for name, builder in BUILDERS:
        try:
            cfg = builder(t)
        except Exception:  # 规则异常不应阻断整条链路，视为未命中
            cfg = None
        if cfg is not None:
            return RuleHit(name=name, strategy=cfg)
    return None
