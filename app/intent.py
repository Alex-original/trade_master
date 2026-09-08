"""自然语言 → 深度分析意图：拆解、澄清、Wind 名称解析。

链路（/chat 端点）：
    前端一句话 → LLM(deepseek-flash) 拆成 {mode, ticker, name, market, date, question}
    ├─ mode=ask  → 直接返回澄清问题（市场/标的不确定等）
    └─ mode=analyze → 后端确定性解析标的：
                        裸 6 位代码 → 用本地前缀映射定交易所，再向 Wind 取简称校验
                        公司名     → Wind get_stock_basicinfo 按名称解析
                       （查不到/多市场 → 转 ask），得到可执行计划 {ticker,date,name}
    前端对计划二次确认后调 /analysis 真正跑 10-15 分钟的多智能体分析。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from tradingagents.dataflows import wind as _wind

# ───────────────────────────────────────────────────────────── LLM 调用

DEEPSEEK_BASE = "https://api.deepseek.com/chat/completions"


def _llm_json(system: str, history: list[dict], max_tokens: int = 8000) -> dict:
    """调 DeepSeek 并要求只输出一个 JSON 对象（用于意图拆解，轻量）。

    注意 max_tokens 必须给足：deepseek-v4-flash 也会消耗 hidden reasoning_tokens，
    预算太小（曾用 1200）时模型"思考"就把额度用光，可见 content 为空串 → 解析必失败。
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("未找到 DEEPSEEK_API_KEY")
    model = os.getenv("DEEPSEEK_QUICK_MODEL", "deepseek-v4-flash")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *history],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        DEEPSEEK_BASE,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from e
    content = (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return _extract_json(content)


def _extract_json(text: str) -> dict:
    """从模型输出里抠出第一个完整 JSON 对象（容忍围栏/前后废话）。"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"模型未返回 JSON: {text[:200]}")
    return json.loads(text[start : end + 1])


# ───────────────────────────────────────────────────────────── 意图拆解

_NOW = _dt.datetime.now()
_IS_BEFORE_CLOSE = _NOW.hour < 15


def _today_token() -> str:
    """'今天'应落到的具体日期：周末→上周五；收盘前→上一个工作日；否则今天。"""
    if _NOW.weekday() >= 5:
        return latest_trading_day(_NOW.date())
    if _IS_BEFORE_CLOSE:
        d = _NOW.date() - _dt.timedelta(days=1)
        return latest_trading_day(d)
    return _NOW.strftime("%Y-%m-%d")


_SYSTEM_INTENT = f"""你是 A股深度分析的意图路由器。用户的输入是一条自然语言需求（可能有多轮对话历史），
你需要判断它是不是"对某一只证券做深度分析（行情+新闻+基本面+投资建议）"，并把它拆成结构化 JSON。

只输出一个 JSON 对象，不要输出任何其它文字。字段如下：
{{
  "mode": "analyze" | "ask",
  "ticker": "6位数字代码，或带后缀代码(如 0700.HK / AAPL)；没给代码就填空串",
  "name": "公司/基金/ETF名称（中文）；没给名称也填空串",
  "market": "A股 | 港股 | 美股 | 北交所 | \"\"",
  "date": "today | yesterday | 具体 YYYY-MM-DD | \"\"（空串=默认最近交易日）",
  "question": "当 mode=ask 时要向用户澄清的问题（中文，一句话）",
  "summary": "用一句话中文复述你要执行的任务"
}}

规则：
1. 用户想分析某只具体证券（给 6 位代码、或公司/基金名，要求行情/建议/投资分析/基本面）→ mode=analyze。
   用户没指明具体标的（如"帮我分析今天涨幅最大的股票"）、或意图与"单只证券深度分析"无关 → mode=ask，
   question 里问清"要分析哪一只/什么需求"。
2. 股票代码：优先用用户原话里的数字，A股 6 位数字直接放 ticker（不带后缀，后端判定交易所）。不要臆造代码。
   若用户只给名称没给代码，把名称放 name，ticker 填空。
3. 公司名同时有 A股和港股（中芯国际=688981.SH+00981.HK、中国平安、比亚迪、招商银行、中信证券等）→ mode=ask，
   question 列出可选项让用户选市场。只在一个市场上市的名称（贵州茅台、腾讯控股[仅港股]）→ mode=analyze。
4. 日期：今天是 {_NOW.strftime('%Y-%m-%d')}（星期{('一二三四五六日')[_NOW.weekday()]}）。
   "今天/现在/今日"→ date="today"；"昨天"→"yesterday"；给了明确日期→原样 YYYY-MM-DD；没提→""。
   日期语义（含周末/收盘前后回退）由后端处理，你不要自己换算。
5. market：能从代码/名称/原话明确推断才填（如用户说"分析下港股腾讯"→market=港股、name=腾讯）；推断不出填空串。
6. 若用户在多轮对话里已经选定了"港股/美股/北交所"（常见于你之前列出 A/港股选项、用户选了非 A股那个）：
   直接把对应市场代码填进 ticker（港股如 00981.HK、美股如 AAPL），不要只给名字——因为后端只能自动把
   A股名字解析成代码，港股/美股需要你给出代码。A股选择则可以只给名字（如 688981 或 中芯国际）。
"""


def parse_intent(messages: list[dict]) -> dict:
    """多轮对话 → 结构化意图。异常统一抛给上层做兜底提示。"""
    return _llm_json(_SYSTEM_INTENT, [m for m in messages if m.get("role") in ("user", "assistant")])


# ───────────────────────────────────────────────────────────── Wind 名称解析

def _normalize_code(code: str, exchange_hint: str) -> str:
    """把 Wind 表格里的代码归一化成可用交易代码（159779.OF→159779.SZ 等）。"""
    c = (code or "").strip().upper()
    digits = re.match(r"(\d{6})", c)
    if not digits:
        return c  # 非 A股数字代码（美股 AAPL、港股 0700.HK 已带后缀）原样
    num = digits.group(1)
    hint = exchange_hint or ""
    if "香港" in hint:
        return num + ".HK"
    if "北京" in hint:
        return num + ".BJ"
    if "上海" in hint:
        return num + ".SH"
    if "深圳" in hint:
        return num + ".SZ"
    return _wind._infer_cn_exchange(num)


def _is_bond(row: dict[str, str]) -> bool:
    """债券/水务等不可做股票分析的主体。"""
    name = row.get("name", "")
    return any(w in name for w in ("债", "水务", "次级", "地方债", "可转"))


def _basicinfo_rows(term: str) -> list[dict[str, str]]:
    """get_stock_basicinfo（NL 表格）→ [{code,name,exchange}]，失败返回空表。"""
    r = _wind._call_tool(
        "stock_data",
        "get_stock_basicinfo",
        {"question": f"用表格返回 {term} 的证券简称、Wind代码、上市地点（不要多余说明）"},
        timeout=90,
    )
    data = r.get("data") or {}
    blocks = data.get("data") if isinstance(data, dict) else data
    if not isinstance(blocks, list):
        return []
    rows: list[dict[str, str]] = []
    for block in blocks:
        cols = [(i, c.get("name", "")) for i, c in enumerate(block.get("columns", []) or [])]
        for raw in block.get("rows", []) or []:
            rec = {}
            for i, name in cols:
                if i < len(raw):
                    rec[name] = str(raw[i])
            rec = _pick_fields(rec)
            if rec.get("code"):
                rows.append(rec)
    # 去重
    seen, out = set(), []
    for row in rows:
        if row["code"] in seen:
            continue
        seen.add(row["code"])
        out.append(row)
    return out


def _pick_fields(rec: dict[str, str]) -> dict[str, str]:
    """把 Wind 表格行里的列名收敛成 {code,name,exchange}。"""
    def find(keys, exclude=()):
        for k, v in rec.items():
            if v in (None, "", "null"):
                continue
            if any(key in k for key in keys) and not any(x in k for x in exclude):
                return v
        return ""
    raw_code = find(["Wind代码", "代码"])
    name = find(["证券简称", "简称", "名称", "标的"], exclude=["代码"])
    exchange = find(["上市地点", "上市板块", "基金上市地点", "交易所"], exclude=["代码"])
    code = _normalize_code(raw_code, exchange)
    return {"code": code, "name": name, "exchange": exchange}


_MARKET_EXCHANGE = {
    "A股": ("SH", "SZ"),
    "港股": ("HK",),
    "北交所": ("BJ",),
    "美股": (),
}


def _fits_market(row: dict[str, str], market: str) -> bool:
    suffix = row["code"].rsplit(".", 1)[-1] if "." in row["code"] else ""
    ex = _MARKET_EXCHANGE.get(market)
    return ex is None or not ex or suffix in ex


# ───────────────────────────────────────────────────────────── 日期

def latest_trading_day(from_date: _dt.date | None = None) -> str:
    """最近一个工作日（忽略法定节假日，够用即可）。"""
    d = from_date or _dt.date.today()
    while d.weekday() >= 5:  # 六/日回退
        d -= _dt.timedelta(days=1)
    return d.isoformat()


def _to_iso(token: str) -> str:
    """把模型给的 date token 解析成确定 YYYY-MM-DD。空串→最近交易日。"""
    t = (token or "").strip().lower()
    if not t:
        return latest_trading_day()
    if t == "today":
        return _today_token()
    if t == "yesterday":
        return latest_trading_day(_dt.date.today() - _dt.timedelta(days=1))
    # 具体日期：若落在周末，回退到最近工作日
    try:
        d = _dt.date.fromisoformat(t)
    except ValueError:
        return latest_trading_day()
    return latest_trading_day(d)


# ───────────────────────────────────────────────────────────── 计划组装

def _plan(row: dict[str, str], date: str, fallback_name: str) -> dict:
    return {
        "type": "plan",
        "ticker": row["code"],
        "name": row.get("name") or fallback_name,
        "market": row.get("exchange") or "",
        "date": date,
        "summary": f"分析 {row.get('name') or fallback_name}（{row['code']}，日期 {date}）",
    }


def _resolve_by_suffix(num: str, suffix: str) -> dict | None:
    """按 代码+交易所后缀 查一次 basicinfo，返回第一条可分析行。"""
    wc = num + "." + suffix
    for row in _basicinfo_rows(wc):
        if _is_bond(row):
            continue
        return row
    return None


def resolve_to_plan(intent: dict) -> dict:
    """把解析意图转成可执行计划；不确定就转成 ask 澄清。

    返回 {type:'plan', ticker,name,market,date,summary} 或 {type:'ask', question}。
    """
    ticker = (intent.get("ticker") or "").strip().upper()
    name = (intent.get("name") or "").strip()
    market = (intent.get("market") or "").strip()
    date = _to_iso(intent.get("date"))

    if not ticker and not name:
        return {"type": "ask", "question": "你想分析哪只股票/基金？直接给我 6 位代码（如 159779）或公司名就行。"}

    row: dict | None = None

    # 情况 A：裸 6 位代码 → 本地前缀映射定交易所（不让 Wind 猜），查不到再试另一市场
    if re.fullmatch(r"\d{6}", ticker):
        primary_suffix = _wind._infer_cn_exchange(ticker).split(".")[-1]
        row = _resolve_by_suffix(ticker, primary_suffix)
        if row is None and primary_suffix in ("SH", "SZ"):
            alt = "SZ" if primary_suffix == "SH" else "SH"
            row = _resolve_by_suffix(ticker, alt)

    # 情况 B：带后缀代码（600519.SH / 0700.HK / 159779.SZ / AAPL）→ 直接用它取简称
    elif re.match(r"^[A-Z0-9.]{1,12}\.(SH|SZ|HK|BJ|N|O|A)$", ticker):
        wc = ticker
        rows = [r for r in _basicinfo_rows(wc) if not _is_bond(r)]
        if rows:
            row = rows[0]
        else:
            # 美股等 Wind 查不到简称也能跑（engine 支持），直接用代码
            row = {"code": wc, "name": name or wc, "exchange": wc.split(".")[-1]}

    # 情况 C：公司名（A股）→ Wind 按名称解析
    elif name:
        rows = [r for r in _basicinfo_rows(name) if not _is_bond(r)]
        if market:
            rows = [r for r in rows if _fits_market(r, market)]
        if len(rows) == 1:
            row = rows[0]
        elif len(rows) > 1:
            opts = "；".join(f"{r['name']}（{r['code']}）" for r in rows[:5])
            return {"type": "ask", "question": f"「{name}」匹配到多个标的：{opts}。你要分析的是哪一个？"}

    if row is None:
        return {
            "type": "ask",
            "question": f"在 Wind 里没查到「{ticker or name}」的可分析标的。确认下代码/名称对不对？也可以给 6 位代码直接试。",
        }
    return _plan(row, date, name or ticker)


def handle_chat(messages: list[dict]) -> dict:
    """/chat 主入口：多轮历史 → ask / plan。"""
    try:
        intent = parse_intent(messages)
    except Exception as e:  # noqa: BLE001
        return {"type": "ask", "question": f"解析需求时出了点问题（{type(e).__name__}），麻烦再说一次？"}

    if intent.get("mode") == "ask" or intent.get("question"):
        return {"type": "ask", "question": intent.get("question") or "你想要做什么？"}
    try:
        return resolve_to_plan(intent)
    except Exception as e:  # noqa: BLE001
        return {
            "type": "ask",
            "question": f"查询标的时候没成功（{type(e).__name__}），稍后再试，或直接给 6 位代码（如 159779）。",
        }
