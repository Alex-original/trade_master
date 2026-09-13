# Trade Master · 模拟交易 + AI 托管

一个跑在浏览器里的 **A 股模拟交易 + AI 托管** 应用：把真实券商持仓同步进来，交给一支
多智能体投资团队（研究 → 风控 → 组合决策）每日出行动计划，按**价格阶梯**分批执行，
并且能用**历史回测**把同一支团队放到过去任意一段真实行情里重跑一遍，看它到底行不行。

> ⚠️ **仅供模拟与研究**：不构成投资建议，不托管资金，**不下真实单**。全部交易发生在应用内部的
> 影子账簿上，真实下单仍由用户在自己的券商 App 手动完成。

---

## 它长什么样

| 能力 | 说明 |
|---|---|
| **同步持仓** | 文本粘贴或**截图**（百炼 qwen3-vl）识别真实券商持仓，确认页全字段可编辑，识别出的代码会按名称向 Wind 回查校正 |
| **财团托管** | 多智能体团队：Stage1 逐票并行研究 → Stage2 组合决策（组合分析师 → 风控官 → 组合经理），产出**次日行动计划** |
| **多档触发** | 计划不是"买/卖"两个字，而是**价格阶梯**（跌破/突破 + 量能门槛），逐档分批推进，T+1 冻结与解冻按真实规则走 |
| **历史回测** | 取过去一段真实行情让同一支团队重跑：影子账户隔离、as-of 取数防未来函数、断点续跑、逐日成交与当日监控条件可见 |
| **归因 / 通知 / AI 对话** | 委托成交流水、已实现盈亏、持仓归因；通知配置；对话式问持仓与调仓 |

---

## 快速开始（本地）

```bash
cd product/trade_master

# 1) 配置（.env 不入库；模板里每一项都有注释）
cp .env.example .env
#    至少需要：DEEPSEEK_API_KEY（对话/解析/托管团队）
#    按需：WIND_API_KEY（行情）、DASHSCOPE_API_KEY（截图识别）

# 2) 依赖
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e engine/          # 分析引擎（tradingagents）

# 3) 起服务
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8010
#    或整栈：docker compose up -d --build
```

健康检查：`curl http://127.0.0.1:8010/health` → `{"status":"ok", ...}`

**数据库**是 PostgreSQL（`DATABASE_URL`），建表在启动时自动完成。
**没配短信时**登录验证码打印到服务端日志（开发兜底）。

---

## 部署

```bash
bash deploy/deploy.sh
```

`scp` 直传 + `docker compose up -d --build`（绕开 GitHub，国内服务器 git 不稳）。

几条**刻意的约束**，改脚本时别踩：

- **生产机地址不写死在仓库里**（本仓库公开）。脚本按 `环境变量 DEPLOY_HOST` →
  本机 `.env` 里的 `DEPLOY_HOST` 的顺序读取，都没有就报错退出，**不猜默认值**。
- **不覆盖服务器上的 `.env`**：那里放的是生产配置，脚本只在它不存在时报错提醒。
- **不传 `scripts/`、`docs/`**：只传 `app engine app_frontend Dockerfile docker-compose.yml
  requirements.txt .dockerignore`。测试与文档属于开发侧。
- SSH 私钥不入库（`.gitignore` 挡 `*.pem`）。

---

## 代码结构

```
app/                     后端（FastAPI + SQLAlchemy + PostgreSQL）
  main.py                路由与服务装配
  trust.py               托管：计划生成 / 多档触发执行 / 监控条件判据（_trigger_satisfied）
  backtest.py            回测驱动：逐日循环、子 tick、检查点续跑、影子账户
  backtest_data.py       as-of 取数 + 磁盘缓存（断网可跑）
  backtest_calc.py       净值/归因的纯算术 + 结果页声明
  analysis_service.py    Stage1 逐票研究 → Stage2 组合决策的编排
  plan_actions.py        动作归组、分档（build_ladders）、校验、截断
  ocr.py                 持仓/自选解析（文本 + 截图），含证券代码按名称回查校正
  account.py trade.py    账户与撮合（费用、印花税、T+1、涨跌停）
  market_clock.py        实时钟 / 历史钟（回测的确定性来源）
  nlparse/               本地规则解析（高频模板免 LLM）

engine/                  分析引擎（TradingAgents → A股适配，Wind 数据层）
  tradingagents/agents/portfolio_team.py   组合决策团队（三个节点）
  tradingagents/graph/portfolio_graph.py   编排
  tradingagents/asof.py                    as-of 作用域（防未来函数）

app_frontend/index.html  前端（单文件原生 JS，无构建步骤）
scripts/                 离线回归 + 回测验收 + 行情预热工具
docs/                    设计 / 测试报告 / 测试用例
deploy/                  部署脚本 + nginx 配置
```

---

## 测试

**全部离线**（不连 Wind、不跑 LLM、不连数据库），秒级出结果：

```bash
for s in scripts/smoke_*.py; do .venv/bin/python "$s" || echo "FAILED: $s"; done   # 后端 964 项
node scripts/check_frontend.js                                                     # 前端 176 项
```

> `scripts/smoke_engine.py` 是**联网** LLM 冒烟，不计入上面这 964 项。

**回测验收**（跑真回测之后做，需要数据库）：

```bash
.venv/bin/python scripts/verify_backtest_run.py --run-id N --owner-user-id 1   # 四表自洽 + 净值恒等式
.venv/bin/python scripts/score_backtest_run.py  --run-id N --owner-user-id 1   # 托管团队六维打分
```

**行情预热**（让回测彻底离线，不再依赖 Wind）：

```bash
.venv/bin/python scripts/warm_bars_cache.py --owner-user-id 1 --windows 2026-08-25:2026-09-11
.venv/bin/python scripts/verify_bars_cache.py    # 断网验收：0 次网络调用、0 条 warning
```

⚠️ 两个容易误解的缓存语义：缓存按 `(pad_start, eff_end)` **成对**落盘，所以
**预热一个"大窗口"不会命中它的子区间**（预热的窗口清单 = 之后能离线跑的窗口清单）；
`_PAD_DAYS` 与 `_CACHE_VERSION` **参与缓存键**，改任一个等于全部缓存作废。

---

## 文档

| 文档 | 内容 |
|---|---|
| [历史回测：设计与验证](docs/历史回测_设计与验证_v1.0.md) | as-of 取数、影子账户、逐日时序、撮合口径、断点续跑、已知限制 |
| [多档触发：设计与判定口径](docs/多档触发_设计与判定口径_v1.0.md) | 价格阶梯怎么分档、什么算触发、监控条件与撮合如何共用一套判据 |
| [测试报告](docs/测试报告_模拟交易AI托管_v1.0.md) | 逐轮缺陷与修复（含每个洞的根因与"通用问句"） |
| [测试用例](docs/测试用例_模拟交易AI托管_v0.1.md) | `TM-<模块>-NN` 编号用例，含回测多轮验证与账户级失败验收 |
| [阶段报告](阶段报告.md) · [需求稿](需求稿/) | 立项与需求 |

**分析引擎**对原版 TradingAgents 的改造见 [engine/README.md](engine/README.md)。

---

## 已实现 / 未做

**已实现**：登录（手机号 + 短信验证码）· 同步持仓（文本/截图，全字段可编辑）· 自选分组 ·
托管簿生命周期 · 策略设置 · 多档触发撮合（T+1、涨跌停、费用）· 逐日行动计划 ·
历史回测（as-of 取数、断点续跑、逐日成交与监控条件）· 归因看板 · 通知 · AI 对话 · 深度分析。

**未做 / 已知限制**：回测**首日结构性不可执行**（计划是"研究日日终产出、次日执行"，
首日没有前置研究日，见设计 §11.1）；托管簿现金恒 0、换仓式，买入依赖"先卖出产生现金"；
移动端底部 Tab 只放三个核心入口，其余为二级页。

---

## 致谢

分析引擎以 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
（v0.4.0，Apache 2.0）为地基改造为 A 股版本，遵循其许可；行情来自 Wind（万得），
截图识别来自阿里云百炼 qwen3-vl。
