"""组合决策层三角色：组合分析师 → 风控官 → 组合经理（Stage 2）。

每个 factory 返回一个 LangGraph 节点函数 ``node(state) -> dict``，读写组合计划
state（portfolio_context / draft / risk_review / plan）。产出结构化对象用
``invoke_structured`` 拿回 Pydantic 对象（而非渲染 markdown），以便落库执行。
"""
from __future__ import annotations

from tradingagents.agents.portfolio_schemas import (
    PortfolioPlan,
    PortfolioPlanDraft,
    RiskReview,
)
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured,
)

# 团队人设：控制回撤、纪律优先、长期稳定增值（组合经理最终决策据此权衡）
_PM_PERSONA = (
    "你是一支专业的 A股/港股组合管理团队负责人，目标是在控制回撤、保持纪律的前提下，"
    "把客户资金经营到长期稳定增值。决策稳健、克制、有依据，不为显示方向而冒进，"
    "拿不准时就降仓或持有。"
)


def _analyst_prompt(portfolio_context: str) -> list[dict]:
    system = (
        "你是交易团队的组合分析师。基于账户现状与各标的的研究结论，起草一份协调的次日组合行动计划。"
        "要求：① 覆盖每只持仓（及你认为值得新建仓的候选）；② 每只给出条件化动作"
        "（buy/add/reduce/sell/hold）与目标占比 target_weight，并给出触发条件 trigger"
        "（如跌破某价才加仓、涨过某价才减仓、开盘即执行）；③ 各 target_weight 与目标现金"
        "加总须合理（不超 100%），体现组合层面的资金分配，而非逐标的孤立打分；"
        "④ 结合组合现状（现有占比、集中度、现金）给出有依据的动作；"
        "⑤ **分批止损/分批止盈要写成多条动作行（梯子）**，不要写进 reason 的散文里——"
        "同一只标的、同一方向（全跌破或全涨过）可以给出多行，按触发价由浅到深排列"
        "（跌破档阈值递减、涨过档阈值递增），每档填**该档触发后**要达到的**绝对**目标占比，"
        "且越深的档越保守（减仓梯子的目标占比逐档不增）。例：同一只票三行——"
        "跌破 8.90 减到 13.8%、跌破 8.76 再减到 9%、跌破 8.75 清仓（target_weight=0）。"
        "执行层会按由浅到深**逐档分批**推进（每个 tick 只走一档），所以档与档之间要有"
        "合理的价格间隔；需要「放量」这类量能确认，就在该档填 volume_ratio_min"
        "（量比门槛，如 1.5）。不需要分档时只给一条。"
        + NO_EXTERNAL_TOOLS
        + " 用中文。"
    )
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"账户与各标的现状：\n{portfolio_context}\n\n请给出次日组合行动计划（结构化输出）。",
        },
    ]


def _risk_prompt(portfolio_context: str, draft) -> list[dict]:
    draft_text = _draft_text(draft)
    system = (
        "你是交易团队的风控官。以账户现状「风控参数」里写的内容为准审查组合分析师的草稿："
        "① 若其中列出了用户显式设定的硬性风控（单票上限 / 止损比例 / 单日笔数），逐条检查草稿"
        "是否违反；② 若写明「未设定任何硬性限制」，则**不得自行发明仓位上限或止损线**——"
        "此时改从集中度、单一标的风险敞口、下行风险、条件动作是否真的可执行、"
        "以及计划与账户现状（现金、可用股数、占比）是否自相矛盾等角度提意见；"
        "③ **逐只核对分档梯子**（同一只标的多条动作行）：是否同向（全跌破或全涨过）、"
        "是否按触发价由浅到深、减仓梯子的目标占比是否逐档不增（加仓逐档不减）、"
        "清仓档的 target_weight 是否真为 0、量能门槛（volume_ratio_min）是否与理由相符、"
        "触发价之间是否留出可执行的间隔；④ **同一只票不得同时给出会互相打架的双向梯子**"
        "——减仓梯子的最高触发价必须低于加仓梯子的最低触发价，否则会在同一价位既买又卖、"
        "执行层来回对冲白付手续费。发现问题就给出具体的修正档位。"
        "逐条指出问题并给出具体修正（verdict=ok 表示合规，adjust 表示需修正）。"
        + NO_EXTERNAL_TOOLS
        + " 用中文。"
    )
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"账户现状：\n{portfolio_context}\n\n组合分析师草稿：\n{draft_text}\n\n请给出风控审查意见（结构化输出）。",
        },
    ]


def _pm_prompt(portfolio_context: str, draft, risk_review) -> list[dict]:
    draft_text = _draft_text(draft)
    risk_text = _risk_text(risk_review)
    system = (
        _PM_PERSONA
        + " 综合组合分析师的计划与风控官的意见，产出最终次日行动计划："
        "对每只持仓/候选给出最终条件化动作（code/name/action/target_weight/trigger/reason），"
        "并给出目标现金占比与风险说明。风控官指出的违规必须修正；证据不充分就保守降仓/持有。"
        "**保留草稿里合理的分档梯子，不要压平成一条**——把多档写进 reason 的散文里等于"
        "不会执行（执行层只认 actions 里的多条动作行，reason 是给人看的）。"
        "输出前自查每只票的梯子：同向、由浅到深、目标占比单调、清仓档为 0、"
        "双向梯子的触发价不重叠。"
        + NO_EXTERNAL_TOOLS
        + " 用中文。"
    )
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                f"账户现状：\n{portfolio_context}\n\n"
                f"组合分析师草稿：\n{draft_text}\n\n"
                f"风控官意见：\n{risk_text}\n\n"
                f"请给出最终次日行动计划（结构化输出）。"
            ),
        },
    ]


def _draft_text(draft) -> str:
    if not draft:
        return "（空）"
    if isinstance(draft, PortfolioPlanDraft):
        return draft.model_dump_json()
    try:
        return PortfolioPlanDraft.model_validate(draft).model_dump_json()
    except Exception:
        return str(draft)


def _risk_text(risk_review) -> str:
    if not risk_review:
        return "（空）"
    if isinstance(risk_review, RiskReview):
        return risk_review.model_dump_json()
    try:
        return RiskReview.model_validate(risk_review).model_dump_json()
    except Exception:
        return str(risk_review)


def create_portfolio_analyst(llm):
    structured_llm = bind_structured(llm, PortfolioPlanDraft, "Portfolio Analyst")

    def node(state: dict) -> dict:
        obj = invoke_structured(structured_llm, _analyst_prompt(state["portfolio_context"]), "Portfolio Analyst")
        return {"draft": obj.model_dump() if obj else {}}

    return node


def create_risk_officer(llm):
    structured_llm = bind_structured(llm, RiskReview, "Risk Officer")

    def node(state: dict) -> dict:
        obj = invoke_structured(
            structured_llm,
            _risk_prompt(state["portfolio_context"], state.get("draft", {})),
            "Risk Officer",
        )
        return {"risk_review": obj.model_dump() if obj else {}}

    return node


def create_portfolio_manager(llm):
    structured_llm = bind_structured(llm, PortfolioPlan, "Portfolio Manager")

    def node(state: dict) -> dict:
        obj = invoke_structured(
            structured_llm,
            _pm_prompt(state["portfolio_context"], state.get("draft", {}), state.get("risk_review", {})),
            "Portfolio Manager",
        )
        return {"plan": obj.model_dump() if obj else {}}

    return node
