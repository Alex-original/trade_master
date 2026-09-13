"""证券代码纠错（按名称回查 Wind 真代码）离线冒烟脚本。

确定性、离线：**不跑 LLM、不连数据库、不碰 Wind**——解析器与名称回查都是替身，
所以整条路一个字节都不出网，可反复运行、秒级出结果。

**为什么单独一个脚本**：这一节治的是一个"看起来对、其实把标的换掉了"的洞——
``app/ocr.py`` 的提示词里曾经拿 ``06862.HK`` 当示例，模型读不出代码时把它照抄了回来
（用户实测：国投白银LOF 的代码显示成 06862.HK，实际 161226）。去示例治的是"不再诱导"，
按名称回查治的是"模型仍然幻觉"。两层的回归断言都在这里。

覆盖：
  1. 提示词卫生：4 套提示词里**不许再出现任何具体代码**（这是防复发的唯一一道闸）
  2. ``_code_core`` 的判别力：同标的的不同写法不许被判成"换了"
  3. 主路径：换了标的 → 覆盖 + 打标；名称不被 Wind 的简称覆盖
  4. 同一标的 → 只采用规范写法，**不打标**（否则每行都挂徽标，提示变噪音）
  5. 失败要优雅：查不到 / 抛错 / 超预算 → 原样保留、不加字段、不抛
  6. 歧义不改：中文简称的**前缀**歧义（国投白银 / 国投白银基金）与 A/C 份额
  7. 去重 / 上限 / 墙钟预算
  8. 自选路径：空代码按名称**补全**（是补全，不是纠正，不打标）
  9. 缓存与开关
 10. 联网隔离自证

用法：
    .venv/bin/python scripts/smoke_ocr_codes.py
"""
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import ocr  # noqa: E402
from tradingagents.dataflows import wind as _wind  # noqa: E402

# ---------------------------------------------------------------- 断言脚手架

_FAILS: list[str] = []
_COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _COUNT
    _COUNT += 1
    if cond:
        print(f"  ✅ {name}")
    else:
        _FAILS.append(name)
        print(f"  ❌ {name}{('  →  ' + detail) if detail else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 替身

_REAL_CALL_TOOL = _wind._call_tool


def _forbidden_tool(server_type, tool_name, params, timeout=120):
    """一被调用就炸。用来证明整条路**确实没有触网**，而不是只返回了哨兵。"""
    raise AssertionError(f"代码纠错不应触网：{server_type}/{tool_name} {params}")


def _install_tool(fn) -> None:
    _wind._call_tool = fn  # type: ignore[assignment]


def _llm_stub(positions=None, stocks=None):
    """冒充 ``ocr._llm_json``：不跑模型，直接给一份"模型输出"。

    ``_normalize`` 会丢掉没有 code 的行，所以持仓侧想造"空代码"是造不出来的（那是既有行为，
    见文件末尾的说明）；自选侧的 ``_normalize_watch`` 才会保留"只有名称"的行。
    """
    payload = {}
    if positions is not None:
        payload = {"positions": positions, "confidence": "high", "available_cash": None}
    if stocks is not None:
        payload = {"stocks": stocks, "confidence": "high"}
    return lambda system, messages: payload


class _Calls:
    """记录替身被问了哪些名称。用类而不是闭包，便于在多线程里安全计数。"""

    def __init__(self, table=None):
        self.names: list[str] = []
        self._table = table or {}
        self._lock = threading.Lock()

    def __call__(self, name, timeout=90):
        with self._lock:
            self.names.append(name)
        return self._table.get(name, [])


# ---------------------------------------------------------------- 1. 提示词卫生

def s1_prompts() -> None:
    section("1. 提示词卫生：不许再出现任何具体代码（防复发的唯一一道闸）")
    # 具体代码的形态：4~6 位数字 + 后缀。示例串一旦回到提示词里，模型就有东西可抄。
    PAT = re.compile(r"\d{4,6}\.(?:HK|SZ|SH|BJ|OF|sh|sz|hk)", re.I)
    for label, text in (
        ("持仓·文本 _POSITION_SYSTEM", ocr._POSITION_SYSTEM),
        ("持仓·图片 _VISION_PROMPT", ocr._VISION_PROMPT),
        ("自选·文本 _WATCH_SYSTEM", ocr._WATCH_SYSTEM),
        ("自选·图片 _VISION_WATCH_PROMPT", ocr._VISION_WATCH_PROMPT),
    ):
        m = PAT.search(text)
        check(f"{label} 里没有具体代码示例", m is None, f"出现了 {m.group(0) if m else ''}")

    # 点名钉死这次的元凶串。上面那条是"形态"层面的，这条是"就是它"层面的。
    check("★ 元凶串 06862 已从图片提示词里清除（它就是被照抄回来的那个）",
          "06862" not in ocr._VISION_PROMPT and "06862" not in ocr._POSITION_SYSTEM,
          "06862 仍在持仓提示词里")

    # 四套提示词都要明确"看不清就留空"，否则模型倾向于"补一个出来"。
    for label, text in (
        ("持仓·文本", ocr._POSITION_SYSTEM),
        ("持仓·图片", ocr._VISION_PROMPT),
        ("自选·文本", ocr._WATCH_SYSTEM),
        ("自选·图片", ocr._VISION_WATCH_PROMPT),
    ):
        check(f"{label} 提示词写明了「看不清/没有就留空」", "留空" in text, text[:60])


# ---------------------------------------------------------------- 2. _code_core

def s2_code_core() -> None:
    section("2. _code_core：判断「换没换标的」的判据本身")
    check("同一标的的两种写法相等（161226.SZ / 161226）",
          ocr._code_core("161226.SZ") == ocr._code_core("161226"))
    check("★ 不同标的**不**相等（06862.HK ≠ 161226.SZ）——这条是打标的依据",
          ocr._code_core("06862.HK") != ocr._code_core("161226.SZ"))
    check("港股前导零不算身份（06862.HK == 6862.HK）",
          ocr._code_core("06862.HK") == ocr._code_core("6862.HK"))
    check("A股前导零不算身份（000001.SZ == 1）",
          ocr._code_core("000001.SZ") == ocr._code_core("1"))
    check("空串不炸", ocr._code_core("") == "")


# ---------------------------------------------------------------- 3. 主路径

def s3_main_path() -> None:
    section("3. 主路径：模型抄了示例 → 按名称换成真代码并打标")
    _install_tool(_forbidden_tool)  # 整节离线自证：任何真实触网都会炸
    real_llm = ocr._llm_json
    ocr._llm_json = _llm_stub(positions=[
        {"code": "06862.HK", "name": "国投白银LOF", "qty": 2000, "cost_price": 1.955},
    ])
    try:
        resolve = _Calls({"国投白银LOF": [{"code": "161226.SZ", "name": "国投瑞银白银期货A"}]})
        out = ocr.parse_holdings("text", "随便一段持仓文本",
                                 code_resolver=lambda nm: (resolve(nm) or [None])[0])
        row = out["positions"][0]
        check("★ 代码被换成 161226.SZ（这就是用户报的那个 bug）",
              row["code"] == "161226.SZ", row["code"])
        check("★ 打了 code_corrected 标（前端据此提示「已按名称校正」）",
              row.get("code_corrected") is True, str(row.get("code_corrected")))
        check("★ 留下了原代码 code_original=06862.HK（用户能看到 AI 原本给的是什么）",
              row.get("code_original") == "06862.HK", str(row.get("code_original")))
        check("**名称没被 Wind 的简称覆盖**（用户看到的还是截图上的「国投白银LOF」）",
              row["name"] == "国投白银LOF", row["name"])
        check("qty / cost_price 原样保留（纠错只碰代码）",
              row["qty"] == 2000 and row["cost_price"] == 1.955, str(row))

        # 同一标的的规范写法：不打标
        ocr._llm_json = _llm_stub(positions=[
            {"code": "600519", "name": "贵州茅台", "qty": 100, "cost_price": 1500.0},
        ])
        out = ocr.parse_holdings("text", "随便一段持仓文本",
                                 code_resolver=lambda nm: {"code": "600519.SH", "name": "贵州茅台"})
        row = out["positions"][0]
        check("★ 同一标的（600519 → 600519.SH）：采用规范写法但**不打标**",
              row["code"] == "600519.SH" and "code_corrected" not in row, str(row))
        check("★ 不打标那行的字段集与改动前**完全相同**（默认路径逐字节不变）",
              set(row.keys()) == {"code", "name", "qty", "cost_price"}, str(sorted(row.keys())))
    finally:
        ocr._llm_json = real_llm


# ---------------------------------------------------------------- 4. 失败要优雅

def s4_graceful() -> None:
    section("4. 失败要优雅：查不到 / 抛错 / 上限外 → 原样保留、不加字段、不抛")
    base = {"code": "06862.HK", "name": "国投白银LOF", "qty": 2000, "cost_price": 1.955}

    def run(resolve):
        rows = [dict(base)]
        return ocr.correct_codes(rows, resolve=resolve)[0]

    r = run(lambda nm: None)
    check("替身返回 None → 保留原代码", r["code"] == "06862.HK" and "code_corrected" not in r, str(r))
    check("★ 且字段集与传入时完全一致（没多出任何字段）",
          set(r.keys()) == set(base.keys()), str(sorted(r.keys())))

    r = run(lambda nm: [])
    check("替身返回空表 → 保留原代码", r["code"] == "06862.HK", str(r))

    def boom(nm):
        raise RuntimeError("Wind 挂了")

    r = run(boom)
    check("★ 替身抛异常 → **不向上抛**，保留原代码（同步持仓绝不能因为校验而整个失败）",
          r["code"] == "06862.HK" and "code_corrected" not in r, str(r))

    # 名称查真代码这条路本身抛错（默认实现里由 _resolve_by_name 兜住）
    check("★ _resolve_by_name 自己吞掉异常并返回 None（不往上传）",
          ocr._resolve_by_name("贵州茅台", rows_fn=boom) is None)


# ---------------------------------------------------------------- 5. 歧义不改

def s5_ambiguity() -> None:
    section("5. 歧义不改：宁可不改，不要改错（改错的代价远大于不改）")

    # 中文简称的歧义大多是**前缀**关系，逐字相似度能到 0.8 —— 只看"差值够大"会把票换掉
    r = ocr._resolve_by_name("国投白银", rows_fn=lambda t: [
        {"code": "A.SZ", "name": "国投白银"}, {"code": "B.SZ", "name": "国投白银基金"}])
    check("★ 前缀歧义（国投白银 / 国投白银基金）：有精确同名 → 采用它，不误判成「分不开」",
          r and r["code"] == "A.SZ", str(r))

    r = ocr._resolve_by_name("国投瑞银白银期货", rows_fn=lambda t: [
        {"code": "A.SZ", "name": "国投瑞银白银期货A"}, {"code": "C.SZ", "name": "国投瑞银白银期货C"}])
    check("★ A/C 份额分不开 → **不改**（说不清是哪一只）", r is None, str(r))

    r = ocr._resolve_by_name("同名", rows_fn=lambda t: [
        {"code": "A", "name": "同名"}, {"code": "B", "name": "同名"}])
    check("真·重名 → 不改", r is None, str(r))

    r = ocr._resolve_by_name("国投白银LOF", rows_fn=lambda t: [
        {"code": "A.SZ", "name": "某某完全不相关"}, {"code": "161226.SZ", "name": "国投白银LOF"}])
    check("候选里有一个明显更匹配 → 采用它", r and r["code"] == "161226.SZ", str(r))

    r = ocr._resolve_by_name("某某", rows_fn=lambda t: [
        {"code": "A", "name": "完全不相干甲"}, {"code": "B", "name": "完全不相干乙"}])
    check("★ 候选都不像（相似度低于阈值）→ 不改", r is None, str(r))

    check("名称为空 → 不改（不打 Wind）",
          ocr._resolve_by_name("", rows_fn=lambda t: [{"code": "X"}]) is None)
    check("候选为空 → 不改", ocr._resolve_by_name("查不到", rows_fn=lambda t: []) is None)


# ---------------------------------------------------------------- 6. 去重/上限/预算

def s6_perf() -> None:
    section("6. 去重 / 上限 / 墙钟预算")
    calls = _Calls()
    rows = [{"code": "06862.HK", "name": "国投白银LOF", "qty": 1, "cost_price": 1.0}
            for _ in range(3)]
    ocr.correct_codes(rows, resolve=lambda nm: (calls(nm) or [None])[0])
    check("★ 同一次请求里同名 3 行只查 **1 次**（按名称去重，不是按行）",
          len(calls.names) == 1, str(calls.names))

    calls = _Calls()
    items = [{"code": "1", "name": f"N{i}", "qty": 1, "cost_price": 1.0} for i in range(40)]
    ocr.correct_codes(items, resolve=lambda nm: (calls(nm) or [None])[0], max_names=25)
    check("★ 超过上限（40 个名字 / 上限 25）时只查前 25 个，其余原样保留",
          len(calls.names) == 25 and items[39]["code"] == "1", f"{len(calls.names)} 次")

    # 墙钟预算：替身 sleep 2s + 预算压到 0.01 → 必须**立刻**返回且不抛。
    # 这条同时是「没有被 `with ThreadPoolExecutor` 包住」的守门人：包了的话 __exit__ 会
    # shutdown(wait=True)，于是这里要等满 2 秒才返回 —— 那样这条断言就测不出东西了。
    old_budget = ocr._CODE_CORRECT_BUDGET_S
    ocr._CODE_CORRECT_BUDGET_S = 0.01
    try:
        t0 = time.time()
        rows = [{"code": "06862.HK", "name": "国投白银LOF", "qty": 1, "cost_price": 1.0}]
        out = ocr.correct_codes(rows, resolve=lambda nm: (time.sleep(2), None)[1])
        dt = time.time() - t0
    finally:
        ocr._CODE_CORRECT_BUDGET_S = old_budget
    check("★ 墙钟预算生效：替身睡 2s、预算 0.01s ⇒ 立刻返回（不是等满 2s）",
          dt < 1.0, f"实测 {dt:.3f}s")
    check("超预算的行保留模型代码，不抛", out[0]["code"] == "06862.HK", str(out[0]))


# ---------------------------------------------------------------- 7. 自选路径

def s7_watchlist() -> None:
    section("7. 自选路径：空代码按名称补全（是补全，不是纠正，不打标）")
    real_llm = ocr._llm_json
    try:
        ocr._llm_json = _llm_stub(stocks=[{"code": "", "name": "国投白银LOF"}])
        out = ocr.parse_watchlist("text", "随便一段自选文本",
                                  code_resolver=lambda nm: {"code": "161226.SZ", "name": "国投瑞银白银期货A"})
        row = out["stocks"][0]
        check("★ 模型没读到代码 → 按名称**补全**成 161226.SZ", row["code"] == "161226.SZ", str(row))
        check("★ 补全**不打标**（空 → 有不是「纠正」，标签会写成「原 空」很奇怪）",
              "code_corrected" not in row, str(row))

        ocr._llm_json = _llm_stub(stocks=[{"code": "06862.HK", "name": "国投白银LOF"}])
        out = ocr.parse_watchlist("text", "随便一段自选文本",
                                  code_resolver=lambda nm: {"code": "161226.SZ", "name": "国投瑞银白银期货A"})
        row = out["stocks"][0]
        check("★ 自选侧的示例幻觉同样被纠正并打标（提示词里那条示例两边都有）",
              row["code"] == "161226.SZ" and row.get("code_corrected") is True, str(row))

        # 名称查不到 → 自选行原样（连"只有名称"的行也保住，不被吞掉）
        ocr._llm_json = _llm_stub(stocks=[{"code": "", "name": "查不到的票"}])
        out = ocr.parse_watchlist("text", "随便一段自选文本", code_resolver=lambda nm: None)
        check("★ 查不到时「只有名称」的行仍然保留（_normalize_watch 的既有行为不被破坏）",
              len(out["stocks"]) == 1 and out["stocks"][0]["name"] == "查不到的票", str(out))
    finally:
        ocr._llm_json = real_llm


# ---------------------------------------------------------------- 8. 缓存与开关

def s8_cache_switch() -> None:
    section("8. 按名称缓存 与 总开关")
    ocr._NAME_CODE_CACHE.clear()
    calls = _Calls({"贵州茅台": [{"code": "600519.SH", "name": "贵州茅台"}]})
    for _ in range(3):
        ocr._cached_resolve("贵州茅台", rows_fn=calls)
    check("★ 跨请求缓存命中：问 3 次只打 Wind **1** 次（同步持仓是高频操作）",
          len(calls.names) == 1, str(calls.names))

    calls = _Calls()
    ocr._cached_resolve("查不到的票", rows_fn=calls)
    ocr._cached_resolve("查不到的票", rows_fn=calls)
    check("★ **负缓存**：查不到的名称也缓存，不反复白等（否则 20 行列表要等 20 次超时）",
          len(calls.names) == 1, str(calls.names))

    ocr._NAME_CODE_CACHE.clear()
    old = ocr._CODE_CORRECT_ENABLED
    calls = _Calls()
    try:
        ocr._CODE_CORRECT_ENABLED = False
        rows = [{"code": "06862.HK", "name": "国投白银LOF", "qty": 1, "cost_price": 1.0}]
        out = ocr.correct_codes(rows, resolve=lambda nm: (calls(nm) or [None])[0])
        check("★ 开关 OCR_CODE_CORRECT=0 → **0 次**调用、原样返回（生产上的回滚开关）",
              not calls.names and out[0]["code"] == "06862.HK", str(calls.names))
    finally:
        ocr._CODE_CORRECT_ENABLED = old
        ocr._NAME_CODE_CACHE.clear()


# ---------------------------------------------------------------- 9. 联网隔离自证

def s9_offline() -> None:
    section("9. 联网隔离自证：把 _wind._call_tool 换成「一调用就炸」之后，上面每一节仍然要过")
    _install_tool(_forbidden_tool)
    try:
        ocr._NAME_CODE_CACHE.clear()
        rows = [{"code": "06862.HK", "name": "国投白银LOF", "qty": 1, "cost_price": 1.0}]
        out = ocr.correct_codes(rows, resolve=lambda nm: {"code": "161226.SZ", "name": "x"})
        check("注入替身的那条路 0 次触网", out[0]["code"] == "161226.SZ")
        # 默认实现（没注入替身）会真的去调 _basicinfo_rows → 现在它必须先被 _forbidden_tool 炸掉、
        # 再被 _resolve_by_name 吞掉，返回 None —— 而不是把异常抛到同步持仓的调用方。
        r = ocr._resolve_by_name("贵州茅台")
        check("★ **不注入替身**时（默认走 Wind）：Wind 挂了也只是返回 None，不向上抛",
              r is None, str(r))
    finally:
        _install_tool(_REAL_CALL_TOOL)


def main() -> int:
    print("证券代码纠错离线冒烟（不跑 LLM / 不连库 / 不碰 Wind）")
    s1_prompts()
    s2_code_core()
    s3_main_path()
    s4_graceful()
    s5_ambiguity()
    s6_perf()
    s7_watchlist()
    s8_cache_switch()
    s9_offline()

    print("\n" + "=" * 60)
    if _FAILS:
        print(f"❌ {len(_FAILS)}/{_COUNT} 项未通过：")
        for f in _FAILS:
            print("   - " + f)
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
