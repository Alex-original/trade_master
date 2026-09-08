"""策略领域模型。

策略 JSON 是 NL→UI 的「中间协议」：本地规则 / LLM 解析器都产出该结构，
前端条件积木渲染它，回测引擎消费它。协议 JSON keys 用英文（便于 LLM
结构化输出与 JSON Schema 校验），展示层再做中文本地化。

设计对齐 PRD v2.0 的 StrategyConfig JSON Schema 示例，并做如下扩展：
- indicator 用「列表达式」表达，既支持静态序列（close_price 等）也支持
  带周期的派生列（ma20 / volume_ma5），从而能表达「ma5 上穿 ma20」这种
  双序列穿越，直接对接回测引擎的指标计算；
- operator 集合开放但确定性校验，非法值走容错（防止 LLM 幻觉）；
- 卖出支持两种写法之一：显式 sell_conditions，或 holding_days 固定持有
  N 日平仓（若都没有，回测层默认持有至区间末，语义在回测 Sprint 定稿）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

# ---- 指标「列表达式」----
# 静态列（无周期后缀）
STATIC_INDICATORS = {
    "close_price", "open_price", "high", "low",           # 价格
    "volume", "turnover_rate",                            # 量能
    "pct_change", "amplitude",                            # 涨跌幅/振幅
    "macd_dif", "macd_dea",                               # MACD（默认 12/26/9）
    "rsi", "kdj_k", "kdj_d", "kdj_j",                     # 摆动指标（默认周期）
    "limit_up", "limit_down",                             # 涨跌停（事件布尔列，回测层预处理）
}
# 需周期后缀的派生列，如 ma20 / ema12 / volume_ma5
PERIODIC_INDICATORS = {"ma", "ema", "volume_ma"}

# 统一提示信息（不含 LLM 的完整 schema，见 app/schemas.py）
_INDICATOR_HINT = (
    "静态列：close_price 收盘/open_price 开盘/high 最高/low 最低/volume 成交量/"
    "pct_change 涨跌幅/macd_dif/macd_dea/rsi；"
    "带周期列：ma20 表示 20 日均线、ema12、volume_ma5 表示 5 日均量；"
    "事件列：limit_up 涨停 / limit_down 跌停"
)


def parse_indicator(value: str) -> tuple[str, int | None] | None:
    """解析列表达式 → (基础名, 周期)。不合法返回 None。

    例：close_price → ("close_price", None)；ma20 → ("ma", 20)。
    """
    v = value.strip().lower()
    if v in STATIC_INDICATORS:
        return v, None
    m = re.fullmatch(r"([a-z_]+?)(\d{1,3})", v)
    if m:
        base, num = m.group(1), int(m.group(2))
        if base in PERIODIC_INDICATORS and num >= 1:
            return base, num
    return None


KNOWN_OPERATORS = {
    "greater_than", "less_than", "cross_above", "cross_below",  # 比较/穿越
    "greater_equal", "less_equal", "equal",                     # 补充比较
    "inside_range", "outside_range",                            # 区间
}
# 补充类型：纯事件条件（无 target），如「涨停」「跌停」
EVENT_ONLY_OPERATORS = {"is", "is_not"}


@dataclass
class Condition:
    """单个触发条件，列表达式写法，如：close_price cross_above ma20。

    - indicator：列表达式（静态列或带周期派生列，见 parse_indicator）
    - operator：比较 / 穿越 / 事件操作符
    - target：数值阈值（如 "20"）或参照列（如 "ma20"）；事件类可为空
    """

    indicator: str
    operator: str
    target: str = ""
    # 可选：解释这句条件（LLM 或本地规则生成，回显给用户确认）
    note: str = ""


@dataclass
class StrategyConfig:
    """一条可回测、可挂载的策略。"""

    strategy_name: str = "未命名策略"
    stock_pool: str = "A股全市场"          # 标的范围（MVP 全市场，保留字段）
    timeframe: str = "1d"                  # 周期：1d 日线（MVP）
    buy_conditions: list[Condition] = field(default_factory=list)
    sell_conditions: list[Condition] = field(default_factory=list)
    holding_days: int | None = None        # 备选卖出方式：固定持有 N 日
    position_control: float = 1.0          # 仓位 0.1~1.0
    max_positions: int = 1                 # 同时持有数量（MVP=1）
    note: str = ""                         # 对整条策略的人工说明

    # ---- 序列化 ----
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # 与 PRD JSON Schema 完全对齐的最小协议视图
        return {
            "strategy_name": self.strategy_name,
            "stock_pool": self.stock_pool,
            "timeframe": self.timeframe,
            "buy_conditions": [_cond_dict(c) for c in self.buy_conditions],
            "sell_conditions": [_cond_dict(c) for c in self.sell_conditions],
            "holding_days": self.holding_days,
            "position_control": self.position_control,
            "max_positions": self.max_positions,
            "note": self.note,
        }

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    # ---- 反序列化 + 校验 ----
    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StrategyConfig":
        errors: list[str] = []

        def _conds(key: str) -> list[Condition]:
            out: list[Condition] = []
            for item in raw.get(key) or []:
                if not isinstance(item, dict):
                    errors.append(f"{key}: 条件项必须是对象")
                    continue
                ind = str(item.get("indicator", "")).strip().lower()
                op = str(item.get("operator", "")).strip().lower()
                target_raw = str(item.get("target", "") or "").strip().lower()
                if not ind or not op:
                    errors.append(f"{key}: 条件缺少 indicator/operator")
                    continue
                if parse_indicator(ind) is None:
                    errors.append(
                        f"{key}: 未知 indicator={ind!r}（静态列或 ma20/volume_ma5 等派生列）"
                    )
                if op not in KNOWN_OPERATORS | EVENT_ONLY_OPERATORS:
                    errors.append(f"{key}: 未知 operator={op!r}")
                # target 若形如均线/列（MA20）归一化为列表达式，纯数字阈值则保留
                if parse_indicator(target_raw) is None and target_raw != "":
                    # 允许数值阈值，含小数/百分比字面量；其余保留原样交给回测层判读
                    if not re.fullmatch(r"\d+(\.\d+)?%?", target_raw):
                        errors.append(
                            f"{key}: target={target_raw!r} 需为数值或参照列（如 20 / ma20）"
                        )
                out.append(
                    Condition(
                        indicator=ind,
                        operator=op,
                        target=target_raw,
                        note=str(item.get("note", "") or ""),
                    )
                )
            return out

        buy = _conds("buy_conditions")
        sell = _conds("sell_conditions")

        pos = raw.get("position_control", 1.0)
        try:
            pos = float(pos)
            if not (0.0 < pos <= 1.0):
                errors.append(f"position_control 必须在 (0,1] 内，得到 {pos}")
        except (TypeError, ValueError):
            errors.append(f"position_control 无法解析: {raw.get('position_control')!r}")

        holding = raw.get("holding_days")
        if holding is not None:
            try:
                holding = int(holding)
                if holding <= 0:
                    errors.append(f"holding_days 必须为正整数，得到 {holding}")
            except (TypeError, ValueError):
                errors.append(f"holding_days 无法解析: {holding!r}")

        if not buy:
            errors.append("buy_conditions 不能为空（策略至少要有一条买入触发）")
        if holding is None and not sell:
            # 允许：无 sell 时回测层默认持有到期（SPRINT 定稿语义）
            pass

        if errors:
            raise StrategyValidationError(errors)

        return cls(
            strategy_name=str(raw.get("strategy_name") or "未命名策略"),
            stock_pool=str(raw.get("stock_pool") or "A股全市场"),
            timeframe=str(raw.get("timeframe") or "1d"),
            buy_conditions=buy,
            sell_conditions=sell,
            holding_days=holding,
            position_control=pos,
            max_positions=int(raw.get("max_positions") or 1),
            note=str(raw.get("note") or ""),
        )


def _cond_dict(c: Condition) -> dict[str, Any]:
    d = {"indicator": c.indicator, "operator": c.operator}
    if c.target:
        d["target"] = c.target
    if c.note:
        d["note"] = c.note
    return d


class StrategyValidationError(ValueError):
    """策略结构不合法，errors 为可读的错误列表。"""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors
