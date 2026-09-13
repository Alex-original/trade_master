"""组合级多智能体图（Stage 2 组合决策层）：组合分析师 → 风控官 → 组合经理。

线性三节点、无迭代循环：每个节点读组合现状、产出结构化对象并写入 state。
最终 ``run()`` 返回最终计划 dict（``PortfolioPlan.model_dump()``），供应用层落库。
"""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from tradingagents.agents.portfolio_team import (
    create_portfolio_analyst,
    create_portfolio_manager,
    create_risk_officer,
)


class PortfolioPlanState(TypedDict, total=False):
    portfolio_context: str
    draft: dict
    risk_review: dict
    plan: dict


# 节点 → 进度文案（应用层据此在 web 端实时显示决策进展）
NODE_LABELS: dict[str, str] = {
    "portfolio_analyst": "组合分析师起草次日行动方案",
    "risk_officer": "风控官审查集中度 / 下行风险 / 可执行性",
    "portfolio_manager": "组合经理综合拍板最终计划",
}


def _with_progress(node_name: str, node_fn, on_progress):
    """把节点包一层进度上报：进入节点时回调 ``on_progress(node_name, 文案)``。"""
    if on_progress is None:
        return node_fn

    def _node(state):
        on_progress(node_name, NODE_LABELS.get(node_name, node_name))
        return node_fn(state)

    return _node


class PortfolioTeamGraph:
    """组合决策团队：把账户现状转成协调的次日条件化行动计划。

    三个角色**统一使用深思考模型**：组合分析师起草、风控官审查、组合经理拍板都直接
    决定真金白银的调仓，决策质量优先于成本；角色差异由各自的 prompt 与人设体现，
    而不再靠模型档位区分（此前分析师/风控官用快思考模型）。
    """

    def __init__(self, deep_thinking_llm):
        self._nodes = [
            ("portfolio_analyst", create_portfolio_analyst(deep_thinking_llm)),
            ("risk_officer", create_risk_officer(deep_thinking_llm)),
            ("portfolio_manager", create_portfolio_manager(deep_thinking_llm)),
        ]
        self.graph = self._build(None)

    def _build(self, on_progress):
        """编译三节点线性图。on_progress 非空时每个节点进入前上报一次进度。"""
        workflow = StateGraph(PortfolioPlanState)
        for name, fn in self._nodes:
            workflow.add_node(name, _with_progress(name, fn, on_progress))
        workflow.add_edge(START, "portfolio_analyst")
        workflow.add_edge("portfolio_analyst", "risk_officer")
        workflow.add_edge("risk_officer", "portfolio_manager")
        workflow.add_edge("portfolio_manager", END)
        return workflow.compile()

    def run(self, portfolio_context: str, on_progress=None) -> dict:
        """跑一次组合决策，返回完整过程（draft/risk_review/plan），供落库与报告展示。

        ``on_progress(node_name, label)`` 可选，用于向上层实时上报阶段进展。
        """
        graph = self.graph if on_progress is None else self._build(on_progress)
        state = graph.invoke(
            {
                "portfolio_context": portfolio_context,
                "draft": {},
                "risk_review": {},
                "plan": {},
            }
        )
        return {
            "draft": state.get("draft") or {},
            "risk_review": state.get("risk_review") or {},
            "plan": state.get("plan") or {},
        }
