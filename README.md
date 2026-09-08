# Trade Master（AlphaPilot）· 智能量化决策助手

面向 **A 股散户**的无代码量化决策工具：**自然语言 → 结构化规则积木 → 历史回测验证 → 模拟条件单与信号推送**。

> ⚠️ 本工具仅作「算法计算 + 规则提醒」，不构成投资建议、不托管资金、不下真实单。交易由用户在自己的券商 App 手动完成。

## 文档
- [阶段报告](阶段报告.md)：立项概述 / Sprint 规划 / 进度 / 问题集合（随开发更新）
- [需求稿](需求稿/)：`AlphaPilot` PRD v1.0（商业化版）与 v2.0（Solo Agent 架构师版）

## 快速开始（当前阶段）

Sprint 0 内核（策略模型 + NL 本地解析）**仅标准库，零依赖**：

```bash
cd product/trade_master
python scripts/demo_parse.py                          # 跑内置示例
python scripts/demo_parse.py "5日均线上穿20日均线"     # 或自定义一句
```

复制 `.env.example` → `.env` 配置 DeepSeek Key，为 Sprint 1 的 LLM 兜底解析做准备。

## 代码结构

```
app/
  config.py            # 环境变量 / 路径（DeepSeek 端点、行情缓存）
  strategy.py          # 策略领域模型（dataclass）+ 列表达式校验 + 确定性反序列化
  schemas.py           # 策略 JSON Schema（LLM 结构化输出 + 前端积木 + 回测对齐）
  nlparse/
    rules.py           # 本地规则匹配器（高频模板免 LLM）
    parser.py          # 编排：本地命中 → LLM 兜底 → miss
scripts/
  demo_parse.py        # CLI Demo
```

## 开发计划（对齐 PRD v2.0 三周节奏）

| Sprint | 内容 |
|---|---|
| 0（当前）| 策略模型 + JSON Schema + 本地规则解析内核（已跑通 demo） |
| 1 | 行情数据 AkShare + 缓存、Pandas 向量化回测（扣印花税/佣金/滑点）、LLM 兜底解析 |
| 2 | FastAPI + 原生前端：条件积木 + ECharts 曲线；条件单撮合 + WxPusher 推送 |
| 3 | 免责声明阻断流 + 免费额度/打码（Teaser）商业化 Demo + 部署上线 |

详见 [阶段报告](阶段报告.md)。
