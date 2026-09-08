# Trade Master 分析引擎（TradingAgents → A股适配）

以原版 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)（v0.4.0，Apache 2.0）为地基，改造为适配 A股市场的多智能体金融分析引擎。

**核心改动**（相对原版）：
1. **数据层全量替换 Wind（万得）**：`tradingagents/dataflows/wind.py` 新增 Wind MCP 客户端 + vendor 函数，`interface.py` 注册，`default_config.data_vendors=wind`，`load_ohlcv` 路由到 Wind，移除 yfinance 依赖。
2. **A股规则注入**：`get_china_market_instruction()`（涨跌停/T+1/ST/北向资金/人民币计价），注入市场与基本面分析师。
3. **中文输出**：`output_language=Chinese`（内部辩论仍英文保质量，对外报告中文）。

## 安装

```bash
cd product/trade_master
python3 -m venv .venv
.venv/bin/pip install -e engine/          # 引擎 + 依赖（langchain/langgraph/yfinance 等）
.venv/bin/pip install fastapi "uvicorn[standard]"   # FastAPI 服务（如需）
```

## 配置

- **DeepSeek**：`.env` 里 `DEEPSEEK_API_KEY=sk-...`（沿用 `OPENAI_API_KEY` 亦可）。
- **Wind**：`WIND_API_KEY=ak_...`，可放 `.env` 或 `~/.wind-aifinmarket/config`（`WIND_API_KEY=xxx` 或 JSON 两种格式均支持）。
- 可选覆盖：`DEEPSEEK_MODEL`（默认 `deepseek-v4-pro`）、`DEEPSEEK_QUICK_MODEL`（默认 `deepseek-v4-flash`）、`DEEPSEEK_MAX_TOKENS`（默认 8000，推理模型需足额）。

## 运行

### CLI 冒烟（快速验证）
```bash
cd product/trade_master
.venv/bin/python scripts/smoke_engine.py                          # 仅 init + 单次 LLM（快）
SMOKE_FULL=1 SMOKE_TICKER=600519.SS SMOKE_DATE=2026-08-15 \
  .venv/bin/python scripts/smoke_engine.py                        # 完整多智能体 propagate（~12min）
```

### FastAPI 服务
```bash
cd product/trade_master
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
# GET  /health             健康检查
# POST /analysis           {"ticker":"600519.SS","date":"2026-08-15"} → 决策
```

## 代码结构

```
engine/
  tradingagents/
    dataflows/wind.py         # 新增：Wind MCP 客户端 + vendor 函数（全量替换 yfinance）
    agents/utils/agent_utils.py  # 新增 get_china_market_instruction()
    agents/analysts/*.py      # 市场/基本面分析师注入 A股规则
    default_config.py         # data_vendors=wind
    dataflows/interface.py    # 注册 wind vendor
    dataflows/stockstats_utils.py  # load_ohlcv 路由到 Wind
  cli/  main.py               # 原版 CLI（保留）
```

详细改造方案见 [../改造方案.md](../改造方案.md)，进度与问题见 [../分析引擎-阶段报告.md](../分析引擎-阶段报告.md)。
