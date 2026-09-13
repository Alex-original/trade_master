/**
 * index.html 纯前端渲染的离线冒烟检查。
 *
 * 覆盖三块：
 *   1. 监控条件卡片（`loadMonitorConditions`，喂固定的 `/api/trust/plan/today` 响应断言行 HTML）
 *   2. 重贴快照的覆盖提示文案（`snapshotOverwriteWarning`，纯函数，直接断言返回串）
 *   3. AI 对话分流（`handleChatReply` / `renderConfirmButtons` / `sendChatMessage`）——
 *      尤其是**深度分析必须先确认**：点确认之前绝不能调 `/analysis`
 *
 * 仓库里唯一能跑前端渲染的办法：把 index.html 的内联 <script> 抽出来，在一个
 * 最小 DOM 桩里执行。不连网、不连库、不跑浏览器。
 *
 * 用法：`node scripts/check_frontend.js`（退出码 0 = 全通过）
 */
const fs = require('fs');
const path = require('path');

const HTML_PATH = path.join(__dirname, '..', 'app_frontend', 'index.html');

// ---------------------------------------------------------------- 断言脚手架
let count = 0;
const fails = [];
function check(name, cond, detail = '') {
  count += 1;
  if (cond) {
    console.log('  ✅ ' + name);
  } else {
    fails.push(name + (detail ? '（实际：' + detail + '）' : ''));
    console.log('  ❌ ' + name + (detail ? '  实际：' + detail : ''));
  }
}
function section(title) {
  console.log('\n=== ' + title + ' ===');
}

// ---------------------------------------------------------------- DOM 桩
/* 一个够用的元素节点：有 children、能 appendChild/insertBefore/remove、
   能按 class 选择器 find 后代、click() 会触发已注册的 click 监听。
   刻意不解析 innerHTML——聊天测试靠替换 addMsg 拿到结构真实的节点（见 makeMsgStub）。 */
function matches(el, sel) {
  if (sel.startsWith('.')) return (' ' + el.className + ' ').includes(' ' + sel.slice(1) + ' ');
  return el.tagName === sel;
}
function findAll(el, sel) {
  const out = [];
  for (const c of el.children) {
    if (matches(c, sel)) out.push(c);
    out.push(...findAll(c, sel));
  }
  return out;
}

function makeEl(tag = 'div') {
  const listeners = {};
  const el = {
    tagName: tag, className: '', textContent: '', innerHTML: '', value: '', disabled: false,
    style: {}, children: [], parent: null,
    /* classList 直接读写 className——之前是个空壳（contains 恒 false、toggle 都没有），
       于是"分组行显示/隐藏"这类断言没法写：代码里 classList.toggle('hidden') 之后，
       测试看到的 className 还是空的。让两者同源，断言才对应界面上真实的样子。 */
    classList: {
      add(c) { const s = new Set(el.className.split(/\s+/).filter(Boolean)); s.add(c); el.className = [...s].join(' '); },
      remove(c) { const s = new Set(el.className.split(/\s+/).filter(Boolean)); s.delete(c); el.className = [...s].join(' '); },
      contains(c) { return el.className.split(/\s+/).includes(c); },
      toggle(c, force) {
        const on = (force === undefined) ? !el.classList.contains(c) : !!force;
        if (on) el.classList.add(c); else el.classList.remove(c);
        return on;
      },
    },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    appendChild(n) { n.parent = el; el.children.push(n); return n; },
    // 下载那段是 appendChild(a) → a.click() → removeChild(a) → revokeObjectURL。
    // 少了 removeChild，异常在 revoke **之前**抛出，于是"临时 blob 用完即 revoke"
    // 永远测不出来——真实浏览器里那条路径是好的，桩却在骗人。
    removeChild(n) { n.remove(); return n; },
    insertBefore(n, ref) {
      n.parent = el;
      const i = el.children.indexOf(ref);
      if (i < 0) el.children.push(n); else el.children.splice(i, 0, n);
      return n;
    },
    remove() {
      if (!el.parent) return;
      const i = el.parent.children.indexOf(el);
      if (i >= 0) el.parent.children.splice(i, 1);
    },
    focus() {}, click() { (listeners['click'] || []).forEach((f) => f()); },
    getAttribute() { return null; }, setAttribute() {}, removeAttribute() {},
    querySelector(sel) { const a = findAll(el, sel); return a.length ? a[0] : null; },
    querySelectorAll(sel) { return findAll(el, sel); },
  };
  return el;
}

/* canvas 桩：真画不了像素，但把"缩放后按多大尺寸画的、用什么类型和质量编码的"记下来——
   这恰好是截图上传路径唯一会算错的地方（尺寸算错 → 又撞 nginx 的请求体上限 → 用户看到 413）。
   喂 3000px 的图进去、断言 drawImage 收到的是 1600px，比断言"源码里有 canvas 三个字"有意义得多。 */
function makeCanvas() {
  const el = makeEl('canvas');
  const draws = [];
  el.width = 0;
  el.height = 0;
  el.getContext = () => ({
    fillStyle: '',
    fillRect() {},
    drawImage(_img, x, y, w, h) { draws.push({ x, y, w, h }); },
  });
  el.toDataURL = (type, quality) => {
    el.__encode = { type, quality };
    // 默认给一段够长的假 dataURL；测试要验"编码失败"分支时可临时置成 ''
    return globalThis.__canvasDataUrl !== undefined
      ? globalThis.__canvasDataUrl
      : 'data:image/jpeg;base64,' + 'A'.repeat(400);
  };
  el.__draws = draws;
  return el;
}

/* Image 桩：src 一赋值就在下一拍触发 onload（或 onerror），尺寸由 __imgSize 控制 */
function makeImageStub() {
  return class ImageStub {
    constructor() {
      !globalThis.__imgSize && (globalThis.__imgSize = { w: 1000, h: 1000 });
      this.width = globalThis.__imgSize.w;
      this.height = globalThis.__imgSize.h;
      globalThis.__lastImage = this;
    }
    set src(v) {
      this._src = v;
      setTimeout(() => {
        if (globalThis.__imgError) this.onerror && this.onerror(new Error('decode'));
        else this.onload && this.onload();
      }, 0);
    }
    get src() { return this._src; }
  };
}

/* addMsg 的结构真实替代品。真 addMsg 靠 innerHTML 字符串建子树，桩体解析不了 HTML，
   所以聊天测试里把 addMsg 整个替换掉，直接产出同样形状的节点树。 */
function makeMsgStub(role, text) {
  const bubble = makeEl();
  bubble.className = 'bubble';
  bubble.textContent = text == null ? '' : String(text);
  const time = makeEl();
  time.className = 'msg-time';
  const body = makeEl();
  body.className = 'msg-body';
  body.appendChild(bubble);
  body.appendChild(time);
  const msg = makeEl();
  msg.className = 'msg ' + role;
  msg.appendChild(body);
  return msg;
}

/** 气泡当前显示的文本：setBubble 走 innerHTML，直接赋值走 textContent */
function rendered(msg) {
  const b = msg.querySelector('.bubble');
  return b ? (b.innerHTML || b.textContent) : '';
}

/**
 * @param {object} [opts]
 * @param {object} [opts.console] 自定义 console（测 console.warn 用）
 * @param {boolean} [opts.stubAddMsg] 用 makeMsgStub 替换 addMsg（聊天测试需要）
 */
function makeSandbox(opts = {}) {
  const html = fs.readFileSync(HTML_PATH, 'utf8');
  const m = [...html.matchAll(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g)];
  if (m.length !== 1) throw new Error('index.html 内联 <script> 块数不是 1：' + m.length);
  const code = m[0][1];

  const els = {};
  const canvases = [];      // 每次 createElement('canvas') 都记一笔，供截图上传测试取回
  const document = new Proxy({}, {
    get(_t, k) {
      if (k === 'getElementById') return (id) => (els[id] = els[id] || makeEl());
      if (k === 'querySelector') return () => makeEl();
      if (k === 'querySelectorAll') return () => [];
      if (k === 'createElement') return (tag) => {
        if (tag === 'canvas') { const c = makeCanvas(); canvases.push(c); return c; }
        const el = makeEl(tag);
        // 记一笔现场造的节点：「下载」是 createElement('a') + click() 的，
        // 不记的话断言不到它设的文件名（那只在节点上）
        (globalThis.__created = globalThis.__created || []).push(el);
        return el;
      };
      if (k === 'addEventListener') return () => {};
      return makeEl();
    },
  });
  const noop = () => {};
  const localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
  // toast 靠 rAF 做退场动画，沙箱里没有它。给一个立刻执行的回调：测试断言的
  // 是"toast 有没有被调用、说了什么"，不是动画帧。
  const raf = (fn) => { if (typeof fn === 'function') fn(); return 0; };
  const window = { document, localStorage, addEventListener: noop, location: { hash: '' },
                   requestAnimationFrame: raf,
                   // 「预览完整报告」走 window.open(blobUrl)。桩成记录调用即可——
                   // 要断言的是"打开的是不是当前那份 blob"，不是浏览器真的开没开标签页。
                   open: (u) => { globalThis.__opened = u; return null; },
                   navigator: { clipboard: { writeText: () => Promise.resolve() } } };

  // 把要测的函数挑出来，并把 api / getToken / addMsg 换成本地桩（都是函数声明，可重赋值）。
  // 计时器必须一起桩掉：脚本顶层注册了若干 setInterval，用真实现的话 node 永不退出。
  const patch = [
    '\n; getToken = () => "tok";',
    // realApi：留着真的 api() 不换桩——测的是它自己把 413 翻成人话的那段逻辑
    opts.realApi ? '' : '\n; api = async (...a) => {',
    opts.realApi ? '' : '  (globalThis.__apiCalls = globalThis.__apiCalls || []).push(a[0]);',
    // 第二个实参（options，含 POST body）也留一份：只记 URL 的话，"起跑时到底带了哪些范围参数"
    // 就无从断言——而这正是这次要守的东西。
    opts.realApi ? '' : '  (globalThis.__apiBodies = globalThis.__apiBodies || []).push(a[1]);',
    opts.realApi ? '' : '  return globalThis.__apiHandler ? globalThis.__apiHandler(...a) : globalThis.__payload;',
    opts.realApi ? '' : '};',
    opts.stubAddMsg ? '\n; addMsg = (role, text) => { const m = makeMsgStub(role, text); (globalThis.__msgs = globalThis.__msgs || []).push(m); return m; };' : '',
    '\n; return { loadMonitorConditions, loadAnalytics, snapshotOverwriteWarning, handleChatReply,',
    '   renderConfirmButtons, sendChatMessage, chatHistory,',
    '   renderBacktestProgress, renderBacktestResult, renderBacktestList, btPreview,',
    '   btStart, setBtScope, btLoadGroups, btRenderGroupChips, btToggleGroup,',
    // 用取值器而不是直接把数组返回去：btLoadGroups 里是 `btGroupChips = btGroupChips.filter(...)`，
    // 那是**重新绑定**，直接返回数组的话导出的是旧对象，勾选状态看起来永远不变。
    '   get btGroupChips() { return btGroupChips; },',
    // 流程报告：日期清单是模块级 let，同样得用取值器导出，否则断言看到的是旧数组
    '   btLoadReports, btReportPick, btReportOpen, btReportDownload, btSyncReports,',
    '   btLoadTrades, btLoadMonitor, tradeRowEl, monitorRowsHtml,',
    '   get btReportDates() { return btReportDates; },',
    '   get btReportDate() { return btReportDate; },',
    '   get btReportBlobUrl() { return btReportBlobUrl; },',
    '   set btCurrentRunIdForTest(v) { btCurrentRunId = v; },',
    // 移动端 Tab 归属表是 const 对象，直接导出即可（断言它归得对不对，见「移动端底部 Tab」一节）
    '   toBase64, api, MOBILE_TAB_OF };',
  ].join('');
  const factory = new Function(
    'document', 'window', 'localStorage', 'navigator', 'console', 'makeMsgStub',
    'setInterval', 'setTimeout', 'clearInterval', 'clearTimeout', 'alert', 'confirm', 'fetch', 'location',
    'requestAnimationFrame', 'Image', 'URL', 'FileReader',
    code + patch);
  const fns = factory(
    document, window, localStorage, window.navigator, opts.console || console, makeMsgStub,
    () => 0, () => 0, noop, noop, noop, () => true,
    opts.fetch || (() => Promise.reject(new Error('no net'))),
    { hash: '' }, raf,
    opts.Image || makeImageStub(),
    // createObjectURL 每次给一个新串、revoke 记一笔：流程报告内嵌的就是这个 blob URL，
    // 「换日期时旧的有没有被 revoke」是这里唯一会泄漏的东西（每切一次日期泄一份报告）
    { createObjectURL: () => 'blob:stub' + (globalThis.__blobN = (globalThis.__blobN || 0) + 1),
      revokeObjectURL: (u) => { (globalThis.__revoked = globalThis.__revoked || []).push(u); } },
    opts.FileReader || class { readAsDataURL() { this.result = 'data:text/plain;base64,VEVYVA=='; setTimeout(() => this.onload && this.onload(), 0); } });
  return Object.assign(fns, { els, makeEl, canvases });
}

/** 跑一次 loadMonitorConditions，返回它渲染到的元素表 */
function loadCard(apiPayload) {
  const sb = makeSandbox();
  const orig = globalThis.__payload;
  globalThis.__payload = apiPayload;
  return sb.loadMonitorConditions().then(() => {
    globalThis.__payload = orig;
    return sb.els;
  });
}

/** 建一个聊天用沙箱：记录 api 调用、捕获 console.warn、预置输入框内容 */
function chatSandbox(opts = {}) {
  const warns = [];
  const cap = Object.create(console);   // 原型链保留 log/error，只遮蔽 warn
  cap.warn = (...a) => warns.push(a);
  const sb = makeSandbox({ stubAddMsg: true, console: cap });
  globalThis.__apiCalls = [];
  globalThis.__msgs = [];
  globalThis.__apiHandler = opts.apiHandler || null;
  globalThis.__payload = opts.payload || {};
  sb.els.chatInput = sb.makeEl();
  sb.els.chatInput.value = opts.input === undefined ? '测试问题' : opts.input;
  sb.els.chatMessages = sb.makeEl();
  return Object.assign(sb, { warns, apiCalls: () => globalThis.__apiCalls, msgs: () => globalThis.__msgs });
}

// ---------------------------------------------------------------- 夹具
// 生产真实的梯子（2026-09-11 盘前计划 518880.SH 那一套），档位字段由 get_monitor_conditions 产出
const LADDER_PAYLOAD = {
  trade_date: '2026-09-14', code_count: 1, actions: [
    { code: '518880.SH', name: '黄金ETF华安', action: 'reduce', target_weight: 0.138,
      trigger_type: 'price_below', trigger_price: 8.90, reason: '放量跌破8.90先减约1/4',
      kind: 'exit', tier_index: 1, tier_count: 3, is_first: true,
      price: 8.85, prev_close: 9.03, volume_ratio: 1.803, triggered: true },
    { code: '518880.SH', name: '黄金ETF华安', action: 'reduce', target_weight: 0.09,
      trigger_type: 'price_below', trigger_price: 8.76, volume_ratio_min: 1.5,
      reason: '有效跌破8.76–8.82再减至半仓以下',
      kind: 'exit', tier_index: 2, tier_count: 3, is_first: false,
      price: 8.85, prev_close: 9.03, volume_ratio: 1.803, triggered: false },
    { code: '518880.SH', name: '黄金ETF华安', action: 'sell', target_weight: 0.0,
      trigger_type: 'price_below', trigger_price: 8.75, reason: '硬止损',
      kind: 'exit', tier_index: 3, tier_count: 3, is_first: false,
      price: 8.85, prev_close: 9.03, volume_ratio: 1.803, triggered: false },
  ],
};

// 旧计划：单档、没有 tier_* / code_count 字段
const LEGACY_PAYLOAD = {
  trade_date: '2026-09-11', actions: [
    { code: '518880.SH', name: '黄金ETF华安', action: 'reduce', target_weight: 0.138,
      trigger_type: 'price_below', trigger_price: 8.90, reason: 'x',
      price: 8.85, prev_close: 9.03, triggered: true },
  ],
};

// 后端 request_deep_analysis 工具产出的确认请求（kind=analysis_confirm）
const CONFIRM_PAYLOAD = {
  kind: 'analysis_confirm', ticker: '518880.SH', name: '黄金ETF华安', date: '2026-09-14',
  question: '是否对 黄金ETF华安（518880.SH）做深度分析？将运行多智能体研究，约耗时 10-15 分钟。',
};

/* 回测面板夹具。形状与 ``GET /api/backtest/{id}``、``/result``、``/list`` 的真实响应一致
   （见 app/backtest.py 的 get_progress / get_result / list_runs）。 */
const PROGRESS_RUNNING = {
  run_id: 7, status: 'running', stage: 'executing',
  message: '第 6 / 15 天：执行 2026-03-10 的计划', done: 6, total: 15,
  cancel_requested: false, start_date: '2026-03-02', end_date: '2026-03-20',
  has_result: false, total_return: null, elapsed_text: '约 12 分钟',
};
const PROGRESS_DONE = Object.assign({}, PROGRESS_RUNNING, {
  status: 'done', stage: 'done', message: '完成：15 个交易日', done: 15, has_result: true,
  elapsed_text: '约 2.3 小时',
});
const PROGRESS_FAILED = Object.assign({}, PROGRESS_RUNNING, {
  status: 'failed', stage: 'loading', message: 'Wind 取数失败：429', done: 3,
});
// 口径升级前的老记录：后端给不出耗时（created_at 缺失）→ 整段省略，不能显示成 0
const PROGRESS_NO_ELAPSED = Object.assign({}, PROGRESS_RUNNING, { elapsed_text: '' });

// 声明标题（与 app/backtest_calc.py 的 DISCLOSURES 一一对应；后端加一条这里也要跟着加）
const DISCLOSURE_TITLES = [
  '撮合口径', '量比口径', '复权口径', '涨跌停 / 停牌', '费用与摩擦',
  '事后选样偏差', '数据工具降级', '研究缓存', '耗时标定与免责',
  // 2026-09-11 补的两条：一条说清楚"建仓范围只是提示词约束"，一条说清楚"回测的空仓起步
  // 与实盘走的不是同一条路"。缺了后者，用户会拿回测的空仓表现去预期实盘。
  '建仓范围是提示词约束', '空仓起步的路径差异',
  // 2026-09-12 补的一条：首日天然没有可执行计划。缺了它，用户会把"复制持仓起步时首日超配"
  // 读成团队失职，也会把首日的盈亏当成团队的决策结果。
  '回测首日没有可执行计划',
];
const DISCLOSURES_FIXTURE = DISCLOSURE_TITLES.map((t, i) => ({
  key: 'k' + i, title: t,
  text: i === 0 ? '日内触发 + 跳空按开盘：日线无法还原当日 high/low 的先后顺序。'
      : (i === 2 ? '全程**后复权**：绝对价位被每只票的常数缩放。' : '口径说明' + i + '。'),
}));

const BT_STEPS = Array.from({ length: 15 }, (_, i) => ({
  trade_date: '2026-03-' + String(2 + i).padStart(2, '0'),
  cash: 100000 - i * 100, market_value: 110000 + i * 900,
  total_assets: 210000 + i * 230, day_pnl: i % 3 === 0 ? -120.5 : 430.25,
  realized_pnl: 0, fees: 25, trade_count: i % 4 === 0 ? 1 : 0,
  status: i === 9 ? 'degraded' : 'ok', error: i === 9 ? '新闻接口不可用' : '',
}));

const BT_RESULT = {
  run_id: 7, status: 'done', start_date: '2026-03-02', end_date: '2026-03-20',
  init_mode: 'cash', init_basis: 200000.0, steps: BT_STEPS,
  result: {
    day_count: 15, init_basis: 200000.0, final_assets: 213450.5, total_pnl: 13450.5,
    total_return: 0.06725, max_drawdown: 0.0412, total_fees: 386.4, realized_pnl: 5120.0,
    trade_count: 11, equity_curve: [],
    degraded_days: [{ trade_date: '2026-03-11', status: 'degraded', error: '新闻接口不可用' }],
    baseline_equal_weight: { total_assets: 209800.0, total_return: 0.049 },
    baseline_index: { total_assets: 203100.0, total_return: 0.0155 },
    excess_return: 0.0183, excess_return_index: 0.0518,
    // 请求 45 只、实际只有 40 只有行情 —— 后端 ``_finalize`` 落的三个字段。
    // 这是"标的池被悄悄换掉"的唯一凭证，前端必须原样画出来。
    universe_requested: 45, universe_loaded: 40,
    missing_symbols: ['002594.SZ', '600030.SH', '601398.SH', '000002.SZ', '600519.SH'],
    warnings: ['002594.SZ：历史行情取不到（NoMarketDataError: no rows），该标的全程不参与'],
    disclosures: DISCLOSURES_FIXTURE,
    start_date: '2026-03-02', end_date: '2026-03-20',
  },
};

// 全部有数据的正常轮：告警块必须**整块隐藏**，不能留一个空边框在 KPI 上面
const BT_FULL = Object.assign({}, BT_RESULT, {
  result: Object.assign({}, BT_RESULT.result, {
    universe_requested: 45, universe_loaded: 45, missing_symbols: [], warnings: [],
  }),
});

// 亏损那一轮：红绿方向必须反过来
const BT_LOSS = Object.assign({}, BT_RESULT, {
  result: Object.assign({}, BT_RESULT.result, {
    final_assets: 181660.0, total_pnl: -18340.0, total_return: -0.0917,
    max_drawdown: 0.128, excess_return: -0.012,
  }),
});

const BT_LIST = [
  { run_id: 8, status: 'running', stage: 'executing', message: '', start_date: '2026-03-02',
    end_date: '2026-03-20', init_mode: 'cash', init_cash: 200000.0, done: 6, total: 15,
    total_return: null, max_drawdown: null, elapsed_text: '约 12 分钟' },
  { run_id: 7, status: 'done', stage: 'done', message: '', start_date: '2026-02-02',
    end_date: '2026-02-27', init_mode: 'copy', init_cash: 0.0, done: 18, total: 18,
    total_return: 0.06725, max_drawdown: 0.0412, elapsed_text: '约 2.1 小时' },
];

const rowsOf = (html) => html.match(/<div class="table-row[^"]*"/g) || [];

// ---------------------------------------------------------------- 检查
(async () => {
  section('1. 多档梯子：逐档一行');
  const L = await loadCard(LADDER_PAYLOAD);
  const lHtml = L['monitor-rows'].innerHTML;
  const lRows = rowsOf(lHtml);
  check('三档渲染成三行', lRows.length === 3, String(lRows.length));
  check('第 1 行是组首（无续行样式）', lRows[0] === '<div class="table-row tier-open"', lRows[0]);
  check('第 2/3 行是续行', /tier-cont/.test(lRows[1]) && /tier-cont/.test(lRows[2]));
  check('组末恢复实线（第 3 行不带 tier-open）', !/tier-open/.test(lRows[2]), lRows[2]);
  check('续行显示 └ 第N档', /└ 第2档/.test(lHtml) && /└ 第3档/.test(lHtml));
  check('首行标注共几档', /518880\.SH · 共 3 档/.test(lHtml));
  check('每档各自的目标占比',
    /→ 目标 13\.8%/.test(lHtml) && /→ 目标 9\.0%/.test(lHtml) && /→ 目标 0\.0%/.test(lHtml));
  check('量能门槛写进条件并带现量比', /量比≥1\.5（现 1\.80）/.test(lHtml));
  check('无门槛的档不显示量比', (lHtml.match(/量比≥/g) || []).length === 1);
  check('每档各自的触发状态',
    (lHtml.match(/class="triggered"/g) || []).length === 1
    && (lHtml.match(/class="monitoring"/g) || []).length === 2);
  check('副标题按标的计数 + 档数',
    L['monitor-sub'].textContent === '2026-09-14 执行 · 1 只标的 / 3 档条件',
    L['monitor-sub'].textContent);

  section('2. 旧计划：渲染与今天一致');
  const O = await loadCard(LEGACY_PAYLOAD);
  const oHtml = O['monitor-rows'].innerHTML;
  const oRows = rowsOf(oHtml);
  check('单档仍是一行', oRows.length === 1, String(oRows.length));
  check('行上没有任何档位样式', oRows[0] === '<div class="table-row"', oRows[0]);
  check('不显示续行标记 / 档数 / 量比',
    !/tier-cont|└ 第|共 \d+ 档|量比≥/.test(oHtml));
  check('触发条件与目标占比逐字保留',
    /<span class="plan-trigger">跌破 8\.9<\/span>/.test(oHtml) && /→ 目标 13\.8%/.test(oHtml));
  check('标的代码不带档数后缀', /<span class="stock-code">518880\.SH<\/span>/.test(oHtml));
  check('副标题按标的计数', O['monitor-sub'].textContent === '2026-09-11 执行 · 1 只标的',
    O['monitor-sub'].textContent);

  section('3. 重贴快照的覆盖提示');
  const warn = makeSandbox().snapshotOverwriteWarning;
  check('没有托管状态 → 不提示', warn(null) === '' && warn(undefined) === '');
  check('还没建簿 → 不提示（首次粘贴不该被吓一跳）',
    warn({ book_created: false, trade_count: 9, plan_count: 9 }) === '');

  const W = warn({ book_created: true, trade_count: 2, order_count: 3, plan_count: 1 });
  check('已建簿：报出三个计数',
    /成交 2 笔/.test(W) && /委托 3 条/.test(W) && /次日行动计划 1 份/.test(W));
  check('说明会先清空（含监控条件）',
    /先清空/.test(W) && /监控条件随计划一并清空/.test(W));
  check('说明会覆盖当前持仓', /覆盖当前持仓/.test(W) && /以新快照为准/.test(W));
  check('说明不可恢复', /不可恢复/.test(W));
  check('说明托管开关会被关掉', /关闭托管开关/.test(W) && /策略设置/.test(W));
  check('分行返回（横幅 pre-line 渲染 / confirm 直接复用）',
    W.split('\n').length === 4, String(W.split('\n').length));

  // 计数为 0 但确实已建簿（持仓是用户手打进去的，被覆盖照样有损失）→ 仍要提示
  const W0 = warn({ book_created: true, trade_count: 0, order_count: 0, plan_count: 0 });
  // 措辞换成「以本次快照为准重建托管簿」（没有流水可清，就不说「覆盖当前持仓」）
  check('已建簿但无流水 → 仍提示重建与关开关',
    W0 !== '' && /重建托管簿/.test(W0) && /以新快照为准/.test(W0) && /关闭托管开关/.test(W0));
  check('无流水时不提「已有数据」/ 不报 0 笔', !/已有数据/.test(W0) && !/成交 0 笔/.test(W0));

  check('字段缺省的半截响应不炸（get_trust 老版本可能没这三个计数）',
    typeof warn({ book_created: true }) === 'string');

  section('4. 深度分析必须用户确认后才发起');
  {
    const sb = chatSandbox();
    const msg = makeMsgStub('ai', '正在查询账户数据…');
    await sb.handleChatReply(CONFIRM_PAYLOAD, msg);
    check('气泡显示后端给的确认问句', rendered(msg) === CONFIRM_PAYLOAD.question, rendered(msg));
    const box = msg.querySelector('.chat-confirm');
    check('气泡下方出现确认按钮组', !!box);
    check('两个按钮：确认 + 取消',
      !!box && box.querySelector('.chat-confirm-ok') && box.children.length === 2,
      box ? String(box.children.length) : 'null');
    check('★ 未点确认前绝不提交 /analysis',
      sb.apiCalls().filter((u) => String(u).includes('/analysis')).length === 0,
      JSON.stringify(sb.apiCalls()));
    check('确认问句进了对话历史（后续轮次能看见）',
      sb.chatHistory.length === 1 && sb.chatHistory[0].content === CONFIRM_PAYLOAD.question,
      JSON.stringify(sb.chatHistory));

    // 取消：不发起分析
    const cancel = box.querySelectorAll('button').find((b) => b.textContent === '取消');
    cancel.click();
    check('点取消 → 按钮消失', msg.querySelector('.chat-confirm') === null);
    check('点取消 → 气泡改文案', /已取消/.test(rendered(msg)), rendered(msg));
    check('点取消 → 仍未提交 /analysis',
      sb.apiCalls().filter((u) => String(u).includes('/analysis')).length === 0,
      JSON.stringify(sb.apiCalls()));
  }
  {
    // 确认：此时才提交 /analysis
    const sb = chatSandbox();
    const msg = makeMsgStub('ai', '正在查询账户数据…');
    await sb.handleChatReply(CONFIRM_PAYLOAD, msg);
    const ok = msg.querySelector('.chat-confirm-ok');
    ok.click();
    check('点确认 → 提交 /analysis（且带 ticker）',
      sb.apiCalls().some((u) => String(u).includes('/analysis')),
      JSON.stringify(sb.apiCalls()));
    check('点确认 → 按钮消失', msg.querySelector('.chat-confirm') === null);
  }
  {
    // 幂等：重复渲染不叠加按钮组
    const sb = chatSandbox();
    const msg = makeMsgStub('ai', 'x');
    sb.renderConfirmButtons(msg, CONFIRM_PAYLOAD);
    sb.renderConfirmButtons(msg, CONFIRM_PAYLOAD);
    check('重复调用只渲染一组按钮',
      msg.querySelectorAll('.chat-confirm').length === 1,
      String(msg.querySelectorAll('.chat-confirm').length));
  }

  section('5. 未知 kind 不再被静默吞掉');
  {
    const sb = chatSandbox();
    const msg = makeMsgStub('ai', '正在查询账户数据…');
    await sb.handleChatReply({ kind: 'plan' }, msg);
    check('落到兜底文案', rendered(msg) === '抱歉，暂时无法回答。', rendered(msg));
    check('★ console.warn 留下线索', sb.warns.length === 1, String(sb.warns.length));
    check('warn 里带上了未知的 kind', String(sb.warns[0] && sb.warns[0][1]) === 'plan');
  }

  section('6. 连点发送不会产生多个并发请求');
  {
    // api 永不 resolve：模拟助手正在跑工具循环（可能 20-60 秒）
    const sb = chatSandbox({ apiHandler: () => new Promise(() => {}) });
    sb.sendChatMessage();   // 同步跑到 await api(...)，请求已发出
    sb.sendChatMessage();   // chatBusy 仍为 true → 直接返回
    sb.sendChatMessage();
    check('三次连点只发出一次请求', sb.apiCalls().length === 1, String(sb.apiCalls().length));
    check('只插入了 2 条消息（1 用户 + 1 AI）', sb.msgs().length === 2, String(sb.msgs().length));
  }

  section('7. 历史回测面板：进度 / 基准 / 逐日净值 / 声明');
  {
    // ---- 进度 ----
    const sb = makeSandbox();
    sb.renderBacktestProgress(PROGRESS_RUNNING);
    check('运行中：进度文案来自后端 message',
      sb.els['bt-progress-msg'].textContent === '第 6 / 15 天：执行 2026-03-10 的计划',
      sb.els['bt-progress-msg'].textContent);
    check('运行中：进度条按天数推进（6/15 = 40%）',
      sb.els['bt-progress-fill'].style.width === '40%',
      sb.els['bt-progress-fill'].style.width);
    check('运行中：取消按钮可用', sb.els['bt-cancel-btn'].disabled === false);
    check('运行中：取消按钮文案就是「取消」',
      sb.els['bt-cancel-btn'].textContent === '取消', sb.els['bt-cancel-btn'].textContent);
    check('运行中：阶段显示「盘中执行」',
      sb.els['bt-progress-step'].textContent === '盘中执行', sb.els['bt-progress-step'].textContent);
    // 「这次跑了多久」——长跑里唯一的体感锚点，必须在界面上，不能只躺在库里
    check('★ 运行中：进度计数同时给出天数与已跑时长',
      sb.els['bt-progress-count'].textContent === '6 / 15 个交易日 · 已跑 约 12 分钟',
      sb.els['bt-progress-count'].textContent);
    // 老记录（口径升级前建的）后端给不出耗时 → 整段省略，不能凑一个 0 出来
    const sbN = makeSandbox();
    sbN.renderBacktestProgress(PROGRESS_NO_ELAPSED);
    check('★ 拿不到耗时就不显示（宁可只留天数，也不给一个像结论的 0）',
      sbN.els['bt-progress-count'].textContent === '6 / 15 个交易日'
      && !/0|已跑|用时/.test(sbN.els['bt-progress-count'].textContent),
      sbN.els['bt-progress-count'].textContent);

    const sbC = makeSandbox();
    sbC.renderBacktestProgress(Object.assign({}, PROGRESS_RUNNING, { cancel_requested: true }));
    check('已请求取消：按钮禁用且改文案（防连点）',
      sbC.els['bt-cancel-btn'].disabled === true
      && sbC.els['bt-cancel-btn'].textContent === '正在停止…',
      sbC.els['bt-cancel-btn'].textContent);

    const sbD = makeSandbox();
    sbD.renderBacktestProgress(PROGRESS_DONE);
    check('★ 已完成：取消按钮必须禁用（对已结束的 run 点取消没有意义）',
      sbD.els['bt-cancel-btn'].disabled === true);
    check('已完成：阶段标 done 样式',
      /plan-progress-step done/.test(sbD.els['bt-progress-step'].className),
      sbD.els['bt-progress-step'].className);
    check('已完成：进度条画满',
      sbD.els['bt-progress-fill'].style.width === '100%',
      sbD.els['bt-progress-fill'].style.width);
    // 终态说「用时」（结论），在跑说「已跑」（当下）——同一个数，两种语气
    check('★ 已完成：耗时改口为「用时」（不是「已跑」）',
      sbD.els['bt-progress-count'].textContent === '15 / 15 个交易日 · 用时 约 2.3 小时',
      sbD.els['bt-progress-count'].textContent);

    const sbF = makeSandbox();
    sbF.renderBacktestProgress(PROGRESS_FAILED);
    check('失败：按钮禁用 + 错误样式 + 如实显示后端消息',
      sbF.els['bt-cancel-btn'].disabled === true
      && /plan-progress-fill error/.test(sbF.els['bt-progress-fill'].className)
      && sbF.els['bt-progress-msg'].textContent === 'Wind 取数失败：429',
      sbF.els['bt-progress-msg'].textContent);

    // ---- 结果 ----
    const sr = makeSandbox();
    sr.renderBacktestResult(BT_RESULT);
    const dHtml = sr.els['bt-disclosures'].innerHTML;
    // 条数由**后端给什么就渲染什么**，前端不写死数字；这里额外钉住夹具与后端常量同宽，
    // 免得后端加了声明而这段断言还在替旧的条数背书。
    check('★ 声明全部渲染出来（一条都不能漏）',
      (dHtml.match(/class="bt-disc"/g) || []).length === BT_RESULT.result.disclosures.length
      && BT_RESULT.result.disclosures.length === DISCLOSURE_TITLES.length,
      String((dHtml.match(/class="bt-disc"/g) || []).length));
    check('★ 声明标题逐条出现在 DOM 文本里',
      DISCLOSURE_TITLES.every((t) => dHtml.includes(t)),
      DISCLOSURE_TITLES.filter((t) => !dHtml.includes(t)).join(','));
    check('声明正文也带上了（不是只渲染标题）',
      dHtml.includes('日线无法还原当日 high/low 的先后顺序')
      && dHtml.includes('全程**后复权**'),
      dHtml.slice(0, 120));

    const bHtml = sr.els['bt-baseline'].innerHTML;
    check('★ 基准对比三行：策略 / 等权买入持有 / 沪深300',
      rowsOf(bHtml).length === 5   // 表头 + 3 行 + 超额
      && bHtml.includes('AI 托管策略') && bHtml.includes('等权买入持有（主基准）')
      && bHtml.includes('沪深300（次基准）'),
      String(rowsOf(bHtml).length));
    check('★ 超额收益两个都展示（相对主基准 / 相对沪深300）',
      bHtml.includes('超额收益') && bHtml.includes('1.83%') && bHtml.includes('5.18%'),
      bHtml);
    check('基准收益按传入值渲染',
      bHtml.includes('4.90%') && bHtml.includes('1.55%'), bHtml);

    const eHtml = sr.els['bt-equity'].innerHTML;
    check('★ 逐日净值表行数 == steps.length（表头另算）',
      rowsOf(eHtml).length - 1 === BT_RESULT.steps.length,
      `${rowsOf(eHtml).length - 1} vs ${BT_RESULT.steps.length}`);
    check('降级的那天如实标出来（不能悄悄混进正常结果）',
      /table-row bt-degraded/.test(eHtml));
    check('净值表副标题给出行数', sr.els['bt-equity-sub'].textContent === '15 行',
      sr.els['bt-equity-sub'].textContent);

    const kHtml = sr.els['bt-kpis'].innerHTML;
    check('KPI 四项：总资产 / 总盈亏 / 收益率 / 费用',
      ['期末总资产', '总盈亏', '总收益率', '累计交易费用'].every((t) => kHtml.includes(t)), kHtml);
    check('盈利带 + 号且不标成跌色',
      kHtml.includes('+13,450.50') && !/num down/.test(kHtml), kHtml);
    check('回撤以正比例展示', kHtml.includes('4.12%'), kHtml);

    // 亏损方向必须反过来——红绿标错是这类页面最伤信任的一类 bug
    const sl2 = makeSandbox();
    sl2.renderBacktestResult(BT_LOSS);
    const lk = sl2.els['bt-kpis'].innerHTML;
    check('亏损时金额带 - 号并标跌色',
      lk.includes('-18,340.00') && /kpi-value num down/.test(lk), lk);
    check('亏损时基准表里的策略行也标跌色',
      /price-cell down/.test(sl2.els['bt-baseline'].innerHTML),
      sl2.els['bt-baseline'].innerHTML);
    check('亏损时超额为负也标跌色',
      sl2.els['bt-baseline'].innerHTML.includes('-1.20%'), sl2.els['bt-baseline'].innerHTML);
    check('标题带区间、副标题带天数与笔数',
      sr.els['bt-result-title'].textContent === '回测结果 · 2026-03-02 至 2026-03-20'
      && sr.els['bt-result-sub'].textContent
        === '15 个交易日 · 标的 40/45 只 · 成交 11 笔 · 起始基准 200,000.00',
      sr.els['bt-result-sub'].textContent);

    // ---- 取数告警：**模拟样本被悄悄换掉**，前端必须把它画在 KPI 上方 ----
    const wHtml = sr.els['bt-warnings'].innerHTML;
    check('★ 请求数与实际参与数都画出来（45 请求 / 40 实际 / 缺 5）',
      wHtml.includes('请求 45 只') && wHtml.includes('实际参与 40 只') && wHtml.includes('缺 5 只'),
      wHtml);
    check('★ 后端给的 warning 原文一条不落地渲染',
      BT_RESULT.result.warnings.every((w) => wHtml.includes(w)), wHtml);
    check('缺失标的逐个列出（不是只说"有标的缺失"）',
      BT_RESULT.result.missing_symbols.every((c) => wHtml.includes(c)), wHtml);
    check('告警块是**显示**状态（class 里没有 hidden）',
      sr.els['bt-warnings'].className === 'bt-warnlist', sr.els['bt-warnings'].className);

    const sf = makeSandbox();
    sf.renderBacktestResult(BT_FULL);
    check('★ 全部有数据时告警块整块隐藏（不留空边框、不喊狼来了）',
      sf.els['bt-warnings'].innerHTML === ''
      && /hidden/.test(sf.els['bt-warnings'].className)
      && sf.els['bt-result-sub'].textContent.includes('标的 45 只'),
      sf.els['bt-warnings'].className + ' | ' + sf.els['bt-result-sub'].textContent);

    // ---- 历史列表 ----
    const sl = makeSandbox();
    sl.renderBacktestList(BT_LIST);
    const lHtml = sl.els['bt-list'].innerHTML;
    check('列表逐行渲染 + 表头', rowsOf(lHtml).length === 3, String(rowsOf(lHtml).length));
    check('未跑完的那条显示进度而非收益率',
      lHtml.includes('6 / 15') && lHtml.includes('运行中'), lHtml);
    check('跑完的那条给出收益率与回撤',
      lHtml.includes('6.73%') && lHtml.includes('4.12%'), lHtml);
    check('起始状态如实标注', lHtml.includes('空仓起步') && lHtml.includes('复制起始持仓'), lHtml);
    // 列表里也要能一眼看出「哪条还在跑、已经跑了多久」——决定要不要重跑就靠它
    check('★ 列表带耗时：在跑的说「已跑」、跑完的说「用时」',
      lHtml.includes('已跑 约 12 分钟') && lHtml.includes('用时 约 2.1 小时'),
      lHtml);
    {
      // 老记录（无 elapsed_text）只渲染天数，不渲染耗时行，也不出现 "undefined"
      const slOld = makeSandbox();
      slOld.renderBacktestList([{
        run_id: 1, status: 'done', stage: 'done', message: '', start_date: '2026-01-05',
        end_date: '2026-01-16', init_mode: 'cash', init_cash: 100000.0, done: 10, total: 10,
        total_return: 0.01, max_drawdown: 0.02, elapsed_text: '',
      }]);
      const oldHtml = slOld.els['bt-list'].innerHTML;
      check('★ 老记录拿不到耗时：不渲染耗时行、不出现 undefined',
        !/已跑|用时/.test(oldHtml) && !/undefined/.test(oldHtml),
        oldHtml);
    }

    const slEmpty = makeSandbox();
    slEmpty.renderBacktestList([]);
    check('没有记录时给一句人话而不是空白表',
      slEmpty.els['bt-list'].innerHTML.includes('还没有回测记录'),
      slEmpty.els['bt-list'].innerHTML);

    // ---- 探数：起跑前的门禁在 UI 上要真的拦得住 ----
    // ⚠️ 第 6 节把 ``__apiHandler`` 设成了"永不 resolve"的桩，而它是挂在 globalThis 上的，
    // 会一直留到这里。不清掉的话 ``btPreview`` 里的 ``await api(...)`` 永远不返回，
    // 异步 IIFE 就此挂起——**node 会静默退出 0，连汇总行都不打**（最难查的一种"通过"）。
    globalThis.__apiHandler = null;
    globalThis.__payload = {
      trading_days: 15, universe_size: 45, stage1_runs: 675, llm_calls: 12825,
      est_seconds: 39900, est_label: '约 11 小时',
      probe: true, data_available: 40, data_missing: 5,
      missing_symbols: ['002594.SZ'], missing_held: [],
      blocking: true, block_reason: '45 只标的里有 30 只在所选区间内取不到行情',
      warnings: ['45 只标的里有 30 只在所选区间内取不到行情'],
    };
    const sp = makeSandbox();
    await sp.btPreview(true);
    const pvUrl = globalThis.__apiCalls[globalThis.__apiCalls.length - 1];
    check('★ 探数走的是 probe=1（点了「检查数据」才真的去打接口）',
      pvUrl.includes('probe=1'), pvUrl);
    check('★ 门禁拦下时**直接禁用开始按钮**，并把理由挂在 title 上',
      sp.els['bt-start-btn'].disabled === true
      && sp.els['bt-start-btn'].title === '45 只标的里有 30 只在所选区间内取不到行情',
      String(sp.els['bt-start-btn'].disabled));
    check('探数结果如实显示"可用 40/45（缺 5）"',
      sp.els['bt-preview-box'].innerHTML.includes('可用 <b>40</b>/45 只')
      && sp.els['bt-preview-box'].innerHTML.includes('缺 5 只'),
      sp.els['bt-preview-box'].innerHTML);
    check('探数按钮用完要解禁（否则一次失败就再也点不动）',
      sp.els['bt-probe-btn'].disabled === false, String(sp.els['bt-probe-btn'].disabled));

    // 普通预览（不探数）不该被上一次的 block 一直卡着按钮
    globalThis.__payload = {
      trading_days: 15, universe_size: 45, stage1_runs: 675, llm_calls: 12825,
      est_seconds: 39900, est_label: '约 11 小时',
      probe: false, data_available: null, data_missing: null,
      missing_symbols: [], missing_held: [], blocking: false, block_reason: '',
      warnings: [],
    };
    const sp2 = makeSandbox();
    await sp2.btPreview(false);
    check('普通预览走 probe=0 且不显示探数行',
      globalThis.__apiCalls[globalThis.__apiCalls.length - 1].includes('probe=0')
      && !sp2.els['bt-preview-box'].innerHTML.includes('核实后可用'),
      globalThis.__apiCalls[globalThis.__apiCalls.length - 1]);
    check('放行时开始按钮可用、title 清空',
      sp2.els['bt-start-btn'].disabled === false
      && sp2.els['bt-start-btn'].title === '',
      String(sp2.els['bt-start-btn'].disabled));
    check('探数会带上 init_mode（复制持仓时后端才能预判门禁二）',
      globalThis.__apiCalls[globalThis.__apiCalls.length - 1].includes('init_mode='),
      globalThis.__apiCalls[globalThis.__apiCalls.length - 1]);
  }

  section('8. 截图上传：先缩放再编码，别撞 nginx 的请求体上限');
  {
    /* 线上真实故障（2026-09-11）：自选股点"上传截图"一直失败，报"请求失败 413"。
       根因是 nginx 默认只收 1MB 请求体，而手机截图 base64 后 1.5MB 起步——
       413 由 nginx 在读完请求体前产生，**后端日志里一条都没有**，
       所以 545 条离线断言 + 87 条前端断言全绿也没拦住。这一段就是补那个盲区。 */

    // ---- 大图必须被缩到长边 1600 ----
    globalThis.__imgSize = { w: 3000, h: 4000 };   // 4:3 竖图，长边 4000
    globalThis.__imgError = false;
    let sb = makeSandbox();
    await sb.toBase64({ type: 'image/png', size: 4.2e6 });
    let cv = sb.canvases[0];
    check('★ 大图按长边 1600 等比缩放（3000x4000 → 1200x1600）',
      cv && cv.width === 1200 && cv.height === 1600,
      cv ? cv.width + 'x' + cv.height : '没有建 canvas');
    check('★ 缩放后是照算出来的尺寸画的（不是把原图直接丢上去）',
      cv && cv.__draws.length === 1 && cv.__draws[0].w === 1200 && cv.__draws[0].h === 1600,
      cv && cv.__draws.length ? JSON.stringify(cv.__draws[0]) : '没画');
    check('★ 编码成 JPEG 且质量 0.85',
      cv && cv.__encode && cv.__encode.type === 'image/jpeg' && cv.__encode.quality === 0.85,
      cv && cv.__encode ? cv.__encode.type + ' q=' + cv.__encode.quality : '没编码');

    // ---- 小图不许放大 ----
    globalThis.__imgSize = { w: 800, h: 600 };
    sb = makeSandbox();
    await sb.toBase64({ type: 'image/png', size: 90e3 });
    cv = sb.canvases[0];
    check('★ 小图原尺寸上传（放大只涨字节、不涨信息）',
      cv && cv.width === 800 && cv.height === 600,
      cv ? cv.width + 'x' + cv.height : '没有建 canvas');

    // ---- 返回值契约：纯 base64，不带 data: 前缀 ----
    globalThis.__imgSize = { w: 1000, h: 1000 };
    sb = makeSandbox();
    const out = await sb.toBase64({ type: 'image/jpeg', size: 200e3 });
    check('返回纯 base64（不带 data:image 前缀，与后端约定一致）',
      typeof out === 'string' && !out.startsWith('data:') && /^[A-Za-z0-9+/=]+$/.test(out),
      String(out).slice(0, 24));

    // ---- 非图片走原路径（交给后端报错）----
    sb = makeSandbox();
    const txt = await sb.toBase64({ type: 'text/plain', size: 100 });
    check('非图片不折腾 canvas，直接读原文件',
      txt === 'VEVYVA==' && sb.canvases.length === 0,
      txt + ' canvases=' + sb.canvases.length);

    // ---- 解不开的图：给人话，不是 browser 原文 ----
    globalThis.__imgError = true;
    sb = makeSandbox();
    let emsg = '';
    try { await sb.toBase64({ type: 'image/png', size: 1e6 }); } catch (e) { emsg = e.message; }
    check('★ 图解不开时给人话（不是 "decode"/空白）',
      emsg.includes('读不出来') && emsg.includes('换一张'),
      emsg || '(没抛错)');
    globalThis.__imgError = false;

    // ---- 编码失败（toDataURL 返回空）：必须抛错，不能把空串传给后端 ----
    globalThis.__canvasDataUrl = '';
    sb = makeSandbox();
    emsg = '';
    try { await sb.toBase64({ type: 'image/png', size: 1e6 }); } catch (e) { emsg = e.message; }
    check('编码失败要当场报错，不能把空 base64 传上去',
      emsg.includes('编码失败'),
      emsg || '(没抛错)');
    globalThis.__canvasDataUrl = undefined;

    // ---- api() 对 413 说人话 ----
    /* 这条是用户实际看到的那句话。413 的响应体是 nginx 的 HTML，
       resp.json() 必然失败 → 落到 '请求失败 413' 这个毫无线索的兜底上，
       不特判的话永远查不出是"图太大"。 */
    const sb413 = makeSandbox({
      realApi: true,
      fetch: () => Promise.resolve({ status: 413, ok: false, json: () => Promise.reject(new Error('不是 JSON')) }),
    });
    let m413 = '';
    try { await sb413.api('/api/watchlist/parse'); } catch (e) { m413 = e.message; }
    check('★ 413 报"图太大"，而不是无线索的"请求失败 413"',
      m413.includes('太大') && m413.includes('413'),
      m413 || '(没抛错)');

    // ---- 对照组：非 413 仍走后端 detail ----
    const sb400 = makeSandbox({
      realApi: true,
      fetch: () => Promise.resolve({ status: 400, ok: false, json: () => Promise.resolve({ detail: '图片数据不合法' }) }),
    });
    let m400 = '';
    try { await sb400.api('/api/watchlist/parse'); } catch (e) { m400 = e.message; }
    check('非 413 仍然如实透传后端 detail',
      m400 === '图片数据不合法',
      m400 || '(没抛错)');
  }

  section('9. 回测股票范围：持仓股 / 自选股 + 分组多选');
  {
    /* 这一段守的是"界面上选了什么"与"起跑时真正发出去什么"是同一个东西。
       两者一旦分家（比如选了自选股但请求里没带），用户看到的是 A、跑的是 B，
       而且要到十小时后才从结果里发现——正是回测这个功能最不该有的形状。 */

    const GROUPS = { active_group: '科技', groups: [
      { name: '科技', count: 3, active: true }, { name: '医药', count: 2, active: false }] };
    const PREVIEW = {
      trading_days: 4, universe_size: 5, stage1_runs: 20, llm_calls: 380,
      est_label: '约 20 分钟', probe: false, data_available: null, data_missing: null,
      missing_symbols: [], missing_held: [], blocking: false, block_reason: '', warnings: [],
      range_label: '自选股 · 科技、医药（5 只）', merged_holdings: ['601398.SH'],
    };
    const urlArg = (u, key) => {
      const m = new RegExp('[?&]' + key + '=([^&]*)').exec(u);
      return m ? decodeURIComponent(m[1]) : null;
    };
    const groupsFor = (payload, preview) => (url) => {
      if (url.indexOf('/api/watchlist/groups') === 0) return Promise.resolve(payload);
      return Promise.resolve(preview);
    };

    // ---- 二选一：分组行只在「自选股」时出现 ----
    globalThis.__apiHandler = groupsFor(GROUPS, PREVIEW);
    globalThis.__apiCalls = [];
    const sb = makeSandbox();
    sb.setBtScope(0);
    check('默认「持仓股」时分组行是收起的',
      sb.els['bt-groups-row'].className.split(/\s+/).includes('hidden'),
      sb.els['bt-groups-row'].className);
    sb.setBtScope(1);
    check('★ 选「自选股」时分组行显示出来',
      !sb.els['bt-groups-row'].className.split(/\s+/).includes('hidden'),
      sb.els['bt-groups-row'].className);
    await sb.btLoadGroups();
    check('分组胶囊带票数渲染',
      sb.els['bt-group-chips'].innerHTML.includes('科技') && sb.els['bt-group-chips'].innerHTML.includes('3'),
      sb.els['bt-group-chips'].innerHTML);
    check('★ 一个都不选时不显示任何 active（= 全部分组）',
      !sb.els['bt-group-chips'].innerHTML.includes('wl-chip active'),
      sb.els['bt-group-chips'].innerHTML);

    // ---- 勾选 / 取消 ----
    sb.btToggleGroup('科技');
    check('★ 勾选后该组带 active，另一个不带',
      sb.els['bt-group-chips'].innerHTML.includes('wl-chip active')
      && (sb.els['bt-group-chips'].innerHTML.match(/wl-chip active/g) || []).length === 1,
      sb.els['bt-group-chips'].innerHTML);
    sb.btToggleGroup('医药');
    check('多选：两个组各一次 active',
      (sb.els['bt-group-chips'].innerHTML.match(/wl-chip active/g) || []).length === 2,
      sb.els['bt-group-chips'].innerHTML);
    sb.btToggleGroup('医药');
    check('再点一次取消勾选',
      sb.btGroupChips.join(',') === '科技', sb.btGroupChips.join(','));

    // ---- 预览必须带上范围，否则预览的标的数与起跑的不是一回事 ----
    await sb.btPreview(false);
    const pvUrl = globalThis.__apiCalls[globalThis.__apiCalls.length - 1];
    check('★ 预览 URL 带 bt_scope 与勾中的分组',
      pvUrl.includes('bt_scope=1') && urlArg(pvUrl, 'bt_groups') === '科技', pvUrl);
    check('★ 预览里显示范围，并把自动并入的持仓股逐只列出',
      sb.els['bt-preview-box'].innerHTML.includes('自选股 · 科技、医药（5 只）')
      && sb.els['bt-preview-box'].innerHTML.includes('601398.SH'),
      sb.els['bt-preview-box'].innerHTML);

    // ---- 起跑：body 必须和预览同源 ----
    globalThis.__apiCalls = [];
    globalThis.__apiBodies = [];
    await sb.btStart();
    const i = globalThis.__apiCalls.indexOf('/api/backtest/run');
    const body = i >= 0 ? JSON.parse(globalThis.__apiBodies[i].body) : {};
    check('★ 起跑 body 带 bt_scope=1 与选中的分组',
      body.bt_scope === 1 && (body.bt_groups || []).join(',') === '科技',
      JSON.stringify(body));

    // ---- 空范围：直接禁用开始按钮，而不是让用户点了才被拒 ----
    const emptyPv = Object.assign({}, PREVIEW, { universe_size: 0, range_label: '自选股（0 只）', merged_holdings: [] });
    globalThis.__apiHandler = groupsFor(GROUPS, emptyPv);
    globalThis.__apiCalls = [];
    const sb2 = makeSandbox();
    await sb2.btPreview(false);
    check('★ 范围是空的就直接禁用开始按钮，并把原因挂在 title 上',
      sb2.els['bt-start-btn'].disabled === true
      && sb2.els['bt-start-btn'].title.includes('空的'),
      sb2.els['bt-start-btn'].title);
    // 空范围的理由要按当前选择给：一句通用文案会出现"让你改选你已经在选的那个"。
    sb2.setBtScope(1);
    await sb2.btPreview(false);
    check('选「自选股」而范围为空时，提示让你去勾分组（不是让你改选自选股）',
      sb2.els['bt-start-btn'].title.includes('勾') && !sb2.els['bt-start-btn'].title.includes('改选「自选股」'),
      sb2.els['bt-start-btn'].title);
    sb2.setBtScope(0);
    await sb2.btPreview(false);
    check('选「持仓股」而范围为空时，提示让你改选自选股',
      sb2.els['bt-start-btn'].title.includes('改选「自选股」'),
      sb2.els['bt-start-btn'].title);

    // ---- 对照组：有标的就放行（别让禁用卡死） ----
    globalThis.__apiHandler = groupsFor(GROUPS, PREVIEW);
    await sb2.btPreview(false);
    check('范围非空时按钮恢复可用（禁用不是一锤子买卖）',
      sb2.els['bt-start-btn'].disabled === false
      && sb2.els['bt-start-btn'].title === '',
      sb2.els['bt-start-btn'].title);

    // ---- 勾着的分组被删掉：如实报告，绝不静默退回"全部分组" ----
    const sb3 = makeSandbox();
    globalThis.__apiHandler = groupsFor(GROUPS, PREVIEW);
    sb3.btGroupChips.push('已删除的组', '科技');
    await sb3.btLoadGroups();
    check('★ 分组已不存在时明说，而不是静默按"没选"处理（那样池子会比用户以为的大）',
      sb3.els['bt-group-hint'].textContent.includes('已不存在')
      && sb3.btGroupChips.join(',') === '科技',
      sb3.els['bt-group-hint'].textContent + ' | ' + sb3.btGroupChips.join(','));
    // 告警修好之后要收回去，否则界面会一直挂着一次已经过期的"已不存在"。
    await sb3.btLoadGroups();
    check('分组恢复正常后告警收回（挂着不放的告警等于没有告警）',
      sb3.els['bt-group-hint'].textContent === '不选 = 全部分组',
      sb3.els['bt-group-hint'].textContent);

    // ---- 分组接口挂了：不许当成"没分组"继续跑 ----
    globalThis.__apiHandler = () => Promise.reject(new Error('500'));
    const sb4 = makeSandbox();
    await sb4.btLoadGroups();
    check('分组加载失败时说清楚"范围算不出来"，别装作没事',
      sb4.els['bt-group-hint'].textContent.includes('分组加载失败'),
      sb4.els['bt-group-hint'].textContent);
  }

  section('10. 归因看板：null（无从追溯）与 0（真的没赚没亏）必须分开显示');
  {
    /* 后端有两个"没有数字"的状态，含义完全不同：
         null → 本簿建于口径升级前，累计值无从追溯；
         0    → 本簿确实一分没赚没亏。
       原先前端写 `data.realized_pnl || 0`，把前者渲染成"+0.00"——一个看起来像
       结论的假数。这一段守的就是这个区别不被重新抹平。 */
    const run = async (payload) => {
      const sb = makeSandbox();
      const orig = globalThis.__payload;
      // 上一节把 __apiHandler 留在"接口 500"上，不清掉的话 loadAnalytics 会走进
      // catch 分支、什么都不渲染（元素都是 undefined），断言就变成假绿/假红。
      globalThis.__apiHandler = null;
      globalThis.__payload = payload;
      await sb.loadAnalytics();
      globalThis.__payload = orig;
      return sb.els['an-pnl'];
    };

    const nullEl = await run({
      realized_pnl: null, realized_pnl_note: '暂未统计：本簿建于口径升级前，重贴一次快照即可启用',
      trade_count: 3, trust: { total_assets: 100000, pnl: 250 }, trades: [],
    });
    check('★ realized_pnl=null 显示「暂未统计」，不冒充 0 元',
      nullEl.textContent === '暂未统计', nullEl.textContent);
    check('null 状态不挂涨跌色（挂了就等于给了它一个方向）',
      !/\b(up|down)\b/.test(nullEl.className), nullEl.className);
    check('null 时把后端 note 挂在 title 上（说明为什么、怎么启用）',
      String(nullEl.title).includes('重贴'), String(nullEl.title));

    const zeroEl = await run({
      realized_pnl: 0, realized_pnl_note: '', trade_count: 0,
      trust: { total_assets: 0, pnl: 0 }, trades: [],
    });
    check('★ realized_pnl=0 显示带符号金额 +0.00（而不是「暂未统计」）',
      zeroEl.textContent === '+0.00' && zeroEl.className.includes('up'),
      zeroEl.textContent + ' | ' + zeroEl.className);

    const posEl = await run({
      realized_pnl: 1280.5, realized_pnl_note: '', trade_count: 9,
      trust: { total_assets: 100000, pnl: 250 }, trades: [],
    });
    check('正数带 + 号并挂 up 色', posEl.textContent.startsWith('+') && posEl.className.includes('up'),
      posEl.textContent + ' | ' + posEl.className);
    check('有数值时清掉 title（别留着上一次的提示）', posEl.title === '', String(posEl.title));

    const negEl = await run({
      realized_pnl: -320.25, realized_pnl_note: '', trade_count: 2,
      trust: { total_assets: 100000, pnl: 250 }, trades: [],
    });
    check('负数带 - 号并挂 down 色', negEl.textContent.includes('-') && negEl.className.includes('down'),
      negEl.textContent + ' | ' + negEl.className);

    // 数字 0 与 null 走的是不同分支——这一条是上面对比的前提，单独钉住
    const sbA = makeSandbox();
    globalThis.__apiHandler = null;
    globalThis.__payload = { realized_pnl: 0, realized_pnl_note: '', trade_count: 0, trust: {}, trades: [] };
    await sbA.loadAnalytics();
    check('0 走数值分支、null 走占位分支（两条路不共用）',
      sbA.els['an-pnl'].textContent !== nullEl.textContent,
      sbA.els['an-pnl'].textContent + ' vs ' + nullEl.textContent);
  }

  section('11. 回测流程报告：按计划生效日切换，正文就是实盘那份报告本身');
  {
    /* 「格式和次日行动报告一样」这条需求，唯一的实现方式是**同一个渲染器**——
       报告 HTML 由后端出，前端只内嵌。这一段守的就是"前端别再抄一遍版式"，
       外加三件会让用户看到错东西的事：
       ① 日期清单是**计划生效日**，不是回测的每一天；没执行到的那天必须点名，
          否则一份从未执行过的计划看起来和跑完的一模一样；
       ② 默认落在最近一天，切日期要 revoke 旧 blob（每切一次泄一份报告）；
       ③ 轮询里日期集合没变就**不许**重建 iframe（会把用户正读的那页刷掉）。 */
    /* 清单的骨架是「这个 run 跑过的交易日」，**含没有计划的那天**。
       2026-09-08 就是那个"报告不见了"的日子：回测首日没有前置研究日，天然无计划。 */
    const DATES = [
      { trade_date: '2026-09-08', created_at: 0, action_count: 0, research_count: 0,
        research_missing: [], has_plan: false, plan_missing_reason: 'first_day',
        executed: true, trade_count: 0, status: 'ok' },
      { trade_date: '2026-09-09', created_at: 1, action_count: 38, research_count: 23,
        research_missing: [], has_plan: true, plan_missing_reason: '',
        executed: true, trade_count: 0, status: 'ok' },
      { trade_date: '2026-09-10', created_at: 2, action_count: 50, research_count: 22,
        research_missing: ['159842.SZ'], has_plan: true, plan_missing_reason: '',
        executed: true, trade_count: 1, status: 'ok' },
      { trade_date: '2026-09-11', created_at: 3, action_count: 26, research_count: 8,
        research_missing: [], has_plan: true, plan_missing_reason: '',
        executed: false, trade_count: 0, status: '' },
    ];
    const reportRouter = (url) => Promise.resolve({
      ok: true, status: 200,
      text: () => Promise.resolve('<!doctype html><html>REPORT ' + url + '</html>'),
    });

    const mk = async (dates) => {
      globalThis.__apiHandler = null;
      globalThis.__fetchUrls = [];
      globalThis.__revoked = [];
      globalThis.__blobN = 0;
      globalThis.__created = [];
      const sb = makeSandbox({
        fetch: (url) => { globalThis.__fetchUrls.push(url); return reportRouter(url); },
      });
      globalThis.__apiHandler = (path) => Promise.resolve(
        path.endsWith('/reports') ? { run_id: 1, dates: dates } : {});
      sb.btCurrentRunIdForTest = 1;
      await sb.btLoadReports(1);
      return sb;
    };

    const sb = await mk(DATES);
    const sel = sb.els['bt-report-date'];
    check('★ 卡片在日期清单取到后展开（不是恒隐藏的空壳）',
      !sb.els['bt-report'].classList.contains('hidden'), sb.els['bt-report'].className);
    check('★ 日期清单按顺序列出每个交易日（含没有计划的那天）',
      sel.innerHTML.indexOf('2026-09-08') < sel.innerHTML.indexOf('2026-09-09')
      && sel.innerHTML.indexOf('2026-09-09') < sel.innerHTML.indexOf('2026-09-10')
      && sel.innerHTML.indexOf('2026-09-10') < sel.innerHTML.indexOf('2026-09-11'),
      sel.innerHTML);
    check('★ **没有计划的那天也在清单里**，并写明原因（不是整块卡片空态）',
      sel.innerHTML.includes('2026-09-08 执行 · 首日无前置研究日'), sel.innerHTML);
    check('★ 副标题按交易日计数，不是只数有计划的天数',
      sb.els['bt-report-sub'].textContent === '4 个交易日 · 3 天有行动计划',
      sb.els['bt-report-sub'].textContent);
    check('★ 没执行过的那一天在下拉里就标出来',
      sel.innerHTML.includes('未执行'), sel.innerHTML);
    check('★ 有成交的那天在下拉里带成交笔数',
      sel.innerHTML.includes('成交 1 笔'), sel.innerHTML);
    check('★ 默认落在最近的计划生效日',
      sb.btReportDate === '2026-09-11' && sel.value === '2026-09-11',
      sb.btReportDate + ' | ' + sel.value);

    const frame = sb.els['bt-report-frame'];
    check('★ 内嵌的是后端渲染的报告正文（前端不重画版式）',
      frame.src === sb.btReportBlobUrl && frame.src.startsWith('blob:stub'),
      frame.src);
    check('★ 报告请求带 inline=1（预览不该触发下载）',
      globalThis.__fetchUrls.length === 1
      && globalThis.__fetchUrls[0].includes('/api/backtest/1/report')
      && globalThis.__fetchUrls[0].includes('date=2026-09-11')
      && globalThis.__fetchUrls[0].includes('inline=1'),
      JSON.stringify(globalThis.__fetchUrls));
    check('★ 没执行到的那一天，说明里直接点破"从未被执行"',
      sb.els['bt-report-note'].textContent.includes('从未被执行'),
      sb.els['bt-report-note'].textContent);

    // 换到有成交、且有研究缺口的那一天
    await sb.btReportPick('2026-09-10');
    check('换日期后内嵌的是新那一天的报告',
      frame.src === sb.btReportBlobUrl
      && globalThis.__fetchUrls[globalThis.__fetchUrls.length - 1].includes('date=2026-09-10'),
      frame.src + ' | ' + globalThis.__fetchUrls.length + ' 次取报告');
    check('★ 换日期时旧 blob 被 revoke（每切一次日期就泄一份报告）',
      globalThis.__revoked.length >= 1, JSON.stringify(globalThis.__revoked));
    check('★ 研究缺口写进说明里，不闷声',
      sb.els['bt-report-note'].textContent.includes('159842.SZ'),
      sb.els['bt-report-note'].textContent);
    check('★ 有成交的那天把成交笔数说出来',
      sb.els['bt-report-note'].textContent.includes('成交 1 笔'),
      sb.els['bt-report-note'].textContent);

    globalThis.__opened = null;
    sb.btReportOpen();
    check('★ 「预览」打开的就是当前这份报告（不是重开一个新的）',
      globalThis.__opened === sb.btReportBlobUrl, String(globalThis.__opened));

    const beforeBlobN = globalThis.__blobN;
    await sb.btReportDownload();
    check('★ 下载走的是选中那天的报告接口',
      globalThis.__fetchUrls[globalThis.__fetchUrls.length - 1].includes('date=2026-09-10')
      && !globalThis.__fetchUrls[globalThis.__fetchUrls.length - 1].includes('inline=1'),
      globalThis.__fetchUrls[globalThis.__fetchUrls.length - 1]);
    const lastA = globalThis.__created.filter((e) => e.tagName === 'a').pop();
    check('★ 下载文件名带当天的计划生效日（多日报告堆一起也分得清）',
      !!lastA && lastA.download === '回测流程报告_2026-09-10.html',
      lastA ? lastA.download : '(没有造出 a 节点)');
    check('下载用的临时 blob 用完即 revoke（不留悬挂的 URL）',
      globalThis.__blobN > beforeBlobN && globalThis.__revoked.length >= 2,
      globalThis.__blobN + ' | ' + JSON.stringify(globalThis.__revoked));

    // 轮询：日期集合没变就什么都不做——否则每 5 秒把用户正读的那一页刷掉
    const blobsBeforeSync = globalThis.__blobN;
    const framesBeforeSync = globalThis.__fetchUrls.length;
    await sb.btSyncReports(1);
    check('★ 日期集合没变时轮询不重建 iframe（不刷掉用户正在读的那页）',
      globalThis.__blobN === blobsBeforeSync
      && globalThis.__fetchUrls.length === framesBeforeSync,
      globalThis.__blobN + ' | ' + globalThis.__fetchUrls.length);

    globalThis.__apiHandler = (path) => Promise.resolve(
      path.endsWith('/reports')
        ? { run_id: 1, dates: DATES.concat([{ trade_date: '2026-09-12', created_at: 4,
            action_count: 9, research_count: 9, research_missing: [],
            has_plan: true, plan_missing_reason: '',
            executed: true, trade_count: 2, status: 'ok' }]) }
        : {});
    globalThis.__fetchUrls = [];
    await sb.btSyncReports(1);
    check('★ 日期集合变了（回测又跑完一天）才重建，且保留用户选中的那天',
      globalThis.__fetchUrls.length === 1
      && globalThis.__fetchUrls[0].includes('date=2026-09-10')
      && sb.btReportDates.length === 5,
      JSON.stringify(globalThis.__fetchUrls) + ' | ' + sb.btReportDates.length);

    // ---- 选中"没有计划"的那一天：如实说明，且**不去请求报告** ----
    globalThis.__fetchUrls = [];
    await sb.btReportPick('2026-09-08');
    check('★ 选中没有计划的日子：不去请求报告（后端只会回一句错误，弹 toast 像页面坏了）',
      globalThis.__fetchUrls.length === 0, JSON.stringify(globalThis.__fetchUrls));
    check('★ 选中没有计划的日子：iframe 收起、预览/下载禁用（它们无东西可给）',
      sb.els['bt-report-frame'].classList.contains('hidden')
      && sb.els['bt-report-open'].disabled === true
      && sb.els['bt-report-download'].disabled === true,
      sb.els['bt-report-open'].disabled + ' | ' + sb.els['bt-report-download'].disabled);
    check('★ 说明里点破"首日没有前置研究日"，而不是笼统说没有',
      sb.els['bt-report-note'].textContent.includes('第一个交易日')
      && sb.els['bt-report-note'].textContent.includes('设计'),
      sb.els['bt-report-note'].textContent);
    await sb.btReportPick('2026-09-10');
    check('换回有计划的日子后按钮恢复可用（禁用不是一次性的）',
      sb.els['bt-report-open'].disabled === false
      && sb.els['bt-report-download'].disabled === false);

    // 一条计划都没有：不许装作有报告
    const sbEmpty = await mk([]);
    check('一条计划都没有时如实说明，不给空报告',
      sbEmpty.els['bt-report-empty'].textContent.includes('还没有跑到任何一个交易日')
      && sbEmpty.els['bt-report-frame'].classList.contains('hidden')
      && sbEmpty.els['bt-report-date'].innerHTML === '',
      sbEmpty.els['bt-report-empty'].textContent);
  }

  section('12. 回测过程可见化：逐日成交明细 + 每日监控条件（执行口径）');
  {
    /* 用户这次点名要看的这两样东西。守三件事：
       ① 成交行**三处共用一份**（分析页「最近调仓」/ 托管「流水」/ 回测「逐日成交」）——
          买/卖标签、理由、股数不能哪一处漏；两处既有版式（股数在中行 vs 在右栏）要保留。
       ② 成交明细按**交易日**分组（回测里 traded_at 是模拟时间，组头用 trade_date）。
       ③ 监控条件是**执行口径**、且缺计划时如实说没有——不画"价格全空、一档不触发"的假卡片；
          并且和托管页**共用同一段行渲染**（同一档在两页显示不同状态是最难查的一类 bug）。 */
    const T = (o) => Object.assign({
      stock_code: '600519.SH', stock_name: '贵州茅台', direction: 0, price: 9.87,
      quantity: 100, amount: 987, fee: 5, ai_reason: '估值回落，按梯子第一档建仓',
      traded_at: 1786000000,
    }, o);

    const sb0 = makeSandbox();
    const buyRow = sb0.tradeRowEl(T());
    check('★ 成交行：买入标签 + 价格 + 股数 + AI 理由（回测的成交记录主要看这个理由）',
      buyRow.className === 'trade-row' && buyRow.innerHTML.includes('买入')
      && buyRow.innerHTML.includes('9.87') && buyRow.innerHTML.includes('100 股')
      && buyRow.innerHTML.includes('估值回落'),
      buyRow.innerHTML);
    check('★ 卖出走另一套标签与颜色（不是一律"买入"）',
      sb0.tradeRowEl(T({ direction: 1 })).innerHTML.includes('tag sell')
      && sb0.tradeRowEl(T({ direction: 1 })).innerHTML.includes('卖出'),
      sb0.tradeRowEl(T({ direction: 1 })).innerHTML);
    const varRow = sb0.tradeRowEl(T(), { qtyInMeta: true, amountAtRight: true });
    check('两处既有版式都保留：股数在中行 + 金额在右栏',
      varRow.innerHTML.includes('100 股 ·') && varRow.innerHTML.includes('987.00')
      && varRow.innerHTML !== buyRow.innerHTML,
      varRow.innerHTML);

    // ---- 逐日成交明细：按交易日分组 ----
    const TRADES = {
      run_id: 7, trade_count: 3,
      days: [
        { trade_date: '2026-09-09', trades: [T()] },
        { trade_date: '2026-09-10', trades: [T({ direction: 1, stock_name: '平安银行' }),
                                              T({ stock_code: '159842.SZ', stock_name: '券商ETF' })] },
      ],
    };
    const mkApi = (routes) => (path) => {
      for (const [suffix, payload] of routes) {
        if (path.includes(suffix)) return Promise.resolve(payload);
      }
      return Promise.resolve({});
    };
    globalThis.__apiHandler = mkApi([['/trades', TRADES]]);
    const sbT = makeSandbox();
    await sbT.btLoadTrades(7);
    const tBody = sbT.els['bt-trades-body'];
    check('★ 成交明细卡片展开（此前前端根本没调过 /trades 这个接口）',
      !sbT.els['bt-trades'].classList.contains('hidden'), sbT.els['bt-trades'].className);
    // 桩不解析 innerHTML，所以逐层取：组 → 组头（.trades-header）→ 它的 innerHTML
    const g0 = tBody.children[0], g1 = tBody.children[1] || {};
    const head0 = (g0 && g0.children[0]) || {}, head1 = (g1.children || []).length ? g1.children[0] : {};
    check('★ 按交易日分组：每天一个组，不是一锅粥',
      tBody.children.length === 2
      && String(head0.innerHTML).includes('2026-09-09')
      && String(head1.innerHTML).includes('2026-09-10'),
      tBody.children.length + ' 组 | ' + String(head0.innerHTML));
    check('★ 组头带当天笔数，副标题带整轮合计',
      String(head1.innerHTML).includes('2 笔')
      && sbT.els['bt-trades-sub'].textContent.includes('共 3 笔'),
      String(head1.innerHTML) + ' | ' + sbT.els['bt-trades-sub'].textContent);
    globalThis.__apiHandler = mkApi([['/trades', { run_id: 7, trade_count: 0, days: [] }]]);
    const sbE = makeSandbox();
    await sbE.btLoadTrades(7);
    check('★ 没有任何成交时说的是「没有任何一档触发条件被满足」，不是一片空白',
      sbE.els['bt-trades-body'].innerHTML.includes('没有任何一档触发条件'),
      sbE.els['bt-trades-body'].innerHTML);

    // ---- 每日监控条件（执行口径）----
    const MON = {
      run_id: 7, trade_date: '2026-09-10', has_plan: true, plan_missing_reason: '',
      price_basis: 'execution', code_count: 2,
      actions: [
        { code: '600519.SH', name: '贵州茅台', action: 'sell', target_weight: 0.05,
          trigger_type: 'price_below', trigger_price: 9.5, volume_ratio_min: null,
          reason: '跌破减仓', kind: 'exit', tier_index: 1, tier_count: 2, is_first: true,
          price: 9.2, prev_close: 10.4, volume_ratio: 1.1,
          open: 10.5, high: 11.8, low: 9.2, close: 10.1, triggered: true },
        { code: '600519.SH', name: '贵州茅台', action: 'sell', target_weight: 0.05,
          trigger_type: 'price_below', trigger_price: 9.0, volume_ratio_min: 2,
          reason: '再跌破减第二档', kind: 'exit', tier_index: 2, tier_count: 2, is_first: false,
          price: 9.2, prev_close: 10.4, volume_ratio: 1.1,
          open: 10.5, high: 11.8, low: 9.2, close: 10.1, triggered: false },
        { code: '000001.SZ', name: '平安银行', action: 'buy', target_weight: 0.05,
          trigger_type: 'price_above', trigger_price: 11.5, volume_ratio_min: null,
          reason: '涨过建仓', kind: 'entry', tier_index: 1, tier_count: 1, is_first: true,
          price: 11.8, prev_close: 10.4, volume_ratio: 1.1,
          open: 10.5, high: 11.8, low: 9.2, close: 10.1, triggered: true },
      ],
    };
    globalThis.__apiHandler = mkApi([['/monitor', MON]]);
    const sbM = makeSandbox();
    await sbM.btLoadMonitor(7, '2026-09-10');
    const mRows = sbM.els['bt-monitor-rows'];
    check('★ 监控卡片展开，副标题按**标的**计数（多档展开成多行，行数≠票数）',
      !sbM.els['bt-monitor'].classList.contains('hidden')
      && sbM.els['bt-monitor-sub'].textContent.includes('2 只标的')
      && sbM.els['bt-monitor-sub'].textContent.includes('3 档条件'),
      sbM.els['bt-monitor-sub'].textContent);
    check('★ 卖档显示的是**当日执行价** 9.200（跌破 9.5 用的是当日最低，不是收盘 10.1）',
      mRows.innerHTML.includes('9.200') && mRows.innerHTML.includes('跌破 9.5'),
      mRows.innerHTML.slice(0, 200));
    check('★ 买档用当日最高：11.800 越过 11.5 → 已触发',
      mRows.innerHTML.includes('11.800') && mRows.innerHTML.includes('涨过 11.5')
      && mRows.innerHTML.includes('已触发'),
      mRows.innerHTML.slice(0, 200));
    check('★ 同一只票的第 2 档渲染成续行（└ 第2档），组内不重复写股票名',
      mRows.innerHTML.includes('└ 第2档') && mRows.innerHTML.includes('共 2 档'),
      mRows.innerHTML.slice(0, 300));
    check('★ 量能门槛写进条件里（9.200 穿过了 9.0，但量比不足 → 监控中而不是已触发）',
      mRows.innerHTML.includes('且量比≥2') && mRows.innerHTML.includes('监控中'),
      mRows.innerHTML.slice(0, 300));
    check('★ 判据的口径写在说明里（卖/减取当日最低、买/建取当日最高）——用户才知道这数从哪来',
      sbM.els['bt-monitor-note'].textContent.includes('当日最低')
      && sbM.els['bt-monitor-note'].textContent.includes('当日最高'),
      sbM.els['bt-monitor-note'].textContent);

    // 同一份 payload 在托管页与回测页必须渲染出**逐字节相同**的行（同一段渲染器）。
    // ``loadCard`` 走的是 ``__payload``，所以必须先把 apiHandler 摘掉——留着的话
    // api() 会先去问 handler，而 handler 只认 /monitor，托管页那个路径会拿到 {}。
    globalThis.__apiHandler = null;
    const trustEls = await loadCard(MON);
    globalThis.__apiHandler = mkApi([['/monitor', MON]]);
    check('★ 托管页与回测页对同一份监控条件渲染出完全相同的行（只有一个渲染器）',
      trustEls['monitor-rows'].innerHTML === mRows.innerHTML,
      trustEls['monitor-rows'].innerHTML.slice(0, 120) + ' VS ' + mRows.innerHTML.slice(0, 120));

    // 缺计划的那天：不画假卡片
    globalThis.__apiHandler = mkApi([['/monitor', {
      run_id: 7, trade_date: '2026-09-08', has_plan: false,
      plan_missing_reason: 'first_day', price_basis: 'execution',
      code_count: 0, actions: [],
    }]]);
    const sbN = makeSandbox();
    await sbN.btLoadMonitor(7, '2026-09-08');
    check('★ 没有计划的日子：一行都不渲染，**不画"价格全空、一档不触发"的假卡片**',
      sbN.els['bt-monitor-rows'].innerHTML === ''
      && sbN.els['bt-monitor-empty'].style.display === 'block',
      JSON.stringify(sbN.els['bt-monitor-rows'].innerHTML));
    check('★ 空态说清是哪一天、为什么没有（首日 = 没有前置研究日，属设计）',
      sbN.els['bt-monitor-empty'].textContent.includes('2026-09-08')
      && sbN.els['bt-monitor-empty'].textContent.includes('首日'),
      sbN.els['bt-monitor-empty'].textContent);
  }

  // ---------------------------------------------------------------- 移动端导航
  // 这一段是**按源码字符串**断言的，不是跑出来的：沙箱里 ``querySelectorAll`` 恒返回 ``[]``，
  // 导航绑定与 Tab 点击在那里根本不执行，所以「点一下看看亮哪个」测不了。
  // 能真跑的是脚本内的常量（MOBILE_TAB_OF），它决定了 Tab 高亮，值得单独钉住。
  section('移动端底部 Tab（导航重构）');
  {
    const html = fs.readFileSync(HTML_PATH, 'utf8');

    // 「模拟交易」占了一格 Tab，而且带 data-view——没有它，绑定那行就绑不到东西。
    check('★ 底部第三格 Tab 存在且指向 trust（「模拟交易」）',
      /class="mobile-tab"\s+data-view="trust"[\s\S]{0,900}?模拟交易/.test(html),
      (html.match(/class="mobile-tab"[^>]*data-view="[^"]*"[^>]*>[\s\S]{0,120}?\S+/) || [''])[0].slice(0, 80));

    // 首页宫格那张「财团托管」卡必须没了——否则一个入口两处出现。
    check('宫格里的「财团托管」卡已删除（入口只留底部 Tab 一处）',
      !html.includes('entry-card trust-entry'),
      'entry-card trust-entry 仍在 index.html');

    // 历史回测入口卡在**托管总览**里，且是移动端独有（默认 display:none）。
    check('★ 「历史回测」入口卡在托管总览里，且默认 display:none（桌面端看不到）',
      html.includes('class="bt-entry-card"') && /\.bt-entry-card\s*\{[^}]*display:\s*none/.test(html),
      'bt-entry-card 或其 display:none 缺失');
    check('★ 该卡在 ≤1023px 的媒体查询里被翻成 flex（移动端才出现）',
      /@media \(max-width: 1023px\)[\s\S]*?\.bt-entry-card\s*\{\s*display:\s*flex/.test(html),
      '媒体查询里没有 .bt-entry-card { display: flex }');

    // view-spec 是自述规范页，写死「仅 2 个 Tab」会与实现直接矛盾。
    check('规范页文案已同步成三个 Tab（不再写「仅 2 个」）',
      !html.includes('仅 2 个 Tab') && html.includes('3 个 Tab：交易 + 模拟交易 + AI Agent'),
      'view-spec 仍写着 2 个 Tab');

    // 页头标题由 viewTitles 驱动，只改 Tab 上的字不够。
    check('页头标题也改成了「模拟交易」（否则点进去页头还写「财团托管」）',
      /trust:\s*\['模拟交易'/.test(html),
      (html.match(/trust:\s*\[[^\]]*\]/) || [''])[0]);

    // ★ 这条能真跑：MOBILE_TAB_OF 是脚本内常量，它决定哪个 Tab 亮。
    //   把 backtest 归到 trust 是「进历史回测时底部仍停在模拟交易」的**唯一**依据。
    const sbT = makeSandbox();
    const map = sbT.MOBILE_TAB_OF;
    check('★ MOBILE_TAB_OF：trust 与 backtest 都归「模拟交易」，其余二级页归「交易」',
      map && map.trust === 'trust' && map.backtest === 'trust'
      && map.positions === 'home' && map.quant === 'home' && map.watchlist === 'home',
      JSON.stringify(map));
    check('★ MOBILE_TAB_OF 没把 trust 归到 home（归错的话点「模拟交易」亮的是「交易」）',
      map && map.trust !== 'home' && map.backtest !== 'home',
      JSON.stringify(map));
  }

  console.log('\n' + '='.repeat(60));
  if (fails.length) {
    console.log('❌ ' + fails.length + '/' + count + ' 项未通过：');
    fails.forEach((f) => console.log('   - ' + f));
    process.exit(1);
  }
  console.log('✅ 全部 ' + count + ' 项通过');
})().catch((e) => {
  console.error('检查脚本自身出错：', e);
  process.exit(2);
});
