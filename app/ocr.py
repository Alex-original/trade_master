"""持仓解析：文本(DeepSeek) + 截图(阿里云百炼 qwen3-vl 视觉模型)。

截图流程：图片(base64) → 百炼 qwen3-vl-flash（原生 multimodal-generation）
直接按 schema 抽取持仓 JSON——不需要单独开通"阿里云 OCR 服务"。
文本流程：直接 DeepSeek 解析。
原始图片不落库，只返回解析结果供前端预览确认。

协议说明：trade_master 走百炼工作空间的**原生** DashScope 协议
（POST {base}/services/aigc/multimodal-generation/generation）。
video-note 的 qwen3-asr-flash 用的是 OpenAI 兼容端点 /compatible-mode/v1
（且 ASR 仅音频、看不了图），两者不能混用——这正是当初沿用失败的原因。
"""
from __future__ import annotations

import base64
import difflib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as _futures_wait

from app.errors import ServiceError
from app.intent import _llm_json  # parse_text 透传给 DeepSeek

# 阿里云百炼（DashScope）视觉配置
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.getenv(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"
).rstrip("/")
DASHSCOPE_VL_MODEL = os.getenv("DASHSCOPE_VL_MODEL", "qwen3-vl-flash")
OCR_CONFIGURED = bool(DASHSCOPE_API_KEY)

_VL_GENERATION_URL = f"{DASHSCOPE_BASE_URL}/services/aigc/multimodal-generation/generation"

_POSITION_SYSTEM = """你是持仓解析器。用户粘贴了券商持仓文本（可能是截图 OCR 结果，格式凌乱）。
提取其中的股票/基金/ETF 持仓和可用资金，只输出一个 JSON 对象：
{"positions": [{"code":"6位数字或带后缀代码","name":"名称","qty":整数数量,"cost_price":数字成本价}], "confidence":"high|medium|low", "available_cash": 数字或 null}
规则：
1. code 只写你在这一行**确实看到**的那串代码：A股/深市基金写 6 位数字，港股写 5 位数字加 .HK。**这一行没有代码列或看不清就留空**；绝对不要套用任何示例格式里的代码、也不要凭名称猜代码。
2. qty 为股数/份额整数；cost_price 为数字，缺失填 0。
3. available_cash：仅当文本含明确的"可用资金/资金余额/可用金额/余额"等字样且给出人民币数值时填写（单位元，正数）；不含则填 null。不要用"总资产/总市值/参考市值/总盈亏"冒充可用资金。
4. 其余"总资产/市值/盈亏"等汇总行一律忽略，不作为持仓。
5. 表格凌乱、多数识别不清时 confidence 填 low，positions 可为空。
"""

# 视觉模型用同一套 schema，图片直接进 prompt
_VISION_PROMPT = (
    "这是一张券商持仓截图。请逐行提取股票/基金/ETF 持仓和可用资金，只输出一个 JSON 对象：\n"
    '{"positions": [{"code": "6位数字或带后缀代码", "name": "名称", '
    '"qty": 整数数量, "cost_price": 数字成本价}], '
    '"confidence": "high|medium|low", "available_cash": 数字或 null}\n'
    "规则：code 只写该行图上确实印着的那串代码（A股/深市基金 6 位数字、港股 5 位数字加 .HK），图上没有代码列或看不清就留空，"
    "不要套用任何示例、不要凭名称猜代码；"
    "qty 为股数/份额整数，cost_price 为数字、缺失填 0；"
    "available_cash：仅当界面上有明确的'可用资金/资金余额/可用金额/余额'且给出人民币数值时填写（元，正数），没有则 null，"
    "不要用'总资产/总市值/参考市值/总盈亏'冒充；"
    "其余'总资产/市值/盈亏'等汇总行忽略、不作为持仓；多数识别不清时 confidence 填 low、positions 可为空。\n"
    "只输出 JSON，不要解释。"
)


def _normalize(result: dict) -> dict:
    """把模型 JSON 规整成 {positions:[...], confidence, available_cash}，丢弃非法行。

    available_cash：尽力识别的"可用资金"（元）；文本里没有明确资金行则为 0.0（由前端可编辑）。
    """
    positions = result.get("positions") or []
    out = []
    for p in positions:
        code = (p.get("code") or "").strip()
        name = (p.get("name") or "").strip()
        try:
            qty = int(p.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        try:
            cost = float(p.get("cost_price") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        if not code:
            continue
        out.append({"code": code, "name": name, "qty": qty, "cost_price": cost})
    cash = result.get("available_cash")
    try:
        cash = round(float(cash), 2) if cash is not None else 0.0
    except (TypeError, ValueError):
        cash = 0.0
    if cash < 0:
        cash = 0.0
    return {"positions": out, "confidence": result.get("confidence", "medium"), "available_cash": cash}


def parse_text(text: str) -> dict:
    """文本 → 结构化持仓。返回 {positions:[...], confidence}。"""
    if not text or not text.strip():
        raise ServiceError("请粘贴持仓文本")
    content = text.strip()
    result = _llm_json(_POSITION_SYSTEM, [{"role": "user", "content": content}])
    normalized = _normalize(result)
    # DeepSeek 偶发输出空 + 低置信度，重试一次提升稳定性
    if not normalized["positions"] and normalized["confidence"] == "low":
        result = _llm_json(_POSITION_SYSTEM, [{"role": "user", "content": content}])
        normalized = _normalize(result)
    return normalized


def _sniff_mime(raw: bytes) -> str:
    """按文件头判断真实图片类型——**不写死、也不信外部标签**。

    前端为了让手机截图能过 nginx 的请求体上限，已统一缩放并转成 JPEG；
    若这里仍写死 ``image/png``，payload 就在撒谎。

    实测（2026-09-11，同一张自选股截图）：PNG 标 ``png``、JPEG 标 ``png``、
    JPEG 标 ``jpeg`` 三种组合，百炼返回的 12 只股票逐条一致——**它是按字节嗅探的**，
    标错不会导致识别失败。所以这里做的是"说真话"，不是修 bug；
    但也不能因此就继续写死：真出问题时日志里的类型得能信。
    """
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:2] == b"BM":
        return "image/bmp"
    return "image/png"  # 认不出时兜底：百炼自己还会嗅探，不至于因此失败


def _call_vision_json(image_b64: str, prompt: str, mime: str = "image/png") -> dict:
    """百炼原生 multimodal-generation → qwen3-vl 视觉模型 → JSON。"""
    body = {
        "model": DASHSCOPE_VL_MODEL,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"image": f"data:{mime};base64,{image_b64}"},
                        {"text": prompt},
                    ],
                }
            ]
        },
        "parameters": {"result_format": "message"},
    }
    req = urllib.request.Request(
        _VL_GENERATION_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ServiceError(f"百炼识别失败（HTTP {e.code}）：{detail}") from e
    except Exception as e:  # noqa: BLE001
        raise ServiceError(f"百炼识别失败：{e}") from e
    if payload.get("success") is False:
        raise ServiceError(f"百炼识别失败：{payload.get('message') or payload.get('code')}")
    choices = (payload.get("output") or {}).get("choices") or []
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content") or ""
    if isinstance(content, list):  # result_format=message 可能返回分段文本
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        raise ServiceError(f"百炼未返回 JSON：{content[:200]}")
    try:
        return json.loads(content[start : end + 1])
    except json.JSONDecodeError as e:
        raise ServiceError(f"百炼返回 JSON 无法解析：{e}") from e


def parse_image(image_bytes: bytes) -> dict:
    """截图 → 百炼视觉模型 → 结构化持仓 {positions, confidence}。"""
    if not OCR_CONFIGURED:
        raise ServiceError("OCR 未配置（缺 DASHSCOPE_API_KEY，请在 .env 填写百炼工作空间 key）")
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    return _normalize(_call_vision_json(image_b64, _VISION_PROMPT, _sniff_mime(image_bytes)))


def parse_holdings(input_type: str, content: str, *, code_resolver=None) -> dict:
    """主入口。input_type: 'text'（纯文本） | 'image'（base64）。

    ``code_resolver``：名称 → ``{code,name}`` 的注入缝，默认 None = 走 Wind（见 correct_codes）。
    测试传替身即可完全离线；``main.py`` 的调用点不用改（按位置传参）。
    """
    if input_type == "image":
        if content.startswith("data:image"):
            content = content.split(",", 1)[1]
        try:
            raw = base64.b64decode(content)
        except Exception as e:  # noqa: BLE001
            raise ServiceError(f"图片数据不合法：{e}")
        result = parse_image(raw)
    else:
        result = parse_text(content)
    result["positions"] = correct_codes(result.get("positions") or [], resolve=code_resolver)
    return result


# ---------- 自选股解析（同步到当前分组，一次一组） ----------

_WATCH_SYSTEM = """你是自选股解析器。用户粘贴券商「自选」列表文本（可能是截图 OCR 结果，格式凌乱，常带现价/涨跌幅/市值等行情列）。
只输出一个 JSON 对象：
{"stocks": [{"code": "6位数字或带后缀代码", "name": "证券简称"}], "confidence": "high|medium|low"}
规则：
1. 只提取每只证券本身（股票/基金/ETF）的代码与名称；现价/涨跌幅/市值/成交量等行情列一律不要写进 code/name。
2. code：只取表中该行**实际印着**的代码（A股/深市基金 6 位数字、港股 5 位数字加 .HK）；该行没有 code 列或看不清就留空（保留 name），不要臆造、不要套用示例、不要凭名称猜代码。
3. name：该行证券简称；缺失可用 code。
4. 列表行通常很多，逐行尽量完整列出，不要截断省略。
5. 多数行识别不清时 confidence=low、stocks 可为空。
"""

_VISION_WATCH_PROMPT = (
    "这是一张券商「自选股」列表截图（含名称/代码/现价等列）。逐行提取每只证券的代码与名称，只输出一个 JSON 对象：\n"
    '{"stocks": [{"code": "6位数字或带后缀代码", "name": "证券简称"}], "confidence": "high|medium|low"}\n'
    "规则：只取证券本身，忽略现价/涨跌幅/市值等行情列；code 只取该行实际印着的代码（A股/深市基金 6 位数字、港股 5 位数字加 .HK），"
    "没有或看不清则留空、不要臆造、不要套用示例、不要凭名称猜代码；name 为该行证券简称、缺失可用 code；逐行尽量完整不要漏行；多数不清时 confidence=low、stocks 可为空。"
    "只输出 JSON，不要解释。"
)


def _normalize_watch(result: dict) -> dict:
    """规整成 {stocks:[{code,name}], confidence}，丢弃空行。"""
    stocks = result.get("stocks") or []
    out = []
    for s in stocks:
        code = (s.get("code") or "").strip()
        name = (s.get("name") or "").strip()
        if code or name:
            out.append({"code": code, "name": name})
    return {"stocks": out, "confidence": result.get("confidence", "medium")}


def parse_watchlist_text(text: str) -> dict:
    if not text or not text.strip():
        raise ServiceError("请粘贴自选列表文本")
    content = text.strip()
    result = _llm_json(_WATCH_SYSTEM, [{"role": "user", "content": content}])
    normalized = _normalize_watch(result)
    if not normalized["stocks"] and normalized["confidence"] == "low":
        result = _llm_json(_WATCH_SYSTEM, [{"role": "user", "content": content}])
        normalized = _normalize_watch(result)
    return normalized


def parse_watchlist_image(image_bytes: bytes) -> dict:
    if not OCR_CONFIGURED:
        raise ServiceError("OCR 未配置（缺 DASHSCOPE_API_KEY，请在 .env 填写百炼工作空间 key）")
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    return _normalize_watch(
        _call_vision_json(image_b64, _VISION_WATCH_PROMPT, _sniff_mime(image_bytes))
    )


def parse_watchlist(input_type: str, content: str, *, code_resolver=None) -> dict:
    """自选列表解析主入口。input_type: 'text'（纯文本） | 'image'（base64）。

    ``code_resolver`` 同 parse_holdings（注入缝，默认走 Wind）。
    自选也要纠：``06862.HK`` 那条示例在两套提示词里都有；而且 ``_normalize_watch`` 会保留
    "只有名称、没有代码"的行，按名称回查还能顺手把空代码补上。
    """
    if input_type == "image":
        if content.startswith("data:image"):
            content = content.split(",", 1)[1]
        try:
            raw = base64.b64decode(content)
        except Exception as e:  # noqa: BLE001
            raise ServiceError(f"图片数据不合法：{e}")
        result = parse_watchlist_image(raw)
    else:
        result = parse_watchlist_text(content)
    result["stocks"] = correct_codes(result.get("stocks") or [], resolve=code_resolver)
    return result


# ---------- 证券代码纠错：用名称向 Wind 回查真代码 ----------
#
# 为什么要有这一节：OCR 的提示词里曾经拿 ``06862.HK`` 当"代码长什么样"的示例，模型读不出
# 某一行的代码时**把示例照抄了回来**（用户实测：国投白银LOF 的代码显示成 06862.HK，实际 161226）。
# 去示例（见 _POSITION_SYSTEM / _VISION_PROMPT）治的是"不再主动诱导"，但治不了模型自己幻觉。
# 所以再加一层：**用名称回查真代码**——名称是人眼可见的、比代码可靠得多。
#
# 底线（这一段的所有设计都服务于它）：**默认路径逐字节不变**。Wind 挂了、查不到、超时、超上限，
# 输出与改动前完全一致——绝不能因为一个锦上添花的校验，让"同步持仓"整个失败。

_CODE_CORRECT_ENABLED = os.getenv("OCR_CODE_CORRECT", "1").strip().lower() not in ("0", "false", "no")
_CODE_CORRECT_MAX_NAMES = 25    # 单次请求最多回查多少个**不同**名称（去重后）
_CODE_CORRECT_WORKERS = 4       # 并发路数：Wind 侧是阻塞 HTTP，4 路能把 25 个名字压进预算
_CODE_CORRECT_BUDGET_S = 20.0   # 整体墙钟预算；到点未回的名字按"保留模型代码"收尾
_CODE_CORRECT_TIMEOUT = 8       # 单个名字的 Wind 超时（见 intent._basicinfo_rows 的 timeout 参数）
_CODE_CACHE_TTL_S = 600.0       # 按名称缓存：同一次请求内去重 + 跨请求短缓存
_CODE_CACHE_MAX = 500
_AMBIGUOUS_MARGIN = 0.15        # 多候选且无精确命中时，第一名与第二名相似度差小于它就不改
_CLEAR_WIN_RATIO = 0.85         # 多候选且无精确命中时，第一名相似度低于它就不改

_NAME_CODE_CACHE: dict[str, tuple[float, dict | None]] = {}
_CACHE_LOCK = threading.Lock()


def _code_core(code: str) -> str:
    """代码的"身份核心"：数字部分去前导零。

    ``161226`` / ``161226.SZ`` → ``161226``（同一个标的，只是写法不同）；
    ``06862.HK`` → ``6862``（港股前导零不算身份）。用它判断"到底换没换标的"——
    若拿字符串直接比，A 股每行（``600519`` → ``600519.SH``）都会被判成"换了"，
    于是每一行都挂上"已校正"徽标，这个提示就变成了噪音。
    """
    c = (code or "").strip().upper()
    m = re.match(r"(\d+)", c)
    if not m:
        return c
    return m.group(1).lstrip("0") or "0"


def _name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").strip().lower(), (b or "").strip().lower()).ratio()


def _norm_name(s: str) -> str:
    """名称归一（只为比"是不是同一个名字"）：去空白与常见分隔符、统一小写。"""
    return re.sub(r"[\s（）()·\-—_]+", "", (s or "").strip().lower())


def _resolve_by_name(name: str, *, rows_fn=None) -> dict | None:
    """名称 → ``{code,name}``；查不到 / 出错 / 候选分不开 → None（调用方保留模型给的代码）。

    ``rows_fn`` 是注入缝：默认 None 走 Wind（``intent._basicinfo_rows``）。测试传替身，
    整条路一个字节都不出网——与 ``scripts/smoke_backtest_data.py`` 把 ``_wind._call_tool``
    换成桩是同一套做法，这里再多给一个显式参数，测试不必打补丁。
    """
    term = (name or "").strip()
    if not term:
        return None
    try:
        if rows_fn is None:
            from app import intent as _intent  # 延迟导入：ocr 已被 intent 依赖，模块级会成环
            rows = [r for r in _intent._basicinfo_rows(term, timeout=_CODE_CORRECT_TIMEOUT)
                    if not _intent._is_bond(r)]
        else:
            rows = [r for r in (rows_fn(term) or []) if r.get("code")]
    except Exception:  # noqa: BLE001 —— 查不到不是错误，走"保留模型代码"
        return None
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    # 多候选：**先看有没有精确同名**，再看"是否明显胜出"。为什么不能只看相似度差——
    # 中文简称的歧义大多是**前缀**关系（国投白银 / 国投白银基金、XXETF / XXETF联接），
    # 逐字相似度能到 0.8、差值 0.2，仅凭"差值够大"会把一只票**静默换成另一只**。
    exact = [r for r in rows if _norm_name(r.get("name")) == _norm_name(term)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None  # 真的重名 → 说不清是哪只，宁可不改
    ranked = sorted(rows, key=lambda r: _name_similarity(term, r.get("name") or ""), reverse=True)
    top = _name_similarity(term, ranked[0].get("name") or "")
    second = _name_similarity(term, ranked[1].get("name") or "")
    if top < _CLEAR_WIN_RATIO or top - second < _AMBIGUOUS_MARGIN:
        return None  # 分不开 → 宁可不改（改错的代价远大于不改）
    return ranked[0]


def _cached_resolve(term: str, rows_fn=None) -> dict | None:
    """按名称缓存（**含负缓存**：查不到的名称也缓存，否则一条 20 行的列表要白等 20 次超时）。

    模块级 dict 的假设是**单进程 uvicorn**（与 backtest._ACTIVE_RUNS、trust 的 60s 缓存同一假设）。
    多进程部署下这层缓存会分散，但正确性不受影响，只是命中率下降。
    """
    now = time.time()
    with _CACHE_LOCK:
        hit = _NAME_CODE_CACHE.get(term)
        if hit and now - hit[0] < _CODE_CACHE_TTL_S:
            return hit[1]
    got = _resolve_by_name(term, rows_fn=rows_fn)
    with _CACHE_LOCK:
        if len(_NAME_CODE_CACHE) >= _CODE_CACHE_MAX:
            _NAME_CODE_CACHE.clear()  # 简单粗暴：容量到了整体丢弃
        _NAME_CODE_CACHE[term] = (now, got)
    return got


def correct_codes(items: list[dict], *, resolve=None,
                  max_names: int = _CODE_CORRECT_MAX_NAMES) -> list[dict]:
    """逐行用名称回查 Wind 真代码，把差异就地落回该行。

    三种结果：
      1. 查到且**换了标的**（``_code_core`` 不同）→ 覆盖 code，补 ``code_corrected=True``
         与 ``code_original=<模型给的>``，前端据此提示"已按名称校正"。
      2. 查到但是**同一个标的**（``161226`` vs ``161226.SZ``）→ 静默采用 Wind 的规范写法，
         **不打标**。
      3. 名称空 / 查不到 / 出错 / 超预算 / 排在 max_names 之外 / 开关关闭 →
         **原样保留模型代码，不加任何字段**。Wind 挂了的时候，输出与本次改动前一模一样。
         这一条也不打标：否则 Wind 一挂，每行都冒出"未核实"徽标，噪音比信号大。

    性能：先按名称**去重**（同一次请求里同一只票只查一次），再最多 ``_CODE_CORRECT_WORKERS``
    路并发；整体墙钟到 ``_CODE_CORRECT_BUDGET_S`` 就收手，未回的行走第 3 条。
    """
    if not _CODE_CORRECT_ENABLED or not items:
        return items
    do_resolve = resolve or _cached_resolve

    # 1) 收集要回查的**不同名称**（保持行序，便于截断可预测）
    names: list[str] = []
    seen: set[str] = set()
    for it in items:
        nm = (it.get("name") or "").strip()
        if nm and nm not in seen:
            seen.add(nm)
            names.append(nm)
    names = names[:max_names]
    if not names:
        return items

    # 2) 并发回查。**不能用 `with ThreadPoolExecutor`**——__exit__ 会 shutdown(wait=True)，
    #    把"墙钟预算"整个吃掉（没回来的名字照样要等满自己的超时）。必须显式 shutdown(wait=False)。
    got: dict[str, dict | None] = {}
    pool = ThreadPoolExecutor(max_workers=min(_CODE_CORRECT_WORKERS, len(names)))
    try:
        futs = {pool.submit(do_resolve, nm): nm for nm in names}
        done, _pending = _futures_wait(futs, timeout=_CODE_CORRECT_BUDGET_S)
        for f in done:
            nm = futs[f]
            try:
                got[nm] = f.result()
            except Exception:  # noqa: BLE001
                got[nm] = None
    finally:
        pool.shutdown(wait=False)

    # 3) 就地落回
    for it in items:
        nm = (it.get("name") or "").strip()
        hit = got.get(nm)
        if not hit or not hit.get("code"):
            continue  # 第 3 条：原样保留
        new_code = str(hit["code"]).strip()
        if not new_code:
            continue
        old_code = (it.get("code") or "").strip()
        if not old_code:
            # 模型没给出代码（自选列表的"只有名称"行）→ 这是**补全**，不是"纠正"。
            # 不打标：标签会写成「已按名称校正（原 空）」，读起来莫名其妙；而按名称查到的代码
            # 本来就比模型没读到更可靠，静默采用即可。
            it["code"] = new_code
            continue
        if _code_core(new_code) == _code_core(old_code):
            it["code"] = new_code  # 第 2 条：同一个标的，只采用规范写法，不打标
            continue
        # 第 1 条：换了标的 → 覆盖并打标（名称保留模型给的，不拿 Wind 的简称覆盖用户看到的字）
        it["code"] = new_code
        it["code_corrected"] = True
        it["code_original"] = old_code
    return items
