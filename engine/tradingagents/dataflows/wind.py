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
import time
from datetime import datetime

import pandas as pd
import requests

from ..asof import current_asof
from .errors import (
    NoMarketDataError,
    VendorError,
    VendorNotConfiguredError,
    VendorRateLimitError,
    VendorRejectedError,
)

logger = logging.getLogger(__name__)

#: 回测中「该数据源结构上无法按历史时点查询」的哨兵文本。
#:
#: 与 ``interface.py`` 的 ``NO_DATA_AVAILABLE`` / ``DATA_UNAVAILABLE`` 同一风格——LLM 已
#: 学会把这类文本当**缺失**而非**信号**处理，所以降级是安全的：角色会如实说"某类信号缺失"，
#: 而不是编一个看起来合理的数值。回测里**禁用**这些工具，而不是"尽力而为"，正是因为它
#: 拿不到历史时点、只能返回"最新"——那是最纯的未来函数。
ASOF_UNAVAILABLE = (
    "DATA_UNAVAILABLE_ASOF: 该数据源不支持历史时点查询（只有自然语言问答接口，"
    "没有日期参数），回测模式下不可用。请**不要**据此推断或编造数值；"
    "本次分析缺少这一类信号。"
)

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


# 瞬时网络错误（连接被重置/不可达、超时、chunked 响应中途截断）值得重试；
# 其余 RequestException（URL 配置错等）是确定性错误，重试无意义，直接抛。
_TRANSIENT_NETWORK_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
_WIND_RPC_MAX_ATTEMPTS = 3
_WIND_RPC_BACKOFF_BASE_SECONDS = 1.0


def _wind_rpc(server_type: str, method: str, params: dict, timeout: int = 60) -> dict:
    """一次 JSON-RPC 调用（Bearer 认证），对瞬时网络错误做指数退避重试。"""
    endpoint = WIND_ENDPOINTS[server_type]
    api_key = _get_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(1, _WIND_RPC_MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(endpoint, json=body, headers=headers, timeout=timeout)
            break  # 拿到响应（任意状态码），进入下方状态码处理
        except _TRANSIENT_NETWORK_ERRORS as e:
            if attempt == _WIND_RPC_MAX_ATTEMPTS:
                raise NoMarketDataError("wind", None, f"网络错误: {e}") from e
            backoff = _WIND_RPC_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "Wind RPC 瞬时网络错误（第 %d/%d 次）：%s；%.1fs 后重试",
                attempt, _WIND_RPC_MAX_ATTEMPTS, e, backoff,
            )
            time.sleep(backoff)
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


# Wind 非 JSON 文本里，「没找到数据/无数据」是「数据不存在」（如 ETF 无财务报表），
# 不是服务端拒绝（如余额不足/鉴权失败）。区分两者：前者走 NoMarketDataError，上层
# route_to_vendor 会降级为 NO_DATA 哨兵、让分析师报告「无数据」而非拖垮整只标的；
# 后者仍走 VendorRejectedError，透出原文命中可读 400。
_NO_DATA_MARKERS = ("没找到数据", "没有数据", "无数据", "未找到数据", "no data", "no records")


def _is_no_data_text(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(m in t for m in _NO_DATA_MARKERS)


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
                # Wind 正常数据均为 JSON（表格/序列）；非 JSON 文本分两类（见 _NO_DATA_MARKERS）。
                if _is_no_data_text(text):
                    raise NoMarketDataError(
                        "wind", None, f"Wind {server_type}/{tool_name} 返回：{text[:200]}"
                    ) from None
                # 余额不足/鉴权失败等真拒绝：透出原文，命中 /analysis 的 ValueError→400 可读分支。
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


def get_wind_ohlcv(
    symbol: str,
    start_date: str,
    end_date: str,
    period: str = "10",
    aftype: int | str | None = None,
    asof: str | None = None,
) -> pd.DataFrame:
    """Wind K 线 → yfinance 形状的 DataFrame（Date/Open/High/Low/Close/Volume）。

    period 为 Wind K 线周期：10=日K(默认)、11=周K、12=月K、1=1分钟、3=5分钟、
    4=10分钟、5=15分钟、6=30分钟、7=60分钟。分钟级保留完整时间戳。

    ``aftype`` / ``asof`` **只在回测里传**；不传时请求参数**逐字节不变**（生产路径）。

    - ``aftype``：0=前复权、1=后复权、2=不复权。实时不传 = 由 Wind 决定（默认前复权），
      请求参数与改造前逐字节相同。**回测下不传则默认 1**——前复权以"今天"为基准回溯
      重算，历史价会随未来的分红送转变化，是在偷看未来；后复权的复权因子只依赖 t 之前的
      除权事件，构造上没有未来函数。之所以把"默认后复权"放在这个收口而不是各个调用方：
      漏传的后果是**静默**的价格错误，不是报错，所以要 fail-safe。代价是绝对价位被一个
      常数缩放，与当前盘面不同——所以计划/触发价/成交价/盯市必须全走同一口径，且结果页要声明。

    - ``asof``：把 ``end_date`` 封顶到该日期。不传时取当前 as-of 作用域，作用域也为空
      才不加限制——这样任何在回测作用域里发的 K 线请求都自动封顶，不依赖调用方记得传。
    """
    if asof is None:
        asof = current_asof()
    if asof and end_date and end_date > asof:
        end_date = asof
    if asof and aftype is None:
        aftype = 1
    wind_code = to_wind_code(symbol)
    params = {
        "windcode": wind_code,
        "begin_date": start_date,
        "end_date": end_date,
        "period": period,
    }
    if aftype is not None:
        # ⚠️ **必须转成字符串。** Wind 这套 MCP 接口的参数口径一律是字符串
        # （``tools/list`` 的 ``default`` 写着 ``period='10'`` / ``aftype='0'``）。
        # 传 int 会被服务端当场拒：``参数格式不正确:aftype``。
        #
        # 这一条和 ``get_index_ohlcv`` 的 ``period`` 是**同一类错**：类型是从 docstring 推的，
        # 不是从 schema 读的；而离线冒烟只断言"请求长什么样"，于是把 ``aftype=1`` 这个
        # **整数**当成契约钉死了（见 ``smoke_backtest_data.py`` 第 2 节）。真打接口才暴露。
        # 对外仍收 int（调用方写 ``aftype=1`` 更清楚），只在线上这一层归一。
        params["aftype"] = str(aftype)
    r = _call_tool("stock_data", "get_stock_kline", params)
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


def get_index_ohlcv(
    symbol: str, start_date: str, end_date: str, period: str = "10"
) -> pd.DataFrame:
    """指数 K 线 → 与 ``get_wind_ohlcv`` 同形状的 DataFrame（回测基准用）。

    ⚠️ **这两个参数一度被写错，而且离线冒烟看不出来**——它用替身、只断言"请求长什么样"，
    于是把错误的口径当成契约钉住了。写死的判据以 ``index_data`` 的 ``tools/list`` 为准：

    - ``period`` 与个股**同一套数字口径**（``10``＝日K、``11``＝周K、``12``＝月K）。
      传 ``"1d"`` 会被服务端当场拒：``Invalid value '1d' for field 'period'``。
    - 这个工具**没有 ``aftype`` 参数**（个股那个才有）。指数是价格指数，本就没有复权概念，
      所以基准与策略的复权口径**不可能在这里对齐**——它是按收益率归一化后使用的**相对**
      基准，不是绝对价位对照。这一点写进了 §3.2 与结果页声明。

    ``asof`` 封顶与个股一致：回测里 ``end_date`` 不能越过 as-of，否则团队会看到未来。
    """
    asof = current_asof()
    if asof and end_date and end_date > asof:
        end_date = asof
    wind_code = to_wind_code(symbol)
    r = _call_tool(
        "index_data",
        "get_index_kline",
        {
            "windcode": wind_code,
            "begin_date": start_date,
            "end_date": end_date,
            "period": period,
        },
    )
    data = r.get("data") or {}
    rows = data.get("rows") or []
    if not rows:
        raise NoMarketDataError(symbol, wind_code, f"no index kline rows between {start_date} and {end_date}")
    cols = [c["name"] for c in data["columns"]]
    df = pd.DataFrame(rows, columns=cols)
    df = df.rename(
        columns={"TIME": "Date", "OPEN": "Open", "MATCH": "Close", "HIGH": "High", "LOW": "Low", "VOLUME": "Volume"}
    )
    keep = [c for c in ["Date", "Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[keep]
    df["Date"] = pd.to_datetime(df["Date"].astype(str).str[:10], errors="coerce")
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["Date", "Close"]).sort_values("Date").reset_index(drop=True)


_NULL_TOKENS = ("", "null", "nan", "none", "-", "--")


def _to_float(v) -> float | None:
    """Wind 数值列 → float。空值/占位符（'', 'null', 'nan', '-', '--'）一律 None，不抛。"""
    if v is None:
        return None
    if str(v).strip().lower() in _NULL_TOKENS:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_price_snapshots(
    stock_codes: list[str],
    indexes: str = "最新交易日,最新成交价,前收盘价,量比",
) -> dict[str, dict]:
    """一次批量取多只股票的时点行情快照 → {wind_code: {"price", "prev_close", "volume_ratio"}}。

    走 Wind 快照接口 get_stock_price_indicators（非 K 线）：1 只取价是 1 次调用，
    N 只合并也是 1 次调用，替代 get_wind_ohlcv 逐只拉 30 天日 K 只为取最近两根的写法。

    - price=最新成交价（盘中为实时现价，收盘后为收盘价）；prev_close=前收盘价，
      新上市/停牌无前收时为 None。
    - volume_ratio=量比（当日每分钟均量 / 过去 5 日每分钟均量），None=该字段没返回或
      不可用。计划里的「放量跌破」条件靠它判定：**取不到时按条件不满足处理**，宁可
      不执行也不能退化成纯价格触发（详见 app/trust.py 的 _trigger_satisfied）。
      注意量比在连续竞价（09:30）前没有意义——集合竞价阶段的比值不可用于判断放量。
    - 单只失效不影响整批：Wind 会把查不到的输入在结果里回填成内部 ID（非请求代码、
      价格列为空）。这里按「返回代码必须精确命中请求列表 且 最新成交价非空」过滤，
      天然丢弃坏标的；请求列表里的好标的照常返回。
    - 整批拿不到（网络/服务错误/无行情）返回 {}，由上层按"无行情"降级，不抛。
    """
    if current_asof():
        # 时点快照接口**没有日期参数**，返回的永远是"最新"。回测里给出一个历史价就是
        # 编造。最安全的失效方向是"没有行情"：上层按无行情降级（跳过交易、持仓保留），
        # 而不是拿着一个未来价去下单。
        return {}
    codes = [str(c).strip() for c in (stock_codes or []) if str(c).strip()]
    if not codes:
        return {}
    try:
        r = _call_tool(
            "stock_data",
            "get_stock_price_indicators",
            {"windcode": ",".join(codes), "indexes": indexes},
            timeout=40,
        )
    except (NoMarketDataError, VendorError):
        return {}
    data = r.get("data") or {}
    cols = {c["name"]: i for i, c in enumerate(data.get("columns") or [])}
    if "Wind代码" not in cols or "最新成交价" not in cols:
        return {}
    i_code, i_price = cols["Wind代码"], cols["最新成交价"]
    i_prev = cols.get("前收盘价")
    i_vr = cols.get("量比")
    want = set(codes)
    out: dict[str, dict] = {}
    for row in data.get("rows") or []:
        raw_code = row[i_code] if i_code < len(row) else None
        code = str(raw_code).strip() if raw_code is not None else ""
        if code not in want:
            continue  # Wind 把坏输入回填成内部 ID，不在请求列表 → 忽略
        price = _to_float(row[i_price] if i_price < len(row) else None)
        if price is None:
            continue
        prev = _to_float(row[i_prev] if i_prev is not None and i_prev < len(row) else None)
        vr = _to_float(row[i_vr] if i_vr is not None and i_vr < len(row) else None)
        out[code] = {"price": price, "prev_close": prev, "volume_ratio": vr}
    return out


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
    """数据获取时间戳，写进返回内容的 ``# Data retrieved on:`` 头。

    回测里返回**模拟日**而非真实今天。不改的话模型会看到「分析日 2026-03-05，数据获取于
    2026-09-11」这种自相矛盾的时间线——那等于直接告诉它"现在是九月"，它可以据此回想
    三月的结局。日期盲的接口已经禁用了，这一个头是最容易漏掉的一处。
    """
    asof = current_asof()
    if asof:
        return f"{asof} 15:05:00"  # 研究在模拟日的盘后跑，与实盘 15:05 的调度时刻一致
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ================= 以下为 interface.py 注册的 vendor 函数 =================

def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """OHLCV 日线，返回 CSV 字符串（对齐 yfinance 格式）。

    回测下的后复权与 ``end_date`` 封顶都由 ``get_wind_ohlcv`` 从 as-of 作用域里取到，
    这里不必显式传——**收口只有一个**，调用方漏传也不会静默出错。
    """
    wind_code = to_wind_code(symbol)
    df = get_wind_ohlcv(symbol, start_date, end_date)
    if df.empty:
        raise NoMarketDataError(symbol, wind_code, f"no rows between {start_date} and {end_date}")
    for c in ["Open", "High", "Low", "Close"]:
        if c in df.columns:
            df[c] = df[c].round(2)
    # 实时：A股本就不复权，Close 即 Adj Close。回测：Close 已经是后复权价，
    # 两者相等同样成立——所以这一行在两种口径下都对。
    df["Adj Close"] = df["Close"]
    csv_string = df.set_index("Date").to_csv()
    label = wind_code if wind_code != symbol.upper() else symbol.upper()
    header = (
        f"# Stock data for {label} from {start_date} to {end_date}\n"
        f"# Total records: {len(df)}\n"
        f"# Data retrieved on: {_now()}\n\n"
    )
    return header + csv_string


def get_fundamentals(ticker: str, curr_date: str = None) -> str:
    """公司基本面核心指标。

    ``curr_date`` 是**签名上的**参数——Wind 这边走的是自然语言问答，它不生效。
    所以回测下一律降级（见 ``ASOF_UNAVAILABLE``），不能拿"最新"的估值去分析历史。
    """
    if current_asof():
        return ASOF_UNAVAILABLE
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
    """三大报表的统一实现——**守卫放在这里**，一处覆盖资产负债表/现金流量表/利润表。"""
    if current_asof():
        return ASOF_UNAVAILABLE
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
    """个股新闻。

    ``start_date``/``end_date`` 看起来像回看窗口，但 Wind 的新闻接口是自然语言问答
    （``get_financial_news``），**根本没有日期参数**——它永远返回"最新"新闻。所以回测下
    这个函数不能只是"忽略日期过滤"，必须整个禁用：读到的会是回测日之后才发生的新闻。
    """
    if current_asof():
        return ASOF_UNAVAILABLE
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
    """宏观/全球新闻（Wind 财经新闻，宏观视角）。同 ``get_news``：问答接口无日期参数。"""
    if current_asof():
        return ASOF_UNAVAILABLE
    r = _call_tool("financial_docs", "get_financial_news", {"query": "宏观经济 货币政策 美联储 央行 全球市场", "top_k": limit})
    items = (r.get("data") or {}).get("items") or []
    if not items:
        return "No global news found"
    blocks = [f"[{it.get('date', '')}] {it.get('title', '')}: {it.get('content', '')}" for it in items]
    return f"# Global News\n# Data retrieved on: {_now()}\n\n" + "\n\n".join(blocks)


def get_insider_transactions(ticker: str) -> str:
    """股东/内部人交易 —— 用 Wind 公司事件（大股东增减持/限售解禁/分红派息/ST 变动）替代。

    无日期参数，回测下禁用。**这类信息的泄漏尤其致命**：增减持与解禁公告往往是
    后续走势的直接原因，读到它等于把答案抄给模型。
    """
    if current_asof():
        return ASOF_UNAVAILABLE
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

    ``observation`` 是"最近 N 期"，天然锚在**现在**——回测下禁用。
    """
    if current_asof():
        return ASOF_UNAVAILABLE
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
    """公司名称/行业/板块（get_stock_basicinfo）—— 用于替代 yfinance 的公司身份解析。

    回测下返回 None（调用方回退用代码作名称）。名称本身不是未来函数，但**所属行业**会随
    时间变化，且这个接口没有日期参数；回测一律由调用方显式传 ``stock_name``，
    所以返回 None 不会造成任何损失。
    """
    if current_asof():
        return None
    wind_code = to_wind_code(ticker)
    r = _call_tool("stock_data", "get_stock_basicinfo", {"question": f"{wind_code} 公司简称、所属行业、上市板块"})
    data = r.get("data") or {}
    # 返回文本描述，供上层解析
    text = data.get("text") or data.get("summary") or ""
    if not text:
        lines = _fmt_table(r)
        text = "\n".join(lines) if lines else ""
    if text:
        s = str(text).strip()
        # Wind 对部分市场（港股/ETF 等）返回多行信息块（含行业明细、上市板等），
        # 这里只取「证券简称」纯名称，避免把整段塞进 varchar(50) 名称字段导致超长报错。
        m = re.search(r"证券简称[:：]\s*([^\n\r]+)", s)
        if m:
            return m.group(1).strip()
        first = s.split("\n")[0].strip()
        return first or None
    return None


def get_company_announcements(ticker: str, limit: int = 5) -> str:
    """上市公司公告原文（get_company_announcements）。无日期参数，回测下禁用。

    公告是**最强的未来函数之一**：业绩预告、重大合同、问询函都直接决定后续走势。
    """
    if current_asof():
        return ASOF_UNAVAILABLE
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
    """风险指标（Beta/Sharpe/VaR/最大回撤）—— 丰富风控分析。

    无日期参数，且回撤/夏普都是**区间统计**，锚在"现在"——回测下禁用。
    """
    if current_asof():
        return ASOF_UNAVAILABLE
    wind_code = to_wind_code(ticker)
    r = _call_tool("stock_data", "get_risk_metrics", {"question": f"{wind_code} 的 Beta、年化波动率、最大回撤、夏普比率、VaR"})
    lines = _fmt_table(r)
    if not lines:
        return f"No risk metrics found for '{wind_code}'"
    return f"# Risk metrics for {wind_code}\n# Data retrieved on: {_now()}\n\n" + "\n".join(lines)
