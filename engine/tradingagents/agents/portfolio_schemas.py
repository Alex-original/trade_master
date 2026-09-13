"""组合级计划的结构化输出模型（Stage 2 组合决策层）。

三角色流水线：Portfolio Analyst（草稿）→ Risk Officer（风控意见）→ Portfolio
Manager（最终计划）。字段描述即模型的输出指令，供 bind_structured 使用。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

_NULLISH_FLOAT = {"", "none", "n/a", "na", "null", "nil", "-", "tbd", "unknown"}


def _coerce_optional_float(value):
    if isinstance(value, str) and value.strip().lower() in _NULLISH_FLOAT:
        return None
    return value


class ConditionalAction(BaseModel):
    """单只持仓/候选的条件化动作——次日行动计划的执行单元。"""

    code: str = Field(description="带交易所后缀的代码，如 518880.SH")
    name: str = Field(description="标的名称")
    action: Literal["buy", "add", "reduce", "sell", "hold"] = Field(
        description=(
            "相对当前持仓的净方向：buy=新建仓、add=加仓、reduce=减仓、"
            "sell=清仓、hold=持有不动。"
        )
    )
    target_weight: float | None = Field(
        default=None,
        description="目标市值占总资产比例（0~1，再平衡锚点）；hold 可不填。",
    )
    trigger_type: Literal["none", "open", "intraday", "price_below", "price_above"] = Field(
        default="none",
        description=(
            "触发条件：none=立即执行、open=开盘即执行、intraday=盘中任意、"
            "price_below=跌破 trigger_price 才执行、price_above=涨过 trigger_price 才执行。"
        ),
    )
    trigger_price: float | None = Field(
        default=None,
        description="price_below / price_above 时的阈值价。",
    )
    volume_ratio_min: float | None = Field(
        default=None,
        description=(
            "量能门槛：量比≥该值才算满足（None=不限）。与 trigger 同时成立才执行，"
            "用于表达「放量跌破」「缩量回踩」这类条件，如 1.5。"
        ),
    )
    reason: str = Field(description="一句话理由，说明为何这么调")

    @field_validator("target_weight", "trigger_price", "volume_ratio_min", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


_TIERS_HINT = (
    "同一只标的**可以给出多条动作行**来形成分档（梯子）：同一方向（全跌破或全涨过）、"
    "按触发价由浅到深排列（跌破档阈值递减 / 涨过档阈值递增）、越深的档越保守"
    "（减仓梯子的目标占比逐档不增）；每档填**该档触发后**要达到的绝对目标占比，"
    "不是增量。要表达「放量」就在该档填 volume_ratio_min。不需要分档时只给一条。"
)


class PortfolioPlanDraft(BaseModel):
    """组合分析师的草稿计划。"""

    summary: str = Field(description="组合层面一句话思路（收益/风险权衡）")
    actions: list[ConditionalAction] = Field(
        description=(
            "对每只持仓/候选的条件化动作（含现金分配思路体现在各 target_weight）。"
            + _TIERS_HINT
        )
    )


class RiskReview(BaseModel):
    """风控官的审查意见。"""

    verdict: Literal["ok", "adjust"] = Field(
        description="ok=计划合规可直接执行；adjust=存在违规需修正"
    )
    issues: list[str] = Field(description="违规项与理由（集中度/单票上限/现金缓冲/止损纪律）")
    adjustments: str = Field(description="对草稿的具体修正建议，供组合经理采纳")


class PortfolioPlan(BaseModel):
    """最终次日行动计划（落库 TrustPlan.plan_json）。"""

    summary: str = Field(description="组合层面一句话思路")
    cash_target: float | None = Field(default=None, description="目标现金占比（0~1）")
    actions: list[ConditionalAction] = Field(description="最终条件化动作列表。" + _TIERS_HINT)
    risk_notes: str = Field(description="风险说明：集中度/回撤/现金缓冲")

    @field_validator("cash_target", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


def render_portfolio_plan(plan: PortfolioPlan) -> str:
    """渲染最终计划为人类可读 markdown（用于报告/前端展示兜底）。

    注意：**当前没有任何调用方**（实际报告走 ``app/plan_report.py``）。保留它是为了让
    「计划 → 人话」有一个不依赖数据库渲染链路的实现。它逐条迭代 ``actions``，因此多档
    梯子会自然渲染成连续多行。
    """
    lines = [f"**Summary**: {plan.summary}"]
    if plan.cash_target is not None:
        lines.append(f"**Cash Target**: {plan.cash_target:.1%}")
    lines.append("**Actions**:")
    for a in plan.actions:
        trig = a.trigger_type
        trig_s = ""
        if trig != "none":
            trig_s = f"（{trig}" + (f" @ {a.trigger_price}" if a.trigger_price is not None else "")
            if a.volume_ratio_min is not None:
                trig_s += f" 且 量比≥{a.volume_ratio_min}"
            trig_s += "）"
        tw = f"{a.target_weight:.1%}" if a.target_weight is not None else "—"
        lines.append(
            f"- {a.code} {a.name}: {a.action} → 目标占比 {tw} {trig_s} — {a.reason}"
        )
    lines.append(f"**Risk Notes**: {plan.risk_notes}")
    return "\n".join(lines)
