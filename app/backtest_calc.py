"""回测的纯计算：已实现盈亏、最大回撤、聚合、基准、检查点序列化、声明文案。

**为什么单独一个模块**：这里的每一件都是"给了输入就该有唯一答案"的算术，没有数据库、
没有网络、没有 LLM。抽出来才能离线断言——而下面这几条恰恰是最容易写错、写错了又最难在
十小时的长跑里发现的东西：

- **已实现盈亏**：仓库至今没有这个算法（``trust.get_analytics`` 里那个 ``None`` 是如实标注的
  桩），而它必须与 ``trade.place_order`` 的移动加权平均成本**口径一致**。这里的
  ``replay_realized`` 逐笔重放 ``place_order`` 的成本更新公式，含 ``round(..., 4)``，
  目的是能与库里的 ``Position.cost_price`` 对上账。
- **起始基准 ``init_basis``**：必须用起始日**市价**，不能用 ``cost_price``。后者会把起始日
  之前的历史浮盈算进"回测收益"——这是最容易写错的一条。
- **等权基准**：手数取整与费用模型必须与策略**同源**（``trade._calc_fee`` + A股 ``//100``），
  否则"团队比躺着不动强吗"这个问题里混进了一个口径差。
"""
from __future__ import annotations

import json

from app import trade as trade_mod

# ---------------------------------------------------------------- 净值曲线


def max_drawdown(equity: list[float]) -> float:
    """最大回撤（正的**比例**，0.12 = 从峰值跌 12%）。

    取「历史峰值 → 之后任一谷底」的最大跌幅。空/单点序列为 0.0。
    """
    peak = None
    worst = 0.0
    for v in equity or []:
        v = float(v)
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            dd = (peak - v) / peak
            if dd > worst:
                worst = dd
    return worst


# ---------------------------------------------------------------- 已实现盈亏


def replay_realized(initial_positions: list[dict], trades: list[dict]) -> float:
    """加权平均成本法逐笔重放，返回**累计已实现盈亏**（含交易费用）。

    ``initial_positions``：``[{stock_code, hold_qty, cost_price}]``，起始持仓（copy 模式）。
    ``trades``：``[{stock_code, direction, price, quantity, fee}]``，**必须按成交先后排序**。

    按 `t` 顺序维护每只票的 ``(qty, cost)``，公式与 ``trade.place_order`` 逐字对齐：
    买入 ``cost = (cost*qty + price*qty')/(qty+qty')`` 后 ``round(4)``；卖出不动成本价，
    已实现 ``(price - cost) * qty' - fee``；卖到零股就把成本清零（对应 ``place_order``
    在 ``hold_qty <= 0`` 时删除该行）。

    **费用只在这里扣一次**：``place_order`` 的成本价不含费用，费用是单独的资金流出，
    所以已实现盈亏 = 价差 − 该笔费用，与 §6 的口径一致。
    """
    book: dict[str, list[float]] = {}
    for p in initial_positions or []:
        qty = int(p.get("hold_qty") or 0)
        if qty > 0:
            book[p["stock_code"]] = [float(qty), float(p.get("cost_price") or 0.0)]

    realized = 0.0
    for t in trades or []:
        code = t.get("stock_code")
        qty = int(t.get("quantity") or 0)
        price = float(t.get("price") or 0.0)
        fee = float(t.get("fee") or 0.0)
        if not code or qty <= 0:
            continue
        held, cost = book.get(code, [0.0, 0.0])
        if t.get("direction") == 0:  # 买入
            total = held + qty
            book[code] = [total, round((cost * held + price * qty) / total, 4)]
        else:  # 卖出
            realized += (price - cost) * qty - fee
            held -= qty
            if held <= 0:
                book.pop(code, None)
            else:
                book[code] = [held, cost]
    return round(realized, 2)


# ---------------------------------------------------------------- 检查点

#: 检查点快照里必须逐项存全的持仓字段。**少任何一个都会让续跑后的账务与原来不同**：
#: ``cost_price`` 影响后续定仓与已实现盈亏，``available_qty`` / ``frozen_qty`` 决定 T+1。
_POS_SNAPSHOT_FIELDS = ("stock_code", "stock_name", "hold_qty", "available_qty", "frozen_qty", "cost_price")


def serialize_checkpoint(cash: float, positions: list[dict]) -> str:
    """日终账务快照 → JSON。字段固定，方便 ``restore_checkpoint`` 逐项覆盖。"""
    blob = {
        "cash": round(float(cash or 0.0), 2),
        "positions": [
            {k: p.get(k) for k in _POS_SNAPSHOT_FIELDS}
            for p in positions or []
            if int(p.get("hold_qty") or 0) > 0
        ],
    }
    return json.dumps(blob, ensure_ascii=False)


def parse_checkpoint(blob: str | None) -> dict:
    """检查点 JSON → ``{cash, positions}``。空/坏值一律当"没有检查点"，返回空 dict。

    坏值不抛异常是刻意的：一个损坏的检查点应该退回**重跑整个回测**，而不是让 run 卡死
    在一个再也起不来的 failed 上（``start_run`` 会据此走全新起跑分支）。
    """
    if not blob:
        return {}
    try:
        data = json.loads(blob)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("positions"), list):
        return {}
    return data


# ---------------------------------------------------------------- 基准

#: 基准买入/持有都在**起始日开盘价**上成交，与策略 §3.1 的"时间档按开盘价"同口径，
#: 这样两者唯一的差别就是"团队决策"本身。
def equal_weight_baseline(
    codes: list[str],
    bars_by_code: dict[str, dict],
    days: list[str],
    capital: float,
    cfg,
) -> dict:
    """等权买入持有（主基准）：起始日开盘价等权买入 ``codes``，持有到期末。

    ``capital`` 传的是 ``init_basis``（起始日**市价**口径的总资产）——基准与策略必须从
    同一个起点出发，否则"超额收益"里会混进一个起始口径差。

    手数取整（A股 ``//100``）与费用（``trade._calc_fee``）**与策略同源**——这正是这个基准
    的用处：拿掉"团队决策"这一个变量，剩下的差异才是团队的贡献。

    起始日无行情的标的直接**不买**，它那份钱留在现金里（不是重分给其他票）：重分会悄悄
    抬高其他票的权重，让基准不再"等权"。
    """
    days = list(days or [])
    out = {"total_assets": 0.0, "total_return": 0.0, "curve": [], "holdings": []}
    if not days or capital <= 0:
        return out

    first = days[0]
    buyable = [
        c for c in (codes or [])
        if _price(bars_by_code, c, first, "open") is not None
    ]
    if not buyable:
        out["total_assets"] = float(capital)
        out["curve"] = [{"trade_date": d, "total_assets": float(capital)} for d in days]
        return out

    per = capital / len(buyable)
    cash = float(capital)
    holds: dict[str, dict] = {}
    for code in buyable:
        px = _price(bars_by_code, code, first, "open")
        is_a = trade_mod._is_a_share(code)
        qty = int(per / px)
        if is_a:
            qty = qty // 100 * 100
        # 佣金算在**这一份预算之内**：不这么减，等分之后必然有一份"加了佣金就买不起"
        # （3 只票时第三只一定超），于是那一份的钱原地躺成现金，基准的收益被系统性压低。
        # 减到买得起为止，每次减一手（A股 100 股 / 其他 1 股）。
        amount = fee = 0.0
        while qty > 0:
            amount = round(qty * px, 2)
            fee = trade_mod._calc_fee(cfg, code, 0, amount)
            if amount + fee <= per + 1e-9:
                break
            qty -= 100 if is_a else 1
        if qty <= 0:
            continue
        cash = round(cash - amount - fee, 2)
        holds[code] = {"stock_code": code, "quantity": qty, "cost": px, "fee": fee}

    for d in days:
        mv = 0.0
        for code, h in holds.items():
            px = _price(bars_by_code, code, d, "close")
            if px is not None:
                mv += px * h["quantity"]
        out["curve"].append({"trade_date": d, "total_assets": round(cash + mv, 2)})

    out["total_assets"] = out["curve"][-1]["total_assets"]
    out["total_return"] = (out["total_assets"] - capital) / capital if capital else 0.0
    out["holdings"] = [
        {**h, "last_close": _price(bars_by_code, h["stock_code"], days[-1], "close")}
        for h in holds.values()
    ]
    return out


def index_baseline(bench: dict, days: list[str], init_basis: float) -> dict:
    """沪深300 归一化到 ``init_basis``：回答"这段行情本身如何"。

    只有**两侧都有值**的日子才进曲线：区间首日缺基准价就没有归一化除数，直接返回空——
    宁可不画这条线，也不能拿一个错的除数画一条看着很正常的线。
    """
    days = list(days or [])
    out = {"total_assets": 0.0, "total_return": 0.0, "curve": []}
    base = (bench or {}).get(days[0]) if days else None
    if not base or not init_basis:
        return out
    for d in days:
        v = (bench or {}).get(d)
        if v is None:
            continue
        out["curve"].append({"trade_date": d, "total_assets": round(init_basis * v / base, 2)})
    if out["curve"]:
        out["total_assets"] = out["curve"][-1]["total_assets"]
        out["total_return"] = (out["total_assets"] - init_basis) / init_basis
    return out


def _price(bars_by_code: dict, code: str, date: str, field: str) -> float | None:
    """某票某日的某个价位；无 bar 返回 None（不猜、不顺延）。"""
    bar = (bars_by_code.get(code) or {}).get(date)
    return None if bar is None else float(getattr(bar, field))


# ---------------------------------------------------------------- 聚合


def aggregate_steps(steps: list[dict], init_basis: float) -> dict:
    """逐日 step → 结果页的总量与曲线。``steps`` 需按 ``trade_date`` 升序。

    总收益率的分母是 ``init_basis``（起始日**市价**口径），不是初始现金——copy 模式下
    两者差别很大，用错就把起始日之前的浮盈算进了回测收益。
    """
    steps = list(steps or [])
    out = {
        "day_count": len(steps),
        "init_basis": float(init_basis or 0.0),
        "final_assets": 0.0,
        "total_pnl": 0.0,
        "total_return": 0.0,
        "max_drawdown": 0.0,
        "total_fees": 0.0,
        "realized_pnl": 0.0,
        "trade_count": 0,
        "equity_curve": [],
        "degraded_days": [],
    }
    if not steps:
        return out

    equity = [float(s.get("total_assets") or 0.0) for s in steps]
    out["equity_curve"] = [
        {"trade_date": s["trade_date"], "total_assets": round(e, 2)}
        for s, e in zip(steps, equity)
    ]
    out["final_assets"] = round(equity[-1], 2)
    out["total_pnl"] = round(equity[-1] - out["init_basis"], 2)
    out["total_return"] = (
        (equity[-1] - out["init_basis"]) / out["init_basis"] if out["init_basis"] else 0.0
    )
    out["max_drawdown"] = max_drawdown(equity)
    out["total_fees"] = round(sum(float(s.get("fees") or 0.0) for s in steps), 2)
    # 已实现盈亏是**累计**量，取最后一天的快照值即可（逐日重放的结果）。
    out["realized_pnl"] = round(float(steps[-1].get("realized_pnl") or 0.0), 2)
    out["trade_count"] = sum(int(s.get("trade_count") or 0) for s in steps)
    out["degraded_days"] = [
        {"trade_date": s["trade_date"], "status": s.get("status"), "error": s.get("error") or ""}
        for s in steps
        if (s.get("status") or "ok") != "ok"
    ]
    return out


# ---------------------------------------------------------------- 声明

#: 结果页必须逐条展示的声明。**改这里的文案就要同步改 ``check_frontend.js`` 的断言**——
#: 前端那份是"后端给的声明都渲染出来了"的守卫，不是文案比对。
DISCLOSURES: list[dict] = [
    {
        "key": "match",
        "title": "撮合口径",
        "text": "日内触发 + 跳空按开盘：条件档用当日 K 线的 low/high 判触发，"
                "开盘已穿越触发价则按开盘价成交，否则按触发价成交；时间档按开盘价成交。"
                "日线无法还原当日 high/low 的先后顺序，因此价格与数量两个维度都按**不利方向**假设。",
    },
    {
        "key": "volume_ratio",
        "title": "量比口径",
        "text": "日线代理量比 = 当日成交量 ÷ 前 5 日均量。它用到**当日全天成交量**（含日内事后信息），"
                "会使 volume_ratio_min 档比实盘更容易被满足。Wind 没有可寻址的历史量比接口。",
    },
    {
        "key": "adjust",
        "title": "复权口径",
        "text": "个股线全程**后复权**：绝对价位被每只票的常数缩放，与当前盘面不一致，不要直接与今天的"
                "价格对照。区间收益已隐含分红再投资。前复权会随未来的分红送转重算历史价，属于未来函数，"
                "故不采用。⚠️ **沪深300 那条基准线不受此口径约束**——指数 K 线接口没有复权参数，它是"
                "价格指数（分红除息后自然回落），与策略不是可比的总收益口径。所以它只按收益率归一化后"
                "当'这段行情本身如何'的参照，**不与策略做绝对对照**；'团队比躺着不动强吗'由等权买入持有"
                "基准回答（那条与策略共用同一套手数与费用模型）。",
    },
    {
        "key": "limit",
        "title": "涨跌停 / 停牌",
        "text": "历史涨跌停价无接口可取，按板块静态规则近似（主板 10%、创业板科创板 20%、"
                "北交所 30%、ST 5%、港美股不限）。新股上市首日、盘中临时停牌、退市整理期的特殊比例、"
                "以及板价的四舍五入规则都不精确。停牌当日不交易，市值沿用停牌前最后有效收盘。",
    },
    {
        "key": "cost",
        "title": "费用与摩擦",
        "text": "只计佣金与印花税，费率取发起回测时的托管配置。**未建模**：滑点、冲击成本、"
                "成交量约束（大额委托无法一次成交）、港股每手股数、融资融券成本。",
    },
    {
        "key": "selection_bias",
        "title": "事后选样偏差",
        "text": "自选股取的是**当前**名单，起始持仓复制的是**当前**托管簿——两者都是站在今天回看的。"
                "这份结果里含有「当时就在自选里」这一层信息优势，真实前瞻时并不存在。",
    },
    {
        "key": "data_gap",
        "title": "数据工具降级",
        "text": "新闻、公告、财报三大表、公司事件、宏观、风险指标、实时快照在回测中**不可用**"
                "（这些接口只有自然语言问答、没有日期参数），相关角色输出占位符。"
                "因此回测的信息完备度**低于**实盘，两者的绝对收益不可直接对比；"
                "可对比的是同一套信息条件下团队决策的相对高低。",
    },
    {
        "key": "cache",
        "title": "研究缓存",
        "text": "深度研究报告的缓存键是 (账户, 标的, 研究日)，**不含组合上下文**。"
                "同一账户、同一天、不同起始资金的两次回测会复用同一份研究报告。"
                "需要完全独立的重跑请勾选「强制重跑研究」。",
    },
    {
        "key": "latency",
        "title": "耗时标定与免责",
        "text": "单次深度分析实测约 35 秒（约 19 次 LLM 调用），30 个交易日 × 5 只标的约需 10 小时量级，"
                "并发度盲目调高会撞 Wind 与模型限流。本结果由历史数据模拟得出，"
                "**不构成任何投资建议**，也不代表未来收益。",
    },
    {
        "key": "range_prompt_only",
        "title": "建仓范围是提示词约束",
        "text": "「本次的建仓与调仓应限定在所选股票范围内」这条要求是**写进决策提示词里的**，"
                "不是代码层的硬保证——团队仍有可能提交范围外的标的。"
                "回测侧有一层天然保护：范围外的标的没有取行情，执行层无从成交；"
                "**但那是'没有行情'，不等于'被拦下'**，不要把它当成风控。",
    },
    {
        "key": "empty_start_path",
        "title": "空仓起步的路径差异",
        "text": "回测的空仓起步走的是**候选池决策**（团队看到范围内全部标的的研究结论后自行建仓）；"
                "实盘在没有计划时走的是**评级兜底**（只对已有持仓做加减仓）。两者机制不同，"
                "**回测里空仓起步建了多少仓，不能直接当作实盘空仓起步的预期**。",
    },
    {
        "key": "first_day_no_plan",
        "title": "回测首日没有可执行计划",
        "text": "计划是**研究日日终产出、次日执行**的：回测第 d 天的计划，用的是第 d-1 天的研究结论。"
                "所以第 1 个交易日**天然没有可执行计划**，那天一笔都不会成交——包括止损。"
                "带来的两点要注意：① 首日的盈亏**纯粹是持有**出来的，不含任何团队决策；"
                "② 「复制当前持仓」起步时，若某只持仓在首日就已超过单票权重上限，"
                "团队最早也只能在**第 2 个交易日**才动手减，首日的超配不代表团队没管。",
    },
]


def disclosures_for(config: dict | None = None) -> list[dict]:
    """结果页的声明列表 = 固定那批 + 本次运行特有的事实。

    「本次运行特有」这类声明**不能做成固定文案**——写死一句"部分持仓被自动并入"而不管真实
    发生了什么，等于把声明变成了装饰。有就有、逐只列出，没有就不出现。
    """
    out = list(DISCLOSURES)
    merged = [c for c in ((config or {}).get("bt_merged") or []) if c]
    scope = (config or {}).get("bt_scope")
    if merged:
        out.append({
            "key": "range_merged",
            "title": "自动并入的持仓",
            "text": f"本次回测的股票范围按「{scope_name(scope)}」选定，但它不包含你当时的全部持仓。"
                    f"为避免'复制当前持仓'时账对不上，以下 {len(merged)} 只持仓股**已自动并入**回测范围："
                    + "、".join(merged) + "。",
        })
    return out


def scope_name(scope) -> str:
    """范围取值 → 人话。取值与 ``backtest.BT_SCOPE_*`` 一致（这里不反向 import，避免循环）。"""
    return {0: "持仓股", 1: "自选股"}.get(scope, "持仓股 ∪ 自选股")
