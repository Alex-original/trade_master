"""P1 冒烟测试：验证原版 TradingAgents 在 DeepSeek 后端下能初始化并运行。

用法（在 product/trade_master 下，.env 已含 DEEPSEEK_API_KEY）：
    .venv/bin/python scripts/smoke_engine.py              # 仅初始化 + 单次 LLM 调用（快）
    SMOKE_FULL=1 .venv/bin/python scripts/smoke_engine.py # 完整多智能体 propagate
    SMOKE_TICKER=600519.SS SMOKE_DATE=2026-08-15 ...      # 覆盖标的/日期
"""
import os
import time

# 5 档评级的中文标签（结构化输出枚举仍为英文，展示层映射为中文）
RATING_ZH = {
    "Buy": "买入",
    "Overweight": "增持",
    "Hold": "持有",
    "Underweight": "减持",
    "Sell": "卖出",
}


def main() -> None:
    ticker = os.environ.get("SMOKE_TICKER", "600519.SS")
    date = os.environ.get("SMOKE_DATE", "2026-08-15")
    full = os.environ.get("SMOKE_FULL") == "1"

    from tradingagents.default_config import DEFAULT_CONFIG

    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = "deepseek"
    config["deep_think_llm"] = "deepseek-v4-pro"
    config["quick_think_llm"] = "deepseek-v4-flash"
    config["max_tokens"] = 8000          # 推理模型需足额正文+推理 token 预算
    config["output_language"] = "Chinese"  # A股引擎默认中文输出（内部辩论仍英文保质量）

    print("== 1) 初始化 TradingAgentsGraph ==")
    t0 = time.time()
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    ta = TradingAgentsGraph(debug=False, config=config)
    print(f"   init OK ({time.time()-t0:.1f}s), provider={config['llm_provider']}")

    print("== 2) 单次 quick LLM 调用验证 key ==")
    t0 = time.time()
    resp = ta.quick_thinking_llm.invoke("Reply with exactly: OK")
    print(f"   LLM OK ({time.time()-t0:.1f}s): {resp.content[:80]!r}")

    if not full:
        print("   (SMOKE_FULL 未设，跳过完整 propagate)")
        return

    print(f"== 3) 完整 propagate({ticker}, {date}) ==")
    t0 = time.time()
    _, decision = ta.propagate(ticker, date)
    print(f"   propagate OK ({time.time()-t0:.1f}s)")
    print(f"   decision: {decision}  →  {RATING_ZH.get(decision, decision)}")


if __name__ == "__main__":
    main()
