"""AI 对话助手的**只读**工具层。

本模块把「账户 / 持仓 / 收益 / 次日计划 / 监控条件 / 研究评级 / 自选 / 历史行情」这些
后端已有能力包装成 LLM 可调用的工具，供 ``app/chat.py`` 的工具循环挂载。

**只读是结构性的，不靠 prompt 自觉：**

- ``build_registry(user_id)`` 里的工具是**手写白名单**。本模块不对 ``app.*`` 做任何反射，
  也不提供「按名字调用任意函数」的通用工具，所以模型能调到的只可能是下面显式列出的只读函数。
- 每个工具**不接受 ``user_id`` 入参**——用户身份由 ``build_registry`` 的闭包捕获鉴权结果，
  模型既不能传也不能伪造。
- 禁止 import 任何会写库或跑 LLM 的函数：``toggle_trust`` / ``update_trust_config`` /
  ``submit_plan_generation`` / ``run_plan_for_user`` / ``reset_trust`` / ``snapshot_trust_book`` /
  ``run_execution`` / ``submit_analysis`` / ``run_analysis`` / ``run_portfolio_plan`` /
  ``run_analysis_cached``（后者缓存未命中会阻塞 10-15 分钟）。``scripts/smoke_chat_tools.py``
  里有一条静态断言守着这条线。

**输出约定**（所有工具返回 ``str``，统一口径）：

1. 首行是 ``【标题】``。
2. **空态必须显式，绝不返回 0**——「尚未建簿 / 未开启托管 / 暂无计划 / 无行情」都是状态，
   不是数字。模型把空态读成 0 就会编出一个不存在的结论。
3. 金额 ``round(x, 2)``；百分比 ``+1.23%`` / ``-1.23%``。
4. 列表自带条数上限，且**显式说明截断**（``（仅显示最近 20 笔，共 137 笔）``）。
"""
from __future__ import annotations

import functools
import json
import re
from datetime import datetime

from langchain_core.tools import tool

from app import account as account_mod
from app import db
from app import intent as intent_mod
from app import notify as notify_mod
from app import plan_actions
from app import risk as risk_mod
from app import trust as trust_mod
from app import watchlist as watchlist_mod
from app.analysis import RATING_ZH
from app.analysis_service import RESEARCH_FIELDS, collect_research_snapshot
from app.errors import ServiceError
from app.plan_report import _ACTION_ZH
from tradingagents.dataflows import wind as _wind
from tradingagents.dataflows.errors import VendorRateLimitError

#: 深度分析确认哨兵。``request_deep_analysis`` 把它拼在 JSON 前面返回，
#: 工具循环识别到前缀就中断整轮、把确认请求交还前端——**绝不会**在这里启动分析。
CONFIRM_PREFIX = "@@CONFIRM_ANALYSIS@@"

_MAX_CODES = 20      # 单次行情/研报查询的标的上限（控 Wind 调用与 token）
_MAX_LIST = 50       # 流水类工具的条数上限
_MAX_PLAN_CHARS = 6000   # 次日计划投影的字符上限（plan_json 原始可达 200KB+）
_MAX_FIELD_CHARS = 1200  # 单份研究报告字段的截断长度


# ---------- 通用小工具 ----------

def _guard(fn):
    """把所有异常转成文本返回给模型。

    工具**永不抛异常给循环**：抛了整轮对话就崩了，而模型其实有能力换一个工具或如实
    告知用户。限流单独识别，是为了让模型别再立刻重试同一个请求。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except VendorRateLimitError:
            return "【取数失败】行情接口限流，请稍后再问（不要立刻重试同一个请求）。"
        except ServiceError as e:
            return f"【取数失败】{e.message}"
        except Exception as e:  # noqa: BLE001 —— 兜底，见 docstring
            return f"【取数失败】{type(e).__name__}"

    return wrapper


def _pct(x) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _money(x) -> str:
    if x is None:
        return "—"
    try:
        return f"{float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _weight(w) -> str:
    """目标占比（0~1）→ ``12.0%``；未设定返回 ``—``。"""
    if w is None:
        return "—"
    try:
        return f"{float(w) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


_TRIGGER_ZH = {
    "none": "无条件",
    "open": "开盘",
    "intraday": "盘中",
}


def _trigger_text(a: dict) -> str:
    """档位触发条件的人话。含量比门槛。"""
    t = (a.get("trigger_type") or "none").lower()
    p = a.get("trigger_price")
    if t == "price_below":
        base = f"现价 ≤ {p}"
    elif t == "price_above":
        base = f"现价 ≥ {p}"
    else:
        base = _TRIGGER_ZH.get(t, t)
    v = a.get("volume_ratio_min")
    if v:
        base += f"（量比 ≥ {v}）"
    return base


def _dir_zh(direction) -> str:
    return "买入" if direction == 0 else "卖出"


def _ts(t) -> str:
    try:
        return datetime.fromtimestamp(float(t)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


def _grab(text: str, key: str) -> str:
    """从研究 markdown 里抓 ``**Key**：值`` 一行。与 analysis_service._research_summary 同口径。"""
    m = re.search(rf"\*\*{re.escape(key)}\*\*[：:]\s*(.+)", text or "")
    return m.group(1).strip() if m else ""


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + f"…（已截断，原文 {len(text)} 字）"


# ---------- 账户上下文（原先在 app/chat.py，挪到这里避免与工具层循环 import） ----------

def account_context(user_id: int) -> str:
    """组装账户上下文文本（账户汇总 + 双簿持仓 + 最近成交）。"""
    acc = account_mod.get_account(user_id)
    mirror_pos = account_mod.get_positions(user_id, 0)
    trust_pos = account_mod.get_positions(user_id, 1)

    lines = [
        # 口径：镜像簿 = 真实券商账户（首页/持仓视图的总资产口径）；托管簿 = 交给 AI 托管的账户。
        # 若托管簿起始仓由真实持仓复制建立，两簿持仓相同是同一笔资产的两种视图，绝不能把两簿相加
        # 当作「总资产」（否则会翻倍：user2 镜像 46.96 万 + 托管复制 46.96 万 → 错误的 93.91 万）。
        # 账户总资产一律以【真实账户（镜像簿）】为准。
        f"【真实账户（镜像簿）】总资产 {acc['mirror']['total_assets']:.2f} 元"
        f"（现金 {acc['mirror']['cash']:.2f} + 持仓市值 {acc['mirror']['market_value']:.2f}，"
        f"浮动盈亏 {acc['mirror']['pnl']:.2f}）",
        f"【AI 托管簿】总资产 {acc['trust']['total_assets']:.2f} 元"
        f"（现金 {acc['trust']['cash']:.2f} + 持仓市值 {acc['trust']['market_value']:.2f}，"
        f"浮动盈亏 {acc['trust']['pnl']:.2f}，托管{'运行中' if acc['trust']['is_active'] else '未开启'}）",
        "口径说明：托管簿是交给 AI 托管的本金账户。若其起始仓由真实持仓复制建立，"
        "两簿持仓相同属同一笔资产的两种视图，不要把镜像簿与托管簿相加；若为空仓独立建仓"
        "（默认 50 万现金），该托管金才独立于真实账户。用户总资产口径以【真实账户（镜像簿）】为准。",
        "【镜像簿持仓（真实账户）】",
    ]
    for p in mirror_pos:
        lines.append(
            f"  {p['stock_name']}（{p['stock_code']}）{p['hold_qty']}股 成本{p['cost_price']} 现价{p['price']} 盈亏{p['pnl']}"
        )
    if not mirror_pos:
        lines.append("  （空）")

    lines.append("【AI 托管簿持仓（独立于真实账户展示，不与镜像簿合并）】")
    for p in trust_pos:
        lines.append(
            f"  {p['stock_name']}（{p['stock_code']}）{p['hold_qty']}股 成本{p['cost_price']} 现价{p['price']} 盈亏{p['pnl']}"
        )
    if not trust_pos:
        lines.append("  （空）")

    trades = trust_mod.list_trades(user_id, limit=10)
    if trades:
        lines.append("【最近成交】")
        for t in trades:
            lines.append(
                f"  {_dir_zh(t['direction'])} {t['stock_name']}（{t['stock_code']}）"
                f"{t['quantity']}股 @ {t['price']} 理由：{t['ai_reason'] or '无'}"
            )

    return "\n".join(lines)


# ---------- 次日计划投影 ----------

def _render_plan_summary(user_id: int) -> str:
    """最新一份次日行动计划的**精简投影**。

    红线：``TrustPlan.plan_json`` 原始可达 200KB+（``process.research`` 里塞着 Stage1 每个角色的
    完整报告）。这里只取 ``summary`` / ``cash_target`` / ``risk_notes`` / 研究缺失清单，
    再把 ``actions`` 过一遍 ``build_ladders``（与监控卡片、执行层**同源**的纯函数）拿到梯子结构。
    """
    row = trust_mod._get_latest_plan_row(user_id)  # noqa: SLF001 —— 同包内复用，避免重复查询
    if not row or not row.get("plan"):
        return "【次日行动计划】暂无计划（尚未生成）。用户可在「财团托管」页手动生成，或等收盘后自动生成。"

    plan = row["plan"]
    ladders, warnings = plan_actions.build_ladders(plan.get("actions"))
    process = plan.get("process") or {}
    missing = process.get("research_missing") or []

    lines = [
        f"【次日行动计划】执行日 {row.get('trade_date') or '—'}"
        f"（生成于 {_ts(row.get('created_at'))}）",
        f"组合摘要：{plan.get('summary') or '—'}",
        f"目标现金占比：{_weight(plan.get('cash_target'))}",
        f"风险提示：{plan.get('risk_notes') or '—'}",
    ]
    if missing:
        lines.append(f"研究缺失（以下标的缺 Stage1 依据）：{'、'.join(map(str, missing))}")

    total = len(ladders)
    shown = 0
    body: list[str] = []
    for code, buckets in ladders.items():
        block = [f"  {code}"]
        for kind in ("hold", "exit", "entry"):
            for a in buckets.get(kind) or []:
                name = a.get("name") or ""
                act = _ACTION_ZH.get((a.get("action") or "").lower(), a.get("action") or "—")
                reason = _clip(a.get("reason") or "", 120)
                block.append(
                    f"    [{act}] {name} 触发：{_trigger_text(a)} → 目标占比 {_weight(a.get('target_weight'))}"
                    f"{(' ｜ 理由：' + reason) if reason else ''}"
                )
        if len("\n".join(lines + body + block)) > _MAX_PLAN_CHARS:
            break
        body.extend(block)
        shown += 1

    lines.extend(body)
    if warnings:
        lines.append("梯子告警：")
        lines.extend(f"  - {w}" for w in warnings)
    if shown < total:
        lines.append(f"（计划较大，仅显示前 {shown} 只；共 {total} 只）")
    return "\n".join(lines)


# ---------- 工具注册表 ----------

def build_registry(user_id: int) -> list:
    """构造当前登录用户可用的只读工具列表。

    ``user_id`` 由闭包捕获（来自鉴权），因此不出现在任何工具的入参 schema 里。
    """

    # ===== 账户 / 持仓 / 行情 =====

    @tool
    @_guard
    def get_account_overview() -> str:
        """账户总览：真实账户（镜像簿）与 AI 托管簿的总资产、现金、浮动盈亏，两簿持仓，最近成交。

        回答「我有多少钱」「总资产多少」「账户情况」这类问题时**先调它**。
        注意其输出已内含两簿口径说明，回答总资产时必须沿用。成本：DB + 1 次批量行情。
        """
        return account_context(user_id)

    @tool
    @_guard
    def get_positions(book: str = "both") -> str:
        """查持仓明细（逐只：数量/可用/成本/现价/浮动盈亏/盈亏%/当日盈亏/占总资产比）。

        book: "mirror"=真实账户（镜像簿）、"trust"=AI 托管簿、"both"=两本都看。
        账户尚未同步或未建托管簿时是**空态**（不是 0 持仓）。成本：DB + 1 次批量行情。
        """
        want = [0, 1] if book == "both" else ([0] if book == "mirror" else [1])
        labels = {0: "镜像簿（真实账户）", 1: "AI 托管簿"}
        out = []
        for b in want:
            pos = account_mod.get_positions(user_id, b)
            out.append(f"【持仓 · {labels[b]}】")
            if not pos:
                out.append(
                    "  尚未同步真实持仓（镜像簿为空）" if b == 0 else "  尚未建簿或托管簿为空"
                )
                continue
            for p in pos:
                out.append(
                    f"  {p['stock_name']}（{p['stock_code']}）{p['hold_qty']}股"
                    f"（可用 {p['available_qty']}）成本 {p['cost_price']} 现价 {p['price']}"
                    f" 市值 {_money(p['market_value'])} 浮动盈亏 {_money(p['pnl'])}（{_pct(p['pnl_pct'])}）"
                    f" 当日 {_money(p['day_pnl'])}（{_pct(p['day_pnl_pct'])}）"
                    f" 占总资产 {_pct(p['assets_ratio'])}"
                )
        return "\n".join(out)

    @tool
    @_guard
    def get_quotes(codes: list[str]) -> str:
        """查一批证券的**实时行情快照**（现价/昨收/较昨收涨跌幅/量比）。

        codes 为 6 位代码或带后缀代码（如 600519 / 600519.SH / 518880.SH），单次最多 20 个。
        只想知道价格用这个；要看一段时间涨跌用 get_stock_history。
        停牌或代码错误的标的会单独列出。成本：1 次批量行情（60 秒内缓存）。
        """
        codes = [c for c in (codes or []) if str(c).strip()][:_MAX_CODES]
        if not codes:
            return "【行情】未提供证券代码。"
        wind_codes = [_wind.to_wind_code(c) for c in codes]
        quotes = account_mod.get_quotes(wind_codes)
        lines = ["【行情快照】"]
        for wc in wind_codes:
            q = quotes.get(wc)
            if not q:
                lines.append(f"  {wc}：无行情（可能停牌或代码错误）")
                continue
            price, prev = q.get("price"), q.get("prev_close")
            chg = (price - prev) / prev if (price and prev) else None
            vr = q.get("volume_ratio")
            lines.append(
                f"  {wc}：现价 {price} 昨收 {prev} 较昨收 {_pct(chg)}"
                f"{f' 量比 {vr}' if vr else ''}"
            )
        return "\n".join(lines)

    # ===== 收益 / 委托 / 成交 =====

    @tool
    @_guard
    def get_trades(limit: int = 20) -> str:
        """查 AI 托管产生的**成交记录**（时间/方向/标的/价格×数量/金额/费用/AI 下单理由）。

        问「为什么买/卖了某只」「最近成交了什么」时用。
        **注意：后端没有已实现盈亏统计，只有浮动盈亏**（见 get_analytics）。成本：DB。
        """
        n = max(1, min(int(limit or 20), _MAX_LIST))
        rows = trust_mod.list_trades(user_id, limit=n)
        if not rows:
            return "【成交记录】暂无成交（托管尚未产生任何成交）。"
        lines = [f"【成交记录】最近 {len(rows)} 笔"]
        for t in rows:
            lines.append(
                f"  {_ts(t['traded_at'])} {_dir_zh(t['direction'])} {t['stock_name']}（{t['stock_code']}）"
                f"{t['quantity']}股 @ {t['price']} 金额 {_money(t['amount'])} 费用 {_money(t['fee'])}"
                f" ｜ AI 理由：{_clip(t['ai_reason'] or '无', 160)}"
            )
        return "\n".join(lines)

    @tool
    @_guard
    def get_orders(limit: int = 20) -> str:
        """查**委托单**（含未成交、已撤、废单及失败原因）。

        想知道「有没有没成交的单」「为什么这单废了」时用；只看成交用 get_trades。成本：DB。
        """
        n = max(1, min(int(limit or 20), _MAX_LIST))
        rows = trust_mod.list_orders(user_id, limit=n)
        if not rows:
            return "【委托单】暂无委托。"
        status = {0: "已报", 1: "已成", 2: "已撤", 3: "废单"}
        lines = [f"【委托单】最近 {len(rows)} 条"]
        for o in rows:
            lines.append(
                f"  {_ts(o['created_at'])} {_dir_zh(o['direction'])} {o['stock_name']}（{o['stock_code']}）"
                f"{o['quantity']}股 @ {o['price']} 状态 {status.get(o['status'], o['status'])}"
                f"{(' ｜ 失败原因：' + o['fail_reason']) if o['fail_reason'] else ''}"
            )
        return "\n".join(lines)

    @tool
    @_guard
    def get_analytics() -> str:
        """收益归因：成交笔数 + 托管簿浮动盈亏 + 本簿累计已实现盈亏。

        已实现盈亏是**本簿累计**（建簿/重贴快照时清零）。若后端返回 ``null``，表示该簿建于
        口径升级前、累计值无从追溯——工具会如实说明，**不要用成交流水自行倒推一个数字**。
        被问「我赚了多少」时：已实现看本工具（null 就说无从追溯、重贴快照可启用），
        未实现（浮动盈亏）用本工具或 get_positions。
        """
        a = trust_mod.get_analytics(user_id)
        trust = a.get("trust") or {}
        realized = a.get("realized_pnl")
        if realized is None:
            line = (
                f"  已实现盈亏：{a.get('realized_pnl_note') or '暂未统计'}"
                "（后端仅有浮动盈亏口径，请勿用成交流水倒推）"
            )
        else:
            line = f"  已实现盈亏：{_money(realized)}（本簿累计，重贴快照即清零）"
        return "\n".join([
            "【收益归因】",
            line,
            f"  托管簿浮动盈亏：{_money(trust.get('pnl'))}",
            f"  成交总笔数：{a.get('trade_count', 0)}",
        ])

    # ===== 托管设置 / 次日计划 / 监控条件 =====

    @tool
    @_guard
    def get_trust_config() -> str:
        """查**财团托管设置**：托管开关是否开启、是否已建簿、在管范围、投资风格、风控三项、费率。

        问「我的托管设置是什么」「风控参数多少」「托管开了吗」时用。成本：DB。
        """
        try:
            d = trust_mod.get_trust(user_id)
        except ServiceError:
            # 从没碰过托管的用户没有 TrustConfig 行，此时 get_trust 抛 ServiceError。
            # 那是**空态**不是错误：如实说「未开启 / 尚未建簿」，别把「暂无」说成取数失败。
            return "\n".join([
                "【财团托管设置】",
                "  托管开关：未开启",
                "  是否已建簿：尚未建簿（未建簿时托管不会执行任何调仓）",
                "  说明：该账户还没有托管配置——需先在「财团托管」页粘贴持仓快照建簿。",
            ])
        scope = {0: "仅持仓", 1: "仅自选", 2: "全市场"}
        return "\n".join([
            "【财团托管设置】",
            f"  托管开关：{'运行中' if d.get('is_active') else '未开启'}",
            f"  是否已建簿：{'已建簿' if d.get('book_created') else '尚未建簿'}"
            "（未建簿时托管不会执行任何调仓）",
            f"  在管范围：{scope.get(d.get('stock_scope'), d.get('stock_scope'))}"
            f"{('（分组：' + str(d.get('stock_scope_group')) + '）') if d.get('stock_scope_group') else ''}",
            f"  投资风格：{risk_mod.style_label(d.get('style'))}",
            f"  风控 · 单日最大交易次数：{d.get('risk_max_trades_day') if d.get('risk_max_trades_day') is not None else '未设置'}",
            f"  风控 · 单票最大仓位：{_weight(d.get('risk_max_position_pct'))}",
            f"  风控 · 单票止损比例：{_weight(d.get('risk_stop_loss_pct'))}",
            f"  费率 · 佣金：{d.get('fee_commission_rate')}"
            f"｜免五：{'是' if d.get('fee_waive_min') else '否'}"
            f"｜印花税：{d.get('fee_stamp_duty_rate')}",
            f"  运行痕迹：成交 {d.get('trade_count', 0)} 笔 / 委托 {d.get('order_count', 0)} 条"
            f" / 计划 {d.get('plan_count', 0)} 份",
        ])

    @tool
    @_guard
    def get_plan_summary() -> str:
        """读**次日行动计划本身**：执行日、组合摘要、目标现金占比、风险提示，
        以及每只标的的**多档触发梯子**（由浅到深：触发条件 → 目标占比 → 理由）。

        问「明天的计划是什么」「某只票打算怎么操作」「目标仓位多少」时用。
        要看这些条件**当前触发没触发**（带现价）用 get_monitor_conditions。成本：DB。
        """
        return _render_plan_summary(user_id)

    @tool
    @_guard
    def get_monitor_conditions() -> str:
        """读**监控条件卡片**：把次日计划的每一档触发条件，配上**现价 / 量比 / 是否已触发**。

        问「哪个条件触发了」「现在到价了吗」「为什么没执行」时用。
        纯计划文本（不含现价与触发状态）用 get_plan_summary。成本：DB + 1 次批量行情（缓存）。
        """
        d = trust_mod.get_monitor_conditions(user_id)
        actions = d.get("actions") or []
        if not actions:
            return "【监控条件】暂无监控条件（还没有次日行动计划，或计划里没有条件动作）。"
        lines = [
            f"【监控条件】监控日 {d.get('trade_date') or '—'}"
            f"，标的 {d.get('code_count', 0)} 只 / 共 {len(actions)} 档"
        ]
        for a in actions:
            tier = (
                f"第{a['tier_index']}/{a['tier_count']}档"
                if a.get("tier_count") and a.get("tier_index") is not None else ""
            )
            act = _ACTION_ZH.get((a.get("action") or "").lower(), a.get("action") or "—")
            lines.append(
                f"  {'✅已触发' if a.get('triggered') else '⏳监控中'} {a.get('name')}（{a.get('code')}）"
                f" [{act}]{tier} 触发：{_trigger_text(a)} → 目标占比 {_weight(a.get('target_weight'))}"
                f" ｜ 现价 {a.get('price')} 量比 {a.get('volume_ratio')}"
            )
        return "\n".join(lines)

    @tool
    @_guard
    def get_plan_status() -> str:
        """查「今天该看哪天的计划」以及**现在能不能生成新计划**。

        涉及「下次计划什么时候出」「现在能生成计划吗」时用；
        不要自行推算交易日——下一个执行日的判断以本工具为准。成本：DB。
        """
        allowed = trust_mod.check_plan_generation_allowed(user_id)
        mode = {"ok": "可以生成", "confirm": "需二次确认", "blocked": "当前不可生成"}
        return "\n".join([
            "【计划状态】",
            f"  下一执行日：{trust_mod.plan_target_date()}",
            f"  生成条件：{mode.get(allowed.get('mode'), allowed.get('mode'))}",
            f"  说明：{allowed.get('message') or allowed.get('reason') or '—'}",
        ])

    @tool
    @_guard
    def get_managed_symbols() -> str:
        """查**在管标的**（当前托管范围下会被纳入分析与调仓的证券）与研究全集。

        问「托管在管哪些票」「都会分析哪些」时用。成本：DB。
        """
        managed = trust_mod.get_managed_symbols(user_id)
        universe = trust_mod.get_analysis_universe(user_id)
        lines = ["【在管标的】"]
        if managed:
            lines.append("  在管：" + "、".join(f"{m['name']}（{m['code']}）" for m in managed))
        else:
            lines.append("  在管：暂无（尚未建簿，或托管范围下没有标的）")
        if universe:
            lines.append("  研究全集：" + "、".join(f"{u['name']}（{u['code']}）" for u in universe))
        else:
            lines.append("  研究全集：暂无")
        return "\n".join(lines)

    # ===== 研究 / 评级（纯 DB，绝不跑 LLM） =====

    @tool
    @_guard
    def get_research_ratings(codes: list[str] | None = None) -> str:
        """查**引擎深度研究的评级**（买入/增持/持有/减持/卖出 + 目标价 + 止损 + 仓位建议 + 执行摘要）。

        问「某只票评级是什么」「AI 怎么看这只票」「自选里哪只评级最高」时用。
        不传 codes 则查**研究全集**（在管 + 持仓）。单次最多 20 只。
        **只读已缓存的研究结果，不触发新分析**（新的深度分析见 request_deep_analysis）。成本：DB。
        """
        if codes:
            want = [_wind.to_wind_code(c) for c in codes if str(c).strip()][:_MAX_CODES]
        else:
            want = [u["code"] for u in trust_mod.get_analysis_universe(user_id)][:_MAX_CODES]
        if not want:
            return "【研究评级】暂无研究标的（尚未建簿，或在管范围为空）。"
        snap = collect_research_snapshot(user_id, want)
        if not snap:
            return "【研究评级】这些标的尚未做过深度研究（无缓存结果）。"

        lines = ["【研究评级】"]
        for code in want:
            item = snap.get(code)
            if not item:
                lines.append(f"  {code}：尚未研究")
                continue
            rep = item.get("report") or {}
            pm = rep.get("portfolio_manager", "") or ""
            trader = rep.get("trader", "") or ""
            rating = item.get("rating") or "N/A"
            lines.append(f"  {code}（评级 {rating} / {RATING_ZH.get(rating, rating)}，"
                         f"研究日 {item.get('trade_date') or '—'}）")
            for label, text in (("执行摘要", _grab(pm, "Executive Summary")),
                                ("目标价", _grab(pm, "Price Target")),
                                ("止损价", _grab(trader, "Stop Loss")),
                                ("仓位建议", _grab(trader, "Position Sizing"))):
                if text:
                    lines.append(f"    {label}：{_clip(text, 200)}")
        return "\n".join(lines)

    @tool
    @_guard
    def get_research_report(code: str, role: str = "portfolio_manager") -> str:
        """读某只证券**某个研究角色的报告原文**。

        role 可选：market(技术面) / sentiment(情绪) / news(消息面) / fundamentals(基本面) /
        bull(多头) / bear(空头) / research_manager(研究经理) / trader(交易员) /
        aggressive / conservative / neutral / portfolio_manager(组合经理)。
        问「为什么给这个评级」「多空双方怎么说」时用。报告较长会截断。成本：DB。
        """
        wc = _wind.to_wind_code(code)
        role = (role or "portfolio_manager").strip()
        if role not in RESEARCH_FIELDS:
            return f"【研究报告】role 取值无效：{role}。可选：{'、'.join(RESEARCH_FIELDS)}"
        snap = collect_research_snapshot(user_id, [wc])
        item = snap.get(wc)
        if not item:
            return f"【研究报告】{wc} 尚未做过深度研究（无缓存结果）。"
        text = (item.get("report") or {}).get(role)
        if not text:
            return f"【研究报告】{wc} 的「{role}」角色报告为空。"
        return f"【研究报告 · {wc} · {role}】（研究日 {item.get('trade_date') or '—'}）\n{_clip(text, _MAX_FIELD_CHARS)}"

    # ===== 自选 / 搜索 / 历史行情 =====

    @tool
    @_guard
    def get_watchlist() -> str:
        """查**自选股**：分组列表、当前活跃分组、组内标的与现价涨跌幅。成本：DB + 1 次批量行情。"""
        d = watchlist_mod.list_groups(user_id)
        rows = d.get("watchlist") or []
        groups = d.get("groups") or []
        lines = [f"【自选股】活跃分组：{d.get('active_group') or '—'}"]
        if groups:
            lines.append("  分组：" + "、".join(f"{g['name']}({g['count']})" for g in groups))
        if not rows:
            lines.append("  该分组暂无自选标的。")
            return "\n".join(lines)
        for w in rows:
            # 注意：watchlist.list_groups 的 change_pct 已经是**百分数**（round(chg/prev*100, 2)），
            # 不能再过 _pct（那会乘 100，把涨幅 1.23% 说成 123%）。
            cp = w.get("change_pct")
            chg = f"{float(cp):+.2f}%" if cp is not None else "—"
            lines.append(
                f"  {w.get('stock_name')}（{w.get('stock_code')}）现价 {w.get('price')} 涨跌 {chg}"
            )
        return "\n".join(lines)

    @tool
    @_guard
    def search_stock(q: str) -> str:
        """按名称或代码**搜索证券**，拿到标准代码（含市场后缀，如 600519.SH / 00700.HK）。

        当用户只给了名字、或不确定代码时先调它。**不缓存、较慢**，不要连续重复调用。成本：1 次行情接口。
        """
        rows = watchlist_mod.search_stocks(user_id, q) or []
        if not rows:
            return f"【搜索】没查到与「{q}」匹配的证券。"
        return "【搜索】" + "\n".join(f"  {r['name']}（{r['code']}）" for r in rows[:10])

    @tool
    @_guard
    def get_stock_history(code: str, days: int = 20) -> str:
        """查某只证券的**区间/历史行情**：区间涨跌幅、区间最高/最低、首末收盘，以及最近收盘序列。

        问「近 20 天涨了多少」「这一个月最高到过多少」时用。
        days 为**交易日根数**（1~60，默认 20）；days ≤ 10 时额外给出完整 OHLC。
        **单次只查一只**（这是最贵的行情路径，不缓存），需要多只请分别调用。成本：2 次行情接口。
        """
        d = max(1, min(int(days or 20), 60))
        detail = watchlist_mod.get_stock_detail(code, days=d)
        kline = detail.get("kline") or []
        if not kline:
            return f"【历史行情】{code} 无可用 K 线数据。"
        first, last = kline[0], kline[-1]
        closes = [k["close"] for k in kline]
        highs = [k["high"] for k in kline]
        lows = [k["low"] for k in kline]
        chg = (closes[-1] - closes[0]) / closes[0] if closes[0] else None
        lines = [
            f"【历史行情】{detail.get('name')}（{detail.get('code')}）近 {len(kline)} 个交易日",
            f"  区间涨跌幅：{_pct(chg)}（{first['date']} 收 {first['close']} → {last['date']} 收 {last['close']}）",
            f"  区间最高：{max(highs)} ｜ 区间最低：{min(lows)}",
            f"  最新收盘：{last['close']}（{last['date']}）",
        ]
        if d <= 10:
            lines.append("  逐日 OHLC：")
            for k in kline:
                lines.append(
                    f"    {k['date']} 开{k['open']} 高{k['high']} 低{k['low']} 收{k['close']}"
                )
        else:
            lines.append("  最近 10 个交易日收盘：" + "、".join(
                f"{k['date']} {k['close']}" for k in kline[-10:]
            ))
        return "\n".join(lines)

    # ===== 通知 =====

    @tool
    @_guard
    def get_notification_configs() -> str:
        """查**交易通知渠道配置**（飞书 / 企微 / 邮件）及各渠道启用状态。成本：DB。"""
        cfgs = notify_mod.get_notification_configs(user_id) or []
        if not cfgs:
            return "【通知配置】尚未配置任何通知渠道。"
        lines = ["【通知配置】"]
        for c in cfgs:
            url = c.get("webhook_url") or ""
            tail = f"…{url[-6:]}" if len(url) > 6 else ("已配置" if url else "未填写")
            lines.append(
                f"  {c.get('channel')}：{'已启用' if c.get('is_enabled') else '未启用'}（{tail}）"
            )
        return "\n".join(lines)

    # ===== 深度分析（只请求确认，绝不触发） =====

    @tool
    @_guard
    def request_deep_analysis(code: str, name: str = "", date: str = "") -> str:
        """请求对某只证券做**深度分析**（多智能体研究，约 10-15 分钟）。

        本工具**不会启动分析**——它只生成一个确认请求，交由用户在界面上点击确认。
        当用户明确要求「深度分析 / 全面研究 / 仔细看看某只票」时调用。
        返回的是确认请求文本时，请把其中的问题原样转达给用户并等待确认，**不要声称已开始分析**；
        若返回的是澄清问题（标的无法唯一确定），请转达该问题，不要重复调用本工具。
        """
        try:
            plan = intent_mod.resolve_to_plan(
                {"mode": "analyze", "ticker": code, "name": name, "date": date}
            )
        except ServiceError as e:
            return f"无法解析标的「{code or name}」：{e.message}"
        if plan.get("type") == "ask":
            return plan.get("question") or f"没查到「{code or name}」对应的标的，请确认代码。"

        ticker = plan["ticker"]
        disp = plan.get("name") or name or ticker
        d = plan.get("date") or intent_mod.latest_trading_day()
        payload = {
            "ticker": ticker,
            "name": disp,
            "date": d,
            "question": (
                f"是否对 {disp}（{ticker}）做深度分析？"
                "将运行多智能体研究（技术面 / 消息面 / 基本面 / 多空辩论），约耗时 10-15 分钟。"
            ),
        }
        return CONFIRM_PREFIX + json.dumps(payload, ensure_ascii=False)

    return [
        get_account_overview, get_positions, get_quotes,
        get_trades, get_orders, get_analytics,
        get_trust_config, get_plan_summary, get_monitor_conditions,
        get_plan_status, get_managed_symbols,
        get_research_ratings, get_research_report,
        get_watchlist, search_stock, get_stock_history,
        get_notification_configs,
        request_deep_analysis,
    ]
