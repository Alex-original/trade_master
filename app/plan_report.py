"""次日行动计划报告渲染：自包含 HTML，供下载/打印。

结构：
1) 所有持仓的明日行动决策总览表（核心）
2) 组合决策过程（组合分析师草稿 → 风控官意见 → 组合经理最终决策）
3) 各标的深度研究报告（Stage1 逐标的深析，各角色报告原文）
"""
from __future__ import annotations

import html as _html
import re

from app.plan_actions import build_ladders

_ACTION_ZH = {"buy": "买入建仓", "add": "加仓", "reduce": "减仓", "sell": "清仓", "hold": "持有"}
_ACTION_CLS = {"buy": "buy", "add": "buy", "reduce": "sell", "sell": "sell", "hold": "hold"}
_TRIG_ZH = {"none": "立即执行", "open": "开盘执行", "intraday": "盘中执行",
            "price_below": "跌破", "price_above": "涨过"}


def _esc(s) -> str:
    return _html.escape("" if s is None else str(s))


def _md(s) -> str:
    """极简 markdown → html：仅处理 **加粗**，保留换行。"""
    s = _esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return s


# PM 决策正文里的稳定小节标题（见 engine 的 render_pm_decision）
_PM_KEYS = ("Rating", "Executive Summary", "Investment Thesis", "Price Target", "Time Horizon")


def _pm_fields(text: str) -> dict:
    """从组合经理决策正文里抠出 **Rating** / **Executive Summary** 等小节。

    报告第一屏只给「结论 + 结论理由」，靠的就是这两段（Executive Summary /
    Investment Thesis），不能把整篇决策原文平铺上去。
    """
    out: dict[str, str] = {}
    for key in _PM_KEYS:
        m = re.search(rf"\*\*{re.escape(key)}\*\*[：:]\s*(.+?)(?=\n\s*\*\*|\Z)", text or "", re.S)
        if m:
            out[key] = m.group(1).strip()
    return out


def _brief(text: str, limit: int = 90) -> str:
    """一句话摘要：折叠空白，超长截断加省略号（只用于速览表）。"""
    s = re.sub(r"\s+", " ", (text or "").strip())
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    return s if len(s) <= limit else s[:limit] + "…"


def _fmt(v, nd=2) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _trigger_text(a: dict) -> str:
    t = a.get("trigger_type") or "none"
    label = _TRIG_ZH.get(t, t)
    s = label
    if t in ("price_below", "price_above") and a.get("trigger_price") is not None:
        s = f"{label} {a['trigger_price']}"
    # 量能门槛只在真的写了的时候才拼，老计划渲染逐字不变
    if a.get("volume_ratio_min") is not None:
        s += f"（量比≥{a['volume_ratio_min']}）"
    return s


def _weight_text(a, fallback: float | None = None) -> str:
    w = a.get("target_weight") if a else None
    if w is None:
        w = fallback
    if w is None:
        return "—"
    return f"{float(w) * 100:.1f}%"


def _tier_line(a: dict) -> str:
    """一档 → 一行文字：``跌破 8.9（量比≥1.5）→ 13.8%``。"""
    return f"{_trigger_text(a)} → {_weight_text(a)}"


def _trigger_cell(tiers: list[dict]) -> str:
    """触发条件列内容（**已经是 HTML**，调用方不要再转义）。

    单档时与历史渲染逐字一致；多档时按由浅到深编号、``<br>`` 堆叠。
    """
    if not tiers:
        return "—"
    if len(tiers) == 1:
        return _esc(_trigger_text(tiers[0]))
    return "<br>".join(
        f"<span class='tier-n'>{i}.</span> {_esc(_tier_line(a))}"
        for i, a in enumerate(tiers, 1)
    )


def _weight_cell(tiers: list[dict], fallback: float | None = None) -> str:
    """目标占比列：单档同历史（含 fallback 到当前占比），多档显示档位序列。"""
    if not tiers:
        return _weight_text(None, fallback)
    if len(tiers) == 1:
        return _weight_text(tiers[0], fallback)
    return " → ".join(_weight_text(a) for a in tiers)


def render_plan_report(plan: dict, positions: list[dict], research: dict,
                       cash: float, trade_date: str, generated_at: float,
                       research_missing: list[str] | None = None,
                       research_frozen: bool = False,
                       banner: str = "") -> str:
    """渲染完整报告 HTML。

    ``research`` 优先来自计划生成时冻结的快照；``research_frozen=False`` 说明是
    回落到实时查库的旧计划。``research_missing`` 列出本次计划无研究依据的标的。

    ``banner`` 非空时在标题下方压一条横幅。**回测**用它标注"这是模拟撮合、不是实盘计划"——
    回测报告与实盘报告共用这一个渲染器（格式因此逐字一致），没有这条横幅，一份回测报告
    落到人手里是**看不出来**它没在实盘执行过的。默认空串 ⇒ 实盘输出与加这个参数之前逐字相同。
    """
    # 与执行层/监控卡片同一口径：先归一+分档，保证报告内部（第一章行动表与 3.0
    # 速览表）以及报告与线上执行看到的是同一套动作。一只票可能有多档（梯子）。
    ladders, ladder_warnings = build_ladders(plan.get("actions"))
    process = plan.get("process") or {}
    for w in process.get("ladder_warnings") or []:
        if w not in ladder_warnings:
            ladder_warnings.append(w)

    def _tiers_of(code: str) -> list[dict]:
        """一只票要展示的档位（由浅到深）：减仓梯子优先，其次加仓，最后 hold 基准。"""
        bk = ladders.get(code) or {}
        for kind in ("exit", "entry", "hold"):
            if bk.get(kind):
                return list(bk[kind])
        return []

    total_mv = sum((p.get("market_value") or 0.0) for p in positions)
    total_assets = cash + total_mv

    # --- 合并持仓 + 动作，构建「所有持仓明日行动」行 ---
    rows = []
    for p in positions:
        tiers = _tiers_of(p["stock_code"])
        a = tiers[0] if tiers else None
        rows.append({
            "code": p["stock_code"], "name": p["stock_name"],
            "price": p.get("price"), "cost": p.get("cost_price"),
            "hold": p.get("hold_qty"), "avail": p.get("available_qty"),
            "mv": p.get("market_value"), "weight": p.get("assets_ratio"),
            "action": (a.get("action") if a else "hold"),
            "tiers": tiers,
            "trigger": _trigger_text(a) if a else "—",
            "reason": (a.get("reason") or "") if a else "团队未特别调整，默认持有",
            "is_new": False,
        })
    held = {p["stock_code"] for p in positions}
    for code, bk in ladders.items():
        if code in held:
            continue
        tiers = _tiers_of(code)
        a = tiers[0] if tiers else None
        rows.append({
            "code": code, "name": (a.get("name") if a else None) or code,
            "price": None, "cost": None, "hold": 0, "avail": 0,
            "mv": 0.0, "weight": 0.0,
            "action": (a.get("action") if a else "hold"),
            "tiers": tiers,
            "trigger": _trigger_text(a) if a else "—",
            "reason": (a.get("reason") or "") if a else "", "is_new": True,
        })

    # 排序：新建仓排后，其余按市值降序
    rows.sort(key=lambda r: (r["is_new"], -(r["mv"] or 0)))

    parts: list[str] = []
    parts.append("""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>次日行动计划报告</title>
<style>
:root{--brand:#2962FF;--brand-2:#5B5BFF;--ink:#1A1A1A;--sub:#666;--mut:#999;--line:#E5E5E5;--bg:#F5F6F8;--card:#fff;}
*{box-sizing:border-box;}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink);line-height:1.65;}
.wrap{max-width:960px;margin:0 auto;padding:32px 24px 64px;}
header{background:linear-gradient(120deg,var(--brand),var(--brand-2));color:#fff;border-radius:16px;padding:28px 32px;margin-bottom:20px;}
header h1{margin:0 0 8px;font-size:24px;font-weight:700;}
header .meta{font-size:13px;opacity:.92;}
.kpis{display:flex;gap:16px;margin:-8px 0 20px;flex-wrap:wrap;}
.kpi{flex:1;min-width:140px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}
.kpi .k{font-size:12px;color:var(--mut);}
.kpi .v{font-size:20px;font-weight:700;margin-top:2px;}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin-bottom:20px;}
section h2{font-size:18px;margin:0 0 4px;color:var(--brand);}
section .sec-sub{font-size:12px;color:var(--mut);margin-bottom:16px;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top;}
th{background:#FAFBFC;color:var(--sub);font-weight:600;white-space:nowrap;}
td.num,th.num{text-align:right;}
.tag{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap;}
.tag.buy{background:rgba(230,57,70,.12);color:#E63946;}
.tag.sell{background:rgba(24,160,88,.12);color:#18A058;}
.tag.hold{background:rgba(120,120,130,.12);color:#6b6b76;}
.block{margin-bottom:18px;}
.block h3{font-size:15px;margin:0 0 8px;padding-left:10px;border-left:3px solid var(--brand);}
.pre{background:#FAFBFC;border:1px solid var(--line);border-radius:10px;padding:12px 14px;font-size:13px;white-space:pre-wrap;word-break:break-word;}
.chip{display:inline-block;background:rgba(41,98,255,.1);color:var(--brand);border-radius:6px;padding:1px 8px;font-size:12px;margin-right:6px;}
.research-card{border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px;}
.research-card h4{margin:0 0 10px;font-size:15px;}
.rating{display:inline-block;padding:2px 12px;border-radius:999px;font-size:12px;font-weight:700;background:rgba(41,98,255,.1);color:var(--brand);}
.note{font-size:12px;color:var(--mut);margin-top:12px;}
/* 口径说明：解释相邻两个区块为何可能「看起来不一致」，正文小一号、左侧品牌色竖线 */
.sec-note{font-size:12px;line-height:1.75;color:var(--sub);background:#FAFBFC;border-left:3px solid var(--brand);border-radius:0 8px 8px 0;padding:10px 14px;margin:0 0 12px;}
.sec-note b{color:var(--brand);}
.tier-n{font-size:11px;color:var(--mut);font-weight:600;}
.warnbox{font-size:12px;line-height:1.7;color:#8a5300;background:rgba(255,159,10,.10);border-left:3px solid #FF9F0A;border-radius:0 8px 8px 0;padding:10px 14px;margin:12px 0 0;}
.warn{font-size:13px;line-height:1.7;color:#8a5a00;background:rgba(255,176,32,.10);border:1px solid rgba(255,176,32,.35);border-radius:10px;padding:10px 14px;margin:10px 0 4px;}
/* 深度研究：每票一个标签页，落地页只给结论 + 结论理由，明细默认收起 */
.rtabs{display:flex;flex-wrap:wrap;gap:8px;margin:4px 0 14px;}
.rtab{border:1px solid var(--line);background:#fff;color:var(--sub);border-radius:999px;padding:6px 14px;font-size:13px;cursor:pointer;font-family:inherit;}
.rtab.active{background:var(--brand);border-color:var(--brand);color:#fff;font-weight:600;}
.rpanel{display:none;}
.rpanel.active{display:block;}
.ov{border:1px solid var(--line);border-radius:12px;padding:14px 16px;background:#FAFBFC;margin-bottom:12px;}
.ov-k{font-size:12px;color:var(--mut);margin:10px 0 4px;}
.ov-k:first-child{margin-top:0;}
.more-btn{border:1px dashed var(--line);background:#fff;color:var(--brand);border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer;font-family:inherit;margin-top:4px;}
@media print{
  body{background:#fff;}.wrap{max-width:none;padding:0;}section{border:none;page-break-inside:avoid;}
  /* 打印时展开全部标的与全部明细，避免导出的 PDF 丢掉正文 */
  .rtabs{display:none;}
  .rpanel{display:block!important;page-break-before:always;}
  .rpanel:first-of-type{page-break-before:auto;}
  [hidden]{display:block!important;}
  .more-btn{display:none;}
}
</style>
</head>
<body>
<div class="wrap">
""")

    from datetime import datetime
    gen_ts = datetime.fromtimestamp(generated_at).strftime("%Y-%m-%d %H:%M:%S") if generated_at else "—"
    parts.append(
        f"""<header>
  <h1>AI 托管 · 次日行动计划报告</h1>
  <div class="meta">计划生效日：{_esc(trade_date or '—')} ｜ 生成时间：{gen_ts}</div>
</header>
"""
    )
    if banner:
        parts.append(f"<div class='warnbox' style='margin:0 0 20px'>{_esc(banner)}</div>")
    parts.append(
        f"""
<div class="kpis">
  <div class="kpi"><div class="k">账户总资产</div><div class="v">¥ {_fmt(total_assets)}</div></div>
  <div class="kpi"><div class="k">可用现金</div><div class="v">¥ {_fmt(cash)}</div></div>
  <div class="kpi"><div class="k">持仓市值</div><div class="v">¥ {_fmt(total_mv)}</div></div>
  <div class="kpi"><div class="k">持仓数量</div><div class="v">{len(positions)} 只</div></div>
  <div class="kpi"><div class="k">目标现金占比</div><div class="v">{_weight_text({"target_weight": plan.get("cash_target")})}</div></div>
</div>
"""
    )

    # --- 一、行动决策总览 ---
    parts.append(
        """<section>
  <h2>一、所有持仓 · 明日行动决策</h2>
  <div class="sec-sub">按计划在下一交易日开盘后，由执行层盯盘按触发条件自动执行；一只票有多档时，触发条件列按<b>由浅到深</b>编号堆叠</div>
  <div style="overflow-x:auto"><table>
  <thead><tr>
    <th>标的</th><th class="num">现价</th><th class="num">成本</th><th class="num">持仓/可用</th><th class="num">市值</th><th class="num">占比</th>
    <th>明日动作</th><th class="num">目标占比</th><th>触发条件</th><th>理由</th>
  </tr></thead><tbody>
"""
    )
    for r in rows:
        tag = _ACTION_ZH.get(r["action"], r["action"])
        cls = _ACTION_CLS.get(r["action"], "hold")
        name = _esc(r["name"]) + ("<span style='color:var(--brand);font-size:11px'>（新建仓）</span>" if r["is_new"] else "")
        n = len(r["tiers"])
        tag_html = f"<span class='tag {cls}'>{tag}</span>"
        if n > 1:
            tag_html += f"<br><span class='tier-n'>共 {n} 档</span>"
        parts.append(
            f"<tr><td><strong>{name}</strong><br><span style='color:var(--mut);font-size:11px'>{_esc(r['code'])}</span></td>"
            f"<td class='num'>{_fmt(r['price'])}</td><td class='num'>{_fmt(r['cost'])}</td>"
            f"<td class='num'>{r['hold']} / {r['avail']}</td><td class='num'>{_fmt(r['mv'])}</td>"
            f"<td class='num'>{_weight_text(None, r['weight'])}</td>"
            f"<td>{tag_html}</td><td class='num'>{_weight_cell(r['tiers'], r['weight'])}</td>"
            f"<td>{_trigger_cell(r['tiers'])}</td><td style='min-width:180px'>{_esc(r['reason'])}</td></tr>"
        )
    parts.append("</tbody></table></div>")
    if ladder_warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in ladder_warnings)
        parts.append(
            "<div class='warnbox'><strong>计划中的部分档位未通过校验，已按由浅到深截断"
            "（保留前面可执行的档，后面被丢弃）：</strong>"
            f"<ul style='margin:6px 0 0;padding-left:20px'>{items}</ul></div>"
        )
    parts.append("</section>")

    # --- 二、组合决策过程 ---
    parts.append("<section><h2>二、组合决策过程</h2>"
                 "<div class='sec-sub'>组合分析师起草 → 风控官审查 → 组合经理最终决策</div>")

    draft = process.get("analyst_draft") or {}
    if draft:
        parts.append("<div class='block'><h3>2.1 组合分析师草稿</h3>")
        if draft.get("summary"):
            parts.append(f"<div class='pre'>{_md(draft['summary'])}</div>")
        if draft.get("actions"):
            parts.append("<div style='overflow-x:auto'><table><thead><tr><th>标的</th><th>动作</th><th class='num'>目标占比</th><th>触发</th><th>理由</th></tr></thead><tbody>")
            for a in draft.get("actions", []):
                tag = _ACTION_ZH.get(a.get("action"), a.get("action"))
                cls = _ACTION_CLS.get(a.get("action"), "hold")
                parts.append(
                    f"<tr><td><strong>{_esc(a.get('name') or a.get('code'))}</strong><br><span style='color:var(--mut);font-size:11px'>{_esc(a.get('code'))}</span></td>"
                    f"<td><span class='tag {cls}'>{tag}</span></td><td class='num'>{_weight_text(a)}</td>"
                    f"<td>{_esc(_trigger_text(a))}</td><td>{_esc(a.get('reason') or '')}</td></tr>"
                )
            parts.append("</tbody></table></div>")
        parts.append("</div>")

    risk = process.get("risk_review") or {}
    if risk:
        parts.append("<div class='block'><h3>2.2 风控官审查意见</h3>")
        verdict = "合规通过" if risk.get("verdict") == "ok" else "需修正"
        parts.append(f"<div>结论：<span class='chip'>{_esc(verdict)}</span></div>")
        issues = risk.get("issues") or []
        if issues:
            parts.append("<div class='pre' style='margin-top:8px'>" + "\n".join("• " + _esc(i) for i in issues) + "</div>")
        if risk.get("adjustments"):
            parts.append(f"<div class='pre' style='margin-top:8px'><strong>修正建议</strong>\n{_md(risk['adjustments'])}</div>")
        parts.append("</div>")

    parts.append("<div class='block'><h3>2.3 组合经理最终决策</h3>")
    if plan.get("summary"):
        parts.append(f"<div class='pre'>{_md(plan['summary'])}</div>")
    if plan.get("risk_notes"):
        parts.append(f"<div class='pre' style='margin-top:8px'><strong>风险说明</strong>\n{_md(plan['risk_notes'])}</div>")
    parts.append("</div></section>")

    # --- 三、各标的深度研究报告（每票一个标签页；落地页只给结论 + 结论理由） ---
    parts.append("<section><h2>三、各标的深度研究报告</h2>"
                 "<div class='sec-sub'>Stage1 逐标的深析 — 首屏只给每个标的的**最终结论与结论理由**，"
                 "切换标签看单只标的，点「展开全部明细」看行情/情绪/新闻/基本面/多空辩论全过程"
                 + ("　｜　计划生成时冻结快照" if research_frozen else "　｜　按最新一次 Stage1 结果回溯") + "</div>")
    if research_missing:
        parts.append(
            "<div class='warn'>"
            + _md(
                "⚠️ 本次计划生成时，以下标的**没有**可用的深度研究结论："
                + "、".join(research_missing)
                + "。组合决策层对它们只能基于账户现状与通用判断，"
                "执行层也不会在没有研究依据的情况下自动买卖。"
            )
            + "</div>"
        )
    if research:
        name_of = {p["stock_code"]: p["stock_name"] for p in positions}
        items = sorted(research.items())  # 按代码稳定排序

        # 速览表：一屏看完所有标的的**双层结论**（研究层观点 + 组合层动作）
        parts.append(
            "<div class='block'><h3>3.0 各标的结论速览</h3>"
            "<div class='sec-note'>本表并列两个决策层的结论，口径不同、<b>不必然一致</b>："
            "「研究评级」是研究层对<b>单只标的</b>独立深析后给出的观点；"
            "「组合决策」是组合决策层统筹<b>整个账户</b>（含现金、占比、集中度）后拍板的"
            "可执行动作。组合层可以给出「研究层 Hold，但跌破某价就减仓」这类条件动作——"
            "那不是与评级矛盾，而是把研究层的风控条件转成了可执行方案，<b>未触发即不动</b>。"
            "两者若有出入，<b>实际执行以第一章「明日行动决策」为准</b>。"
            "另注：条件动作的目标占比是<b>触发之后</b>要达到的水平，未触发时保持现状；"
            "一只票可以给出<b>多档梯子</b>（如「跌破 8.90 减到 13.8%、再跌破 8.76 减到 9%、"
            "跌破 8.75 清仓」），触发条件列按<b>由浅到深</b>编号堆叠，每档的目标占比是该档触发后"
            "要达到的<b>绝对</b>水平（不是增量）；执行层每分钟判定一次、<b>每 tick 每票最多推进一档</b>，"
            "所以穿越多档会分批成交而不是一次打到底；带量能门槛的档（量比≥N）在连续竞价开始"
            "（≥09:30）后才判定。</div>"
            "<div style='overflow-x:auto'><table><thead><tr>"
            "<th>标的</th><th>研究评级</th><th>组合决策</th><th class='num'>目标占比</th>"
            "<th>触发条件</th><th>结论</th><th class='num'>目标价</th>"
            "</tr></thead><tbody>"
        )
        for code, r in items:
            rep = r.get("report") or {}
            pm = _pm_fields(rep.get("portfolio_manager") or "")
            tiers = _tiers_of(code)
            a = tiers[0] if tiers else None
            if a:
                cls = _ACTION_CLS.get(a.get("action"), "hold")
                tag = _ACTION_ZH.get(a.get("action"), a.get("action"))
                combo = f"<span class='tag {cls}'>{_esc(tag)}</span>"
                weight = _weight_cell(tiers)
                # 速览列窄：只给最浅档的触发条件 + 档数，完整梯子看第一章
                trig = _trigger_text(a)
                if len(tiers) > 1:
                    combo += f"<br><span class='tier-n'>共 {len(tiers)} 档</span>"
                    weight = " → ".join(_weight_text(x) for x in tiers)
                    trig += f" 等 {len(tiers)} 档"
            else:
                combo = "<span style='color:var(--mut)'>—</span>"
                weight, trig = "—", "—"
            parts.append(
                f"<tr><td><strong>{_esc(name_of.get(code, code))}</strong><br>"
                f"<span style='color:var(--mut);font-size:11px'>{_esc(code)}</span></td>"
                f"<td><span class='rating'>{_esc(r.get('rating') or '—')}</span></td>"
                f"<td>{combo}</td>"
                f"<td class='num'>{_esc(weight)}</td>"
                f"<td style='min-width:110px'>{_esc(trig)}</td>"
                f"<td>{_esc(_brief(pm.get('Executive Summary') or '—', 80))}</td>"
                f"<td class='num'>{_esc(pm.get('Price Target') or '—')}</td></tr>"
            )
        parts.append("</tbody></table></div></div>")

        # 标签按钮
        parts.append("<div class='rtabs'>")
        for i, (code, r) in enumerate(items):
            active = " active" if i == 0 else ""
            parts.append(
                f"<button class='rtab{active}' data-i='{i}' onclick=\"showResearch({i})\">"
                f"{_esc(name_of.get(code, code))} {_esc(code)} · {_esc(r.get('rating') or '—')}</button>"
            )
        parts.append("</div>")

        # 每个标的的面板：概览（结论/理由）+ 可展开的明细
        for i, (code, r) in enumerate(items):
            rep = r.get("report") or {}
            pm = _pm_fields(rep.get("portfolio_manager") or "")
            active = " active" if i == 0 else ""
            parts.append(f"<div class='rpanel{active}' data-i='{i}'>")
            parts.append(
                f"<h4 style='margin:0 0 10px;font-size:15px'>{_esc(name_of.get(code, code))} "
                f"<span style='color:var(--mut);font-size:12px'>{_esc(code)}</span> "
                f"<span class='rating'>评级 {_esc(r.get('rating') or '—')}</span></h4>"
            )
            parts.append("<div class='ov'>")
            parts.append("<div class='ov-k'>最终结论</div>"
                         f"<div class='pre'>{_md(pm.get('Executive Summary') or '（本次未产出组合经理结论）')}</div>")
            parts.append("<div class='ov-k'>结论理由</div>"
                         f"<div class='pre'>{_md(pm.get('Investment Thesis') or '—')}</div>")
            extra = []
            if pm.get("Price Target"):
                extra.append(f"目标价 {_esc(pm['Price Target'])}")
            if pm.get("Time Horizon"):
                extra.append(f"持有周期 {_esc(pm['Time Horizon'])}")
            if extra:
                parts.append(f"<div class='ov-k'>{'　｜　'.join(extra)}</div>")
            parts.append("</div>")

            parts.append(
                f"<button class='more-btn' id='rmore-{i}' onclick=\"toggleDetail({i})\">"
                f"展开全部明细（行情 / 情绪 / 新闻 / 基本面 / 多空辩论 / 交易员 / 风控 / 组合经理原文）</button>"
            )
            parts.append(f"<div class='detail-block' id='rdetail-{i}' hidden style='margin-top:12px'>")
            sections = [
                ("行情/技术面", rep.get("market")), ("情绪", rep.get("sentiment")),
                ("新闻/宏观", rep.get("news")), ("基本面", rep.get("fundamentals")),
                ("看多研究员", rep.get("bull")), ("看空研究员", rep.get("bear")),
                ("研究经理计划", rep.get("research_manager")), ("交易员方案", rep.get("trader")),
                ("激进风控", rep.get("aggressive")), ("保守风控", rep.get("conservative")),
                ("中性风控", rep.get("neutral")), ("组合经理决策（原文）", rep.get("portfolio_manager")),
            ]
            for label, text in sections:
                if text and str(text).strip():
                    parts.append(f"<div class='block' style='margin-bottom:10px'><h3 style='font-size:13px'>{_esc(label)}</h3>"
                                 f"<div class='pre'>{_md(text)}</div></div>")
            parts.append("</div></div>")
    else:
        parts.append(
            "<div class='warn'>"
            + _md(
                "本计划**没有任何**深度研究依据：生成时要么未跑通 Stage1 研究层，"
                "要么研究层未产出有效结论。这份计划完全来自组合决策层对账户现状的通用判断，"
                "请谨慎对待其中的方向性观点。"
            )
            + "</div>"
        )
    parts.append("</section>")

    parts.append("""
<div class="note">本报告由 AI 托管组合决策团队自动生成，仅供模拟交易学习参考，不构成投资建议。</div>
</div>
<script>
function showResearch(i){
  document.querySelectorAll('.rtab').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-i') === String(i));
  });
  document.querySelectorAll('.rpanel').forEach(function(p){
    p.classList.toggle('active', p.getAttribute('data-i') === String(i));
  });
}
function toggleDetail(i){
  var d = document.getElementById('rdetail-' + i);
  var b = document.getElementById('rmore-' + i);
  if (!d) return;
  if (d.hasAttribute('hidden')) {
    d.removeAttribute('hidden');
    if (b) b.textContent = '收起明细';
  } else {
    d.setAttribute('hidden', '');
    if (b) b.textContent = '展开全部明细（行情 / 情绪 / 新闻 / 基本面 / 多空辩论 / 交易员 / 风控 / 组合经理原文）';
  }
}
</script>
</body>
</html>
""")

    return "".join(parts)
