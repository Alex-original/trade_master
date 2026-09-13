"""回测「每日监控条件」：**执行口径** + 只读缓存 + 缺计划不造假。全部离线。

不连 Postgres、不连 Wind、不跑 LLM。内存 SQLite + 临时候存目录 + Wind 替身，
直接调 ``backtest.get_day_monitor_conditions``，断言它给前端的每一行。

这个功能存在的理由只有一句：**卡片必须能解释当天的成交**。所以这里守四件事：

  A. **判据与撮合同一个口径**——卖/减看当日最低、买/建看当日最高、其余看收盘，
     而不是"回测跑到此刻的价"或"收盘价"。用收盘价判会出现"卡片说没触发、账上却成交了"
     这种自相矛盾；这一条是本文件里最重要的断言（§1）。
  B. **量能不能悄悄退化**——``Bar.volume_ratio`` 为 None（区间太短算不出来）时，带
     ``volume_ratio_min`` 的档必须**不触发**，宁可不动也不能在最需要量能确认时退化成
     纯价格触发（§1 最后一档）。
  C. **没有计划就如实说没有**——返回 ``has_plan=false`` + 原因词（``first_day``/``missing``），
     且**一次网都不打**。返回一张"价格全空、一档不触发"的假卡片，看起来像"团队看了但没动"，
     而事实是这天团队根本没有可执行的东西（§3）。
  D. **时间闸门用的是模拟时间**，不是真实墙钟——否则盘前跑一次回测，整片档位会被判成
     "未触发"（§5）。

用法（在 product/trade_master 下）：
    .venv/bin/python scripts/smoke_backtest_monitor.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import db  # noqa: E402

# ---------------------------------------------------------------- 内存库
# StaticPool + 单连接：多个 session 必须看到同一个内存库（默认每个连接一个独立内存库）
_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
db.Base.metadata.create_all(_engine)
db.SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)  # type: ignore[assignment]

from app import backtest as bt  # noqa: E402  （必须在 patch 之后）
from app import backtest_data as bd  # noqa: E402
from app import trust as trust_mod  # noqa: E402
from app.errors import ServiceError  # noqa: E402
from tradingagents.dataflows import wind as _wind  # noqa: E402


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
        print(f"  ❌ {name}{('  →  ' + detail) if detail else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- Wind 替身

_COLS = [{"name": n} for n in ("TIME", "OPEN", "MATCH", "HIGH", "LOW", "VOLUME")]

#: 只给两个交易日。这两行**同时**充当交易日历（指数 K 线）和个股 K 线——替身不区分。
#: 07-10 这一行是刻意造的：收盘 10.1 平静，但当日最低 9.2、最高 11.8 都穿过了档位。
#: 于是"用收盘价判"和"用执行口径判"会给出**相反**的结论，这一节才有判别力。
#:
#: 日期必须落在同一周内的连续交易日，否则 ``_PAD_DAYS`` 那 12 天余量里没有别的行，
#: 量比（前 5 日均量）永远算不出来 —— 这正是 §1 最后一档要用的 None。
D1, D2 = "2026-07-09", "2026-07-10"
_ROWS = [
    [D1, 10.00, 10.50, 10.60, 9.90, 1_000_000],
    [D2, 10.50, 10.10, 11.80, 9.20, 1_100_000],
]

CALLS: list[tuple] = []


def _recording_tool(server_type, tool_name, params, timeout=120):
    CALLS.append((server_type, tool_name, dict(params)))
    return {"data": {"columns": _COLS, "rows": _ROWS}}


def _forbidden_tool(server_type, tool_name, params, timeout=120):
    """一被调用就炸。用来证明"无计划日一次网都不打"，而不是只返回了空壳。"""
    CALLS.append(("!!触网!!", server_type, tool_name, dict(params)))
    raise AssertionError(f"这条路径不应触网：{server_type}/{tool_name} {params}")


def _install(fn) -> None:
    _wind._call_tool = fn  # type: ignore[assignment]


def _reset() -> None:
    CALLS.clear()


# ---------------------------------------------------------------- 造数
NOW = time.time()
OWNER_PHONE = "13900000011"
OTHER_PHONE = "13900000012"


def fresh_db() -> int:
    """清空所有表，建一个真实用户（含托管配置），返回 owner id。"""
    session = db.get_session()
    try:
        for table in reversed(db.Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
        owner = db.User(phone=OWNER_PHONE, created_at=NOW, is_backtest=False)
        session.add(owner)
        session.flush()
        session.add(db.TrustConfig(
            user_id=owner.id, is_active=True, book_created=True, available_cash=0.0,
            stock_scope=1, updated_at=NOW,
        ))
        session.commit()
        return int(owner.id)
    finally:
        session.close()


def add_user(phone: str) -> int:
    session = db.get_session()
    try:
        u = db.User(phone=phone, created_at=NOW, is_backtest=False)
        session.add(u)
        session.flush()
        uid = int(u.id)
        session.add(db.TrustConfig(
            user_id=uid, is_active=True, book_created=True, available_cash=0.0,
            stock_scope=1, updated_at=NOW,
        ))
        session.commit()
        return uid
    finally:
        session.close()


def make_run(owner: int) -> int:
    return bt.create_run(
        owner, start_date=D1, end_date=D2, init_mode="cash", init_cash=500000.0,
        universe=["600519.SH", "000001.SZ", "159842.SZ", "300750.SZ", "601318.SH"],
    )


def shadow_id(owner: int) -> int:
    session = db.get_session()
    try:
        return int(bt.ensure_shadow_user(session, owner))
    finally:
        session.close()


def put_plan(user_id: int, trade_date: str, actions: list[dict]) -> None:
    blob = {
        "summary": "思路一句话", "cash_target": 0.2, "risk_notes": "风险提示",
        "actions": actions, "process": {"research": {}, "research_missing": []},
    }
    session = db.get_session()
    try:
        session.add(db.TrustPlan(
            user_id=user_id, trade_date=trade_date,
            plan_json=json.dumps(blob, ensure_ascii=False), created_at=NOW,
        ))
        session.commit()
    finally:
        session.close()


def put_step(rid: int, trade_date: str) -> None:
    session = db.get_session()
    try:
        session.add(db.BacktestStep(
            run_id=rid, trade_date=trade_date, cash=500000.0, market_value=0.0,
            total_assets=500000.0, day_pnl=0.0, realized_pnl=0.0, fees=0.0,
            positions_json="[]", trades_json="[]", plan_json="{}",
            status="ok", error="", created_at=NOW,
        ))
        session.commit()
    finally:
        session.close()


def act(code: str, name: str, action: str, trig: str, price: float | None,
        vr_min: float | None = None) -> dict:
    return {"code": code, "name": name, "action": action, "target_weight": 0.05,
            "trigger_type": trig, "trigger_price": price, "volume_ratio_min": vr_min,
            "reason": "理由"}


def row_of(payload: dict, code: str) -> dict:
    for a in payload["actions"]:
        if a["code"] == code:
            return a
    return {}


#: 07-10 的计划：五只票，五个各自独立的判据。逐条对着上面的 ``_ROWS[1]`` 看：
#:   最低 9.2 / 最高 11.8 / 收盘 10.1
ACTIONS = [
    # 卖档 9.5：最低 9.2 穿过了 → 触发。**收盘 10.1 没穿过** —— 用收盘价判会得出相反的结论。
    act("600519.SH", "贵州茅台", "sell", "price_below", 9.5),
    # 买档 11.5：最高 11.8 穿过了 → 触发。收盘 10.1 同样看不出任何动静。
    act("000001.SZ", "平安银行", "buy", "price_above", 11.5),
    # 买档 12.0：最高 11.8 差一点 → 不触发（证明上面那条不是因为"买档一律触发"）
    act("159842.SZ", "券商ETF", "buy", "price_above", 12.0),
    # 持有档：不依赖价格，price 取收盘
    act("300750.SZ", "宁德时代", "hold", "none", None),
    # 量能档：价格穿过了（最低 9.2 ≤ 9.5），但区间太短算不出量比 → **必须不触发**
    act("601318.SH", "中国平安", "sell", "price_below", 9.5, vr_min=2.0),
]


def main() -> int:  # noqa: C901 —— 冒烟脚本，线性罗列各场景
    _install(_recording_tool)

    # **整个脚本跑在临时缓存目录里**：替身返回的是假 K 线，写进真实缓存目录会让下一次
    # 真实回测读到假行情。``_CACHE_DIR`` 是模块级变量，直接换掉即可。
    tmp_cache = Path(tempfile.mkdtemp(prefix="bt_monitor_smoke_"))
    saved_cache_dir = bd._CACHE_DIR
    bd._CACHE_DIR = tmp_cache
    # 监控卡片有自己的进程内 TTL 缓存，键是 ``(run_id, trade_date)``。每个场景都从
    # 空库重来、run_id 会重头编号 —— 不清的话第二个场景会直接命中第一个场景的缓存。
    saved_monitor_cache = dict(bt._MONITOR_CACHE)

    try:
        owner = fresh_db()
        bt._MONITOR_CACHE.clear()
        sh = shadow_id(owner)
        rid = make_run(owner)
        put_step(rid, D1)
        put_step(rid, D2)
        put_plan(sh, D2, ACTIONS)

        # ------------------------------------------------------------ 1
        section("1. 执行口径：卡片与撮合同一个判据（这一节就是它存在的理由）")
        _reset()
        p = bt.get_day_monitor_conditions(rid, owner, D2)
        check("★ 有计划的日期：has_plan=true、口径标为 execution",
              p["has_plan"] is True and p["price_basis"] == "execution",
              json.dumps({k: v for k, v in p.items() if k != "actions"}, ensure_ascii=False))
        check("回传 run_id / trade_date，前端能确认拿到的是哪一天",
              p["run_id"] == rid and p["trade_date"] == D2, str(p.get("trade_date")))

        r_sell = row_of(p, "600519.SH")
        check("★ 卖档用**当日最低**判：9.2 ≤ 9.5 → 触发",
              r_sell.get("kind") == "exit" and r_sell.get("price") == 9.2
              and r_sell.get("triggered") is True,
              json.dumps(r_sell, ensure_ascii=False))
        check("★ 同一行的收盘价是 10.1（> 9.5）——用收盘价判会得出相反的结论，这就是口径的意义",
              r_sell.get("close") == 10.1, str(r_sell.get("close")))

        r_buy = row_of(p, "000001.SZ")
        check("★ 买档用**当日最高**判：11.8 ≥ 11.5 → 触发",
              r_buy.get("kind") == "entry" and r_buy.get("price") == 11.8
              and r_buy.get("triggered") is True,
              json.dumps(r_buy, ensure_ascii=False))

        r_miss = row_of(p, "159842.SZ")
        check("买档差一点（最高 11.8 < 12.0）→ 不触发（并非买档一律触发）",
              r_miss.get("price") == 11.8 and r_miss.get("triggered") is False,
              json.dumps(r_miss, ensure_ascii=False))

        r_hold = row_of(p, "300750.SZ")
        check("持有档不依赖价格，price 取**收盘**",
              r_hold.get("kind") == "hold" and r_hold.get("price") == 10.1
              and r_hold.get("trigger_type") == "none",
              json.dumps(r_hold, ensure_ascii=False))

        r_vr = row_of(p, "601318.SH")
        check("★ 量比算不出（None）时带 volume_ratio_min 的档**不触发**——宁可不动也不退化",
              r_vr.get("triggered") is False and r_vr.get("volume_ratio") is None
              and r_vr.get("price") == 9.2,
              json.dumps(r_vr, ensure_ascii=False))

        check("每行都带回开/高/低/收，前端才能把判据写在脸上",
              all(row.get(k) is not None for row in p["actions"]
                  for k in ("open", "high", "low", "close")),
              json.dumps([{k: a.get(k) for k in ("code", "open", "high", "low", "close")}
                          for a in p["actions"]], ensure_ascii=False))
        check("票数与计划一致（5 只）", p["code_count"] == 5, str(p["code_count"]))

        # ------------------------------------------------------------ 2
        section("2. 进程内 TTL 缓存：切日期来回点时不再重复解析缓存文件")
        _reset()
        p2 = bt.get_day_monitor_conditions(rid, owner, D2)
        check("★ 第二次调用 0 次取数（行情走 TTL 缓存）", CALLS == [], str(CALLS))
        check("第二次调用给的是同一份内容", p2 == p)

        # ------------------------------------------------------------ 3
        section("3. 缺计划的日子：如实说没有，且一次网都不打")
        _install(_forbidden_tool)
        bt._MONITOR_CACHE.clear()
        _reset()
        d1 = bt.get_day_monitor_conditions(rid, owner, D1)
        check("★ 首日没有前置研究日 → has_plan=false + reason=first_day（属设计，不是故障）",
              d1["has_plan"] is False and d1["plan_missing_reason"] == "first_day",
              json.dumps(d1, ensure_ascii=False))
        check("★ 返回的是空卡片，不是「价格全空、一档不触发」的假卡片",
              d1["actions"] == [] and d1["code_count"] == 0, json.dumps(d1, ensure_ascii=False))
        check("★ 这条路径一次网都没打（替身是「一调用就炸」的那个）", CALLS == [], str(CALLS))

        # 非首日、但确实没有计划行：reason 必须能区分出来 —— 前者是设计，后者是"环断了"
        owner_b = add_user(OTHER_PHONE)
        bt._MONITOR_CACHE.clear()
        sh_b = shadow_id(owner_b)
        rid_b = make_run(owner_b)
        put_step(rid_b, D1)
        put_step(rid_b, D2)
        put_plan(sh_b, D1, ACTIONS)  # 只在 D1 有计划 ⇒ D2 就是"跑了但没计划"

        _reset()
        mid = bt.get_day_monitor_conditions(rid_b, owner_b, D2)
        check("★ 非首日缺计划 → reason=missing（与 first_day 分开，否则分不清「设计」和「环断了」）",
              mid["has_plan"] is False and mid["plan_missing_reason"] == "missing",
              json.dumps(mid, ensure_ascii=False))
        check("首日那天有计划时照常出卡片",
              bt.get_day_monitor_conditions(rid_b, owner_b, D1)["has_plan"] is True)
        check("这一段同样 0 次网络调用", CALLS == [], str(CALLS))

        # ------------------------------------------------------------ 4
        section("4. 越权与越界：都不许返回数据")
        try:
            bt.get_day_monitor_conditions(rid, owner_b, D2)
            check("别人的 run 一律 ServiceError", False, "没有抛出")
        except ServiceError as e:
            check("别人的 run 一律 ServiceError", True, str(e))
        try:
            bt.get_day_monitor_conditions(rid, owner, "2026-08-03")
            check("区间外的日期一律 ServiceError", False, "没有抛出")
        except ServiceError as e:
            check("区间外的日期一律 ServiceError", True, str(e))

        # ------------------------------------------------------------ 5
        section("5. 时间闸门走**注入的模拟时间**，不是真实墙钟")
        a = {"trigger_type": "price_below", "trigger_price": 9.5}
        check("★ 09:00（未进连续竞价）→ 不触发",
              trust_mod._trigger_satisfied(a, 9.2, D2, None, datetime(2026, 7, 10, 9, 0)) is False)
        check("★ 10:00（已进连续竞价）→ 触发（模拟日盘中就是按这个时刻判的）",
              trust_mod._trigger_satisfied(a, 9.2, D2, None, datetime(2026, 7, 10, 10, 0)) is True)

    finally:
        bd._CACHE_DIR = saved_cache_dir
        bt._MONITOR_CACHE.clear()
        bt._MONITOR_CACHE.update(saved_monitor_cache)

    print("\n" + "=" * 60)
    if _FAILS:
        print(f"❌ {len(_FAILS)}/{_COUNT} 项未通过：")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
