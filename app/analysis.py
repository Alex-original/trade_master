"""深度分析引擎封装：把 TradingAgents 多智能体引擎接到 Trade Master。

提供 `run_analysis(ticker, date)`，内部构造 DeepSeek + Wind + 中文 的引擎，
运行多智能体流水线并返回结构化决策。
"""
from __future__ import annotations

import os

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# 5 档评级的中文标签（结构化输出枚举仍为英文，展示层映射为中文）
RATING_ZH = {
    "Buy": "买入",
    "Overweight": "增持",
    "Hold": "持有",
    "Underweight": "减持",
    "Sell": "卖出",
    "REVIEW": "待复核",
}


def build_engine_config() -> dict:
    """构造引擎配置：DeepSeek 后端 + 中文输出 + Wind 数据（已在 default_config 默认）。"""
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = "deepseek"
    config["deep_think_llm"] = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    config["quick_think_llm"] = os.getenv("DEEPSEEK_QUICK_MODEL", "deepseek-v4-flash")
    config["max_tokens"] = int(os.getenv("DEEPSEEK_MAX_TOKENS", "8000"))
    config["output_language"] = "Chinese"
    return config


def run_analysis(ticker: str, date: str) -> dict:
    """运行一次完整多智能体分析，返回决策 + 完整报告。

    注意：propagate() 同步阻塞约 10-15 分钟（多智能体多次 LLM 调用）。
    """
    config = build_engine_config()
    ta = TradingAgentsGraph(config=config)
    final_state, decision = ta.propagate(ticker, date)

    debate = final_state.get("investment_debate_state", {}) or {}
    risk = final_state.get("risk_debate_state", {}) or {}

    return {
        "ticker": ticker,
        "date": date,
        "decision": decision,
        "decision_zh": RATING_ZH.get(decision, decision),
        "report": {
            "market": final_state.get("market_report", ""),
            "sentiment": final_state.get("sentiment_report", ""),
            "news": final_state.get("news_report", ""),
            "fundamentals": final_state.get("fundamentals_report", ""),
            "bull": debate.get("bull_history", ""),
            "bear": debate.get("bear_history", ""),
            "research_manager": debate.get("judge_decision", ""),
            "trader": final_state.get("trader_investment_plan", ""),
            "aggressive": risk.get("aggressive_history", ""),
            "conservative": risk.get("conservative_history", ""),
            "neutral": risk.get("neutral_history", ""),
            "portfolio_manager": risk.get("judge_decision", ""),
        },
    }
