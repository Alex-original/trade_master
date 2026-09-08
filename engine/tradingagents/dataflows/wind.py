"""Wind（万得）数据 vendor —— 全量替换 yfinance，覆盖 A股/港股/美股。

通过 Wind 的 7 个 MCP 服务取数（HTTP JSON-RPC + Bearer 认证），
返回格式对齐原版 yfinance vendor 的字符串约定，使上层 agent 无感切换。

认证顺序：环境变量 WIND_API_KEY > ~/.wind-aifinmarket/config。
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime

import pandas as pd
import requests

from .errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorRateLimitError,
    VendorRejectedError,
)

logger = logging.getLogger(__name__)

# ---- Wind MCP 端点（7 个数据域）----
WIND_ENDPOINTS = {
    "stock_data": "https://mcp.wind.com.cn/vserver_stock_data/mcp/",
    "fund_data": "https://mcp.wind.com.cn/vserver_fund_data/mcp/",
    "index_data": "https://mcp.wind.com.cn/vserver_index_data/mcp/",
    "bond_data": "https://mcp.wind.com.cn/vserver_bond_data/mcp/",
    "financial_docs": "https://mcp.wind.com.cn/vserver_financial_docs/mcp/",
    "economic_data": "https://mcp.wind.com.cn/vserver_economic_data/mcp/",
    "analytics_data": "https://mcp.wind.com.cn/vserver_analytics_data/mcp/",
}

_WIND_KEY_FILE = os.path.join(os.path.expanduser("~"), ".wind-aifinmarket", "config")


def _get_api_key() -> str:
    """取 Wind API Key：环境变量 > 全局配置文件（兼容 shell/JSON 两种格式）。"""
    key = os.getenv("WIND_API_KEY", "").strip()
    if key:
        return key
    try:
        if os.path.exists(_WIND_KEY_FILE):
            with open(_WIND_KEY_FILE, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            # 格式 1：WIND_API_KEY=xxx
            m = re.search(r"WIND_API_KEY\s*=\s*(\S+)", raw)
            if m:
                key = m.group(1).strip().strip('"').strip("'")
                if key:
                    return key
            # 格式 2：JSON {"wind_api_key": "xxx"}
            cfg = json.loads(raw)
            key = (cfg.get("wind_api_key") or "").strip()
            if key:
                return key
    except Exception:
        pass
    raise VendorNotConfiguredError("WIND_API_KEY 未配置")


def _parse_sse(text: str):
    """解析 MCP 响应：纯 JSON 或 SSE（data: 行取最后一条）。"""
    trimmed = text.strip()
    if trimmed.startswith("{"):
        return json.loads(trimmed)
    last = None
    for line in trimmed.splitlines():
        if line.startswith("data: "):
            last = line[6:]
    if last:
        return json.loads(last)
    raise RuntimeError(f"Wind 响应格式无法识别: {text[:200]}")


def _wind_rpc(server_type: str, method: str, params: dict, timeout: int = 60) -> dict:
    """一次 JSON-RPC 调用（Bearer 认证）。"""
    endpoint = WIND_ENDPOINTS[server_type]
    api_key = _get_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        resp = requests.post(endpoint, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise NoMarketDataError("wind", None, f"网络错误: {e}") from e
    if resp.status_code == 429:
        raise VendorRateLimitError("Wind rate limited (429)")
    if resp.status_code != 200:
        raise NoMarketDataError("wind", None, f"HTTP {resp.status_code}: {resp.text[:200]}")
    # 响应为 text/event-stream 无 charset，requests 会误判 ISO-8859-1，
    # 导致中文多字节（0x85 等）被当成 NEL 换行符切碎 JSON —— 必须显式 UTF-8 解码。
    payload = _parse_sse(resp.content.decode("utf-8"))
    if payload.get("error"):
        msg = payload["error"].get("message", str(payload["error"]))
        raise NoMarketDataError("wind", None, f"MCP error: {msg}")
    return payload.get("result", {})


def _call_tool(server_type: str, tool_name: str, params: dict, timeout: int = 120) -> dict:
    """MCP 初始化握手（容忍失败）+ tools/call，返回解析后的 JSON 结果。"""
    try:
        _wind_rpc(
            server_type,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "tradingagents-wind", "version": "0.4.0"},
            },
            timeout=30,
        )
    except Exception:
        pass  # 部分服务无需 initialize，忽略
    result = _wind_rpc(
        server_type,
        "tools/call",
        {"name": tool_name, "arguments": params},
        timeout=timeout,
    )
    content = result.get("content") or []
    if content and isinstance(content[0], dict) and content[0].get("text"):
        text = content[0]["text"].strip()
        if text:
            try:
                return json.loads(content[0]["text"])
            except ValueError:
                # Wind 正常数据均为 JSON（表格/序列）；非 JSON 文本即服务端拒绝文案
                # （"余额不足，请先充值"/鉴权失败等）。原实现降级为 {"raw": ...} 后被
                # 上层当成"空数据"吞成 NoMarketDataError（如 no kline rows），掩盖真因，
                # 必须在这里透出 Wind 原文，命中 /analysis 的 ValueError→400 可读分支。
                raise VendorRejectedError(
                    f"Wind {server_type}/{tool_name} 返回：{text[:300]}"
                ) from None
    return result


def to_wind_code(symbol: str) -> str:
    """yfinance/通用代码 → Wind 代码。

    - `.SS`（Yahoo 沪市遗留写法，用户常当"A股"通用后缀用）→ 按 6 位前缀判交易所，
      避免把 159/16/18 深市基金、000/300 深股误写成 .SH。
    - `.SZ` / `.HK` / `.BJ` / `.O` / `.N` / `.A` 原样保留（已是 Wind/指定市场）。
    - 6 位纯数字 → 按前缀推断交易所。
    - 其余原样返回（由上层决定）。
    """
    s = (symbol or "").strip().upper()
    if not s:
        return s
    if s.endswith(".SS"):
        base = s[:-3]
        if base.isdigit() and len(base) == 6:
            return _infer_cn_exchange(base)
        return base + ".SH"  # 非 6 位数字：保持旧行为默认沪
    if s.endswith(".HK"):
        # Wind 港股代码统一为 5 位补零（0700.HK → 00700.HK；0981.HK → 00981.HK）
        digits = re.match(r"^(\d{1,5})\.HK$", s)
        if digits:
            return digits.group(1).zfill(5) + ".HK"
        return s
    if s.endswith((".SZ", ".BJ", ".O", ".N", ".A")):
        return s
    if s.isdigit() and len(s) == 6:
        return _infer_cn_exchange(s)
    return s


def _infer_cn_exchange(code: str) -> str:
    """6 位中国证券代码 → Wind 交易所后缀。

    - 沪市：6/9/5 开头（A股 600/601/603/605/688/689，B股 900，基金/ETF/REITs 5xx）
    - 北交所：920 新号段、8/4 开头
    - 深市：其余（A股 000/001/002/003/300/301，基金/ETF/LOF 150/159/16/18，B股 200）
    """
    if code.startswith("92"):
        return code + ".BJ"
    if code.startswith(("6", "9", "5")):
        return code + ".SH"
    if code.startswith(("8", "4")):
        return code + ".BJ"
    return code + ".SZ"


def get_wind_ohlcv(symbol: str, start_date: str, end_date: str, period: str = "10") -> pd.DataFrame:
    """Wind K 线 → yfinance 形状的 DataFrame（Date/Open/High/Low/Close/Volume）。

    period 为 Wind K 线周期：10=日K(默认)、11=周K、12=月K、1=1分钟、3=5分钟、
    4=10分钟、5=15分钟、6=30分钟、7=60分钟。分钟级保留完整时间戳。
    """
    wind_code = to_wind_code(symbol)
    r = _call_tool(
        "stock_data",
        "get_stock_kline",
        {"windcode": wind_code, "begin_date": start_date, "end_date": end_date, "period": period},
    )
    data = r.get("data") or {}
    rows = data.get("rows") or []
    if not rows:
        raise NoMarketDataError(symbol, wind_code, f"no kline rows between {start_date} and {end_date}")
    cols = [c["name"] for c in data["columns"]]
    df = pd.DataFrame(rows, columns=cols)
    rename = {
        "TIME": "Date", "OPEN": "Open", "MATCH": "Close",
        "HIGH": "High", "LOW": "Low", "VOLUME": "Volume",
    }
    df = df.rename(columns=rename)
    keep = [c for c in ["Date", "Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[keep]
    # 分钟级（period<10）保留完整时间戳（去掉时区后缀）；日线+只取日期部分。
    _minute = period.isdigit() and int(period) < 10
    if _minute:
        clean = df["Date"].astype(str).str.replace(r"\.000\+08:00$", "", regex=True)
        df["Date"] = pd.to_datetime(clean, errors="coerce")
    else:
        df["Date"] = pd.to_datetime(df["Date"].astype(str).str[:10], errors="coerce")
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["Date", "Close"]).sort_values("Date").reset_index(drop=True)
    return df


def _fmt_table(r: dict) -> list[str]:
    """把 Wind 表格结果（{columns, rows}）格式化为 '字段: 值' 行。"""
    lines: list[str] = []
    data = r.get("data") or {}
    tables = data.get("data") or data.get("rows") or []
    if isinstance(tables, dict):
        tables = [tables]
    for t in tables:
        cols = [c["name"] for c in t.get("columns", [])]
        for row in t.get("rows", []):
            for c, v in zip(cols, row):
                if v is not None and v != "":
                    lines.append(f"{c}: {v}")
    return lines


def _fmt_edb(r: dict) -> list[str]:
    """把 Wind EDB 结果（{metrics:[{meta, date[], value[]}]}）格式化为文本。"""
    lines: list[str] = []
    for m in r.get("metrics") or []:
        meta = m.get("meta") or {}
        name = meta.get("name", "指标")
        unit = meta.get("unit", "")
        freq = meta.get("freq", "")
        src = meta.get("source", "")
        lines.append(f"{name}（{freq}，单位：{unit}，来源：{src}）")
        dates = m.get("date") or []
        values = m.get("value") or []
        for d, v in list(zip(dates, values))[-6:]:  # 最近 6 期
            lines.append(f"  {d}: {v}")
    return lines


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ================= 以下为 interface.py 注册的 vendor 函数 =================

def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """OHLCV 日线，返回 CSV 字符串（对齐 yfinance 格式）。"""
    wind_code = to_wind_code(symbol)
    df = get_wind_ohlcv(symbol, start_date, end_date)
    if df.empty:
        raise NoMarketDataError(symbol, wind_code, f"no rows between {start_date} and {end_date}")
    for c in ["Open", "High", "Low", "Close"]:
        if c in df.columns:
            df[c] = df[c].round(2)
    df["Adj Close"] = df["Close"]  # A股不复权，用 Close 代替
    csv_string = df.set_index("Date").to_csv()
    label = wind_code if wind_code != symbol.upper() else symbol.upper()
    header = (
        f"# Stock data for {label} from {start_date} to {end_date}\n"
        f"# Total records: {len(df)}\n"
        f"# Data retrieved on: {_now()}\n\n"
    )
    return header + csv_string


def get_fundamentals(ticker: str, curr_date: str = None) -> str:
    """公司基本面核心指标。"""
    wind_code = to_wind_code(ticker)
    q = (
        f"{wind_code} 公司基本面核心指标：所属行业、总市值、市盈率、市净率、"
        f"营业收入、净利润、净资产收益率ROE、资产负债率、每股收益、毛利率"
    )
    r = _call_tool("stock_data", "get_stock_fundamentals", {"question": q})
    lines = _fmt_table(r)
    if not lines:
        raise NoMarketDataError(ticker, wind_code, "no fundamentals returned")
    return f"# Company Fundamentals for {wind_code}\n# Data retrieved on: {_now()}\n\n" + "\n".join(lines)


def _statement(ticker: str, freq: str, sheet: str, fields: str) -> str:
    wind_code = to_wind_code(ticker)
    q = f"{wind_code} 最新{sheet}：{fields}"
    r = _call_tool("stock_data", "get_stock_fundamentals", {"question": q})
    lines = _fmt_table(r)
    if not lines:
        raise NoMarketDataError(ticker, wind_code, f"no {sheet} data")
    return f"# {sheet} for {wind_code} ({freq})\n# Data retrieved on: {_now()}\n\n" + "\n".join(lines)


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    return _statement(ticker, freq, "资产负债表", "总资产、总负债、股东权益、货币资金、存货、应收账款、流动比率、资产负债率")


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    return _statement(ticker, freq, "现金流量表", "经营活动现金流净额、投资活动现金流净额、筹资活动现金流净额、期末现金及等价物")


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
    return _statement(ticker, freq, "利润表", "营业收入、营业成本、毛利润、净利润、每股收益、销售费用、管理费用")


def get_news(ticker: str, start_date: str = None, end_date: str = None) -> str:
    """个股新闻。start_date/end_date 为回看窗口，Wind 新闻接口返回近期新闻，暂忽略日期过滤。"""
    wind_code = to_wind_code(ticker)
    r = _call_tool("financial_docs", "get_financial_news", {"query": wind_code, "top_k": 20})
    items = (r.get("data") or {}).get("items") or []
    if not items:
        return f"No news found for symbol '{wind_code}'"
    blocks = []
    for it in items:
        blocks.append(f"[{it.get('date', '')}] {it.get('title', '')}: {it.get('content', '')}")
    return f"# News for {wind_code}\n# Data retrieved on: {_now()}\n\n" + "\n\n".join(blocks)


def get_global_news(curr_date: str = None, look_back_days: int = 7, limit: int = 10) -> str:
    """宏观/全球新闻（Wind 财经新闻，宏观视角）。"""
    r = _call_tool("financial_docs", "get_financial_news", {"query": "宏观经济 货币政策 美联储 央行 全球市场", "top_k": limit})
    items = (r.get("data") or {}).get("items") or []
    if not items:
        return "No global news found"
    blocks = [f"[{it.get('date', '')}] {it.get('title', '')}: {it.get('content', '')}" for it in items]
    return f"# Global News\n# Data retrieved on: {_now()}\n\n" + "\n\n".join(blocks)


def get_insider_transactions(ticker: str) -> str:
    """股东/内部人交易 —— 用 Wind 公司事件（大股东增减持/限售解禁/分红派息/ST 变动）替代。"""
    wind_code = to_wind_code(ticker)
    r = _call_tool(
        "stock_data",
        "get_stock_events",
        {"question": f"{wind_code} 的大股东增减持、限售解禁、分红派息、ST风险警示事件"},
    )
    lines = _fmt_table(r)
    if not lines:
        return f"No insider/event data reported for symbol '{wind_code}'"
    return f"# Corporate events for {wind_code}\n# Data retrieved on: {_now()}\n\n" + "\n".join(lines)


def get_macro_indicators(indicator: str, curr_date: str = None, look_back_days: int = 30) -> str:
    """宏观指标 —— Wind EDB（economic_data），按自然语言查询指标时间序列。

    EDB 要求显式 observation（近 N 期）或 beginDate/endDate；这里取近 10 期。
    """
    q = str(indicator)
    r = _call_tool(
        "economic_data",
        "query_economic_indicator_data",
        {"question": q, "observation": "10"},
    )
    lines = _fmt_edb(r)
    if not lines:
        raise NoMarketDataError("wind", None, f"no macro data for: {q}")
    return f"# Macro indicators: {q}\n# Data retrieved on: {_now()}\n\n" + "\n".join(lines)


def get_prediction_markets(*args, **kwargs) -> str:
    """预测市场（Polymarket）—— Wind 无对应，返回不可用（可选类目）。"""
    raise NoMarketDataError("wind", None, "prediction markets not supported by Wind")


def get_company_name(ticker: str) -> str | None:
    """公司名称/行业/板块（get_stock_basicinfo）—— 用于替代 yfinance 的公司身份解析。"""
    wind_code = to_wind_code(ticker)
    r = _call_tool("stock_data", "get_stock_basicinfo", {"question": f"{wind_code} 公司简称、所属行业、上市板块"})
    data = r.get("data") or {}
    # 返回文本描述，供上层解析
    text = data.get("text") or data.get("summary") or ""
    if text:
        return str(text)
    lines = _fmt_table(r)
    return "\n".join(lines) if lines else None


def get_company_announcements(ticker: str, limit: int = 5) -> str:
    """上市公司公告原文（get_company_announcements）。"""
    wind_code = to_wind_code(ticker)
    r = _call_tool("financial_docs", "get_company_announcements", {"query": wind_code, "top_k": limit})
    data = r.get("data") or {}
    items = data.get("items") or data.get("announcements") or []
    if not items:
        return f"No announcements found for '{wind_code}'"
    blocks = []
    for it in items:
        blocks.append(f"[{it.get('date', it.get('ann_date', ''))}] {it.get('title', '')}: {it.get('content', it.get('summary', ''))}")
    return f"# Announcements for {wind_code}\n# Data retrieved on: {_now()}\n\n" + "\n\n".join(blocks)


def get_risk_metrics(ticker: str) -> str:
    """风险指标（Beta/Sharpe/VaR/最大回撤）—— 丰富风控分析。"""
    wind_code = to_wind_code(ticker)
    r = _call_tool("stock_data", "get_risk_metrics", {"question": f"{wind_code} 的 Beta、年化波动率、最大回撤、夏普比率、VaR"})
    lines = _fmt_table(r)
    if not lines:
        return f"No risk metrics found for '{wind_code}'"
    return f"# Risk metrics for {wind_code}\n# Data retrieved on: {_now()}\n\n" + "\n".join(lines)
