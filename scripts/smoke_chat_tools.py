"""AI 对话助手（只读工具层 + 工具循环）的离线回归。

不连 Postgres、不连 Wind、不跑真 LLM：
- ``app.db.SessionLocal`` 换成绑**内存 SQLite** 的 sessionmaker（照抄 ``smoke_snapshot_reset.py``）。
  **不要**调 ``db.init_db()``——里面的 ``_ensure_schema`` 是 PG 方言。
- 行情出口（``account.get_quotes`` / ``watchlist.get_stock_detail`` / ``watchlist.search_stocks``）
  直接给模块属性赋值打桩。
- LLM 出口（``chat.create_llm_client``）换成按脚本回放的假客户端。

覆盖三件事：
1. 工具层的**结构性只读**（白名单、无 ``user_id`` 入参、源码里没有写函数/``tool_choice``）。
2. 工具输出约定（空态显式、计划投影不吐 200KB 原文）。
3. 工具循环的六个陷阱（``reasoning_content`` 往返、并发 tool_calls、异常、轮次耗尽、确认哨兵）。

用法（在 product/trade_master 下）：
    .venv/bin/python scripts/smoke_chat_tools.py
"""
import ast
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 假 key：_build_llm 只做存在性检查，真调用会被下面的假客户端截走。绝不写真实 key。
os.environ.setdefault("DEEPSEEK_API_KEY", "smoke-test-not-a-real-key")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import db  # noqa: E402

# ---------------------------------------------------------------- 内存库
_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
db.Base.metadata.create_all(_engine)
db.SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)  # type: ignore[assignment]

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from app import account as account_mod  # noqa: E402
from app import analysis_service  # noqa: E402
from app import chat  # noqa: E402
from app import chat_tools  # noqa: E402
from app import intent as intent_mod  # noqa: E402
from app import watchlist as watchlist_mod  # noqa: E402

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

# ---------------------------------------------------------------- 断言脚手架
_COUNT = 0
_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _COUNT
    _COUNT += 1
    if cond:
        print(f"  ✅ {name}")
    else:
        _FAILS.append(name)
        print(f"  ❌ {name}" + (f"  实际：{detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 期望的白名单
# 这里**显式列出**全部工具名。多一个少一个都要改这里——加工具必须过审。
EXPECTED_TOOLS = {
    "get_account_overview", "get_positions", "get_quotes",
    "get_trades", "get_orders", "get_analytics",
    "get_trust_config", "get_plan_summary", "get_monitor_conditions",
    "get_plan_status", "get_managed_symbols",
    "get_research_ratings", "get_research_report",
    "get_watchlist", "search_stock", "get_stock_history",
    "get_notification_configs",
    "request_deep_analysis",
}

#: 绝不允许出现在工具层/循环里的名字：写库、跑 LLM、或会阻塞 10-15 分钟的入口。
FORBIDDEN = {
    "toggle_trust", "update_trust_config", "submit_plan_generation", "run_plan_for_user",
    "reset_trust", "snapshot_trust_book", "run_execution", "submit_analysis",
    "run_analysis", "run_portfolio_plan", "run_analysis_cached",
}

#: 通用反射逃生口：只在**工具层**禁（有了它，白名单就形同虚设）。
#: 循环里 ``getattr(ai, "tool_calls", None)`` 是正常属性读取，不在此列。
FORBIDDEN_REFLECTION = {"eval", "exec", "globals", "setattr", "vars", "compile", "__import__"}

# ---------------------------------------------------------------- 打桩：行情
UID = 1

QUOTES = {
    "518880.SH": {"price": 8.85, "prev_close": 9.03, "volume_ratio": 1.803},
    "600519.SH": {"price": 1700.0, "prev_close": 1680.0, "volume_ratio": 0.92},
    "300750.SZ": {"price": 215.5, "prev_close": 213.0, "volume_ratio": None},
}
account_mod.get_quotes = lambda codes: {  # type: ignore[assignment]
    c: QUOTES[c] for c in (codes or []) if c in QUOTES
}

_KLINE = [
    {"date": f"2026-08-{i:02d}", "open": 8.0 + i * 0.01, "high": 8.2 + i * 0.01,
     "low": 7.9 + i * 0.01, "close": 8.1 + i * 0.01, "volume": 1e6}
    for i in range(1, 21)
]
watchlist_mod.get_stock_detail = lambda code, days=60: {  # type: ignore[assignment]
    "code": code, "name": "黄金ETF华安", "price": _KLINE[-1]["close"], "kline": _KLINE[-days:]
}
watchlist_mod.search_stocks = lambda user_id, q: [  # type: ignore[assignment]
    {"code": "518880.SH", "name": "黄金ETF华安"}
]


# ---------------------------------------------------------------- 夹具
def wipe() -> None:
    s = db.get_session()
    try:
        for model in (db.Trade, db.Order, db.TrustPlan, db.Position, db.TrustConfig,
                      db.Watchlist, db.WatchlistMeta, db.WatchlistGroup, db.EngineRun,
                      db.NotificationConfig, db.Account, db.User):
            s.query(model).delete()
        s.commit()
    finally:
        s.close()


def seed_user() -> None:
    s = db.get_session()
    try:
        s.add(db.User(id=UID, phone="13800000000", created_at=0.0))
        s.commit()
    finally:
        s.close()


def seed_trust(**kw) -> None:
    s = db.get_session()
    try:
        s.add(db.TrustConfig(
            user_id=UID,
            is_active=kw.get("is_active", True),
            book_created=kw.get("book_created", True),
            available_cash=kw.get("available_cash", 120000.0),
            stock_scope=kw.get("stock_scope", 0),
            style=kw.get("style", 1),
            risk_max_trades_day=kw.get("risk_max_trades_day", 6),
            risk_max_position_pct=kw.get("risk_max_position_pct", 0.2),
            risk_stop_loss_pct=kw.get("risk_stop_loss_pct", 0.08),
            updated_at=0.0,
        ))
        s.commit()
    finally:
        s.close()


def seed_position(book: int, code: str, name: str, qty: int, cost: float) -> None:
    s = db.get_session()
    try:
        s.add(db.Position(
            user_id=UID, book=book, stock_code=code, stock_name=name,
            hold_qty=qty, available_qty=qty, frozen_qty=0, cost_price=cost, updated_at=0.0,
        ))
        s.commit()
    finally:
        s.close()


def seed_trade(n: int = 2) -> None:
    s = db.get_session()
    try:
        for i in range(n):
            s.add(db.Trade(
                trade_id=f"T{i}", order_id=f"O{i}", user_id=UID, stock_code="600519.SH",
                stock_name="贵州茅台", direction=1, price=1700.0, quantity=100,
                amount=170000.0, fee=51.0, ai_reason=f"理由{i}", traded_at=1789000000.0 + i,
            ))
        s.commit()
    finally:
        s.close()


def seed_order(n: int = 3) -> None:
    s = db.get_session()
    try:
        for i in range(n):
            s.add(db.Order(
                order_id=f"O{i}", user_id=UID, stock_code="600519.SH", stock_name="贵州茅台",
                direction=0, price=1700.0, quantity=100, status=3 if i == 0 else 1,
                source=1, fail_reason="资金不足" if i == 0 else "", created_at=1789000000.0 + i,
            ))
        s.commit()
    finally:
        s.close()


def seed_plan(actions: list, summary: str = "减黄金、加茅台", cash_target: float = 0.25,
              risk_notes: str = "注意隔夜跳空", missing: list | None = None,
              research_blob: str = "") -> None:
    plan = {
        "summary": summary,
        "cash_target": cash_target,
        "risk_notes": risk_notes,
        "actions": actions,
        "process": {"research_missing": missing or [], "research": research_blob},
    }
    s = db.get_session()
    try:
        s.add(db.TrustPlan(user_id=UID, trade_date="2026-09-14",
                           plan_json=json.dumps(plan, ensure_ascii=False), created_at=1789000000.0))
        s.commit()
    finally:
        s.close()


def seed_research(rating: str = "Buy") -> None:
    s = db.get_session()
    try:
        s.add(db.EngineRun(
            user_id=UID, ticker="518880.SH", market="CN", trade_date="2026-09-10",
            rating=rating,
            report_json=json.dumps({
                "portfolio_manager": "**Executive Summary**：黄金短空长多\n**Price Target**：9.50",
                "trader": "**Stop Loss**：8.70\n**Position Sizing**：不超过 10%",
                "bull": "看多理由若干" * 500,
            }, ensure_ascii=False),
            created_at=1789000000.0,
        ))
        s.commit()
    finally:
        s.close()


def seed_watchlist() -> None:
    s = db.get_session()
    try:
        s.add(db.WatchlistMeta(user_id=UID, active_group="默认", created_at=0.0))
        s.add(db.Watchlist(user_id=UID, stock_code="518880.SH", stock_name="黄金ETF华安",
                           source=0, group_name="默认", created_at=0.0))
        s.commit()
    finally:
        s.close()


def seed_notify() -> None:
    s = db.get_session()
    try:
        s.add(db.NotificationConfig(user_id=UID, channel="feishu",
                                    webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/" + "a" * 30,
                                    is_enabled=True))
        s.commit()
    finally:
        s.close()


def ladder_actions(n_codes: int = 3, reason_pad: int = 0) -> list:
    """造 n_codes 只标的：每只一条减仓梯（两档）+ 一条加仓档，触发价由浅到深。"""
    acts: list[dict] = []
    pad = "补" * reason_pad
    for i in range(n_codes):
        code = f"{600000 + i}.SH"
        acts.append({"code": code, "name": f"标的{i}", "action": "reduce", "target_weight": 0.12,
                     "trigger_type": "price_below", "trigger_price": 9.00 - i * 0.01,
                     "volume_ratio_min": 1.5, "reason": "放量跌破先减一半" + pad})
        acts.append({"code": code, "name": f"标的{i}", "action": "sell", "target_weight": 0.0,
                     "trigger_type": "price_below", "trigger_price": 8.70 - i * 0.01,
                     "reason": "硬止损" + pad})
        acts.append({"code": code, "name": f"标的{i}", "action": "add", "target_weight": 0.18,
                     "trigger_type": "price_above", "trigger_price": 9.60 + i * 0.01,
                     "reason": "突破加仓" + pad})
    return acts


def monitor_actions() -> list:
    """518880.SH 现价 8.85 / 量比 1.803：一档已触发（≤9.00 且量比够）、一档没到（≤8.70）。"""
    return [
        {"code": "518880.SH", "name": "黄金ETF华安", "action": "reduce", "target_weight": 0.12,
         "trigger_type": "price_below", "trigger_price": 9.00, "volume_ratio_min": 1.5,
         "reason": "放量跌破先减一半"},
        {"code": "518880.SH", "name": "黄金ETF华安", "action": "sell", "target_weight": 0.0,
         "trigger_type": "price_below", "trigger_price": 8.70, "reason": "硬止损"},
        {"code": "518880.SH", "name": "黄金ETF华安", "action": "add", "target_weight": 0.18,
         "trigger_type": "price_above", "trigger_price": 9.60, "reason": "突破加仓"},
    ]


def tools_by_name(user_id: int = UID) -> dict:
    return {t.name: t for t in chat_tools.build_registry(user_id)}


def call(name: str, **kw) -> str:
    return tools_by_name()[name].invoke(kw)


# ---------------------------------------------------------------- 假 LLM
class _FakeLLM:
    """按脚本回放的假 LLM。

    ``script`` 是每轮一个 ``callable(history) -> AIMessage``；脚本用完后返回纯文本收尾，
    模拟「轮次/预算耗尽后剥掉工具再问一轮」。``bind_tools`` 返回自身，于是 bound.invoke
    与 llm.invoke 是同一条路径——正好也断言了 bind_tools 不带 tool_choice。
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.rounds = 0
        self.seen: list[list] = []
        self.bound_tool_names: list[str] = []
        self.bind_kwargs: dict = {}

    def bind_tools(self, tools, **kwargs):
        self.bind_kwargs = kwargs
        self.bound_tool_names = [t.name for t in tools]
        return self

    def invoke(self, history, **kwargs):
        self.seen.append(list(history))
        i = self.rounds
        self.rounds += 1
        if i < len(self.script):
            return self.script[i](history)
        return AIMessage(content="（已就手头数据作答）")


class _FakeClient:
    def __init__(self, llm: _FakeLLM):
        self.llm = llm

    def get_llm(self) -> _FakeLLM:
        return self.llm


_CURRENT_LLM: _FakeLLM | None = None


def _fake_factory(provider, model, **kwargs):
    return _FakeClient(_CURRENT_LLM)  # type: ignore[arg-type]


chat.create_llm_client = _fake_factory  # type: ignore[assignment]


def run_loop(script: list, messages=None) -> tuple[dict, _FakeLLM]:
    """用假 LLM 跑一轮 answer_with_tools，返回 (结果, 假 LLM)。"""
    global _CURRENT_LLM
    fake = _FakeLLM(script)
    _CURRENT_LLM = fake
    try:
        out = chat.answer_with_tools(UID, messages or [{"role": "user", "content": "问一句"}])
    finally:
        _CURRENT_LLM = None
    return out, fake


def tool_call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _tool_messages(history: list) -> list:
    return [m for m in history if isinstance(m, ToolMessage)]


# ---------------------------------------------------------------- 静态只读断言
def identifiers_in(path: str) -> set:
    """源码里出现的所有标识符（Name.id + Attribute.attr）。

    走 AST 而不是字符串匹配，是因为 ``chat_tools`` 的**模块 docstring 里就列着那些禁止的
    写函数名**（作为「禁止 import 清单」）——字符串匹配会误报，AST 只看得见真正的代码。
    """
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg:
                    out.add(kw.arg)
    return out


# ================================================================ 主流程
def main() -> int:  # noqa: C901 —— 冒烟脚本，线性罗列各场景
    # ------------------------------------------------------------ 1. 注册表
    section("1. 工具注册表：白名单完整、schema 可用")
    reg = chat_tools.build_registry(UID)
    names = [t.name for t in reg]
    check("工具数量与白名单一致", set(names) == EXPECTED_TOOLS,
          f"多={sorted(set(names) - EXPECTED_TOOLS)} 少={sorted(EXPECTED_TOOLS - set(names))}")
    check("工具名无重复", len(names) == len(set(names)), str(len(names)))
    check("每个工具都有非空 name/description",
          all(t.name and (t.description or "").strip() for t in reg))
    check("每个工具 args_schema 可取到 properties",
          all(isinstance(t.args_schema.model_json_schema().get("properties"), dict) for t in reg))

    # ------------------------------------------------------------ 2. 只读静态断言
    section("2. 结构性只读（静态断言，廉价回归护栏）")
    for fname in ("chat_tools.py", "chat.py"):
        ids = identifiers_in(os.path.join(APP_DIR, fname))
        hit = sorted(ids & FORBIDDEN)
        check(f"{fname} 源码不含任何写库/跑 LLM 入口", not hit, str(hit))
        check(f"{fname} 不出现 tool_choice", "tool_choice" not in ids,
              str(sorted(i for i in ids if "tool_choice" in i)))
    tool_ids = identifiers_in(os.path.join(APP_DIR, "chat_tools.py"))
    refl = sorted(tool_ids & FORBIDDEN_REFLECTION)
    check("★ chat_tools.py 无反射逃生口（否则白名单形同虚设）", not refl, str(refl))

    # bind_tools 必须只传工具、不带任何关键字（tool_choice 会 400）
    with open(os.path.join(APP_DIR, "chat.py"), "r", encoding="utf-8") as f:
        chat_tree = ast.parse(f.read())
    bind_calls = [n for n in ast.walk(chat_tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "bind_tools"]
    check("chat.py 里恰好一处 bind_tools", len(bind_calls) == 1, str(len(bind_calls)))
    check("★ bind_tools 不传任何关键字参数（thinking 模型带 tool_choice 直接 400）",
          all(not c.keywords for c in bind_calls),
          str([{k.arg: ast.unparse(k.value) for k in c.keywords} for c in bind_calls]))

    # user_id 绝不能出现在入参里（模型既不能传也不能伪造）
    leaked = []
    for t in reg:
        props = t.args_schema.model_json_schema().get("properties", {})
        if "user_id" in props:
            leaked.append(f"{t.name}.args_schema")
        if "user_id" in t.func.__code__.co_varnames[:t.func.__code__.co_argcount]:
            leaked.append(f"{t.name}.signature")
    check("★ 没有任何工具接受 user_id 入参", not leaked, str(leaked))

    # ------------------------------------------------------------ 3. 空态
    section("3. 空态：如实说「暂无」，不把空态读成 0")
    wipe()
    seed_user()          # 只有用户行，没有托管配置/持仓/计划/自选/研究
    empty_expect = {
        "get_account_overview": ["真实账户（镜像簿）", "AI 托管簿"],
        "get_positions": ["尚未同步真实持仓", "尚未建簿或托管簿为空"],
        "get_trust_config": ["托管开关：未开启", "尚未建簿"],
        "get_plan_summary": ["暂无计划"],
        "get_monitor_conditions": ["暂无监控条件"],
        "get_trades": ["暂无成交"],
        "get_orders": ["暂无委托"],
        "get_managed_symbols": ["暂无"],
        "get_watchlist": ["暂无自选标的"],
        "get_notification_configs": ["尚未配置"],
        "get_research_ratings": ["暂无"],
    }
    for tool_name, needles in empty_expect.items():
        out = tools_by_name()[tool_name].invoke({})
        ok = isinstance(out, str) and all(n in out for n in needles)
        check(f"{tool_name} 空态文案正确", ok, repr(out[:120]))

    out = call("get_analytics")
    check("get_analytics 空态：已实现盈亏如实说暂未统计",
          "暂未统计" in out and "请勿用成交流水倒推" in out, repr(out))
    out = call("get_plan_status")
    check("get_plan_status 给得出下一执行日（不靠模型自己推算交易日）",
          "下一执行日" in out and len(out.split("下一执行日：")[1].split("\n")[0].strip()) == 10, repr(out))
    out = call("get_quotes", codes=[])
    check("get_quotes 空入参不炸", "未提供证券代码" in out, repr(out))
    out = call("get_research_report", code="518880")
    check("get_research_report 未研究时说「尚未做过深度研究」",
          "尚未做过深度研究" in out, repr(out))
    out = call("get_research_report", code="518880", role="不存在的角色")
    check("get_research_report 非法 role 回可选值（不抛异常）",
          "role 取值无效" in out and "portfolio_manager" in out, repr(out))
    out = call("get_stock_history", code="518880", days=20)
    check("get_stock_history 用 20 根交易日算出区间涨跌幅",
          "区间涨跌幅" in out and "近 20 个交易日" in out, repr(out))
    out = call("get_stock_history", code="518880", days=5)
    check("get_stock_history days≤10 时给逐日 OHLC", "逐日 OHLC" in out, repr(out)[:120])

    # ------------------------------------------------------------ 4. 计划投影红线
    section("4. 次日计划投影：绝不吐 plan_json 原文（可达 200KB+）")
    wipe()
    seed_user()
    seed_trust()
    blob = "【Stage1 全文】" + ("这是一段会被原样塞进 plan_json 的研究原文。" * 4000)
    seed_plan(monitor_actions() + ladder_actions(n_codes=3), missing=["600519.SH"], research_blob=blob)
    out = call("get_plan_summary")
    check("★ 投影里没有 process.research 原文", "Stage1 全文" not in out and "研究原文" not in out)
    check("★ 投影里不出现 process 字样", "process" not in out)
    check("投影长度受控（< 6000 字）", len(out) < 6000, f"{len(out)} 字")
    check("含执行日与生成时间", "执行日 2026-09-14" in out and "生成于" in out, repr(out[:80]))
    check("含组合摘要/现金目标/风险提示",
          "组合摘要：减黄金、加茅台" in out and "目标现金占比：25.0%" in out
          and "风险提示：注意隔夜跳空" in out, repr(out[:200]))
    check("含研究缺失清单", "研究缺失" in out and "600519.SH" in out, repr(out[:200]))
    check("逐档给出动作/触发条件/目标占比",
          "[减仓]" in out and "现价 ≤ 9.0（量比 ≥ 1.5）" in out and "目标占比 12.0%" in out,
          repr(out[:400]))
    check("sell 档被归一为目标 0.0%（清仓语义）", "目标占比 0.0%" in out, repr(out[:400]))
    check("加仓档来自 entry 梯子（现价 ≥）", "现价 ≥ 9.6" in out, repr(out[:400]))

    # 超长计划必须截断并说明
    wipe()
    seed_user()
    seed_trust()
    seed_plan(ladder_actions(n_codes=60, reason_pad=200))
    out = call("get_plan_summary")
    check("★ 标的过多时按上限截断并标注",
          "计划较大，仅显示前" in out and "共 60 只" in out, repr(out[-120:]))
    check("截断后长度仍受控（< 6500 字）", len(out) < 6500, f"{len(out)} 字")

    # 监控卡片是另一份投影（带现价与触发状态），换回有行情的计划再测
    wipe()
    seed_user()
    seed_trust()
    seed_plan(monitor_actions() + ladder_actions(n_codes=3))
    out = call("get_monitor_conditions")
    check("★ 监控卡片逐档各带触发状态（518880 第 1 档已触发、后两档监控中）",
          "✅已触发 黄金ETF华安（518880.SH） [减仓]第1/3档" in out
          and "⏳监控中 黄金ETF华安（518880.SH） [清仓]第2/3档" in out,
          repr(out[:400]))
    check("监控卡片每档带现价与量比", "现价 8.85 量比 1.803" in out, repr(out[:400]))
    check("监控卡片副标题按标的计数（不是按档数）",
          "标的 4 只 / 共 12 档" in out, repr(out[:120]))
    check("监控卡片与计划同源（同样不吐原文）", "研究原文" not in out)

    # ------------------------------------------------------------ 5. 有数据形态
    section("5. 有数据：关键字段齐全")
    wipe()
    seed_user()
    seed_trust()
    seed_position(0, "600519.SH", "贵州茅台", 100, 1750.0)
    seed_position(1, "518880.SH", "黄金ETF华安", 9500, 9.0)
    seed_trade(2)
    seed_order(3)
    seed_plan(ladder_actions(n_codes=2))
    seed_research()
    seed_watchlist()
    seed_notify()

    out = call("get_positions", book="both")
    check("双簿持仓都列出来",
          "【持仓 · 镜像簿（真实账户）】" in out and "【持仓 · AI 托管簿】" in out
          and "贵州茅台（600519.SH）" in out and "黄金ETF华安（518880.SH）" in out, repr(out[:300]))
    check("持仓带浮动盈亏与当日盈亏", "浮动盈亏" in out and "当日" in out)
    out = call("get_positions", book="mirror")
    check("book=mirror 只给镜像簿", "AI 托管簿" not in out and "镜像簿" in out, repr(out[:80]))

    out = call("get_account_overview")
    check("账户总览含两簿口径说明（防翻倍红线）",
          "不要把镜像簿与托管簿相加" in out, repr(out[:400]))
    check("账户总览列出最近成交", "最近成交" in out, repr(out[:200]))

    out = call("get_quotes", codes=["518880.SH", "600519"])
    check("行情快照给出涨跌幅与量比",
          "现价 8.85" in out and "量比 1.803" in out, repr(out))
    out = call("get_quotes", codes=["999999.SH"])
    check("查不到的标的显式说无行情，不编价格",
          "无行情（可能停牌或代码错误）" in out, repr(out))

    out = call("get_trades")
    check("成交流水带 AI 下单理由", "成交记录" in out and "AI 理由：理由0" in out, repr(out[:200]))
    out = call("get_orders")
    check("委托单带状态与失败原因", "废单" in out and "资金不足" in out, repr(out[:200]))
    out = call("get_analytics")
    check("收益归因：笔数真实、已实现仍为暂未统计",
          "成交总笔数：2" in out and "暂未统计" in out, repr(out))

    out = call("get_trust_config")
    check("托管设置含开关/风格/风控/费率/运行痕迹",
          "托管开关：运行中" in out and "投资风格：均衡" in out
          and "单日最大交易次数：6" in out and "佣金" in out, repr(out))

    out = call("get_research_ratings")
    check("研究评级给出英文 + 中文 + 目标价/止损/仓位",
          "评级 Buy / 买入" in out and "目标价：9.50" in out
          and "止损价：8.70" in out and "仓位建议：不超过 10%" in out, repr(out))

    out = call("get_research_report", code="518880", role="bull")
    check("单角色报告能取到", "研究报告 · 518880.SH · bull" in out, repr(out[:120]))

    out = call("get_watchlist")
    check("自选股带现价与涨跌幅", "黄金ETF华安（518880.SH）" in out, repr(out))
    # 8.85 / 9.03 → -1.99%。曾经过了会再乘 100 的 _pct，此处会显示 -199.00%。
    check("★ 涨跌幅不重复乘 100（应为 -1.99%，不是 -199.00%）",
          "-1.99%" in out and "199.00%" not in out, repr(out))

    out = call("search_stock", q="黄金ETF")
    check("搜索返回标准代码", "518880.SH" in out, repr(out))

    out = call("get_notification_configs")
    check("通知配置只显示 webhook 尾号，不吐完整地址",
          "已启用" in out and "hook/aaaaaa" not in out and "…aaaaaa" in out, repr(out))

    # ------------------------------------------------------------ 6. 工具循环
    section("6. 工具循环：六个陷阱")
    wipe()
    seed_user()
    seed_trust()
    seed_position(1, "518880.SH", "黄金ETF华安", 9500, 9.0)
    seed_trade(2)

    # 场景 1：多轮 —— 第 1 轮调工具，第 2 轮出答案
    out, fake = run_loop([
        lambda h: AIMessage(content="", tool_calls=[tool_call("get_trades", {"limit": 5}, "c1")]),
        lambda h: AIMessage(content="最近成交两笔。", tool_calls=[]),
    ])
    check("多轮：最终返回 kind=account + 模型答案",
          out == {"kind": "account", "answer": "最近成交两笔。"}, str(out))
    check("多轮：第 2 轮历史里有对应 tool_call_id 的 ToolMessage",
          any(m.tool_call_id == "c1" for m in _tool_messages(fake.seen[1])),
          str([m.tool_call_id for m in _tool_messages(fake.seen[1])]))
    check("多轮：工具结果真的进了历史（不是空串）",
          "成交记录" in _tool_messages(fake.seen[1])[0].content, repr(_tool_messages(fake.seen[1])[0].content[:80]))
    check("多轮：挂载的工具就是白名单全量",
          set(fake.bound_tool_names) == EXPECTED_TOOLS, str(len(fake.bound_tool_names)))
    check("★ bind_tools 未传任何关键字参数", fake.bind_kwargs == {}, str(fake.bind_kwargs))

    # 场景 2：一条 AIMessage 带并发 tool_calls —— 每个 id 都要有 ToolMessage
    out, fake = run_loop([
        lambda h: AIMessage(content="", tool_calls=[
            tool_call("get_trades", {"limit": 3}, "a"),
            tool_call("get_orders", {"limit": 3}, "b"),
        ]),
        lambda h: AIMessage(content="都查到了。", tool_calls=[]),
    ])
    ids = sorted(m.tool_call_id for m in _tool_messages(fake.seen[1]))
    check("★ 并发 tool_calls：两个 id 各回一条 ToolMessage", ids == ["a", "b"], str(ids))
    check("并发：两条结果内容不同（各查各的）",
          len({m.content for m in _tool_messages(fake.seen[1])}) == 2)

    # 场景 3：reasoning_content 往返（thinking 模型最容易踩死的坑）
    ai1 = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "先看托管簿持仓，再决定要不要查行情"},
        tool_calls=[tool_call("get_positions", {"book": "trust"}, "r1")],
    )
    def _round2(h):
        return AIMessage(content="托管簿一只。", tool_calls=[])

    out, fake = run_loop([lambda h: ai1, _round2])
    # 用 fake.seen[1]（invoke 时刻的快照）而不是回调收到的列表对象——
    # 后者会被循环继续 append，等断言时已经不是当轮的样子了。
    same = [m for m in fake.seen[1] if isinstance(m, AIMessage)]
    check("★ reasoning_content 往返：第 2 轮历史里仍是同一个 AIMessage 对象",
          len(same) == 1 and same[0] is ai1, f"{len(same)} 条 AI 消息，同一对象={bool(same and same[0] is ai1)}")
    check("★ additional_kwargs 未丢（重建消息就会丢，下一轮 400）",
          same and same[0].additional_kwargs.get("reasoning_content") == "先看托管簿持仓，再决定要不要查行情",
          str(same[0].additional_kwargs) if same else "无")

    # 场景 4：工具出错不崩整轮
    out, fake = run_loop([
        lambda h: AIMessage(content="", tool_calls=[
            tool_call("delete_everything", {}, "x"),          # 模型幻觉出的工具名
            tool_call("get_trades", {"limit": "很多"}, "y"),   # 入参类型错
        ]),
        lambda h: AIMessage(content="换个说法回答。", tool_calls=[]),
    ])
    msgs = {m.tool_call_id: m.content for m in _tool_messages(fake.seen[1])}
    check("★ 幻觉工具名：返回可用工具清单，不崩",
          msgs.get("x", "").startswith("【工具出错】没有名为") and "get_trades" in msgs.get("x", ""),
          repr(msgs.get("x", "")))
    check("★ 入参类型错：转成文本回给模型，不崩",
          msgs.get("y", "").startswith("【工具出错】") or msgs.get("y", "").startswith("【取数失败】"),
          repr(msgs.get("y", "")))
    check("出错后循环照常收尾", out.get("kind") == "account" and out.get("answer") == "换个说法回答。", str(out))

    # 场景 5：轮次耗尽 —— 剥掉工具再问一轮，保证有回答
    always = lambda h: AIMessage(  # noqa: E731
        content="", tool_calls=[tool_call("get_trades", {"limit": 1}, "loop")])
    out, fake = run_loop([always] * chat.MAX_TOOL_ROUNDS)
    check(f"轮次耗尽：恰好在 MAX_TOOL_ROUNDS={chat.MAX_TOOL_ROUNDS} 轮后停",
          fake.rounds == chat.MAX_TOOL_ROUNDS + 1, str(fake.rounds))
    check("轮次耗尽：仍返回 kind=account 且有回答",
          out.get("kind") == "account" and bool(out.get("answer")), str(out))
    check("轮次耗尽：最后一轮带上了「别再请求工具」的收尾提示",
          any(isinstance(m, HumanMessage) and chat._FINAL_NUDGE in m.content  # noqa: SLF001
              for m in fake.seen[-1]), str([type(m).__name__ for m in fake.seen[-1]]))

    # 场景 5b：收尾那一轮也失败 → 退回固定文案，不往上抛
    def _boom(h): raise RuntimeError("网络断了")

    out, fake = run_loop([always] * chat.MAX_TOOL_ROUNDS + [_boom])
    check("收尾失败：退回固定兜底文案而非抛异常",
          out.get("kind") == "account" and out.get("answer") == chat._BUDGET_EXHAUSTED, str(out))  # noqa: SLF001

    # 场景 6：确认哨兵 —— 只产出确认请求，绝不启动分析
    submitted: list = []
    real_submit = getattr(analysis_service, "submit_analysis", None)
    if real_submit is not None:
        analysis_service.submit_analysis = lambda *a, **k: submitted.append(a)  # type: ignore[assignment]
    real_resolve = intent_mod.resolve_to_plan
    intent_mod.resolve_to_plan = lambda intent: {  # type: ignore[assignment]
        "type": "plan", "ticker": "518880.SH", "name": "黄金ETF华安",
        "market": "CN", "date": "2026-09-14", "summary": "x",
    }
    try:
        out, fake = run_loop([
            lambda h: AIMessage(content="", tool_calls=[
                tool_call("request_deep_analysis", {"code": "518880", "name": "黄金ETF华安"}, "d1")]),
        ])
    finally:
        intent_mod.resolve_to_plan = real_resolve  # type: ignore[assignment]
        if real_submit is not None:
            analysis_service.submit_analysis = real_submit  # type: ignore[assignment]

    check("★ 哨兵：返回 kind=analysis_confirm",
          out.get("kind") == "analysis_confirm", str(out))
    check("哨兵：带 ticker / name / date / question",
          out.get("ticker") == "518880.SH" and out.get("name") == "黄金ETF华安"
          and out.get("date") == "2026-09-14" and "是否对" in (out.get("question") or ""), str(out))
    check("★ 哨兵中断整轮：没有多跑一轮 LLM", fake.rounds == 1, str(fake.rounds))
    check("★ 哨兵不落进历史（ToolMessage 一条都没 append）",
          not _tool_messages(fake.seen[0]) and
          all(not isinstance(m, ToolMessage) for m in (fake.seen[0])), "有 ToolMessage")
    check("★ analysis_service.submit_analysis 未被调用", submitted == [], str(submitted))

    # 哨兵识别必须是「前缀 + 可解析 + 有 ticker」三者同时成立
    check("_parse_confirm 拒绝无前缀文本", chat._parse_confirm("普通回答") is None)  # noqa: SLF001
    check("_parse_confirm 拒绝坏 JSON",
          chat._parse_confirm(chat_tools.CONFIRM_PREFIX + "{不是 json") is None)  # noqa: SLF001
    check("_parse_confirm 拒绝缺 ticker 的载荷",
          chat._parse_confirm(chat_tools.CONFIRM_PREFIX + '{"name":"x"}') is None)  # noqa: SLF001
    check("_parse_confirm 认出完整载荷",
          (chat._parse_confirm(chat_tools.CONFIRM_PREFIX + '{"ticker":"518880.SH"}') or {}).get("ticker")
          == "518880.SH")

    # 历史裁剪：前端每次都把整份会话带上，不裁会无限增长
    long_msgs = [{"role": "user", "content": f"q{i}"} for i in range(40)]
    trimmed = chat._to_lc_messages(long_msgs)  # noqa: SLF001
    check(f"历史裁到最近 {chat.MAX_HISTORY_TURNS} 条",
          len(trimmed) == chat.MAX_HISTORY_TURNS, str(len(trimmed)))
    check("空历史兜底成一句默认问题", len(chat._to_lc_messages([])) == 1)  # noqa: SLF001
    check("工具消息不会从前端历史里混进来（前端只存纯文本）",
          all(isinstance(m, (HumanMessage, AIMessage)) for m in trimmed))

    # ------------------------------------------------------------ 汇总
    wipe()
    print(f"\n{'=' * 60}")
    if _FAILS:
        print(f"❌ {len(_FAILS)}/{_COUNT} 项未通过：")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
