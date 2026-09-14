# Trade Master · 模拟交易 + AI 托管 — 测试报告 v1.0

> 日期：2026-09-09 ｜ 依据用例：`docs/测试用例_模拟交易AI托管_v0.1.md`
> 执行方式：自动化脚本（HTTP 接口 + venv 内直调后端函数）+ 前端静态检查 + 真实浏览器问题回归（第二轮）。
> 结论：**75 条自动化用例全部通过**；真实浏览器使用暴露的 **5 项前端联调缺陷已修复**（第二轮 4 项 + 第三轮 1 项）；深度分析全链路真实跑单验证见 M10/结论；第四轮按用户需求增强 **AI 解析确认页**（明细全字段可编辑、券商可用资金纳入总资产实时汇总、托管现金加仓/减仓、空仓 50 万起步、首页与托管总览去双计）；第五轮按真实浏览器反馈修复 **4 项 UX**（「同步成功」原生 alert 弹窗改非阻塞轻提示、账户问答长回答截断、托管同步上传截图无反应、托管同步页新增「复制首页持仓」一键带入）；第六轮按用户逐条 9 项需求完成改造（**REQ-1~9**：托管总览操作范围可选并与策略设置联动、AI 助手卡片置于财团托管下方、模拟量化占位入口、个股详情悬停/触摸显示当日收盘价、详情页返回按钮、持仓占比进度条+百分比且分母=总资产、解析确认页 总资产/总市值/浮动盈亏 三栏可编辑（快照口径）、托管资产栏/持仓列表样式对齐首页、自选股多组命名+图片/文本解析整组同步+检索添加——REQ-7/9 本轮 HTTP 实测通过）；最终渲染仍建议浏览器人工确认。

---

## 一、测试概览

| 项 | 内容 |
|---|---|
| 服务 | `uvicorn app.main:app --host 0.0.0.0 --port 8010` |
| 数据库 | 本地 PostgreSQL `trade_master`（11 张表） |
| 行情 | Wind 可用（600519.SH=1309.3 / 00700.HK=435.4 等） |
| 大模型 | DeepSeek（文本解析/对话）+ 百炼 qwen3-vl-flash（截图 OCR） |
| 短信 | 开发模式（验证码打印日志，60s 频控） |
| 执行时间 | 约 1 小时（含深度分析异步提交） |

---

## 二、通过率汇总（修复后）

| 模块 | 用例数 | 通过 | 失败 | 说明 |
|---|---|---|---|---|
| M0 冒烟 | 2 | 2 | 0 | 服务健康/首页 |
| M1 登录注册 | 9 | 9 | 0 | 含建号默认值 DB 校验 |
| M2 同步持仓 | 11 | 11 | 0 | 文本/图片解析、清50万、覆盖、双簿 |
| M3 自选/个股 | 6 | 6 | 0 | 含港股/ETF 加自选（修复后） |
| M4 托管策略设置 | 4 | 4 | 0 | 保存/回读/部分字段/401 |
| M5 托管簿生命周期 | 7 | 7 | 0 | 建簿/开关/拦快照/reset/重建 |
| M6 撮合引擎 | 12 | 12 | 0 | 费用/最低5元/免五/印花/T+1/拦截 |
| M7 托管执行链路 | 9 | 9 | 0 | 评级→买卖/止损/次数上限/调度器 |
| M8 委托/成交/归因 | 4 | 4 | 0 | 流水/空态/analytics |
| M9 通知设置 | 5 | 5 | 0 | 配置读写/401 |
| M10 AI对话/意图/深度分析 | 7 | 7 | 0 | 问答/意图路由/异步提交/缓存 |
| **小计（自动化）** | **75** | **75** | **0** | ✅ |
| M11 前端回归（静态） | 9 | 9 | 0 | node 语法 + 结构 grep；动态交互待人工 |

---

## 三、发现的缺陷与修复（BUG 共 12 项：第一轮 5 + 第二轮 4 + 第三轮 1 + 第十三轮 1 + 第十六轮 1；另含第四轮 7 项需求增强 ENH、第五轮 4 项浏览器 UX 修复、第六轮 9 项改造 REQ-1~9、第七轮 6 项改造 R7-1~6、第八轮 2 项改造 R8-1~2、第九轮 2 项修复 R9-1~2、第十轮 1 项改版 R10-1、第十一轮 4 项改造 R11-1~4、第十二轮 3 项 R12-1~3、第十三轮 1 项 R13-1、第十六轮 5 项回测过程可见化 R16-1~5、第十七轮 3 项 R17-1~3、第十八轮 1 项 R18-1、第十九轮 2 项 R19-1~2）

### 第一轮（自动化用例发现，5 项）

| # | 级别 | 缺陷 | 根因 | 修复 | 回归 |
|---|---|---|---|---|---|
| BUG-1 | P0 | 港股/ETF 加自选报 500 | `get_company_name` 对非 A 股返回多行信息块（`证券简称`+行业明细+上市板），塞进 `varchar(50)` 的 `stock_name` → `StringDataRightTruncation` | `wind.py:get_company_name` 统一提取「证券简称」纯名称（含 `_fmt_table` 分支） | ✅ 00700.HK→腾讯控股、159779→招商中证消费电子主题ETF |
| BUG-2 | P1 | 风控参数（止损比例等）一旦设置无法清空 | `trust.update_trust_config` 用 `v is not None` 跳过 None；`main.py` 用 `exclude_none=True` 把 null 也过滤掉 | `main.py` 改 `exclude_unset=True`；`update_trust_config` 去掉 `v is not None`，None=显式清空 | ✅ 传 null 可清空止损参数 |
| BUG-3 | P1 | 同步持仓重复 code 触发唯一键冲突 500 | `sync_positions`/`snapshot_trust_book` 未对 rows 按 code 去重，OCR 偶发产出重复标的 | 两处循环加 `seen` 集合去重 | ✅ 重复 code 只落库 1 条 |
| BUG-4 | P2 | 无效代码下单抛 `VendorRejectedError`（engine 层），与其余 `ServiceError` 不一致 | `place_order` 补名阶段 `get_company_name` 直接抛 Wind 异常 | 补名包 try，失败回退用代码作名称，统一由 `get_latest_price`→None→`ServiceError` 收口 | ✅ 无效代码抛 ServiceError |
| BUG-5 | P2 | 文本持仓解析偶发返回空 + `confidence=low`（DeepSeek 输出不确定） | 模型单次调用偶发低置信 | `ocr.parse_text` 对空+low 结果自动重试一次 | ✅ 重跑稳定通过 |

### 第二轮（真实浏览器使用暴露，4 项前端联调缺陷）

> 来源：用户浏览器实测「同步持仓→首页我的持仓」「左侧栏 AI 对话助手」「查海底捞股价/分析海底捞市场」四条路径。
> 共性根因：此前仅做 HTTP/后端验证，**前端动态绑定与渲染链路是盲区**（上轮 M11 已标注"动态交互待人工确认"）。

| # | 级别 | 缺陷 | 根因 | 修复 | 回归（HTTP 端到端） |
|---|---|---|---|---|---|
| BUG-6 | P0 | 同步持仓后交易首页「我的持仓」仍是写死的演示股，当日盈亏 `--`，总资产错 | ①首页持仓表格/卡片从未被 JS 填充，是静态演示行；②后端 `get_positions` 从不计算当日盈亏（只返回浮动 pnl）；③持仓视图 summary 把「总市值」误填进「总资产」槽 | ①`account.py` 新增 `get_quote`（一次 Wind 返回现价+昨收），`get_positions` 每项加 `prev_close`/`day_pnl`/`day_pnl_pct`，`get_account` 汇总 `mirror/trust.day_pnl`；②前端删演示行，新增 `renderHomeHoldings` 用 `/api/positions` 渲染首页桌面表格+移动卡片，`loadPositions` 末尾调用；③`loadAccount` 修 KPI：总资产=现金+市值合计、当日盈亏=镜像+托管 day_pnl 求和并着色，`vals[0]`=镜像簿总资产、`vals[3]`=镜像簿当日盈亏 | ✅ 6 只 ETF 均返回 `prev_close`+`day_pnl`（如 518880 黄金ETF -684.0 / 159779 消费电子ETF +610.3）；`/api/account` `mirror.day_pnl=-169.6`、`total_assets=340353.90` |
| BUG-7 | P1 | 左侧栏「AI 对话助手」按钮点击无响应 | `.ai-cta`（`data-view="chat"`）不在导航点击绑定选择器 `.nav-item, .mobile-tab` 内 | 绑定选择器扩为 `.nav-item, .mobile-tab, .ai-cta` | ✅ 静态确认：`<div class="ai-cta" data-view="chat">` 已绑定 → `switchView('chat')` |
| BUG-8 | P1 | 聊天问「查询海底捞今日股价」答"无行情数据，请去交易软件查看" | 发送按钮只调 `/api/chat/account`（账户问答，无行情数据） | 新增统一分流端点 `POST /api/chat/ask`；`intent.py` mode 扩 `analyze/quote/ask`（纯报价→quote）；`chat.py` 新增 `market_quote`（Wind 现价/昨收/涨跌幅）；前端按 `kind=quote` 渲染「海底捞（06862.HK）现价 10.45 · 昨收 11.38 · 较昨收 -8.17%」 | ✅ `{"kind":"quote","quote":{"code":"06862.HK","name":"海底捞","price":10.45,"prev_close":11.38,"change_pct":-0.0817}}` |
| BUG-9 | P1 | 聊天问「分析一下今天海底捞的市场」无法完成（预期 trade_agent 调研报告） | 深度分析链路（`/chat` 意图路由 → `/analysis` 异步 trade_agent）后端已就绪，但聊天发送按钮从未调用；异步提交+轮询逻辑只挂在个股详情页 | 前端抽离 `runDeepAnalysis`（提交 `/analysis` → 10s 轮询 → 组装多段报告文本，复用 decision_zh 评级），`/api/chat/ask` 返回 `kind=analysis` 时调用；`goAnalyze()` 同步复用；首次真实对话清空页面演示气泡 | ✅ `/api/chat/ask`「分析一下今天海底捞的市场」→ `{"kind":"analysis","ticker":"6862.HK","name":"海底捞","date":"2026-09-08"}`，前端据此触发 `/analysis` 异步执行（10-15min） |

### 第三轮（浏览器实测财团托管，1 项前端联调缺陷）

> 来源：用户浏览器实测「财团托管」页。与前两轮同源——前端动态渲染盲区。

| # | 级别 | 缺陷 | 根因 | 修复 | 回归 |
|---|---|---|---|---|---|
| BUG-10 | P0 | 财团托管页「无粘贴持仓功能」，一直显示「AI 托管未开启」，无法开启托管 | `loadTrust()` 里 `if (!data.book_created) bannerRight.innerHTML='需先粘贴…'`：当托管簿未建立（每个新账号初始态）**把「粘贴持仓快照」按钮整段清空**成一行死提示 → 永远无法粘贴建簿；而 `toggle_trust` 要求先 `book_created` → 死循环「未开启」 | `loadTrust()` 改为右侧操作区整段幂等重建：**粘贴持仓快照按钮 + 刷新流水始终保留**，前置提示随状态切换（未建簿→「尚未建立托管簿 → 粘贴持仓快照」/ 已建簿未开启→「托管簿已建立 → 到策略设置开启」）；banner 状态文案区分三态；删除被遮蔽的重复 `openSync()`；sync Sheet 标题/确认按钮随目标簿切换（托管=「建立托管簿 · 粘贴持仓快照」） | ✅ HTTP 全链路（scratch 账号）：`/api/trust/snapshot` → `book_created=True`；`/api/trust/toggle{active:true}` → `is_active=True`（验证后已 reset 清理）；JS `node --check` 通过；served HTML 已含修复；浏览器点开「托管总览」→「粘贴持仓快照」→ 粘贴 → 确认建簿 →「策略设置」开启 |

### 第四轮（按需求增强：确认页可编辑 + 实时汇总 + 券商现金纳入总资产 + 托管现金加仓）

> 来源：用户需求「AI 解析确认页需显示总资产/总市值/浮动盈亏，字段全可编辑」，并明确「券商现金纳入总资产」「财团托管全面代替个人：用现有现金加仓减仓」「保证所有数据与粘贴持仓一致」。经确认的账户模型：**首页镜像簿=真实账户只读展示（手动重贴刷新、用于与托管成绩比对）；托管簿=从同一份粘贴起步、托管全自动交易的另一本独立账（独立流水/盈亏）**；托管无持仓时默认 50 万资金起步。
> 影响面：同步真实持仓与粘贴托管快照共用同一 AI 解析确认 Sheet；镜像/托管各带现金，首页与托管总览口径去双计。

| # | 类型 | 改动 | 说明 | 回归 |
|---|---|---|---|---|
| ENH-1 | 后端 | 新增 `GET /api/quote/{code}` → `account.resolve_quote` | 6 位或带后缀代码一次 Wind 拉取返回 `{code, price, prev_close}`；代码非法/无行情时 `price:null`（前端回退成本价），不抛错 | ✅ `600519→600519.SH 1292.87`、`159779→159779.SZ 1.435`、`06862.HK→10.36`、`999999→{price:null}`；`py_compile` 通过 |
| ENH-2 | 前端 | AI 解析确认行全部可编辑（`se-grid` 五行输入：名称/代码/股数/成本价/现价），逐行实时计算市值与浮动盈亏并着色 | 现价初值=成本价占位，随后按代码 `fetchQuote` 拉实时行情覆盖；**手改过现价后不再被行情覆盖**；改代码失焦自动重取实时价；✕ 删除该行不纳入同步；缺代码/股数≤0 的行同步时自动跳过 | ✅ `node --check` 通过；served HTML 含 `.se-row/.se-summary/renderSyncRows/recalcAll/collectSyncRows/fetchQuote` |
| ENH-3 | 前端 | 确认页顶部 `.se-summary` 三态汇总：**总资产 / 总市值 / 浮动盈亏** | 随任意字段输入实时重算（`recalcAll`）；正负浮动盈亏红绿着色；确认按钮统一收集编辑后行 POST `/api/trust/snapshot`（托管）或 `/api/positions/sync`（镜像） | ✅ 绑定核查：`sync-confirm-btn → collectSyncRows → 按 syncTarget 选端点`，成功后清空列表并 `refreshAll` |
| ENH-4 | 后端 | 解析尽力识别**可用资金**：文本(DeepSeek)/截图(百炼) schema 加可选 `available_cash`（仅认"可用资金/资金余额/可用金额"，不拿总资产/总市值冒充；无则 0） | `ocr.py` `_POSITION_SYSTEM`/`_VISION_PROMPT`/`_normalize` | ✅ 文本含"可用资金：123456.78"→ `available_cash:123456.78` 且持仓行不变 |
| ENH-5 | 前后端 | 确认页新增**「券商可用资金」输入框**（AI 预填、可编辑、`input` 即重算）；**总资产 = 可用资金 + 持仓市值**（实时，现金注释随输入刷新）；确认一并提交 `cash` | 落库：`/api/positions/sync`、`/api/trust/snapshot` 接收 `cash` → 镜像簿 `Account.available_cash`（首同步清 50 万种子）/ 托管簿 `TrustConfig.available_cash`。托管由此**用真实现金加仓**、卖出现金回流（`place_order` 原生支持） | ✅ HTTP：镜像同步带 cash=123456.78 → `mirror.cash=123456.78`、`total_assets=现金+市值`；二次同步换现金=200000 生效；托管粘贴带 cash → 托管簿 `available_cash=123456.78`、总资产=现金+市值（266107.28 校验一致） |
| ENH-6 | 引擎 | **托管空仓起步**：无持仓但现金>0 且选池为自选/全市场时，按在管标的最新评级 Buy/Overweight 加权**自由建仓**（Buy=2/Overweight=1）；无评级候选则 `no_buy_rating` 等下一次 | `trust.run_execution` 移除"无持仓直接跳过"，新增空仓候选构建；`DEFAULT_TRUST_CASH=50 万`；前端托管总览「空仓启动(50万)」按钮 `startEmptyTrust()` POST 空快照 | ✅ 直调：空仓50万+全市场+评级 Buy(600519)/Overweight(159779) → 实际建仓 2 笔（200股茅台 ≈25.8万、56300份ETF ≈8.1万），现金 161,238.03+市值 342,951.30 一致，reason 标"建仓"；无候选路径返回 `no_buy_rating` 不崩 |
| ENH-7 | 前端 | **口径去双计**：首页 KPI/持仓视图=镜像簿（真实账户，含现金），不再镜像+托管重复相加；未同步真实持仓前不显示 50 万种子，提示先同步。**托管总览接托管簿真数据**：总资产=现金+市值、浮动盈亏、持仓明细（实时盈亏红绿）+空仓启动入口；首页托管条随托管簿资产实时显示 | `loadAccount`/`loadPositions` 改镜像口径；新增 `renderTrustOverview`/`renderTrustPositions`/`startEmptyTrust` | ✅ served HTML 含 `se-cash`/`startEmptyTrust`/`trust-assets`/`trust-pos-list`；托管粘贴建簿后 `renderTrustOverview` 显示现金+市值+明细；两表独立、互不覆盖 |

### 第五轮（真实浏览器反馈，4 项 UX 修复）

> 来源：用户浏览器实测 4 条反馈：①「页面总是提示同步成功（触发什么环节？）」②「AI 对话分析今日持仓，信息没显示全」③「财团托管同步页上传截图无反应」④「托管同步页加一个复制首页持仓的按钮」。均为纯前端交互/配置层问题，后端协议已就绪。

| # | 级别 | 缺陷 / 需求 | 根因 | 修复 | 回归 |
|---|---|---|---|---|---|
| UX-A | P1 | 「同步成功」用**原生 alert** 弹窗：阻塞操作、观感突兀；且确认后先 `await refreshAll()`（逐标的 Wind 行情 + 多接口，秒级）**再**弹 → 提示迟到、像"页面自己弹出来" | `toast(msg){alert(msg)}` 全站阻塞弹窗；`sync-confirm-btn` 把 toast 放在慢刷新之后 | ①`toast` 改为**非阻塞轻提示**：`.toast-wrap` 顶部居中、2.6s 自动消失、最多叠 5 条，替换全站所有 `alert`；②确认成功**先关面板 + 立即轻提示**（含 `N 只 · 含现金 ¥…`），`refreshAll()` 后台执行不阻塞；③提交中禁用按钮置「提交中…」，防连点重复建簿 | ✅ `alert(` 全库无残留；served HTML 含 `toast-wrap`；确认按钮含禁用态；同步/托管确认两条文案带数量与现金 |
| UX-B | P1 | 账户问答（分析今日持仓）长回答被截断、只显示一部分 | `chat._llm_text` 默认 `max_tokens=2000`，逐只分析时被输出上限截断 | `answer_account_question` 改传 `max_tokens=8000`（DeepSeek 思考型模型输出长度要求）；system prompt 追加「逐只完整列出、不要省略/截断」 | ✅ 真实 `/api/chat/ask`（已登录 user2）：「分析今日持仓逐只给看法与建议」→ 返回结构化完整分析，覆盖全部 6 只（其中 2 只因行情缺失标注说明），以正常结语收尾不再断句；`py_compile` 通过 |
| UX-C | P1 | 财团托管同步页**上传截图无反应**（同一张图点了没反应 / 截图模式无视觉反馈 / 托管页点「AI 智能解析」把 base64 当文本解析） | ①`#sync-file.value` 从不重置 → 之前选过的同文件再次选择不触发 `change`；②`#mode-image` 点击不切换激活高亮、无"识别中"反馈；③解析按钮对 `syncTarget==='trust'` 强制按 `text` 走 | ①新增 `pendingImageB64` 记录已选图，读文件后**立即清空 `input.value`** → 同一张图可重复选择触发识别；②`setSyncMode` 让「上传截图」pill 高亮，解析/识别中按钮置灰 + `.parse-hint` 转圈文案；③解析按钮按 `pendingImageB64` 走**图**识别、否则走**文本**（不再按同步目标硬编码） | ✅ `node --check` 通过；served HTML 含 `pendingImageB64`/`setSyncMode`/`parse-hint`；模式切换/清空逻辑静态核对无残留旧分支 |
| UX-D | P2 | 托管同步页缺快捷建簿入口：从已同步首页持仓复制 | 无此功能 | 同步 Sheet 新增「↩ 复制首页持仓」按钮（仅 `syncTarget==='trust'` 显示）：`copyHomeToTrust()` 并行拉 `/api/positions`（镜像 6 字段）+ `/api/account`（镜像现金）→ 填入可编辑确认行 + `#se-cash` + 逐行拉实时行情 | ✅ 真实接口：`/api/positions` 返回 6 只（`stock_code/hold_qty/cost_price` 齐）、`/api/account` `mirror.cash=127636.64`；served HTML 含 `copy-home-btn`/`copyHomeToTrust`；镜像空仓时按钮给引导提示不报错 |

### 第六轮（用户逐条 9 项改造：托管范围联动 / 布局占位 / 图表提示 / 快照口径可编辑 / 自选分组）

> 来源：用户 9 条需求（编号 REQ-1~9）。REQ-1~6、REQ-8 为纯前端交互/布局/图表改造，REQ-7 为前后端「快照口径可编辑」，REQ-9 为自选股「多组 + 图片/文本解析整组同步 + 检索添加」新功能（后端+前端全量）。

| # | 需求（用户原话要点） | 实现 | 回归 |
|---|---|---|---|
| REQ-1 | 托管总览→操作股票范围不可选，状态未随策略设置同步 | 托管总览策略卡内新增三态胶囊 `#trust-scope-pills`（持仓股/自选股/全市场）→ `setTrustScope(v)`；与「策略设置」页 `selectScope` 共用同一函数，即时 `PUT /api/trust/config {stock_scope}` 持久化，并同步高亮两处（`loadTrust` 用 `cfg.stock_scope` 回读 setRadioGroup + 胶囊 active），DB `stock_scope` 单源 | ✅ 接线静态确认：总览胶囊与设置行 onclick 均收敛到 `setTrustScope`，`loadTrust` 回读高亮两处；served HTML 含 `#trust-scope-pills`/`selectScope`/`setTrustScope`（该字段读写端点 M4 既有轮次 HTTP 通过） |
| REQ-2 | 左侧「AI 对话助手」卡片排在「财团托管」下方 | 侧栏结构调整：`财团托管(nav) → ai-cta(data-view=chat)` 紧随其后 | ✅ 静态确认：DOM 顺序 1551（财团托管）→ 1554（ai-cta），同属侧栏 |
| REQ-3 | 左侧再留「模拟量化」入口占位（内容可为空） | 侧栏新增 `nav-item data-view=quant`；点开落到空壳视图，标题文案占位，不接任何逻辑 | ✅ served HTML 含 `data-view="quant"`；`switchView('quant')` 映射到首页移动 Tab 高亮不报错 |
| REQ-4 | 个股详情：鼠标/触摸移到某日期显示该日具体收盘价 | 个股详情 K 线改用 ECharts candlestick，`tooltip.trigger:'axis'` + `axisPointer.type:'cross'`，`formatter: stockTip` 随光标/触摸所在日显示：**收盘价**（红涨绿跌加粗）+ 开盘/最高/最低/成交量；`confine:true` 防溢出 | ✅ 接线静态确认：`stockTip` 读 `kline[dataIndex]` 取当日 close 并按 close≥open 着色；ECharts tooltip 天然响应 hover 与触摸 |
| REQ-5 | 个股详情页无返回按钮 | 详情页头部加 `back-btn onclick=goBack()`；`lastTopView` 记录最近顶级页（首页/托管…），`switchView` 只在其为顶级页时更新返回目标，详情等二级页保留来源 | ✅ `goBack → switchView(lastTopView)`；served HTML 含 back-btn；静态核对无 404/空跳转 |
| REQ-6 | 持仓列表仓位占比：进度条 + 百分比（分母=总资产） | 后端 `account.get_positions` 每项新增 `position_ratio`（市值/总市值）+ `assets_ratio`（市值/**总资产** = 现金+市值，用户指定分母）；前端首页持仓表/卡片新增 `position-bar/position-fill` 进度条 + 「占资产 xx.xx%」文本，桌面表列为独立「仓位占比」列（宽度 56px），移动卡片内联小条（40px）；`assets_ratio` 缺失时回退 `position_ratio` | ✅ 接线静态确认：`ratioBar/ratioText` 优先 `assets_ratio`（总资产分母）；DB/HTTP 复核：`get_positions` 计算式 = mv/(cash+mv)；`node --check` 通过 |
| REQ-7 | 解析持仓确认页：总资产栏、总市值、浮动盈亏需可编辑 | 后端：`accounts`/`trust_configs` 增 `broker_mv/broker_pnl` 快照口径列；`sync_positions`/`snapshot_trust_book` 增 `mv/pnl/reset_snapshot`，`get_account`/`_cfg_to_dict` 返回 `snapshot_overridden` 并优先快照口径。前端确认页 `.se-ov-*` 三输入：编辑「总资产」→ `mv=总资产−可用资金`(≥0) 落库、「总市值/浮动盈亏」直接落库；任一手改 → 总览以快照为权威口径、行级仍实时；「↺ 还原自动」`reset_snapshot:true` 清快照回实时计算 | ✅ 本轮真实 HTTP（scratch 账号）：镜像同步 mv=170000/pnl=20000/cash=120000 → `snapshot_overridden:true`、`total_assets=290000`(现金+快照市值)、pnl=20000；`/api/account` 复核 overridden:true；再传 `reset_snapshot:true` → overridden:false、口径回落；托管快照 mv=340000/pnl=40000 → `broker_mv/pnl` 正确写入，`reset_snapshot` 还原为 null；`py_compile`+`node --check` 通过 |
| REQ-8 | 托管页资产栏与持仓列表风格改与首页一致 | 托管总览/明细复用首页同款 `summary-cell`/`holding-row`/`stock-tag`/红绿 `num` 体系与卡片结构，两表独立不互相覆盖 | ✅ served HTML：`holding-row`/`summary-cell` 统一复用（33 处），`renderTrustOverview`/`renderTrustPositions` 与 `loadPositions` 并行渲染互不干扰 |
| REQ-9 | 左侧新增「自选股」；可编辑添加分组；粘贴/截图解析同步一组（一次一组）；可新建多组命名；可检索添加 | 后端：新增 `watchlist_groups` 表（空组可持久）+ `watchlist.group_name` 分组列（唯一键放开为 user+group+code）；`watchlist.py` 全量重写分组 CRUD（建/改名/删/激活）、当前激活组（`watchlist_meta`）、整组替换同步（code 缺失用 Wind 名称尽力反查，失败计 skipped）、检索（Wind 名称 NL + 本地代码归一兜底）；`ocr.py` 新增 `parse_watchlist`（文本 DeepSeek/截图百炼，忽略现价涨跌幅列）；`main.py` 8 条自选路由。前端：自选视图重写为 分组胶囊条（含计数/激活高亮/分组管理/新建/重命名/删除）+ 检索输入（350ms 防抖下拉候选点选/回车直接加 6 位代码）+「粘贴/截图同步自选」面板（文本或上传截图→AI 解析出候选 chips 可删→确认后**替换当前激活分组**，同步按钮实时指向当前组）；仅当前激活组拉行情控 Wind 调用 | ✅ 本轮真实 HTTP（scratch 账号）：建组 白马/科技 ✅、重名/空名拦截 ✅；切激活组后加自选落当前组（0981.HK→00981.HK 归一）✅；改名 科技→半导体 ✅；删当前激活组 白马 → 自动回退「默认」且组内 3 行清除 ✅；仅剩 1 组拒绝删除（「至少保留一个分组」）✅；`sync` 整组替换 4 条（含重复招商银行）→ added=3、去重、code 归一 600519.SH/000858.SZ/600036.SH ✅；文本解析（DeepSeek）4 行 code+name 全对、现价/汇总行被剔除 ✅。**Wind 名称检索/名称反查/实时行情曾因 key 配置指向低配额账号报「单日请求次数超限」而降级，已切回原 key 修复**；复测：NL 检索「海底捞」→6862.HK、行业词「白酒」→8 只候选、纯名称 sync（五粮液/中国平安）added=2/skipped=0、自选行价回填（600519.SH=1290.88）✅ |

### 第七轮（用户逐条 6 项交互改造：重命名提示时序 / 解析动画 / 自选行情列 / 自选分组限定 / 归因资产一致 / 搜索框不清空）

> 来源：用户 6 条需求（编号 R7-1~6）。R7-3、R7-4 涉及后端（自选行情字段 + 托管「仅自选某组」作用域），其余为前端交互时序/动画/一致性修复。

| # | 需求（用户原话要点） | 实现 | 回归 |
|---|---|---|---|
| R7-1 | 自选分组重命名提示「已生效」过早，实际生效要等列表刷新完才看到；预期生效后再提示 | 前端 `wlSaveInline`：提交时先禁用保存按钮防连点 → 调用 API → `wlCloseInline()` → **`await loadWatchlist()`（chips/列表真正切到新组）之后才 toast**「已新建/已重命名」；异常进 catch 提示，`finally` 恢复按钮 | ✅ 静态接线：toast 移到 `loadWatchlist()` 之后、保存中 `disabled`；`node --check` 通过 |
| R7-2 | 自选→粘贴/上传截图→解析→同步，过程无引导/动画，像卡死 | 解析面板新增可见转圈状态行 `#wl-parse-busy`（spinner 复用 `tspin` keyframe + 阶段文案）；`wlSetParseBusy` 在 AI 识别/整组同步期间显示转圈并禁用「AI 解析/上传截图」；选图后立刻提示「正在读取截图…」再编码（覆盖读取耗时），解析与同步两阶段都有引导 | ✅ served HTML 含 `#wl-parse-busy`/`.wl-parse-busy .spin`；`wlRunParse`/`wlSyncParse`/文件 change 三处均驱动 busy 态；`node --check` 通过 |
| R7-3 | 自选数据列表格式参考持仓列表（无成本）：显示 股票代码/最新价/涨跌幅/涨跌价格；分钟级行情回来即更新 | 后端 `watchlist.list_groups` 每项改用 `account.get_quote`（一次 Wind）补齐 `change/change_pct`；前端 `renderWlList` 改持仓样式卡片：tag+名称+**代码**｜**最新价** + **涨跌幅/涨跌额**（红涨绿跌，平盘灰）｜「移出」；新增 60s `setInterval`：仅当自选视图 active 时静默重拉 `/api/watchlist` 更新列表（用户在分组管理/内联编辑时跳过 chips 重绘、不打断输入） | ✅ HTTP（scratch）：600519.SH price=1290.88 / change=-18.42 / change_pct=-1.41 三字段齐；`node --check` 通过 |
| R7-4 | 策略设置→操作股票范围→选「仅限自选股」时可进一步选某一分组（多组时） | 后端：`trust_configs` 增 `stock_scope_group` 列 + `_ensure_schema` 迁移；`get_managed_symbols` scope=1 且指定组存在时**只取该组**自选、组被删→回退全部分组（不让托管池意外清空）；`_cfg_to_dict`/`TrustConfigRequest` 暴露该字段；新增 `GET /api/watchlist/groups`（`watchlist.groups_meta` 纯名册，不含明细）。前端：「仅限自选股」行下新增分组下拉 `#cfg-scope-group`（`#cfg-scope-group-row` 容器）——scope≠1 隐藏、scope=1 显示；改动即 `PUT` 持久化（含组名；空=全部自选）；`fillTrustConfig→syncScopeGroupOptions` 回读回填（组已删则保留并标注「已删除」，不静默回退）；`buildConfigBody` 带上 `stock_scope_group` | ✅ HTTP + 函数级（scratch）：PUT scope=1/group=白马 持久化且 cfg 返回 白马 ✅；`get_managed_symbols`：组=白马→仅 000858.SZ、组=默认→仅 600519.SH、组=不存在→回退 2 只全量 ✅；PUT scope=2 自动清 group 为 null ✅；GET `/api/watchlist/groups` 返回名册+激活组 ✅（测试用「白马」组已清理） |
| R7-5 | 归因看板「托管总资产」与托管总览不一致；预期一致 | 根因=前端摘要滞后：切子 Tab/进托管页从不重拉数据，不同托管动作刷新子集不同（服务端 `/api/trust`、`/api/trust/analytics`、`/api/account` 本就同源一致）。修复：新增 `trustPanelLoader` + `loadCurrentTrustPanel` ——**进入托管视图时按当前子 Tab 刷新**、**每次切换子 Tab 刷新对应面板**（总览→loadTrust、归因→loadAnalytics、委托成交→loadOrders+loadTrades、策略→loadTrust+loadNotifyConfig），确保归因看板总资产与总览同源同刻 | ✅ HTTP（scratch）：三来源 trust `total_assets` 均为 **308176.0** 一致；接线静态确认（switchView('trust') 与 `.trust-tab` click 均触发）+ `node --check` 通过 |
| R7-6 | 检索添加自选：输入 600519→候选 贵州茅台→点候选后输入框被清空（预期不清空） | 前端 `addStock` 成功后不再 `value=''`：仅收起候选、保留输入框内容并 `focus()`（便于连续添加/核对），重载列表后 toast | ✅ 静态接线：`addStock` 移除清空改为保留+聚焦；`wlResolveAdd`/`pickSuggest` 路径共用；`node --check` 通过 |

### 第八轮（用户发现 Wind 后台 get_stock_kline 高频调用 ~80 次/17min → 行情快照化 + 短缓存）

> 来源：用户查看 Wind 后台，17:25–17:42 内 `get_stock_kline` 调用约 80 次，疑异常。

| # | 现象 | 根因 | 实现 | 回归 |
|---|---|---|---|---|
| R8-1 | Wind 后台 `get_stock_kline` 短时高频（~80 次） | 「实时行情」实现成**逐只拉 30 天日 K**（`get_wind_ohlcv` 只为取最近 2 根）+ **无缓存**：`watchlist.list_groups` 每只自选发 1 次、`account.get_positions` 每只持仓发 1 次、AI 报价/撮合取价亦同；真实账号 24 自选 + 12 持仓 → 一次页面刷新即几十次 K 线调用（60s 自选轮询再把量放大），非循环/后台任务（`engine_runs` 15:18 后无落库、调度时段外，服务 17:37 重启前的旧实例日志已被覆盖） | Wind 侧存在**批量时点快照接口** `get_stock_price_indicators`（多只逗号分隔、返回 最新成交价/前收盘价 等，非 K 线、一次覆盖 N 只）→ `wind.py` 新增 `get_price_snapshots(codes)`；`account.py` `get_quote` 改走快照 + 进程内 **60s TTL 缓存**，新增批量 `get_quotes`（未命中合并成一次快照补齐）；`get_positions`/`list_groups` 改**一次批量取全组行情**；坏码被 Wind 回填内部 ID → 解析按「代码必须命中请求列表且成交价非空」过滤，单只失效不影响整批 | ✅ 调用计数（真实 user2，monkeypatch `_call_tool`）：24 只自选冷读 = **1 次快照**（原 24 次 K 线）、6 只持仓 = 1 次、热缓存二次读取 = **0 次** |
| R8-2 | 行情口径切换有无回归 | 快照「最新成交价」收盘后 = 日 K 最新收盘；前收两路一致 | `place_order` 内 `get_latest_price`（→快照+缓存）顺带受益；涨跌停判断 `_check_limit` 仍用日 K（仅在真实下单触发，低频，不动） | ✅ HTTP（scratch，重启后）：600519.SH price=1290.88 / change=-18.42 / change_pct=-1.41 与改前**完全一致**；`py_compile`、`import app.main` 干净、服务重启 health 200 |

### 第九轮（用户实测发现 2 项前端/口径缺陷：聊天输入框回车不发送 / 持仓诊断总资产口径重复）

> 来源：用户实测。「AI 对话框键入回车不发送」「选择'持仓诊断'提示『总资产 93.91 万与现金+股票市值 46.96 万不一致，差额约 46.96 万』」。

| # | 现象 | 根因 | 实现 | 回归 |
|---|---|---|---|---|
| R9-1 | AI 对话框键入回车不发送 | 发送逻辑只绑在 `.send-btn` 的 click（`index.html` 旧匿名 handler），`.chat-input .input-field` 无 Enter `keydown` 监听 | 把点击发送抽成公共 `async function sendChatMessage()`；`.send-btn` click 与其 `.input-field` Enter（`preventDefault` + 发送）都调它 | ✅ `node --check` 内联 JS 通过；served HTML 含 `sendChatMessage()` 与 keydown 绑定，`input-field` 全局唯一（聊天视图独占，无串扰） |
| R9-2 | 「持仓诊断」提示『总资产 93.91 万 ≠ 现金+股票市值 46.96 万，差额 46.96 万』（对账告警） | `chat.account_context` 唯一把**双簿求和**当「总资产」注入（`acc['total_assets']` = 镜像簿 + 托管簿）；而托管簿起始仓由真实持仓复制建立时两簿完全同源（user2：同 6 只持仓、同现金 12.76 万），模型核对「现金+市值=46.96 万」后发现与 93.91 万对不上（差额恰为另一簿 46.96 万）；前端首页 KPI 早已只取镜像簿、托管簿独立展示不重复相加（`index.html` loadAccount 注释），仅 chat 上下文仍在求和 | `account_context` 改为**以【真实账户（镜像簿）】为总资产口径**，托管簿单列并附口径说明（起始仓复制自真实持仓→同源两视图、**不得相加**；空仓 50 万独立建仓→才独立于真实账户），持仓段落标题亦标注「真实账户 / AI 托管簿持仓（不与镜像簿合并）」 | ✅ 真实 `/api/chat/ask`（user2 token）发「持仓诊断」：开篇即按「两簿同源、不做相加，总资产 469,563.84 元」对账，诊断表格现金+市值+总资产自洽，**不再出现『不一致/差额约』告警**；`py_compile` 干净、服务重启 health 200 |

### 第十轮（按 UI 稿重做登录页 R10-1）

> 来源：用户提供登录页 UI 稿（`/Users/wjx/Downloads/ai-trading-login/index.html`）要求以此稿作登录页。已确认取向：配色=**固定浅色稿**（不随 app 深/浅主题切换）；第三方登录（微信/Apple/短信）与 +86 区号=**直接去掉**；协议勾选=**加门禁**（未勾选登录按钮禁用）。

| # | 现象 | 根因 | 实现 | 回归 |
|---|---|---|---|---|
| R10-1 | 旧登录页为居中单卡片深色卡片，与交付 UI 稿（双栏浅色：左品牌区 + 右登录卡）不符 | 旧 `.login-*` 样式/结构直接套 app 主题变量 | `app_frontend/index.html` 登录视图整体换成**双栏固定浅色稿**：登录页作用域局部变量 `--login-*`（浅色板）写死为浅色、不读 `data-theme`，故恒浅色且不影响 app 主题；左栏 `brand-panel`（品牌 logo「AI模拟交易」+ 大标题「AI 驱动，让交易更从容」+ 副标题 + 3 条特性），右栏 `login-card`（欢迎登录 / 手机号 / 验证码 + 发码 / **协议勾选行** / 登录按钮）。**去掉**微信/Apple/第三方按钮与 +86 区号。协议门禁：`.agree-row` 点击翻转复选框 `.checked`，`checkLoginForm` 要求 `11 位手机号 && 6 位码 && 已勾协议` 才点亮登录按钮，handler 再加 `if(!loginAgreed)` 防护。四个 id（`login-phone/login-code/login-send-btn/login-submit-btn`）原样保留，真实发码/登录 API 不变。侧栏 logo 类名改 `login-brand-logo/login-brand-name`，避开与主界面 `.brand-logo/.brand-name` 冲突（后者需保持 36px） | ✅ `node --check`（内联 JS 无语法错）；served HTML 含 `login-agree`/四 id、无 `social`/区号残留；**CDP 无头浏览器交互**（真实 8010 页面）：无 token→login-view 显示、双栏渲染（品牌区 560px / 卡片 460px 白底 / 标题「欢迎登录」/ 占位符与稿一致）、初始登录按钮 disabled、填 11 位手机号+6 位码未勾协议**仍禁用**、点勾选→`.checked` 生效且按钮 enabled、取消勾选→恢复 disabled；**隔离回归**：`html[data-theme]` 仍 dark、body 深底 rgb(18,18,18) 不被登录层浅色污染、主界面侧栏 `.brand-logo` 仍 36px（改名未串扰）；`hideLogin()/showLogin()` 正常切换。注：伪造 token 注入 → init `hideLogin` 后 `refreshAll` 401 → 全局 api 自动 `clearToken+showLogin`（既有安全行为，符合预期）；真实登录态终检建议浏览器人工走一遍验证码 |

### 第十一轮（组合级交易团队改造 R11-1~4）

> 来源：用户认为托管「评级→清仓/减半/加半仓」太粗糙，要求交易引擎像**真实团队**一样结合整个账户现状（持仓/现金/成本/占比），产出**协调的次日条件化行动计划**（如「跌破X加仓到Y%、涨过Z减仓到W%」），执行层开盘后按计划机械再平衡。已确认取向：①组合级多智能体图；②昨夜计划+机械执行；③人设+显式风控（style/单票上限/止损真正接入）。

| # | 现象 | 根因 | 实现 | 回归 |
|---|---|---|---|---|
| R11-1 | 逐标的深析彼此隔离、看不见组合（每票只出 5 档评级） | 引擎单标的、`user_id` 不进引擎、`AgentState` 无持仓/现金字段 | 研究层注入**组合上下文**：`AgentState` 增 `portfolio_context`、`create_initial_state`/`propagate`/`_run_graph` 透传、`run_analysis(ticker,date,portfolio_context)` 增参；单标的 PM / Trader / Research Manager 把 `state["portfolio_context"]` 插进 prompt，使研究结论变成组合视角；`analysis_service.build_portfolio_context` 组装「全仓+现金+每票成本/现价/占比/盈亏」 | ✅ `py_compile` 全量通过；`build_portfolio_context` 单测（monkeypatch）确认含总资产/现金/持仓/占比 |
| R11-2 | 无组合级计划，跨标的资金分配无法协调 | 引擎逐标的、彼此隔离 | 新增**组合级多智能体图**（`portfolio_schemas.py`/`portfolio_team.py`/`portfolio_graph.py`）：组合分析师→风控官→组合经理三节点线性 LangGraph，读「账户快照 + 各票研究摘要 + 显式风控」，产出结构化 `PortfolioPlan`（`actions[]`：code/action/target_weight/trigger_type/trigger_price/reason + cash_target + risk_notes）；`structured.py` 增 `invoke_structured` 返回原始对象；`analysis.run_portfolio_plan` + `analysis_service.build_portfolio_plan_context/run_portfolio_plan_for_user` 编排并落库 `TrustPlan`（含完整决策过程 draft/risk_review/portfolio_context） | ✅ **真实端到端**（合成组合上下文，3 次 LLM）：产出 6 条条件化动作，正确识别黄金+白银集中 36%、主动不追高、清仓无研究小仓、消费电子「涨过 1.49 才减」、现金目标升 30%，并主动指出数据口径不自洽 |
| R11-3 | 执行层硬编码「Sell清仓/Underweight减半/Buy加仓权重2」 | `run_execution` 只读评级映射动作 | 重写为**机械再平衡**：`_execute_plan` 按触发门控（none/open/intraday/price_below/price_above）→ 目标占比换股数 → 算 delta 买卖；止损最高优先、单票上限截断、现金协调（按 delta 降序）；`_execute_legacy` 保留作无计划回落；`style_risk_band`（style→仓位带）+ `next_trading_day` 入 `app/risk.py`；`_sched_plan` 15:05 追加 Stage2 | ✅ 执行层单测（monkeypatch 行情）5 场景全过：减仓/加仓正确股数、触发不满足不下单、止损覆盖计划、单票 0.3 截断、现金不足只买得起的部分 |
| R11-4 | 无手动生成入口、计划不可下载 | 仅 15:05 定时生成、只存最终计划 | `POST /api/trust/plan/generate`（异步 task_id + 轮询）+ `GET /api/trust/plan/download`（自包含 HTML：行动决策总览 + 三角色决策过程 + 各标的 Stage1 深度研究）；前端「生成次日行动计划」「下载报告」按钮 + `generateTrustPlan/pollPlanGen/downloadTrustPlan`；`_get_latest_plan`（展示用最新）与 `_get_today_plan`（执行用今日）分离，盘中生成的「明天」计划不会被今天剩余 tick 提前执行 | ✅ 异步机制单测（提交/去重/轮询/done）；报告渲染单测（三块齐全）；决策过程落库单测（draft/risk_review/portfolio_context 完整持久化）；服务重启 health 200 |

### 第十二轮（2 项修复 + 阿里云部署 R12-1~3）

| # | 现象 | 根因 | 实现 | 回归 |
|---|---|---|---|---|
| R12-1 | 托管「次日行动计划」web 端预览表格丑 | `.table-row` 默认 6 列网格、计划表只有 5 列导致错位；「持有」动作无标签样式（裸标签） | 计划表改专用 `.plan-table` 5 列网格；补 `.tag.hold` 灰标签（买入红/卖出绿/持有灰，与下载报告一致）；顶部加「思路 + 目标现金占比」摘要行；理由列独立样式自动换行 | ✅ `node --check` 通过；served HTML 含 `plan-table`/`tag.hold` 标记 |
| R12-2 | 次日行动计划偶发「现价全部为空」仍继续生成 | Wind 行情源瞬时失败时 `get_quotes` 返回空 → `build_portfolio_context` 写「现价 N/A」，流程无校验继续生成 | 新增 `trust.validate_portfolio_data`：生成计划前校验每只持仓现价可得，缺失即**停止并提示**；`POST /api/trust/plan/generate` 同步校验（缺价直接返回错误）+ `_plan_worker` 异步内二次校验（置任务 error） | ✅ 单测：有价→放行、无价→拦截并列出「黄金ETF（518880.SH）现价缺失」等、空仓→放行；`py_compile` 通过 |
| R12-3 | 仅本地单机，无法两端（桌面/手机）公网访问 | 无生产部署 | 阿里云 ECS `<生产机>`（与 video-note 同机隔离）部署：Dockerfile（python:3.12 + `pip install -e engine/` + 清华 pip 镜像）+ docker-compose（app:8010 只绑回环 + postgres:16 + data 卷）+ nginx 反代 + certbot HTTPS（`trade.videotonote.cn`）+ 时区 `Asia/Shanghai`（防调度错位）+ 每日 pg_dump 备份 cron；生产 `.env` 只读放服务器、本地密钥临时文件已清理 | ✅ 公网 `https://trade.videotonote.cn/health` 200、首页 200、HTTP 301 跳 HTTPS；容器 `trade-master-app/db` 均 Up、14 表建全、备份试跑 32K；证书自动续期 |

### 第十三轮（回测静默空转：计划层失败被吞，R13-1）

| # | 现象 | 根因 | 实现 | 回归 |
|---|---|---|---|---|
| R13-1 | 回测跑得完、报 `done`、结果页正常，**实际全程零成交**（线上 run 1：Stage1 研究 23/23 成功、评级 `{Hold:22, Overweight:1}`、报告 33.7–45.0 KB 齐全，但影子账户 `TrustPlan=0`、`Order/Trade=0/0`） | `analysis_service.run_portfolio_plan_for_user` 在**结构化输出解析失败**时 `return None` **而不抛**；`backtest._drive` 把返回值丢掉了 → `TrustPlan` 不落库 → 次日执行层无计划可依 → 又一天零成交，而 `step` 照样标 `ok`。**实盘入口对同一情形本来就是 `raise ServiceError` 的**，所以这个洞只在回测侧、只在回测侧静默。底层成因：DeepSeek 全系是 thinking 模型、拒 `tool_choice`，schema 只能当**工具**发出去、模型**可以不调**——实测与并发/限流强相关（回测里 23 路并行必现，单发同上下文 8/8 成功）。已逐个证伪：机制断了（否，4/4 OK）、`json_mode` 能修（否，4/4 HTTP 400）、上下文超长（否，真实仅 4330/11557 字符）、`max_tokens` 截断（未复现） | ①`app/backtest.py::_drive` **必须看返回值**：空计划 = 失败日，`raise ServiceError` 由既有 `except` 标 `degraded`、计入 `consecutive_errors`，连错 `_MAX_CONSECUTIVE_ERRORS`(=3) 天中止整轮——**不新增失败通道**，复用既有"单日抖动只记 degraded、连错才中止"口径。②`invoke_structured` 失败**重试一次**再放弃（再失败即系统性问题，应让调用方看见） | ✅ `scripts/smoke_backtest_calc.py` 第 10 节新增「丁」段 **16 条**（两种坏返回值 `None`/空 `actions` 各 8 条）：落 `failed`、连错 3 天即中止而非跑满 5 天、每天 `degraded`、原因写明、中止后**后两天连执行层都没进**、检查点与实库一致；**反例**（计划层给得出方案 ⇒ 全 5 天 `ok`/`done`）由同节甲段守着——新判据不能把正常路径也判成失败。全量离线回归 12 脚本 0 失败 + 前端 122 项 |

> **这是同一形状的第三个洞**（前两个见 §六 的 M8 镜像断链与 Stage2 候选池）：**每个环节各自"看起来对"，只是两环之间少了一次核对**。前两个断在"读的不是同一份数据"，这一个断在"计划层说我没产出，而编排层没听"。三次都不违反任何单点断言——所以离线断言的正确写法是**断"该报的错报了"**，而不是断"跑完了"。

### 第十四轮（新增：回测流程报告·按计划生效日查看，R14-1）

用户要求：「查看回测中的流程报告，结构和财团托管→次日行动报告的格式一样，但因为多日期，需要能够查看不同日期的报告」。属于**新增功能**，非缺陷。

| # | 项 | 口径 | 实现 | 回归 |
|---|---|---|---|---|
| R14-1 | 回测流程报告：与实盘「次日行动报告」同一份 HTML，按计划生效日切换 | "格式一样"只能由**同一个渲染器**保证，不能两边各画一遍版式 | 后端 `backtest.list_plan_reports` / `backtest.get_plan_report` + `GET /api/backtest/{run_id}/reports` 与 `/report?date=&inline=`；正文直接调实盘 `plan_report.render_plan_report`。三条要害：① 账户快照取 `trade_date < D` 的**最后一个** `BacktestStep`（研究日收盘的账），不是回测结束时的账——否则 09-09 那份计划会显示"0 持仓 + 38 条建仓动作"；② 无冻结研究快照时**不回落实时查库**（影子 `EngineRun` 跨研究日累积，兜底会取到更晚的日期 = 未来函数），宁可如实报"没有研究依据"；③ "有计划、没 step" = 该日未执行，`executed=False` 一路传到前端点名。渲染器只加了一个**纯叠加**的 `banner` 参数，默认空串 ⇒ 实盘输出逐字不变 | ✅ 新增 `scripts/smoke_backtest_report.py` **37 条**（日期清单/不串号、快照取哪一天、`assets_ratio` 补齐、研究缺口不回落实时查库、越权、横幅纯叠加）；`scripts/check_frontend.js` 第 11 节 **19 条**（日期下拉、未执行点名、blob revoke、轮询不重建 iframe）。全量离线回归 **619 项** 0 失败 |

> 顺带修了 `check_frontend.js` 的一处**桩在骗人**：DOM 桩缺 `removeChild`，于是「下载」那条路径
> （`appendChild(a)` → `click()` → `removeChild(a)` → `revokeObjectURL`）在 revoke **之前**就抛异常，
> 而异常被 `toast` 吞掉——"临时 blob 用完即 revoke"这条断言于是永远测不出来。真实浏览器里是好的。
> 桩缺方法造成的假绿，比断言写少一条更危险。

### 第十五轮（回测已跑时长上界面，R15-1）

用户提问：「这次回测到底跑了多久？」——一个**界面上根本查不到**的数。库里 `BacktestRun` 有
`created_at` / `finished_at` 两个时间戳，前端一个都没渲染；用户只能去翻库，而这恰恰是决定
「要不要再来一次」的那个数。属于**信息缺口**，非缺陷。

| # | 项 | 口径 | 实现 | 回归 |
|---|---|---|---|---|
| R15-1 | 回测已跑时长（在跑 / 跑完）显示在进度条与列表 | **跑完取 `finished_at`，在跑取"现在"**；措辞与取数处的**预估**共用 `_human_duration`（"约 2.3 小时"），预估和实际必须是同一套说法，否则用户对不起来 | 后端 `backtest._elapsed_seconds`（单一取数点）+ `elapsed_text`；`get_progress` 加 `elapsed_seconds`/`elapsed_text`，`list_runs` 每行加 `elapsed_text`。前端进度计数由「6 / 15 个交易日」变为「6 / 15 个交易日 · 已跑 约 12 分钟」（终态改口「用时」），列表「进度」列加一行同款小字。三条要害：① **在跑不能取 `updated_at`**——那是心跳时间，一个卡住十分钟的 run 会把时长冻在十分钟前，界面看着像"没动"，实际还在跑；拿墙上时钟算，UI 每轮询一次就往前跳一格。② **拿不到就不显示**（`created_at=None` 的老记录）——整段省略，绝不写 `0`，沿用本项目"宁可 `None` 也不要一个看起来像结论的 0"的既定立场。③ 终态判定 `BT_TERMINAL` 收成**一处**，进度条（禁用取消）与列表（"用时"/"已跑"）共用，将来加终态不会漏掉一边 | ✅ `scripts/smoke_backtest_calc.py` 第 11 节新增 **7 条**（跑完 = 差值且给人话、在跑按现在算、老记录给不出、时钟回拨夹到 0、两个接口都带上字段）；`scripts/check_frontend.js` 第 7 节新增 **5 条**（进度计数含时长、终态改口"用时"、无耗时整段省略且不出现 `0`/`undefined`、列表两种语气齐全、老记录不渲染耗时行）。全量离线回归实测 **后端 837 项 + 前端 146 项 = 983 项，0 失败**（12 个 `smoke_*.py` 逐个跑，`smoke_engine.py` 是联网 LLM 冒烟、不计入断言数） |

> 断言里对"在跑"那条特意**不喂死时间戳**去算，而是断言"按现在算的结果必然远大于一个固定值"——
> 否则断言会随运行时刻漂移，今天绿明天红。

### 第十六轮（回测过程可见化 + 计划不被运行痕迹清掉，R16-1~5）

用户三条诉求：①「9.7-9.8 的报告去哪了」；②「我要看到回测过程中的**成交记录**以及每天的
**监控条件**」；③ Wind 余额不足 → 换 key、预缓存近一月行情，之后要**多轮跑回测**。

第二轮的问题**不是一个前端 bug，是一次静默的数据删除**。三个缺陷 + 两个信息缺口：

| # | 项 | 口径 | 实现 | 回归 |
|---|---|---|---|---|
| R16-1 | **计划不是"运行痕迹"**：影子账户重置不许删计划 | 09-07 属设计（首日无前置研究日），09-08 是**被删的**。影子账户被**刻意跨 run 复用**（`ensure_shadow_user`：`EngineRun` 研究缓存是最贵的成本项），所以「共享影子」不是 bug；既然如此"run B 开跑按运行痕迹清一遍"就必然抹掉 run A 唯一的历史记录 | `trust._clear_book_runtime(session, user_id, *, plans: bool = True)` 加**显式关键字**：真实账簿走默认 `True`（重置托管**必须**清计划，否则监控卡片展示已作废的旧计划 —— docstring 里记着的老坑），影子路径 `plans=False`；`delete_run` 改为按 run 窗口**显式**删本 run 的计划。跨 run 串味改由 R16-3 的窗口收口，不靠删行 | ✅ `scripts/smoke_backtest_isolation.py` 第 6 节：先给影子名下落一份计划，再调 `reset_shadow_book`，断言「★ 计划**留着**」「委托/成交/持仓照旧清干净（计划是唯一被豁免的那一样）」，同时真实用户的委托/成交/计划/持仓/自选股分毫不动 |
| R16-2 | **执行日缺计划要响亮失败**（读侧） | `_run_execution` 无计划时返回 `skipped="no_plan_backtest"`，而 `_drive` 只取 `res.get("trades")` —— `skipped` 被丢掉，于是「一天什么都没做」与「计划正常但没触发」在库里**长得一模一样**（R13-1 那个静默空转的读侧孪生，上次只修了生成侧） | `_drive` 子 tick 循环后认 `skipped`：`no_plan_backtest` 且 **`i > 0`** 时按 `degraded` 计错。**首日豁免写进注释**——首日无计划是设计 | ✅ `smoke_backtest_calc.py`：缺计划日 → `degraded`；**首日仍为 `ok`**（豁免的回归，新判据不能把正常路径也判成失败） |
| R16-3 | **报告清单以"跑过的交易日"为骨架** + 按 run 窗口收口 | 原先只列有 `TrustPlan` 行的日期 ⇒ 缺计划的日子**从列表里消失**（用户看到的就是"报告页少了两天"，而不是"这两天没有报告"）；且只按 `shadow_user_id` 过滤 ⇒ 影子共用时混入**别的 run 的日期**。关键区分：「没计划」和「有计划但一档没触发」是**两回事**，前者说明团队那天根本不可能做任何事 | `list_plan_reports` 以 `BacktestStep` 为骨架左连接计划，每条给 `has_plan` / `plan_missing_reason`（`first_day` = 首日无前置研究日属设计；`missing` = 非首日却真没有 = 环断了）；计划查询按 run `[start_date, end_date]` 收口，`get_plan_report` 同样收口。前端据此把无计划的日子也列出来并标注原因，而不是整块卡片退化成空态 | ✅ `scripts/smoke_backtest_report.py`（45 项）：清单日期集合 == step 日期集合；`first_day` 与 `missing` **必须分得开**；`check_frontend.js` 断言无计划日期渲染成对应选项，选中它时**不发请求、隐藏 iframe、禁用下载**，改选有计划的日期后按钮恢复 |
| R16-4 | **逐日成交明细**（后端早有，前端从未接线） | 「看不到成交记录」的全部原因：`GET /api/backtest/{run_id}/trades` **早就存在且可用**，**前端一次都没调用过**。回测里 `traded_at` 是**模拟时间**（`place_order(ts=…)`），复用现有时间格式化即可，不会显示成回测跑的真实钟点 | 新增逐日成交卡片，放在 `#bt-report` 卡片**之外**——理由：`#bt-report` 在没有计划日期时整块隐藏，而成交与报告无关，放里面就会在"缺计划"这个**正是本轮 bug 的场景**下一起消失。三处（分析页/托管页/回测页）已各写一遍的行渲染抽成 `tradeRowEl(t)` 共用 | ✅ `check_frontend.js`：`tradeRowEl` 三处共用不回归（买/卖标签、金额、理由为空时不出现空的「理由：」） |
| R16-5 | **每日监控条件**（执行口径） | 原 `get_monitor_conditions` **没有注入缝**：硬编码最新计划 + 实时快照，且调 `_trigger_satisfied` **不传 `now`**；而在 `asof_scope` 内 `get_price_snapshots` 直接返回 `{}`，照搬会得到"每档价格 None、全不触发"的空卡片。**口径是这一节的核心**：卡片必须与撮合**同一个判据**——卖/减看当日**最低**、买/建看当日**最高**。用"跑到此刻的价"或收盘价判，会出现**卡片说没触发、账上却成交了**的自相矛盾，那这张卡片就没有存在的理由（它唯一的理由就是解释当天的成交） | 加三个**默认 None = 今天行为逐字节不变**的注入参数（`plan_row`/`bars`/`now`），新增 `backtest.get_day_monitor_conditions` + `GET /api/backtest/{run_id}/monitor?date=`。触发判据**仍走** `trust._trigger_satisfied`/`_pick_tier` 不另写一份（`market_clock.py` 明令）。逐档取价：`exit`→`low`、`entry`→`high`、`hold`→`close`，并把开/高/低/收四价一并回传让卡片把判据写在脸上。加 `(run_id, date)` 的 60s 进程内 TTL 缓存（23 只票解析约百毫秒级）。**无计划日如实说没有** + 一次网都不打，绝不返回"价格全空、一档不触发"的假卡片——那看起来像"团队看了但没动"，事实是这天团队根本没有可执行的东西 | ✅ `scripts/smoke_backtest_monitor.py`（22 项，本次新增）专钉四件事：**执行口径**（卖档看 low、买档看 high，用收盘价判会得出相反结论的那一行就是判别力的来源）、**量比算不出时带 `volume_ratio_min` 的档必须不触发**（宁可不动也不退化）、**无计划日 0 次网络调用**（替身是一调用就炸的那个）、**时间闸门走注入的模拟时间而不是真实墙钟** |

> 修复的实测影响**不是"少一份报告"，是"吞掉一整天的执行"**：同一窗口 2026-09-07~09-11、
> 同参数、空仓起步，修前 09-08 无计划 ⇒ 那天零成交、整轮 **1 笔**；修后 09-08 有计划 ⇒
> **3 笔**。所以「报告少一天」和「回测少一笔」是同一件事的两面。直接证据：修前 `trust_plans`
> 的 id 从 5 跳到 8（中间 6/7 被消耗而无行），修后第 1 天日终出现 `TrustPlan date=2026-09-08`。

> ⚠️ **顺带定下一条排序约束（写脚本时踩到）**：`plans=False` **只保住计划，委托与成交两表
> 仍然会被后起的 run 清掉**。所以「`step.trades_json` 与 `trades` 表逐笔相等」这条判据
> **只对影子账户上最新的那个 run 有效** —— 要验就先把这一轮验完，再起下一轮。
> `verify_backtest_run.py` 的 A 条已按此加护栏（检测到更晚的 run 就跳过并明确提示，
> 而不是报一个假的失败）。

**全量离线回归**：后端 **884 项**（13 个 `smoke_*.py`，`smoke_engine.py` 是联网 LLM 冒烟、
不计入）+ 前端 **168 项** = **1052 项，0 失败**。

### 第十七轮（复制持仓的复权口径：一只票凭空少掉七成市值，R17-1~3）

第十六轮修完、C1/C2/C6 三轮真回测跑完之后，**在结果里读出了三个新缺陷**。
其中 R17-1 是本项目到目前为止**代价最大**的一个：它不报错、不违例、结果页上完全看不出来，
只是**让一只票凭空少掉 71.8% 的市值**。

| # | 项 | 发现过程 | 根因 | 修法 / 处置 |
|---|---|---|---|---|
| R17-1 ★ ★ | **`copy` 起步的持仓成本价与行情不同口径** | C6（run 4）的成交明细里有一条刺眼的记录：起跑**次日**就把 **159300.SZ 沪深300ETF富国**全部清仓，`卖 10400 股 → 14414.40 元`；而这只票在真实托管簿里市值 **51043 元**。回测凭空少掉 **36629 元（−71.8%）**，且这笔钱当场变成现金，污染其后每一天 | 回测全程走后复权（§3.2 的设计），而 `copy` 把真实簿的 `cost_price`（**不复权**盘面价）原样搬进影子 ⇒ **同一个持仓行里 `cost` 与 `price` 不同口径**。该票复权因子 `f = 0.279`：不复权收盘 **4.9080**（= 真实盘面、= 真实成本价的口径），后复权收盘 **1.3690**（= 回测 bar）。于是止损判据 `bar.low < cost×(1−pct)` 变成 `1.379 < 5.031×0.92` —— **恒为真**，任何一天都触发。同一处失真还波及三处：`init_basis` 成了混合口径（430876.14，真实 464273.74）、`calc.replay_realized` 的加权平均成本初值错、该持仓**权重被低估 3.585 倍** | 新增 `backtest._rescale_copied_book`：取起始日 `f = 后复权收盘/不复权收盘`，**数量 `Q_h = Q_r/f`**（起始市值不变）、**成本 `C_h = f·C_r`**（浮亏比例不变），止损判据化为 `f·low_r < f·C_r·(1−pct)`，**与不复权口径完全等价**。换算后才调 `_basis` 算 `init_basis`。三个取舍：`|f−1| < 1e-3` 的票**原样不动**（本例 6 只里只有 1 只有因子，另 5 只逐字节不变）、**取不到不复权价 = 硬错误**（绝不按 `f=1` 兜底，那会让 5 万的持仓变成 1.4 万且**看不出来**）、换算过的票**逐只进 `warnings`**（"后复权等值股数"不是账户里的真实股数，必须声明）。详见设计 §2.9 |
| R17-2 | **回测首日结构上不可执行**（记录，**本次不改**） | R17-1 的排查顺带暴露：`_run_execution` 在回测路径"无计划"时直接返回 `skipped="no_plan_backtest"`，**不会回退**到 `_execute_legacy`；而**实盘在无计划时会走评级兜底**那条路 | 两层原因叠在一起：①首日没有前置研究日 ⇒ 没有计划（**设计如此**）；②即使想兜底也不走实盘那条路（**保真缺口**）。合计效果 = **首日一笔都不会成交，包括止损** | **不改**：改动点在执行入口、与实盘共用同一段函数，而 2026-09-14（周一）要用**真实盘中触发**验收实盘路径——为回测首日一天的表现去动实盘执行入口，风险与收益不成比例。记录在设计 §11.1，等实盘验收通过后再评估。同时把这条**写进结果页声明**（`first_day_no_plan`），否则用户会把"复制持仓首日超配"读成团队失职 |
| R17-3 | **打分脚本把"起跑线的位置"记成团队失职** | run 4 的"风控纪律"得 **40 分**（3 条违例）。逐条看：**3 条全部落在首日**（09-07）—— 两条超配（518880.SH 19.9%、161226.SZ 19.3% > 15%）+ 一条 `159300.SZ 浮亏 −72.5%`（**R17-1 造出来的假浮亏**，真实是 −2.4%）。而首日**没有可执行计划**，团队那天**没有任何手段**去减仓 | 判据本身没错（它如实反映了那天的状态），错在**归属**：把继承来的起始状态算成团队的作为 | `score_backtest_run.py` 把首日的**持仓类**违例单列进 `inherited_violations`、注明"首日·起始状态，当天无可执行计划"、**不计分**——照「梯子合理性」里"首日没有研究日，跳过"的**同一先例**。成交笔数上限仍全量判（首日天然为 0）。⚠️ 这条改的是**判据**不是事实：违例照样打印，只是不再扣团队的印象分 |

> **R17-1 是第五个"每个环节各自看起来对"的洞，也是最该记进经验库的一个。**
> `load_bars` 取后复权（对，§3.2）、`apply_initial_state` 复制真实成本价（对，那是唯一真实的成本）、
> 撮合按 `bar.low < cost×(1−pct)` 止损（对，实盘 `_execute_legacy` 就这么判）——
> **三处都没错，错在它们读的不是同一个坐标系**。所以这类洞的通用问句是：
> **凡是把两个来源的数放在一起比较，先问一句"这两个数是同一个坐标系里的吗"**。
> 它的代价不是报错，是**结论反向**，而结果页上完全看不出来。

> ⚠️ **由此定下一条验收纪律**：**C2（run 3）与 C6（run 4）的分数作废，不能进最终报告**。
> 它们是 `copy` 起步、被 R17-1 直接污染的轮次（第 1 天就清仓一只、之后每天多出一笔现金）。
> 有效的只有 **C1（run 2）与 C4（run 5）**——两者都是**空仓起步**，起始簿里没有复制持仓，
> 因此**与复权口径无关**。C2/C6 在修好并部署后**重跑**，再一起进最终报告。

**全量离线回归**：后端 **903 项**（13 个 `smoke_*.py`，本次 +19 条复权换算断言）+
前端 **168 项** = **1071 项，0 失败**。

---

### 第十八轮（账户级失败被伪装成"管线 bug"，R18-1）

起因是用户的一句**现象描述**：「run 5 到什么进度了？**貌似 API 接口中断了**」。
这句话本身是错的 —— 但它错得很有价值，因为它正是**当时系统给出的说法**逼出来的：

| # | 项 | 发现过程 | 根因 | 修法 / 处置 |
|---|---|---|---|---|
| R18-1 ★ ★ | **余额耗尽（HTTP 402）被记成「连续 3 天无法生成计划」** | run 5 于 2026-09-12 22:45:57 中止，`run.message` = 「连续 3 天无法生成计划，已中止回测」。按这句话去查，方向必然是 Stage2 图、并发、限流。逐层排除后（只读重放同一研究日的 Stage2，draft/risk_review/plan **全部非空、actions 20 条**，代码路径健康）回到 `docker logs -t`，才看到 22:45:35 与 22:45:55 打给 Portfolio Analyst / Risk Officer / Portfolio Manager 三个节点的 **HTTP 402 `Insufficient Balance`**；更早 22:45:13 Stage1 也已崩（08-27 只写出 12/23 份报告）。查余额：`is_available=True`、**¥98.20**（用户已充值），即"接口中断"是假象，**真因是余额耗尽** | `invoke_structured` 的契约是「失败返回 `None` 让调用方回落」，它对**偶发解析失败**成立，对**账户级失败**（401 鉴权失效 / 402 余额耗尽）不成立 —— 后者不是"这一次没拿到"，而是"**后面每一次都会失败**"。返回 None 于是触发三段连锁：① 每个 agent 白跑一次必然也失败的 free-text 回落；② `run_portfolio_plan_for_user` 的两个**静默** `return None` 守卫交出空计划；③ `_drive` 按普通失败日累计，**照「连错 3 天」慢慢走完** —— 多烧的两天墙钟全白烧，而最终抛出的是那句完全读不出真相的中性话 | 新增 `account_level_reason(exc)` + `AccountLevelLLMError`，并**打破 None 契约向上抛**（`structured.py` 的 `invoke_structured` 重试循环 + `invoke_structured_or_freetext` 的 except）。判据**只认死错**：`status_code in {401,402}` 或文本命中 `insufficient balance`/`invalid api key`/`authentication fails`/`no credit`；**刻意不含 429（限流是瞬时的，当成致命会让一次抖动废掉几小时进度 —— 那正是要避免的反面）与 403（同 provider 上常只是某端点权限）**。`backtest._drive` 在 `except PlanCancelled: raise` 之后插入 `except AccountLevelLLMError`：照普通失败日的形状收尾（step 标 `degraded` + 写明原因、当天执行入库、检查点停在最后一个完整日），但**不再等**，直接 `raise ServiceError` 并在 `run.message` 里写真实原因与处置建议。实盘（`app/trust.py`）同样接住，译成「请检查 API key 与账户余额」，与「这次没拿到结构化结果，请稍后重试」**分开**（前者重试无用）。详见设计 §2.10 |

> **R18-1 是第六个"每个环节各自看起来对"的洞，也是第一个"错在说辞而不是错在行为"的洞。**
> 前三段链路各自都没错：`invoke_structured` 返回 None 是它的契约、`run_portfolio_plan_for_user`
> 的两个守卫是防御性写法、`_drive` 的"连错 3 天"是为慢性失败设计的。**错在把"账户没了"
> 这种一次性的、终局的失败，塞进了一套为"慢性、可恢复失败"设计的通道**，而通道出口的那句话
> 只说"慢性"的部分（连续 3 天没计划），把真相留在了三行日志里。
> 所以这类洞的通用问句是：**「这个失败会被下游当成什么？」** —— 如果答案是"另一个失败"，
> 就得检查这条路是不是为它设计的。它的代价不是报错，是**把人送到错误的排查方向上**：
> 这一轮为它多查了一轮 Stage2、一轮并发、一轮网络。

> ⚠️ **残留（已知，记在这里）**：账户级失败那天照旧落一个 `degraded`，而**它后面那一天天然没有
> 计划**（计划是前一日日终产出的），所以即便 fail-fast，续跑仍会多出**恰好 1 个**无计划日
> （原先最多 3 个）。这是逐日时序的必然后果，不是缺陷。

**回归断言**（`smoke_backtest_calc.py` 新增 **15 项**）：402/401 判为账户级；
**429 / 403 / `ValueError("structured output returned no parsed result")` 明确不判**（这条保证判据
有鉴别力，不是"什么都算致命"）；`invoke_structured` 遇 402 **只调 1 次**就抛（不是 2 次）；
500 仍然重试两次后返回 None；`invoke_structured_or_freetext` 遇 402 时 **plain 路径 0 次调用**；
真实库上把 `run_portfolio_plan_for_user` 打桩成 402，断言 run 停在**第 1 天**（不是第 3 天）、
`run.message` 含「LLM 账户不可用」+ `Insufficient Balance` 且**不含**「未产出有效行动方案」、
检查点现金与账面现金一致。

**全量离线回归**：后端 **918 项**（13 个 `smoke_*.py`）+
前端 **168 项** = **1086 项，0 失败**。

> ⚠️ **部署状态**：F4 **已实现、回归全绿、尚未部署** —— run 5 正在跑，重建容器会打断它
> （`deploy.sh` 会 `docker compose up -d --build`）。等 run 5 结束后与下一轮一起上。

---

### 第十九轮（移动端导航重构 R19-1 + 解析代码按名称纠错 R19-2）

用户提了两件事：**①**「同步持仓时识别出的国投白银LOF 代码显示为 `06862.HK`，实际是 `161226`」；
**②**「把财团托管搬到移动端底部 Tab 并改名『模拟交易』，历史回测入口放进模拟交易内部」。
**改动仅限移动端，桌面端保持现状**（唯一例外是页头标题，用户已确认接受）。

| # | 项 | 发现过程 | 根因 | 修法 / 处置 |
|---|---|---|---|---|
| R19-1 | **移动端「财团托管」够不着、历史回测手机上根本没有入口** | 用户要"搬到底部 Tab"。查下来发现移动端底部只有「交易 / AI Agent」两格，`view-trust` / `view-backtest` 只能从**首页宫格**进（宫格有托管/量化/自选三张卡），**历史回测连宫格都没有** —— 手机上完全不可达 | 移动端只做了两个核心 Tab，其余二级页靠宫格兜；历史回测是后加的功能（第十六轮 P1），当时只补了桌面侧边栏 | 底部 Tab 加第三格 `data-view="trust"`「模拟交易」（`.mobile-tab{flex:1}` 自动三等分，不用改 CSS）；`switchView` 里那段 `mobileActiveView` 三元式换成**映射表 `MOBILE_TAB_OF`**（`trust`/`backtest` → `trust`，其余二级页 → `home`）；托管总览顶部加**移动端独有**的历史回测入口卡（默认 `display:none`，媒体查询里翻 `flex`）；首页宫格删掉重复的托管卡；`view-spec` 自述页那两处「仅 2 个 Tab」同步改掉 |
| R19-2 ★ ★ | **模型把提示词里的示例代码照抄回来当成识别结果** | 用户报「国投白银LOF 显示成 06862.HK」。grep 全仓：**`06862` 只出现在 `app/ocr.py` 的 4 处提示词里**，全部是拿它当"代码长什么样"的示例 —— 而模型返回的正是这个串 | **示例串泄漏**：模型读不出某一行的代码时，把提示词里唯一那个具体代码抄了回来。旁证两条：喂**带真实代码列**的文本 → 读对 `161226`；喂**没有代码列**的文本 → 返回**空**而不是幻觉 ⇒ 文本路径不触发，**图片路径（`_VISION_PROMPT`）才是出事的那条**，正是"同步持仓截图"的场景。更要紧的是**服务端完全不校验**（`ocr._normalize` 只 strip），LLM 给什么就存什么 | **两层都修**。①**去掉示例**：4 处提示词改成只描述**形态**（"A股/深市基金 6 位数字、港股 5 位数字加 .HK"），并写明"看不清就留空、不要套用示例、不要凭名称猜"——**不给任何具体代码**（换成别的真实代码只是把幻觉换个值，而且新值更难被发现）；②**服务端按名称回查**：新增 `correct_codes`，解析后逐行用名称走 `intent._basicinfo_rows` 拿真代码，与模型给的**不一致时以名称解析为准**并打 `code_corrected` 标（前端渲染成「· 代码已按名称校正（原 06862.HK）」）。实测 `_basicinfo_rows('国投白银LOF')` → `161226.SZ`，真 Wind 跑通耗时 **1.5s** |

> **R19-2 是第七个"每个环节各自看起来对"的洞，而且是第一个"错在提示词写得太具体"的洞。**
> 提示词里给一个具体代码当例子，**人类读起来是澄清，模型读起来是备选项**：读不出来时，
> 上下文里那个"看起来该长这样"的串就是最有吸引力的答案。所以这类洞的通用问句是：
> **「我写在提示词里的示例，模型会不会把它当成答案？」** —— 凡是给"格式示例"，要么用明显非法的
> 占位符，要么只描述形态。**去示例治的是"不再诱导"，治不了模型自己幻觉，所以两层都要有。**

**三个刻意的取舍（都写进了代码注释，避免下次重新论证）**：

1. **判"换没换标的"用代码核心（数字去前导零），不是字符串**。`161226` 与 `161226.SZ` 是同一个标的
   —— 拿字符串直接比，A 股每行（`600519` → `600519.SH`）都会被判成"换了"，于是**每行都挂"已校正"徽标**，
   提示就变成了噪音。归一还顺带治了港股 `6862.HK` / `06862.HK` 的写法差异。
2. **歧义宁可不动**。中文简称的歧义大多是**前缀**关系（国投白银 / 国投白银基金、A份额 / C份额），
   逐字相似度能到 0.8 —— 只按"相似度差够大"判会把一只票**静默换成另一只**。所以先看有没有**精确同名**，
   再看是否**明显胜出**（相似度 ≥0.85 且领先 ≥0.15），否则一律不改。**改错的代价远大于不改**。
3. **失败一律退回"改动前的行为"**：查不到 / 抛错 / 超预算（20s）/ 超上限（25 个名字）/ 开关关闭，
   都**原样保留模型代码且不加任何字段**。既不能让一个锦上添花的校验把「同步持仓」整个搞失败，
   也不能让 Wind 一挂就每行冒"未核实"徽标（噪音比信号大）。生产回滚开关：`.env` 加 `OCR_CODE_CORRECT=0`。

**新增离线回归** `scripts/smoke_ocr_codes.py`（**46 条**，不跑 LLM / 不连库 / 不碰 Wind）：
提示词卫生（4 套提示词里不许再出现任何具体代码形态 + 点名钉死 `06862`）、`_code_core` 的判别力、
主路径（抄了示例 → 换成真码 + 打标 + **名称不被 Wind 简称覆盖**）、同一标的**不打标**且字段集与改动前完全一致、
失败优雅（查不到/抛错/超预算都原样返回不抛）、歧义不改（前缀歧义、A/C 份额、真重名）、
去重/上限/墙钟预算、自选路径（空代码是**补全**不是纠正，不打标）、缓存与开关、**联网隔离自证**。

> ⚠️ **"墙钟预算生效"那条同时是"实现没有被 `with ThreadPoolExecutor` 包住"的守门人**：
> `with` 的 `__exit__` 会 `shutdown(wait=True)`，把预算整个吃掉（没回来的名字照样等满自己的超时）。
> 用 `with` 写的话，那条断言会挂 2 秒后照样通过 —— **它就测不出任何东西了**。

**全量离线回归**：后端 **964 项**（14 个 `smoke_*.py`，本次 +46）+
前端 **176 项**（本次 +8，补上了移动端导航此前**零覆盖**的缺口）**= 1140 项，0 失败**；
`import app.main` 路由注册正常（64 条）。

---

### 第二十轮（真回测暴露：计划跨 run 串味 R20-1 + 验收脚本自身的陈旧断言 R20-2）

R19 部署完，把 C4/C2/C6 三轮**真回测**重跑了一遍。这一轮的缺陷**不是离线断言能发现的**——
它们只在"影子账户被多个 run 复用、且窗口重叠"时才现形。

| # | 项 | 发现过程 | 根因 | 修法 / 处置 |
|---|---|---|---|---|
| R20-1 ★ ★ | **新 run 的第一天会执行上一轮留下的计划** | C2（run 3）第一次重跑后，验收的 E 条挂了两条：断言是「首日 `has_plan` 必须为 False」，而 run 3 的 **2026-09-07 显示 `has_plan=True`、19 条动作**。查 `trust_plans` 表列：只有 `(id, user_id, trade_date, plan_json, created_at)` —— **没有 `run_id`**；再对时间线：run 3 的 `09-07` step 写于 **15:55:14**，而那份计划 `id=20` 写于 **15:29:17**，是**上一个 run（run 5）**生成的。而 run 3 的日循环只生成 `days[i+1]` 的计划，**永远不会生成 09-07 的** | 三处**各自都对**的设计叠出来的：① `trust_plans` 无 run 归属；② P0-1 刻意让影子的计划**跨 run 保留**（否则 run 的报告会被别的 run 的生命周期抹掉）；③ P0-2 只把 `skipped == "no_plan_backtest"` 判成降级日，而 `trust.py:900` 是 `if has_plan: return _execute_plan(...)`。于是"库里那天有计划"就足以让新 run 的**首日豁免失效**，去执行一份**为另一个账簿生成**的计划（run 5 是空仓起步逐步累计出来的账，run 3 是复制来的真实持仓），并且报告页把别人的计划算成自己的 | **取数用临时处置**：起跑前清掉影子在**本 run 窗口内**的计划行（窗口外的不动——那是别的 run 报告页的底稿）。实测：C2 的成交从污染那轮的 **7 笔**变成 **13 笔**，验收 12/12、首日正确回到 `has_plan=False`。**正式修法待定**，两条路见下 |
| R20-2 | **验收脚本自身有一段从没执行过的断言**（引用了不存在的列） | C4（run 5）第一次验收直接崩：`'BacktestStep' object has no attribute 'trade_count'` | 本地版本的 `verify_backtest_run.py` 里写了 `s.trade_count`，而 `BacktestStep` **根本没有这个列**。这段断言**从未被执行过**——之前那次"13/13"是**旧的、可用的**那份跑出来的（我先把它 scp 覆盖了） | 换成真有意义且不依赖两表的恒等式：**`step.fees` == 当天各笔成交 `fee` 之和**（对任何 run 都成立，不受 TM-M12-27 那条排序约束影响）。并扫了全脚本对模型列的引用，无第二处。**代价认知**：本轮 C4 的 14/14 是这套脚本**第一次真正跑完**——包括此前从未执行过的 F 条（监控卡片） |

> **R20-1 是第八个"每个环节各自看起来对"的洞**，而且它和前面七个不同：**前面七个错在数据，这个错在"归属"**
> —— 三处代码都在正确地做自己的事（保留计划对、只认 `no_plan_backtest` 对、按日期取计划对），
> 合起来却是"**A 轮的执行吃了 B 轮的计划**"。所以这类洞的通用问句是：
> **「这条记录是谁产生的？读它的人知道吗？」** —— 只要一张表**没有产生者标识**，
> 而它又会被多个"作业"共享，就一定会有某一天被张冠李戴。
> 它的代价不是报错，是**让一整轮验证的结论悄悄失真**（首日豁免失效、账簿与计划不匹配），
> 而结果页上完全看不出来。

> ⚠️ **由此回看前几轮的分数**：**C1（run 2，88.5）有同样的嫌疑** —— 它与 run 1 同为
> `09-07~09-11` 窗口，run 2 的 09-07 可能吃到了 run 1 留下的计划（当年没有这条断言，
> 所以没暴露）。**C4（run 5，78.2）确认干净**（`08-25` 前面没有别的轮次留计划，验收 E 条全过）。
> 故最终报告只收 **C4 + 重跑后的 C2/C6**；C1 若要进报告，需先按新流程重跑一遍。

**R20-1 的两条正式修法（待定，已记录）**：

- **A（小、对准）**：`start_run` 在**从零起跑**（检查点为空）时，清掉影子在本 run 窗口内的计划。
  语义是"一轮回测拥有它自己的窗口"，与既有的 **TM-M12-27**（"A 条只对影子最新的那个 run 有效"）
  是同一个立场。缺点：同窗口的两个 run 仍无法各自归属。
- **B（大、治本）**：给 `trust_plans` 加 `run_id`，写入时带上、读取时按 run 收口（实盘计划 `run_id` 为 NULL）。
  要迁移 + 改所有读写点，**会在实盘验收前动到实盘计划路径**，风险与收益需权衡。

**R20-3（同一轮顺带修的）**：`_newer_runs_on_same_shadow` 原本按 **id** 判断"有没有更晚的 run"
（`id > run_id`），而**起跑顺序与 id 顺序不一定一致** —— C6（run 4）正是在 C4（run 5）之后**重跑**的，
按 id 会误判成"有更晚的 run"，于是白白跳过 A 条，而那两表明明就是 run 4 自己刚写进去的。
改成比 **`updated_at`（最后一次写库的时刻）**：只有"比我更晚还写过库"的 run 才可能清掉我的
orders/trades。改完 C6 从 12/12 变成 **14/14**（A 条也验上了）。

**本轮真回测结果（验收脚本统一用修好后的版本）**：

| 轮 | run | 窗口/起步 | 验收 | 综合分 | 六维（覆盖率/依据性/梯子/风控/前后一致/现金） |
|---|---|---|---|---|---|
| C4 | 5 | 14 日 / 空仓 | **14/14** | **78.2** | 30.8 / 100 / 96.7 / 100 / 72.1 / 70.0 |
| C2 | 3 | 5 日 / 复制持仓 | **12/12**（A 条按排序约束**正当**跳过：run 4 确实更晚） | **73.6** | 25.0 / 94.0 / 96.8 / **—** / 52.2 / 100 |
| C6 | 4 | 5 日 / 复制持仓 | **14/14** | **80.4** | **0.0** / 100 / 100 / 100 / 82.4 / 100 |
| C1 | 2 | 5 日 / 空仓 | ⚠️ **作废** | ~~88.5~~ | 与 run 1 同窗口，`09-07` 疑似同样吃到 run 1 留下的计划；要进报告须按新流程重跑 |

> **C2 的"风控 —"**：冻结配置里三条限值全空 ⇒ 无从评估，**按其余五项等权**算出 73.6
> （不是"没违规所以满分"的假绿灯）。冷启动的轮次（早于限值设置）都会命中这条。

> **C6 的"覆盖率 0.0"**：四天分别只对 9/24、5/24、7/24、10/24 只表过态。它与其余维度**反向**
> —— 表态少的那几天，动作反而更聚焦、理由更长（中位 78 字 vs C2 的 58 字）、梯子更收敛
> （最大距离 7.9% vs C2 的 13.4%）。这一条是四轮里最值得在能力评估里展开讲的信号。

---

### 第二十一轮（移动端列表重做 + 全站排序 + 历史回测管理员门禁，R21-1~3）

用户实测后提的六条。逐条核现状时发现**两条的真实症状与描述不同** —— 先纠这个，再改。

| # | 项 | 发现过程 | 根因 | 修法 / 处置 |
|---|---|---|---|---|
| R21-1 | **「查看全部」点了没反应；「12 笔」是写死的** | 用户报"点击最下方的查看全部无反应" | `index.html` 那个 `<span>查看全部 ›</span>` **根本没有 `onclick`** —— 只是被 `cursor:pointer` + 亮色做成了可点的样子。同块的 `<span>AI 调仓记录 · 12 笔</span>` 也是**硬编码**：`loadTrades()` 只用真实数据替换 `.trade-row`、**从不更新表头那句计数**（代码里还留着"保留 trades-header"的注释） | 加 `onclick="openTrustRecords()"`，切到托管页**已有**的「委托成交」子 Tab（那里本来就是完整流水）——复用既有处理器，不另写一套列表。计数交给 `loadTrades()` 按 `trades.length` 写 |
| R21-2 ★ ★ | **移动端监控条件「挤在一起」——实际是整块被隐藏** | 用户报"监控条件也无法正常显示，全部挤在一起" | `@media (max-width:1023px)` 里 `.holdings-card .table-row { display: none }` 与 `.monitor-table .table-row` **特异性平手（都是 0,2,0）**，前者在源码中更靠后 ⇒ **它赢**。于是监控条件的表头和每一行在手机上全部不显示（不是挤，是不见了）。⚠️ **同一条规则还顺带隐藏了回测结果页的三张表**（`#bt-baseline`/`#bt-equity`/`#bt-list`） | 给"手机上要以表格形态保留"的表打 `.m-table`，在媒体查询里用**更高特异性 (0,3,0)** 定向翻回 `display:grid`。⚠️ **绝不能放宽那条支点规则** —— 它是「桌面表格 vs 移动卡片」互斥的依据，放宽会让两套 DOM 同时画出来。`check_frontend.js` 里加了一条断言专门钉住"支点规则仍在、覆盖走 .m-table" |
| R21-3 | **移动端持仓列表确实挤**（这条属实） | 同上 | `.holding-row` 只有**两个 flex 子块**（`.holding-left` / `.holding-right`）在一行里争宽度，长名字把右侧「占资产 X%」压扁 | **先按「卡片牌组」做了一版，用户看过说方向不对** —— 他要的是**和桌面一样的一张表**：表头在第一行、横向滑动看全列、点表头排序。于是牌组整个删掉，改成**桌面与手机同一张表**（`.mobile-holdings`/`.desktop-holdings` 两套互斥、卡片构造器、手机专用排序条全部移除）。顺带把 `renderHomeHoldings` / `renderTrustOverview` 里**互相抄的四份模板**收敛成一个共用构造器 `holdingCellsHtml`，两个入口只剩一条渲染路径 —— 排序与列对齐都不可能再「两边不一致」 |

**顺带做的两件**（用户同批提的）：

- **四个列表都可排序**（首页持仓 / 托管总览持仓 / 持仓页 / 自选股）。此前仓里**零排序代码**。
  统一 `SORTS` 状态 + `sortRows`：桌面点表头、手机点排序 chip 条，**同一份状态**，两边必然一致。
  两条设计纪律：**`key === null` = 不排序**（默认行为与加排序之前逐字节相同，既有断言零回归）；
  **`null` 值恒排末尾**（行情拿不到时 `price` 就是 `null`，当成 0 会被排成"跌得最狠"）。
  排序每列的取值器**只写一次**，桌面与移动共用。
- **历史回测只对管理员开放**（R21-4，见下）。

> **R21-2 是第九个「每个环节各自看起来对」的洞，而且是第一个纯 CSS 特异性的洞。**
> 两条规则各自都对：`.monitor-table .table-row` 定义了一张宽表要横滑（对），
> `.holdings-card .table-row` 让卡片内的表格在手机上让位给移动卡片（也对）——
> **错在它们撞上了同一个元素，而胜负只由源码顺序决定**。这类洞的通用问句是：
> **「我这条规则，会不会被另一条同样具体的规则按源码顺序压掉？」** ——
> 只要答案是"可能"，就该把意图写成**更高特异性**的显式覆盖，而不是依赖书写次序。
> 它的代价不是报错，是**整个模块在某个断点下静默消失**，而在宽屏上一切正常。

| # | 项 | 根因 | 修法 |
|---|---|---|---|
| R21-4 ★ | **历史回测此前对所有登录用户开放** | 仓里**完全没有"管理员"概念**：`User` 只有 `id/phone/created_at/is_backtest`，`auth.get_current_user` 只查 `Session`、返回裸 int，11 条回测路由只挂"登录" | 新增 `config.ADMIN_PHONES`（逗号分隔手机号）+ `auth.require_admin` 依赖；11 条回测路由换成它。**fail-closed**：白名单为空 ⇒ 谁都不是管理员 ⇒ 回测整体关闭（启动日志打出实际数量，避免"忘了配"表现成"功能凭空消失"）。与 `backtest._require_own` 是**两道不同的防线**，都保留：一道管"能不能碰回测这个功能"，一道管"这条 run 是不是你的" |
| R21-5 ★ | **能力评估报告要在应用内展示** | `docs/` 被 `.dockerignore` 排除、`deploy.sh` 也不传它 ⇒ 报告进不了镜像；且应用用 `Authorization: Bearer`（token 在 localStorage），**`<iframe src="/api/...">` 是浏览器裸请求、带不上这个头** | 报告复制到 `app_frontend/capability.html`（随镜像发布），新增管理员专属 `GET /api/backtest/capability`；前端**用带头的 fetch 取回文本写进 `iframe.srcdoc`**（同时把报告的 CSS 关进独立源 —— 它自己也定义 `:root` 变量，内联会和应用打架）。两份文件用一条断言逐字节比对，防止改了文档页而应用内不更新 |

**后续追加的一条统一约束（用户要求）**：**所有表格的表头与数据一律左对齐、向右延伸，且不许侵犯其他列**。
左对齐只是第一步 —— 真正「压住邻列」的机制是 **grid 子项默认 `min-width: auto`**：
它不允许被压到内容宽度以下，于是任何一列装不下时就会溢出自己的轨道盖住邻居
（监控条件的整句、以及各数值列，都是这么把邻列挤破的）。所以加了统一护栏
`.table-row > div { min-width: 0; text-align: left; overflow-wrap: break-word }`，
并清掉全部 52 处 `text-align:right` 内联；配合表格自己的 `min-width` 与横向滚动，
**宁可整张表横向滑动，也不让列与列重叠**。

> 又一处**我自己的断言写错了**：起初写的是「全站没有一处右对齐」，结果被三处**非表格**的右对齐判失败
> （持仓页卡片的右半块、成交流水的两列、现金输入框）。用户要的是**表格**，断言就该按表格收窄 ——
> 把断言写得比需求还宽，只会制造噪音。

**再追加四条（用户要求）**：

> ✅ **2026-09-14 人工确认**：用户实测「**持仓表格滑动正常**」。登录页宽度与监控条件的移动端表现建议再看一眼。

1. **所有持仓列表增加「当日盈亏」列**。首页持仓表与托管总览表各从 6 列变 7 列（插在「浮动盈亏」与「仓位占比」之间，
   可点表头排序），持仓页的卡片行也补了一行「当日」。数据用 `position.day_pnl`（= (现价 − 昨收) × 数量）。
   ⚠️ 两个容易踩的点：① 两张持仓表因此要**单独的 7 列 grid 模板**（`.pos-table`）——
   `.table-row` 的默认模板是 6 列，回测的净值曲线表也在用它，不能一起改；
   ② **`day_pnl` 为 null 时（缺昨收：新上市/停牌）不能套 `_upDown`** ——
   它写成 `(v || 0) >= 0`，对 null 会返回 `'up'`，把"没有数据"画成绿色的涨。现在显示「—」且不带涨跌色。
2. **剩下三处非表格的右对齐也一并改左**：持仓页卡片行的右半块、成交流水的金额列、同步确认页的现金输入框。

3. **移动端登录页宽度**：卡片与输入框加 `min-width: 0`，移动端改用对称的 `max(16px, env(safe-area-inset-*))` 边距。
   根因与表格列**是同一个**：`<input>` 有**固有的最小内容宽度**（默认 `size≈20`，约 180px），
   而 flex 子项默认 `min-width: auto` 不允许被压到内容宽度以下 ——
   于是手机上「验证码 + 获取验证码(110px nowrap)」这一行压不下去，把整张卡片顶得比屏幕还宽；
   卡片又是居中的，超出部分偏向一侧被裁掉，看起来就是「太宽 + 输入框两侧间距不一样」。
4. **登录接真实短信**：`SMS_SIGN_NAME` / `SMS_TEMPLATE_CODE`（外加两个已有的 AccessKey）配进生产 `.env`，
   与 video-note **同一账号、同一签名、同一模板**（模板变量都是 `code`，所以两边可共用）。
   实测：应用返回「验证码已发送，请查收短信」，阿里云 `QuerySendDetails` 记录 **状态=3（发送成功）**。
   ⚠️ 移动/电信的报备状态仍未确认，非联通号收不到码时先查这个、不是先查代码。

**新增离线断言**：`scripts/smoke_admin_gate.py`（**20 项**，仓里**第一个走 `Authorization: Bearer` 的测试**）+
`check_frontend.js` +19 项（牌组默认隐藏与翻显、**支点规则仍在且覆盖走 .m-table**、排序的 null 语义与方向切换、
入口 fail-closed 默认态、`switchView` 兜底、报告走 srcdoc 不走 iframe src）。

**全量离线回归**：后端 **987 项**（15 个 `smoke_*.py`）+
前端 **195 项** = **1182 项，0 失败**；`import app.main` 正常。

> **一处自己写错、被测试抓住的**：`check_frontend.js` 里我起初把"降序 + null 排末尾"的期望写成了 `CAB`，
> 实际正确值是 `BAC`（降序数值 5→−3，null 垫底）—— **是断言写错了，不是代码错了**。
> 这条断言的鉴别力正在于：若把 `null` 当成 0，结果会是 `BCA`。

---

## 四、逐模块用例结果（摘要）

### M0 + M1 登录注册（11/11 ✅）
服务健康、首页可访问、手机号格式校验、正常发码（开发模式）、60s 频控、错误验证码拒绝、正确验证码登录+建号、无 token 401、退出后 token 失效、建号默认值 DB 校验（现金 50 万 / 未同步 / stock_scope=0 / style=1 / 佣金万2.5 / 免五 off / 印花 0.0005）——全部通过。

### M2 同步持仓（11/11 ✅）
文本 AI 解析（A股+港股 3 只）、汇总行忽略、空文本拦截、图片 AI 解析（百炼 qwen3-vl-flash）、base64 非法拦截、首次同步清 50 万、镜像簿规范化代码落库（600519.SH / 00700.HK）、二次同步覆盖、展示含行情字段（price/market_value/pnl/占比）、双簿汇总、qty≤0 行忽略。

### M3 + M4 自选/个股 + 策略设置（10/10 ✅）
加自选 A股/港股规范化、列表、重复去重、删除、个股详情 K 线；策略设置默认值回显、保存全部+回读、部分字段保存、未登录 401。（radio 互斥属前端，归 M11）

### M5 + M6 托管簿生命周期 + 撮合引擎（18/18 ✅）
未建簿开托管被拦、快照建簿（全可用无冻结）、开/关托管、有成交后再快照被拦、reset 清空、reset 后重建；撮合：港股卖出无印花、A股卖出印花税、可用不足拦截、A股买 100 整数倍拦截、最低 5 元佣金、买入 T+1 冻结、免五生效、资金不足拦截、日结释放 T+1、错误代码拦截、卖出成交记账。

### M7 托管执行链路（9/9 ✅）
未开启不执行、Hold 不动、评级 Sell 清仓、Underweight 减半、止损触发清仓、Buy 用卖出现金加仓、委托/成交落库、单日次数上限、调度器注册（4 个 cron job）。

### M8 + M9 委托/成交/归因 + 通知（9/9 ✅）
委托/成交流水动态化、归因看板、委托/归因空态；通知默认无配置、保存飞书/企微并启用、停用、未登录 401。
> ⚠️ 本节是**当时**的记录。`realized_pnl=0` 曾记为已知缺口 G2，**该缺口已闭环**（见 §六）：现在真实值取自 `TrustConfig.realized_pnl`，老簿如实返回 `None` + 提示而非 0。

### M10 AI 对话/意图/深度分析（7/7 ✅ + 第三轮真实跑单）
账户问答-持仓诊断（上下文注入）、无持仓兜底、意图路由直接给代码、模糊需澄清、深度分析异步提交（返回 task_id）、状态轮询、引擎缓存命中。
**第三轮真实跑单**：6862.HK（海底捞）@2026-09-08 完整跑完约 12 分钟，评级 **Hold/持有**，report 12 段齐全（市场/情绪/消息面/基本面/多空/交易计划/组合经理结语）；随后按修复后的前端路径（chat 分流 date=2026-09-08 → `/analysis`）二次提交 **命中 engine_run 缓存秒回**（`cached=True`，报告结构完整，`buildReportText` 所需字段全部 present）。前端 `runDeepAnalysis` 现透传 `data.date`（意图已解析的交易日），并对客户端日期做周末回退，避免非交易日空跑。

### M11 前端交互回归（静态 ✅，第二轮已修 4 项，动态渲染待人工）
- JS 语法 `node --check` 通过（inline script 无语法错误）。
- 导航结构：仅「交易首页 / 托管」两个一级 nav，无重复「持仓」入口。
- OCR 错误提示：无旧「阿里云 OCR 未配置」文案（错误透传服务端 detail）。
- radio 互斥：`selectScope(this)` + `radio-circle.checked`。
- 401 自动登出：`resp.status===401 → clearToken + showLogin`。
- 同步持仓入口（文本/图片）、托管空态→建簿提示、自选/个股详情/深度分析按钮、聊天卡片均存在。
- **第二轮新增接线静态确认**：`.ai-cta[data-view=chat]` 已纳入导航点击绑定（BUG-7）；`renderHomeHoldings(positions)` 由 `loadPositions` 调用并填充 `#home-desk-rows`/`#home-mob-rows`（BUG-6）；`.send-btn` → `POST /api/chat/ask`，`handleChatReply` 按 `kind∈{account,ask,quote,analysis}` 分流，`analysis` 走 `runDeepAnalysis` 提交 `/analysis` 并轮询（BUG-8/9）。
- ⚠️ 按钮点击后的最终渲染效果（首页持仓表格/移动卡片、聊天气泡逐条展示）仍建议浏览器人工过一遍。
- **第四轮增强接线静态确认**：`sync-confirm-btn` → `collectSyncRows` + `se-cash` → `/api/trust/snapshot`（trust）或 `/api/positions/sync`（mirror，均带 cash）；解析后逐行 `fetchQuote` 补实时现价；`input` 任意字段（含可用资金）即 `recalcAll` 刷新总资产/总市值/浮动盈亏，总资产=可用资金+市值；改代码失焦重取行情、手改现价标记 `priceEdited` 不被覆盖；托管总览 `renderTrustOverview`/持仓明细、空仓启动 `startEmptyTrust` 已接线（BUG-6/8/9、ENH-1~7 相关）。
- **第六轮改造接线静态确认（REQ-1~9）**：总览 `#trust-scope-pills` + `setTrustScope/selectScope` 共用 `PUT /api/trust/config` 且 `loadTrust` 回读高亮两处（REQ-1）；侧栏顺序 财团托管→ai-cta(chat)、新增 `nav-item data-view=quant` 占位（REQ-2/3）；个股详情 ECharts `tooltip trigger:axis + cross`、`stockTip` 输出当日收盘（REQ-4）、`back-btn→goBack→switchView(lastTopView)`（REQ-5）；首页持仓 `.position-bar/.position-fill` + 「占资产 %」，`ratioBar/ratioText` 优先 `assets_ratio`=市值/总资产（REQ-6）；确认页 `se-ov-assets/mv/pnl` + `se-ov-bar`/`se-ov-reset`（还原自动），确认体带 `reset_snapshot`（REQ-7）；自选视图整段重写：`wl-chips`/`wl-manage`/`wl-inline`/`wl-suggest`/`wl-parse-*` + `switchView('watchlist')→loadWatchlist()`，后端 8 条 `/api/watchlist*` 路由齐（REQ-9）；`node --check` 通过。
- **第七轮改造接线静态确认（R7-1~6）**：`wlSaveInline` 的 toast 移到 `await loadWatchlist()` 之后且保存按钮禁用（R7-1）；`#wl-parse-busy`+`.wl-parse-busy .spin` 转圈行，`wlSetParseBusy`/`wlSyncParse`/文件 change 驱动 busy（R7-2）；`renderWlList` 持仓样式（名称+代码/最新价/涨跌幅/涨跌额+移出）+ 60s 静默刷新 interval（R7-3）；设置页 `#cfg-scope-group-row`/`#cfg-scope-group` 下拉、`syncScopeGroupOptions` 显隐/回填、change→PUT 持久化、`buildConfigBody` 带 `stock_scope_group`（R7-4）；`trustPanelLoader/loadCurrentTrustPanel` 接线 `switchView('trust')` 与 `.trust-tab` click 按面板刷新（R7-5）；`addStock` 移除清空、保留输入并 focus（R7-6）；`node --check` 通过。

---

## 五、未覆盖 / 待人工确认项

| 项 | 状态 | 说明 |
|---|---|---|
| 涨跌停拦截（M6-11/12、M7-08） | 规则已全覆盖 ✅，仍**未用真实涨停股实弹跑** | 需当日真实涨停/跌停标的。板块规则（ST 5% / `.BJ` 30% / `300,301,688,689` 20% / `.SH,.SZ` 10%）已由 `scripts/smoke_limit_rules.py` 62 项逐档钉死，且口径已从「收盘涨幅 ±9.5% 近似」改为「成交价是否到板价」（见 §六 G5） |
| 计划层深度分析（M7-10） | 已真实跑单 ✅ | 6862.HK @2026-09-08 完整跑完约 12min，评级 Hold，12 段报告齐全（详见 M10/结论）；当日同一用户二次询命中 engine_run 缓存秒回 |
| 聊天→深度分析全链路（BUG-9 后续） | 已回归 ✅ | chat 分流（date）→ `/analysis` → 缓存命中 → 报告结构完整（`buildReportText` 消费字段全 present）；前端气泡最终渲染仍建议浏览器人工过一眼 |
| 前端最终渲染（M11） | 待人工 | 首页持仓表格/移动卡片、聊天气泡展示需浏览器实际操作确认（第二轮 BUG-6/7/8/9 均已 HTTP/静态回归） |
| 真实券商截图 OCR | 用户已验 | 用户反馈真实持仓数据测试通过，本轮未重复 |

---

## 六、缺口闭环（G1/G2/G3/G5 已修，G4 仍未做）

### 6.1 本轮已闭环

| # | 原缺口 | 本轮做法 | 判据与证据 |
|---|---|---|---|
| G1 | 成交后自动 webhook 通知未接（`trade.py` 仍 `TODO`） | 钩子**不放** `trade.place_order` 内部，而是把 `trust.run_execution` 改名 `_run_execution` 后包一层壳派发。理由：`_execute_plan` 对每处 `place_order` 只捕 `ServiceError`，通知在 `place_order` 里抛非 `ServiceError` 会贯穿整个循环、静默吞掉该 tick 剩余全部档位；且逐笔发会刷屏。壳里拿得到完整列表 → 合并成一条。`clock is not None`（回测）**绝不发**通知 | `scripts/smoke_notify.py` 46 项；**生产实测**：真实飞书机器人收到「单笔带理由」与「三笔合并（含一笔无理由）」各一条 |
| G2 | 归因 `realized_pnl` 恒 0 | `TrustConfig.realized_pnl`（**本簿累计**，建簿/重贴快照时经 `_clear_book_runtime` 归零）；卖出分支在改 `hold_qty` **之前**按当时成本价累计。口径与 `backtest_calc.replay_realized` 逐字一致 | `scripts/smoke_realized_pnl.py` 27 项，核心是**双算法交叉核对**（增量计数器 vs 逐笔重放）；生产 Postgres 上 `scripts/e2e_closure.py` 三路（计数器/重放/手算）逐分一致 |
| G3 | 无手动触发执行的端点 | 新增 `POST /api/internal/run-execution` 与 `GET /api/internal/scheduler`，**fail-closed**（`TRADE_MASTER_INTERNAL_TOKEN` 未配置即恒 403）+ `secrets.compare_digest`；nginx 公网一律 404 | `scripts/smoke_internal_api.py` 34 项（走真实 FastAPI 路由，断言 HTTP 状态码）；生产实测 无 token→403 / 错 token→403 / `confirm=false`→400 / 15:00 后→409。**两道防线均已实测**：公网 `https://trade.videotonote.cn/api/internal/*` → **404**（nginx `^~` 规则，2026-09-12 于该机 443 block 补上并 `nginx -t` 通过后 reload），回环 `127.0.0.1:8010` 无 token → **403**（应用层 fail-closed）——公网 404 而不是 403 是有意的：403 等于对外承认"这里有个端点，只是你没权限" |
| G5 | 涨跌停判断为 ±9.5% 近似 | `market_clock._limit_pct` 提升为公开 `limit_pct`（保留别名），实时分支改接板块规则；判据从「收盘相对昨收涨幅」改为「**成交价是否到板价**」，与 `HistoryClock` 口径统一 | `scripts/smoke_limit_rules.py` 62 项，含专门钉住「口径变化本身」的断言（原先 9.5%~10% 的窗口由拦变放） |

**G2 的两态是刻意区分的**：老簿（本列上线前建的）如实返回 `None` + 「暂未统计」提示，**不是 0**——原先硬编码 `0.0` 会让「已实现盈亏 0 元」看起来像一个真实结论。`None` 的语义是「不可知」。前端 `index.html` 与对话工具层都保留了这一区分（`scripts/check_frontend.js` 有 `realized_pnl=null → 「暂未统计」，不冒充 0 元` 的断言）。

**G5 的口径变化有实际后果**：原先 `9.5% < 涨幅 < 10%` 的窗口被误拦，现在可成交；创业板/科创板票涨 9.6% 原先误拦、现在不拦。这是修正，但实时行为会与历史不同。**不动** `style`、不动 ST 判定顺序（改它会动到历史回测 KPI）。

### 6.2 仍未做

| # | 缺口 | 影响 | 建议 |
|---|---|---|---|
| G4 | `style` / `risk_max_position_pct` 只存不用 | 风格、单票仓位上限不参与执行 | 在执行层引入（`risk.py:6-9` 注明这是产品决策后**有意废弃**，改前先确认） |
| G6 | `_sched_release` 排在**同一交易日 15:10** | 当日买入的冻结股当天就解冻，按 A股 T+1 本该次日才可卖。当前只因 15:10 后无执行 job 才未违规 | 日结挪到**次日开盘前**（如 09:15）。已在 `trust._sched_release` 与 `trade.release_t1` 加 ⚠️ 注释，并让 `/api/internal/run-execution` 在 15:00 后拒绝触发 |
| G7 | `_sched_*` 全无节假日判断 | 法定节假日计划层会白烧一轮 LLM | 加交易日历。**不是闭环断点**——链路会走完，只是空转 |
| G8 | 通知是守护线程 + fire-and-forget | 进程重启会静默丢在途通知 | 生产级答案是 outbox 表（先落库、再由单独发送器投递重试）。已在 `notify.notify_trades` docstring 记为已知债 |

---

## 七、结论

1. **核心链路已可用**：登录 → 同步持仓（文本/图片）→ 托管建簿 → 撮合（费用/T+1/风控）→ 托管执行（评级→调仓/止损）→ 委托/成交/归因 → 通知配置 → AI 对话/深度分析，全链路自动化测试通过。
2. **第一轮 5 处缺陷全部修复并回归通过**，其中 BUG-1（港股/ETF 加自选 500）为 P0 阻断级。
3. **第二轮（真实浏览器）4 项前端联调缺陷已修复并经 HTTP/静态回归通过**：BUG-6 首页「我的持仓」接真实数据并补当日盈亏/总资产、BUG-7 AI 对话助手入口可点击、BUG-8 聊天可查实时行情、BUG-9 聊天可触发 trade_agent 深度分析。聊天助手至此具备「行情查询→报价 / 深度分析→调研报告 / 持仓诊断→账户问答」三类分流能力。
4. **第三轮 BUG-10（财团托管无粘贴入口）已修复**：`loadTrust` 不再清空「粘贴持仓快照」按钮，新账号可走「托管总览 → 粘贴持仓快照 → 确认建簿 → 策略设置开启」建簿并启动托管，HTTP 全链路回归通过（scratch 账号建簿+开启后已 reset 清理）。
5. **深度分析真实跑单通过**：6862.HK @2026-09-08 约 12min 出报告（Hold），chat→`/analysis` 全链路与引擎缓存命中均已回归（详见 M10）；前端气泡/首页/托管页卡片的最终视觉效果仍建议浏览器人工过一眼。
6. **遗留**：原 5 项「按现状」缺口中的 **G1（成交通知）、G2（归因盈亏）、G3（内部运维端点）、G5（涨跌停板块化）已全部闭环**（详见 §六）。仍开放：G4（`style`/`risk_max_position_pct` 有意废弃）、G6（T+1 解冻时刻）、G7（无节假日闸门）、G8（通知的 outbox 可靠性）。
7. **第十三轮 R13-1（回测静默空转）已修复并部署**：这是一处**只在回测侧存在、且完全静默**的断链——计划层在结构化输出失败时返回 `None` 而不抛，编排层又把返回值丢掉，于是回测跑完报 `done` 却全程零成交。判据"团队有没有真的动过手"正是回测唯一要验的东西，所以这类静默是**给出反向结论**，比缺功能更严重。修法是让编排层必须看返回值（空计划 = 失败日，连错 3 天中止整轮）+ 结构化调用失败重试一次。**它与前两次（M8 镜像断链、Stage2 候选池）同形**：都不违反任何单点断言，断在"两环之间少了一次核对"。

7. **第四轮（需求增强）**：AI 解析确认页升级为全字段可编辑（名称/代码/股数/成本价/现价 + 券商可用资金），总资产=可用资金+持仓市值实时汇总；新增 `/api/quote/{code}` 轻量行情端点（现价初值自动取实时行情、改代码失焦重取、手改不被覆盖、删除行即排除）。**券商现金进入双簿**：镜像同步/托管快照均接收 `cash` 落库，托管簿由此可用真实现金加仓、卖出现金回流再分配；**空仓托管默认 50 万起步**（全市场/自选按评级自由建仓，实测 2 笔成交流水正确）；**口径去双计**：首页 KPI=镜像真实账户、托管总览接托管簿实时盈亏与明细，两表独立。接口/结构/真实跑单回归均通过，编辑交互与视觉最终效果建议浏览器过一眼。
8. **第五轮（浏览器 UX）**：①**「同步成功」不再是原生 alert**——`toast` 全面改为非阻塞轻提示（顶部浮层、自动消失），且确认落库后先提示再后台刷新，消除"页面自己弹窗/提示迟到"的观感；②**账户问答回答不再截断**——`answer_account_question` 输出上限提到 8000 tokens 并强制逐只完整回答（真实问答已覆盖 6 只完整收尾）；③**托管同步上传截图恢复可用**——同一张图可重复识别、截图模式高亮、识别中转圈提示、解析按钮按图/文本自动判定（不再把 base64 当文本）；④托管同步 Sheet 新增 **「复制首页持仓」** 一键把已同步的真实持仓 + 券商可用资金带入托管起始仓（仍可编辑后确认）。回归：JS `node --check`、`py_compile`、真实登录 HTTP 问答均通过；浏览器最终交互仍建议过一眼。
9. **第六轮（用户逐条 9 项改造 REQ-1~9）**：①托管总览「操作股票范围」改为三态胶囊可选，与「策略设置」页同一 `stock_scope` 字段实时联动（改哪边都同步、进入回读高亮两处）；②「AI 对话助手」卡片调整到「财团托管」下方；③新增「模拟量化」占位入口（空壳视图不报错）；④个股详情 K 线悬停/触摸即显示该日**收盘价**（红涨绿跌，含开高低/量）；⑤详情页新增「返回上一页」按钮（记住来源顶级页，不误回二级页）；⑥持仓列表仓位占比 = 进度条 + **百分比文本，分母为总资产**（现金+市值，后端 `assets_ratio`）；⑦解析确认页**总资产/总市值/浮动盈亏 三栏可编辑**，写入 `broker_mv/broker_pnl` 快照口径作为总览权威口径（总资产输入按 总资产−可用资金 折算市值），任一手改即生效、一键「还原自动」清快照回实时——本轮 scratch 账号 HTTP 实测：镜像 overridden:true、total_assets=现金+快照市值、reset 后回落实时；托管簿同口径写入/还原正确；⑧托管页资产栏/持仓列表样式统一复用首页组件风格；⑨**自选股升级为多组管理**（建组/重命名/删除/当前组胶囊高亮）、「粘贴/截图同步自选」解析预览后可**整组替换当前组（一次一组）**、输入代码/名称/行业词**检索候选点选或回车添加**——本轮 scratch 账号 HTTP 实测：建组/重名/空名拦截、切组落组、改名、删当前组自动回退默认、仅剩 1 组拒绝删除、整组替换去重 + 代码归一、文本解析（DeepSeek）全部正确。⚠️ 测试期间 Wind 报「单日请求次数超限」经排查为 **key 配置指向低配额账号**（当前 config 的 key 为 9/8 换入的新账号）；已把 `.env` 的 `WIND_API_KEY` 切回 9/8 前一直生效的原 key 并重启，Wind 即刻恢复（贵州茅台 1290.88 / 腾讯控股 434.0），名称 NL 检索、纯名称反查、自选行情价复测全部通过——属配置问题非代码缺陷。回归：JS `node --check`、`py_compile`、真实登录 HTTP 全部通过；多组切换/截图同步/图表 tooltip 等视觉效果仍建议浏览器人工过一眼。
10. **第七轮（用户逐条 6 项交互改造 R7-1~6）**：①**重命名/新建分组提示改到真正生效后**——toast 移到 `await loadWatchlist()` 之后（界面切到新组再提示），保存期间按钮禁用防连点；②**自选解析/同步加引导动画**——新增转圈状态行（识别阶段 + 选图读取阶段 + 整组同步阶段各有文案），不再像卡死；③**自选列表改持仓样式行情列**——显示 股票代码/最新价/**涨跌幅/涨跌额**（后端 `list_groups` 补 `change/change_pct`），并新增 60s 静默刷新（仅自选视图可见时拉最新行情更新列表）；④**托管「仅限自选股」可选某一分组**——`trust_configs.stock_scope_group` 落库、执行层取池按组过滤（组被删回退全部）、策略设置 scope=1 时显示分组下拉即改即存——scratch 账号实测：group=白马→仅该组、group=默认→仅该组、组不存在→回退全部，scope≠1 自动清空分组限定；⑤**归因看板与总览资产口径统一**——根因是前端子 Tab/进页从不重拉数据，已让「进入托管视图 / 切换子 Tab」都按面板刷新，三来源（`/api/trust`、`/api/trust/analytics`、`/api/account`）trust 总资产 scratch 实测一致为 308176.0；⑥**检索点选添加后输入框不再被清空**（保留输入并聚焦，便于连续添加）。回归：`py_compile`、JS `node --check`、真实登录 HTTP 全部通过；转圈动画/60s 刷新/分组下拉的视觉效果仍建议浏览器人工过一眼。
11. **第八轮（Wind 后台 get_stock_kline 高频调用优化 R8-1~2）**：真实账号自选 24 只 + 双簿持仓 12 只，任何一次页面刷新 = 每只标的一次 K 线调用（列表/账户/托管/AI 报价取价全部逐只拉 30 天日 K 且无缓存），故 Wind 后台 17:25–17:42 出现 ~80 次 `get_stock_kline`；排查排除后台分析/调度风暴（`engine_runs` 15:18 后无落库、非调度时段）。**修复**：改用 Wind **批量时点快照接口** `get_stock_price_indicators`（多只 1 次调用、最新成交价/前收盘价）——`wind.get_price_snapshots` 单只/多只同价，坏码被 Wind 回填内部 ID → 按「命中请求列表 + 成交价非空」过滤；`account.get_quote` 改快照 + 进程内 **60s TTL 缓存**，新增批量 `get_quotes`，`get_positions`/`list_groups` 一次批量取全。**回归**：monkeypatch 计数（真实 user2）24 只自选冷读=1 次快照、6 只持仓=1 次、热缓存=0 次；HTTP（scratch）600519 price/change/change_pct 与改前完全一致（快照收盘价=日 K 最新收盘）；`py_compile`、服务重启 health 200。
12. **第九轮（用户实测 2 项修复 R9-1~2）**：①**AI 对话框回车不发送**——发送逻辑此前只绑 `.send-btn` click，抽成公共 `sendChatMessage()` 后 click 与输入框 Enter（`preventDefault`）共用，`node --check` 通过；②**「持仓诊断」对账告警**——根因是 `chat.account_context` 是唯一把**双簿求和**当「总资产」注入的消费方（`acc['total_assets']`=镜像+托管），而 user2 托管簿起始仓=真实持仓完整复制（同 6 只/同现金），模型核对「现金+市值=46.96 万」发现与 93.91 万差 46.96 万；前端首页 KPI 早已只取镜像簿、托管簿独立展示。**修复**：上下文改以**真实账户（镜像簿）为总资产口径**，托管簿单列并附「起始仓复制自真实持仓→同源两视图、不得相加；空仓 50 万独立建仓→才独立」口径说明。**回归**：真实 `/api/chat/ask`（user2）发「持仓诊断」开篇即按 469,563.84 元自洽对账、无『不一致/差额约』告警；`py_compile`、服务重启 health 200。
13. **第十轮（按 UI 稿重做登录页 R10-1）**：登录页由「居中深色单卡片」整版换成 UI 稿的**双栏固定浅色**布局（左品牌区 logo/大标题/副标题/3 特性，右登录卡 欢迎登录/手机号/验证码+发码/协议勾选/登录按钮）；配色用登录页作用域 `--login-*` 局部变量写死浅色、**不随 app 深/浅主题**；第三方登录与 +86 区号去掉；**协议勾选门禁**（未勾选登录按钮禁用，handler 内再防护）。四个登录 id 与真实发码/登录 API 不变；登录页类名前缀 `login-` 与主界面侧栏错开。**回归**：`node --check` + served HTML 静态（四 id/`login-agree` 齐、无第三方残留）+ **CDP 无头浏览器真实交互**（双栏渲染尺寸、初始 disabled、填号填码未勾协议仍禁用、勾选点亮、取消回灰、`.checked` 生效；`html[data-theme]` 恒 dark、body 深底不被浅色登录层污染、侧栏 `.brand-logo` 仍 36px 无串扰）全部通过。伪造 token 注入会被 401 自动登出逻辑正确弹回登录页（既有安全行为）；真实验证码登录终检仍建议浏览器人工走一遍。
14. **第十一轮（组合级交易团队改造 R11-1~4）**：托管决策由「评级→清仓/减半/加半仓」硬编码，升级为**两阶段组合级交易团队**——①**研究层组合感知**（`portfolio_context` 注入单标的引擎，PM/Trader/Research Manager 结合全仓做组合视角研究）；②**组合级多智能体图**（组合分析师→风控官→组合经理三节点，读账户快照+各票研究+显式风控，产出协调的次日条件化 `PortfolioPlan`，落库 `TrustPlan` 含完整决策过程）；③**执行层机械再平衡**（触发门控 + 目标占比换股数 + 止损/单票上限/现金协调，无计划回落旧逻辑）；④**手动生成入口 + 次日计划卡片 + HTML 下载报告**（行动决策总览 + 三角色决策过程 + 各标的深度研究）。真实端到端跑通 6 条条件化动作，执行层 5 场景单测、异步机制/报告渲染/决策过程落库单测全过。
15. **第十二轮（2 项修复 + 阿里云部署 R12-1~3）**：①**次日计划 web 预览表格优化**——修 5/6 列网格错位、补「持有」灰标签、加「思路+目标现金」摘要行，对齐下载报告风格；②**计划前数据完整性校验**——`validate_portfolio_data` 双层校验（端点同步 + 异步 worker）持仓现价，缺失即停止并提示（根因是 Wind 瞬时失败导致现价空、无校验仍生成计划）；③**阿里云 ECS 生产部署**——Docker（app:8010 回环 + postgres:16）+ nginx 反代 + certbot HTTPS（`trade.videotonote.cn`）+ `Asia/Shanghai` 时区 + 每日 pg_dump 备份，公网 `https://trade.videotonote.cn` 两端可用、HTTP 301 跳 HTTPS、证书自动续期。
16. **第十三轮（闭环补全）**：以「验证系统真正跑通并闭环」为目标，补齐四环并新增一个**确定性端到端闭环脚本** `scripts/e2e_closure.py`——它不替换 `trade.place_order`、不替换 `db`，只把行情用 `HistoryClock` 钉死，因此「行情确定 → 哪档被穿越确定 → 成交股数确定 → 落库行数确定 → 已实现盈亏确定」全部可断言。**生产 Postgres 实测 34/34**（含一条"跑完在库里不留任何残迹"，保证可反复跑、不污染生产）：一根 `low` 跨越三档的 bar 被**逐档**推进（500→300→200 股，每 tick 至多一笔，不是一笔到底），三笔成交价按**各自档位**的 `fill_price = min(开盘, 该档触发价)` 取到 1510/1510/1500，`orders`/`trades` 各落 3 行、持仓卖到 0 股时行被删除、现金回款正确，归因三路（增量计数器 / `replay_realized` 逐笔重放 / 手算）逐分一致为 −193131.00。同轮新增：三个 `_sched_*` 的心跳日志（同时是容器时区证据）、`TRADE_MASTER_INTERNAL_TOKEN` fail-closed、nginx `^~ /api/internal/ → 404` 规则。**生产实测**：容器 TZ=Asia/Shanghai（`GET /api/internal/scheduler` 的 `next_run_time` 全带 `+08:00`，4 个 job 齐全）、端点鉴权四态（403/403/400/409）全部符合预期、真实用户老簿的归因如实返回 `None` + 提示而非 0。**同轮补测（2026-09-12）**：443 block 补上 `^~ /api/internal/ → 404` 与 `client_max_body_size 24m` 后，公网内部端点由 403 变 **404**、回环仍 **403**（两道防线各就各位）、业务页 200、2MB 请求体不再被 nginx 413 拦下；`_sched_plan` 补了**成功日志**（此前成功静默，只能靠数表行判断）；新增只读取证脚本 `scripts/verify_live_session.py`（零成交时如实标「无法判定」，不把"什么都没有"渲染成"验过了"）；并用执行层**自己的** `build_ladders` 对 2026-09-14 计划做了**开盘前彩排**——读出 6 只票的退出梯子（3/3/3/2/1/2 档）与 3 只进场梯子、**零告警**，证明「计划写得进库、执行层读得出来」。
