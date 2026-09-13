"""回测的计算层与续跑：纯算术 + 真库上的检查点，全部离线。

不连 Postgres、不连 Wind、不跑 LLM。前一半是**纯函数**（手算可验），后一半在**内存 SQLite**
上用真实的 ``app.db`` 打真实的库——断点续跑这种东西只有在真库上跑过才算验过。

覆盖：
  1. 已实现盈亏：加权平均成本、含费用、卖到零股清账（与 place_order 的成本公式逐字对齐）
  2. 最大回撤
  3. 检查点 JSON 往返；坏值一律当"没有检查点"而不抛
  4. **``_restore_checkpoint`` 幂等**，且只删 id > last_order_id 的半截残迹、只碰影子账户
  5. **``init_basis`` 用起始日市价，不用 cost_price**（§6，最容易写错的一条）
  6. 等权买入持有基准：与策略同一套手数取整与费用模型；含费用后终值低于无费用版
  7. 指数基准归一化
  8. ``aggregate_steps``：总收益率/最大回撤/累计费用 == 手算值
  9. 起跑前的快速否决（判据是**冻结标的池**，不是发起人托管设置里的 stock_scope）
 10. 编排端到端：跑一遍 vs 崩在中途再续跑，账务**逐项相同**
 11. API 层：越权拦截、删除不留痕但保留研究缓存、预览的估算与告警（含探数 probe=1）
 12. 起跑前的数据完整性门禁：标的池缺得太多就拒绝起跑、copy 模式下持仓缺起始日行情也拒绝
 13. 回测股票范围：``resolve_range`` 的持仓股/自选股/多组并集、已删除的分组**不静默退回全量**、
     copy 自动并入持仓、以及预览与起跑同源（不传 ``bt_scope`` 时零回归）

用法（在 product/trade_master 下）：
    .venv/bin/python scripts/smoke_backtest_calc.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace

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
_MAIN_SESSIONMAKER = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
db.SessionLocal = _MAIN_SESSIONMAKER  # type: ignore[assignment]

from app import backtest as bt  # noqa: E402  （必须在 patch 之后）
from app import backtest_calc as calc  # noqa: E402
from app import trade as trade_mod  # noqa: E402
from app import trust  # noqa: E402
from app.errors import ServiceError  # noqa: E402
from app.market_clock import Bar  # noqa: E402
from tradingagents.dataflows.errors import (  # noqa: E402
    NoMarketDataError,
    VendorRejectedError,
)

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


# ---------------------------------------------------------------- 造数

NOW = time.time()
OWNER_PHONE = "13900000001"
D1, D2 = "2026-03-02", "2026-03-03"
DAYS5 = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]

#: 与真实托管配置一致的费用口径（万 2.5 / 不免五 / 印花税万 5）
FEE_CFG = SimpleNamespace(
    fee_commission_rate=0.00025, fee_waive_min=False, fee_stamp_duty_rate=0.0005
)
#: 真正的"零费用"：光把费率调 0 不够——``_calc_fee`` 的**最低 5 元佣金**还在，
#: 必须同时 fee_waive_min=True 才是零摩擦。这个坑本身就是下面一条断言。
ZERO_FEE_CFG = SimpleNamespace(
    fee_commission_rate=0.0, fee_waive_min=True, fee_stamp_duty_rate=0.0
)


def bar(o, h, low, c, prev=None, vr=None, tradable=True) -> Bar:
    return Bar(o, h, low, c, prev_close=prev, volume_ratio=vr, tradable=tradable)


def fresh_db() -> None:
    """清空所有表，建一个真实用户（含托管配置）与它的影子账户。"""
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
    finally:
        session.close()


def owner_id() -> int:
    session = db.get_session()
    try:
        return int(session.query(db.User.id).filter(db.User.phone == OWNER_PHONE).scalar())
    finally:
        session.close()


def shadow_id() -> int:
    session = db.get_session()
    try:
        return int(bt.ensure_shadow_user(session, owner_id()))
    finally:
        session.close()


def make_run(**kw) -> int:
    """建一个 run（真实 create_run），再按需改写几个字段。"""
    rid = bt.create_run(
        owner_id(),
        start_date=kw.pop("start_date", D1),
        end_date=kw.pop("end_date", D2),
        init_mode=kw.pop("init_mode", "cash"),
        init_cash=kw.pop("init_cash", 100000.0),
        universe=kw.pop("universe", ["600519.SH"]),
    )
    if kw:
        session = db.get_session()
        try:
            row = session.query(db.BacktestRun).filter(db.BacktestRun.id == rid).first()
            for k, v in kw.items():
                setattr(row, k, v)
            session.commit()
        finally:
            session.close()
    return rid


def _run_dict(rid: int) -> dict:
    return bt.get_run(rid)


def _cash(sh: int) -> float:
    session = db.get_session()
    try:
        return float(
            session.query(db.TrustConfig.available_cash)
            .filter(db.TrustConfig.user_id == sh)
            .scalar()
        )
    finally:
        session.close()


def _pos(sh: int, code: str) -> dict | None:
    session = db.get_session()
    try:
        r = (
            session.query(db.Position)
            .filter(db.Position.user_id == sh, db.Position.book == 1,
                    db.Position.stock_code == code)
            .first()
        )
        if r is None:
            return None
        return {"hold_qty": r.hold_qty, "available_qty": r.available_qty,
                "frozen_qty": r.frozen_qty, "cost_price": r.cost_price}
    finally:
        session.close()


def _count(table) -> int:
    session = db.get_session()
    try:
        return int(session.query(table).count())
    finally:
        session.close()


def _plans(sh: int) -> list[str]:
    """该影子名下落过库的计划生效日（升序）。"""
    session = db.get_session()
    try:
        return [
            p.trade_date
            for p in session.query(db.TrustPlan)
            .filter(db.TrustPlan.user_id == sh)
            .order_by(db.TrustPlan.trade_date)
            .all()
        ]
    finally:
        session.close()


# ------------------------------------------------------------- 1. 已实现盈亏


def realized_section() -> None:
    section("1. 已实现盈亏（加权平均成本，含费用）")

    # §8.3 点名的那条：买 100@10 → 卖 100@12、费 5 → 195
    r = calc.replay_realized([], [
        {"stock_code": "600519.SH", "direction": 0, "price": 10.0, "quantity": 100, "fee": 5.0},
        {"stock_code": "600519.SH", "direction": 1, "price": 12.0, "quantity": 100, "fee": 5.0},
    ])
    check("买 100@10 → 卖 100@12 费 5 ⇒ 已实现 195（只用卖出那笔的费用）", r == 195.0, str(r))

    # 两次不同价买入 → 加权平均成本 (10*100 + 14*300)/400 = 13.0；卖 200@15、费 8
    # ⇒ (15-13)*200 - 8 = 392
    r = calc.replay_realized([], [
        {"stock_code": "A.SH", "direction": 0, "price": 10.0, "quantity": 100, "fee": 5.0},
        {"stock_code": "A.SH", "direction": 0, "price": 14.0, "quantity": 300, "fee": 5.0},
        {"stock_code": "A.SH", "direction": 1, "price": 15.0, "quantity": 200, "fee": 8.0},
    ])
    check("两次买入的加权平均成本 (10×100+14×300)/400 = 13 ⇒ 已实现 392", r == 392.0, str(r))

    # 起始持仓（copy 模式）要成为成本基准：200@1000 的成本卖 100@1100、费 10
    # ⇒ (1100-1000)*100 - 10 = 9990
    r = calc.replay_realized(
        [{"stock_code": "B.SH", "hold_qty": 200, "cost_price": 1000.0}],
        [{"stock_code": "B.SH", "direction": 1, "price": 1100.0, "quantity": 100, "fee": 10.0}],
    )
    check("起始持仓的成本价是卖出成本的基准 ⇒ 9990", r == 9990.0, str(r))

    # 卖到零股 → 成本清零（对应 place_order 在 hold_qty<=0 时删除该行）。
    # 若不清理：第二次买 100@20 会与残留的 0 股老成本加权，得到错的成本。
    r = calc.replay_realized([], [
        {"stock_code": "C.SH", "direction": 0, "price": 10.0, "quantity": 100, "fee": 0.0},
        {"stock_code": "C.SH", "direction": 1, "price": 12.0, "quantity": 100, "fee": 0.0},
        {"stock_code": "C.SH", "direction": 0, "price": 20.0, "quantity": 100, "fee": 0.0},
        {"stock_code": "C.SH", "direction": 1, "price": 25.0, "quantity": 100, "fee": 0.0},
    ])
    check("卖到零股后成本清零 ⇒ 再买再卖是 200+500=700", r == 700.0, str(r))

    check("空成交 ⇒ 0", calc.replay_realized([], []) == 0.0)


# ------------------------------------------------------------- 2. 最大回撤


def drawdown_section() -> None:
    section("2. 最大回撤")

    check("峰谷 (120→90)/120 = 0.25", calc.max_drawdown([100, 120, 90, 110]) == 0.25)
    check("单调上升 ⇒ 0", calc.max_drawdown([100, 110, 120]) == 0.0)
    check("空序列 ⇒ 0", calc.max_drawdown([]) == 0.0)
    check("单点 ⇒ 0", calc.max_drawdown([100]) == 0.0)
    # 回撤要取**历史峰值**之后的谷：110 → 105 只有 4.5%，远小于 120 → 90 的 25%
    check("取历史峰值而非前一日 ⇒ 0.25", calc.max_drawdown([100, 120, 90, 110, 105]) == 0.25)


# ------------------------------------------------------------- 3. 检查点 JSON


def checkpoint_json_section() -> None:
    section("3. 检查点 JSON 往返")

    blob = calc.serialize_checkpoint(12345.678, [
        {"stock_code": "600519.SH", "stock_name": "贵州茅台", "hold_qty": 200,
         "available_qty": 100, "frozen_qty": 100, "cost_price": 1500.0},
        {"stock_code": "000001.SZ", "stock_name": "平安银行", "hold_qty": 0,
         "available_qty": 0, "frozen_qty": 0, "cost_price": 10.0},
    ])
    data = calc.parse_checkpoint(blob)
    check("现金按分取整", data["cash"] == 12345.68, str(data["cash"]))
    check("零股持仓不写进快照（它没有意义，只会让快照越滚越长）",
          len(data["positions"]) == 1, str(data["positions"]))
    p = data["positions"][0]
    check("五个账务字段一个不少（cost_price 决定定仓与已实现盈亏）",
          set(p) == {"stock_code", "stock_name", "hold_qty", "available_qty",
                     "frozen_qty", "cost_price"}, str(sorted(p)))
    check("available / frozen 分开存（T+1 靠它）",
          p["available_qty"] == 100 and p["frozen_qty"] == 100, str(p))
    check("成本价原样往返", p["cost_price"] == 1500.0, str(p))

    # 坏值一律当"没有检查点"：一个损坏的快照该退回重跑，而不是让 run 永远卡在 failed 上
    for bad in ("", None, "{不是 JSON", "[]", '{"cash": 1}', '{"positions": "x"}'):
        check(f"坏值 {bad!r} ⇒ 空 dict（退回全新起跑）",
              calc.parse_checkpoint(bad) == {}, str(calc.parse_checkpoint(bad)))
    check("_checkpoint_usable 对坏快照说 False",
          bt._checkpoint_usable({"checkpoint_json": "{坏了"}) is False)
    check("_checkpoint_usable 对好快照说 True",
          bt._checkpoint_usable({"checkpoint_json": blob}) is True)


# ------------------------------------------------------------- 4. 断点续跑


def restore_section() -> None:
    section("4. _restore_checkpoint：幂等 + 只截半截残迹 + 够不到真实账簿")

    fresh_db()
    owner = owner_id()
    sh = shadow_id()

    # 真实用户在跑的真实账簿痕迹——第 4 节全程**一条都不许少**
    session = db.get_session()
    try:
        session.add(db.Order(
            order_id="real-o1", user_id=owner, stock_code="600519.SH", stock_name="贵州茅台",
            direction=1, price=1600.0, quantity=100, status=1, source=1, created_at=NOW,
        ))
        session.add(db.Trade(
            trade_id="real-t1", order_id="real-o1", user_id=owner, stock_code="600519.SH",
            stock_name="贵州茅台", direction=1, price=1600.0, quantity=100,
            amount=160000.0, fee=96.0, traded_at=NOW,
        ))
        session.add(db.Position(
            user_id=owner, book=1, stock_code="600519.SH", stock_name="贵州茅台",
            hold_qty=200, available_qty=200, frozen_qty=0, cost_price=1500.0, updated_at=NOW,
        ))
        session.commit()
    finally:
        session.close()
    real_max_oid = bt._max_order_id(db.get_session(), owner)
    real_orders = _count(db.Order)
    real_trades = _count(db.Trade)

    rid = make_run()
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == rid).first()
        # 影子账簿：5 万现金 + 200 股
        cfg = bt._ensure_shadow_config(session, sh)
        cfg.available_cash = 50000.0
        session.add(db.Position(
            user_id=sh, book=1, stock_code="600519.SH", stock_name="贵州茅台",
            hold_qty=200, available_qty=200, frozen_qty=0, cost_price=1500.0, updated_at=NOW,
        ))
        session.flush()
        # 检查点：就是上面这个状态
        row.checkpoint_json = calc.serialize_checkpoint(50000.0, [{
            "stock_code": "600519.SH", "stock_name": "贵州茅台", "hold_qty": 200,
            "available_qty": 200, "frozen_qty": 0, "cost_price": 1500.0,
        }])
        row.last_step_date = D1
        row.last_order_id = bt._max_order_id(session, sh)   # = 0，影子账户还没有委托
        session.commit()
    finally:
        session.close()

    # ---- 模拟：D2 跑到一半崩了。已落库的残迹是「现金变了 + 持仓多了 100 股 + 一笔委托」----
    session = db.get_session()
    try:
        session.add(db.Order(
            order_id="half-o1", user_id=sh, stock_code="000001.SZ", stock_name="平安银行",
            direction=0, price=10.0, quantity=1000, status=1, source=1, created_at=NOW,
        ))
        session.commit()
        half_oid = bt._max_order_id(session, sh)
        session.add(db.Trade(
            trade_id="half-t1", order_id="half-o1", user_id=sh, stock_code="000001.SZ",
            stock_name="平安银行", direction=0, price=10.0, quantity=1000,
            amount=10000.0, fee=5.0, traded_at=NOW,
        ))
        cfg = bt._ensure_shadow_config(session, sh)
        cfg.available_cash = 39995.0
        session.add(db.Position(
            user_id=sh, book=1, stock_code="000001.SZ", stock_name="平安银行",
            hold_qty=1000, available_qty=0, frozen_qty=1000,
            cost_price=10.0, updated_at=NOW,
        ))
        # 还有一条 D2 的 step（写完 step 还没落检查点就崩了）
        session.add(db.BacktestStep(
            run_id=rid, trade_date=D2, cash=39995.0, market_value=10000.0,
            total_assets=49995.0, created_at=NOW,
        ))
        session.commit()
    finally:
        session.close()
    check("残迹已就位：现金被改、持仓被加、委托/成交各多一条、还有一条 D2 的 step",
          _cash(sh) == 39995.0 and _pos(sh, "000001.SZ")["hold_qty"] == 1000
          and _count(db.Order) == real_orders + 1 and _count(db.Trade) == real_trades + 1
          and _count(db.BacktestStep) == 1,
          f"cash={_cash(sh)} pos={_pos(sh, '000001.SZ')} "
          f"orders={_count(db.Order)} trades={_count(db.Trade)} steps={_count(db.BacktestStep)}")

    # ---- 恢复 ----
    session = db.get_session()
    try:
        bt._restore_checkpoint(session, _run_dict(rid), sh)
        session.commit()
    finally:
        session.close()
    check("现金回滚到检查点", _cash(sh) == 50000.0, str(_cash(sh)))
    check("半截买入的持仓被删掉", _pos(sh, "000001.SZ") is None, str(_pos(sh, "000001.SZ")))
    check("检查点里的持仓回来了（含 available/frozen/cost）",
          _pos(sh, "600519.SH") == {"hold_qty": 200, "available_qty": 200,
                                    "frozen_qty": 0, "cost_price": 1500.0},
          str(_pos(sh, "600519.SH")))
    check("半截的委托被删（只剩真实用户那一条）", _count(db.Order) == real_orders,
          str(_count(db.Order)))
    check("半截的成交被删（只剩真实用户那一条）", _count(db.Trade) == real_trades,
          str(_count(db.Trade)))
    check("半截的 D2 step 被清（否则它会以错误的净值留在曲线上）",
          _count(db.BacktestStep) == 0)

    # ---- 幂等：再恢复一次，什么都不该变 ----
    before = (_cash(sh), bt._max_order_id(db.get_session(), sh),
              _count(db.Order), _count(db.Trade), _count(db.Position), _count(db.BacktestStep))
    session = db.get_session()
    try:
        bt._restore_checkpoint(session, _run_dict(rid), sh)
        session.commit()
    finally:
        session.close()
    after = (_cash(sh), bt._max_order_id(db.get_session(), sh),
             _count(db.Order), _count(db.Trade), _count(db.Position), _count(db.BacktestStep))
    check("第二次恢复的结果与第一次**逐项相同**（幂等）", before == after,
          f"{before} vs {after}")

    # ---- 真实账簿分毫未动 ----
    check("真实用户的委托没被碰", bt._max_order_id(db.get_session(), owner) == real_max_oid)
    check("真实持仓还在（id 截断带 user_id 过滤，够不到它）",
          _pos(owner, "600519.SH")["hold_qty"] == 200, str(_pos(owner, "600519.SH")))

    # ---- 坏检查点必须喊出来，而不是静默地把账算歪 ----
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == rid).first()
        row.checkpoint_json = "{坏了"
        session.commit()
    finally:
        session.close()
    try:
        session = db.get_session()
        try:
            bt._restore_checkpoint(session, _run_dict(rid), sh)
        finally:
            session.close()
        check("坏检查点抛 ServiceError", False, "没有抛")
    except ServiceError as e:
        check("坏检查点抛 ServiceError（而不是拿半截账簿硬续）", "检查点" in str(e), str(e))


# ------------------------------------------------------------- 5. 起始基准


def basis_section() -> None:
    section("5. init_basis：用起始日**市价**，不用 cost_price")

    fresh_db()
    sh = shadow_id()
    session = db.get_session()
    try:
        cfg = bt._ensure_shadow_config(session, sh)
        cfg.available_cash = 20000.0
        session.add(db.Position(
            user_id=sh, book=1, stock_code="600519.SH", stock_name="贵州茅台",
            hold_qty=100, available_qty=100, frozen_qty=0, cost_price=1000.0, updated_at=NOW,
        ))
        session.commit()
    finally:
        session.close()

    all_bars = {"600519.SH": {D1: bar(1500.0, 1520.0, 1490.0, 1510.0, prev=1495.0)}}
    basis = bt._basis(sh, D1, all_bars)
    check("基准 = 现金 20000 + 100 股 × **收盘 1510** = 171000", basis == 171000.0, str(basis))
    check("**不是**按成本价算的 120000（那会把起始日前的浮盈算成回测收益）",
          basis != 20000.0 + 100 * 1000.0)

    # 起始日完全没有这只票的行情 → 只能用成本价兜底（市值口径在这里不存在）
    basis2 = bt._basis(sh, D1, {})
    check("取不到起始日行情时退回成本价（下策里唯一不制造虚假收益的那个）",
          basis2 == 120000.0, str(basis2))
    check("不传 unpriced 时返回值与传了的一致（既有调用方一行不用改）",
          bt._basis(sh, D1, {}) == basis2)

    # ---- 兜底是"下策"，那就必须有人知道发生过 ----
    # 兜底会让分母失真，而失真在结果页上**完全看不出来**。所以把"兜了哪几只"回传给
    # 调用方，由 ``_drive`` 决定这是可接受的降级还是必须停下来问人（门禁二）。
    up: list[str] = []
    check("★ 退回成本价时把票名回传出去（否则没人知道分母已经失真）",
          bt._basis(sh, D1, {}, unpriced=up) == 120000.0 and up == ["600519.SH"], str(up))
    up2: list[str] = []
    bt._basis(sh, D1, all_bars, unpriced=up2)
    check("全部有行情时不报任何票（不然会凭空拦下一次合法起跑）", up2 == [], str(up2))

    # 0 股的空仓：本来就不该有市值，兜不兜底都是 0 —— 报出来只会凭空拦下一次起跑
    session = db.get_session()
    try:
        session.add(db.Position(
            user_id=sh, book=1, stock_code="000001.SZ", stock_name="平安银行",
            hold_qty=0, available_qty=0, frozen_qty=0, cost_price=9.0, updated_at=NOW,
        ))
        session.commit()
    finally:
        session.close()
    up3: list[str] = []
    check("★ 0 股的空仓不算缺失（报出来只会凭空拦下一次起跑）",
          bt._basis(sh, D1, {}, unpriced=up3) == 120000.0
          and sorted(up3) == ["600519.SH"], str(up3))


# ------------------------------------------------------------- 5b. 复制起始簿的复权口径


_ORIG_RAW_LOADER = bt.bd.load_unadjusted_closes
_RAW_CALLS: list[list[str]] = []


def _patch_raw(mapping: dict, warnings: list[str] | None = None, record: bool = True) -> None:
    """把不复权取数换成固定 fixture（离线，不碰 Wind、不碰磁盘缓存）。"""

    def fake(codes, start_date, end_date, on_progress=None):
        if record:
            _RAW_CALLS.append(list(codes))
        return {c: mapping[c] for c in codes if c in mapping}, list(warnings or [])

    bt.bd.load_unadjusted_closes = fake


def _restore_raw() -> None:
    bt.bd.load_unadjusted_closes = _ORIG_RAW_LOADER


def _book_map(sh: int) -> dict:
    session = db.get_session()
    try:
        return {
            p.stock_code: (int(p.hold_qty), float(p.cost_price or 0.0))
            for p in session.query(db.Position)
            .filter(db.Position.user_id == sh, db.Position.book == 1).all()
        }
    finally:
        session.close()


def rescale_section() -> None:
    section("5b. 复制起始簿：不复权 → 后复权（否则止损判据恒为真）")

    fresh_db()
    sh = shadow_id()
    session = db.get_session()
    try:
        bt._ensure_shadow_config(session, sh).available_cash = 0.0
        # 159300.SZ 是实测里复权因子 ≠1 的那只（沪深300ETF富国，因子 0.279）：
        # 真实盘面 4.971 元，后复权只有 1.386 元。另外五只因子都是 1。
        session.add(db.Position(
            user_id=sh, book=1, stock_code="159300.SZ", stock_name="沪深300ETF富国",
            hold_qty=10400, available_qty=10400, frozen_qty=0,
            cost_price=5.031, updated_at=NOW,
        ))
        session.add(db.Position(
            user_id=sh, book=1, stock_code="518880.SH", stock_name="黄金ETF华安",
            hold_qty=9500, available_qty=9500, frozen_qty=0,
            cost_price=9.195, updated_at=NOW,
        ))
        session.commit()
    finally:
        session.close()

    H, R = 1.386, 4.971          # 起始日：后复权收盘 / 不复权收盘
    f = H / R
    seed = [
        {"stock_code": "159300.SZ", "hold_qty": 10400, "cost_price": 5.031},
        {"stock_code": "518880.SH", "hold_qty": 9500, "cost_price": 9.195},
    ]
    bars_hfq = {
        "159300.SZ": {D1: bar(1.384, 1.389, 1.379, H, prev=1.380)},
        "518880.SH": {D1: bar(9.021, 9.076, 9.009, 9.024, prev=9.164)},
    }
    _patch_raw({
        "159300.SZ": {D1: R, D2: 4.967},      # 全程 0.2788，不漂移
        "518880.SH": {D1: 9.024, D2: 9.041},  # 与后复权同值 ⇒ f == 1
    })
    session = db.get_session()
    try:
        got, notes = bt._rescale_copied_book(session, sh, D1, D2, D1, bars_hfq, seed)
        session.commit()
    finally:
        session.close()
    _restore_raw()

    book = _book_map(sh)
    q_h, c_h = book["159300.SZ"]
    check("★ 复权因子 ≠1 的票：股数按 1/f 换算（10400 → 37301 附近）",
          abs(q_h - 10400 / f) <= 1, f"{q_h} vs {10400 / f:.1f}")
    check("★ 换算后**起始市值不变**（后复权等值股数 × 后复权价 == 真实股数 × 真实价）",
          abs(q_h * H - 10400 * R) <= H, f"{q_h * H:.2f} vs {10400 * R:.2f}")
    check("★ 成本价按 f 换算，使**浮亏比例与真实一致**（不取整 ⇒ 这条是恒等式，不是近似）",
          abs((c_h / H - 1) - (5.031 / R - 1)) < 1e-12 and c_h == 5.031 * f,
          f"{c_h} vs {5.031 * f}")

    # ---- 这条才是修复的**目的**：止损判据在两个口径下必须等价 ----
    stop = 0.08
    raw_low, hfq_low = 4.947, 4.947 * f       # 一只"没破线"的票（09-10 的真实低点）
    check("★ 修复后：不复权口径不触发的止损，在后复权口径下同样不触发",
          not (hfq_low < c_h * (1 - stop)) and not (raw_low < 5.031 * (1 - stop)),
          f"hfq {hfq_low:.4f} vs 线 {c_h * (1 - stop):.4f}")
    check("★ 不换算就会**恒为真**（run 4 就是这么把一只浮亏 2% 的持仓按后复权价清掉的）",
          1.379 < 5.031 * (1 - stop),
          f"1.379 < {5.031 * (1 - stop):.4f}")
    check("真的破线时两边仍然都触发（换算没有把止损改钝）",
          (4.0 * f < c_h * (1 - stop)) and (4.0 < 5.031 * (1 - stop)))

    check("f == 1 的票**逐字节不变**（不需要换算的票一个字节都不许动）",
          book["518880.SH"] == (9500, 9.195), str(book["518880.SH"]))
    check("★ 换算过的票进 notes（结果页要声明「这是后复权等值股数，不是真实股数」）",
          len(notes) == 1 and "159300.SZ" in notes[0] and "后复权等值股数" in notes[0],
          str(notes))
    check("seed 被**原地**更新（调用方落盘的 init_positions_json 必须是换算后的）",
          got[0]["hold_qty"] == q_h and got[0]["cost_price"] == c_h, str(got[0]))
    check("没被换算的票不进 notes（否则结果页被噪音淹掉）",
          all("518880.SH" not in n for n in notes), str(notes))

    # ---- 换算必须发生在 init_basis 之前，否则分母仍是混合口径 ----
    basis = bt._basis(sh, D1, bars_hfq)
    check("★ init_basis 用换算后的持仓 ⇒ 起始净资产回到真实值（不再是混合口径）",
          abs(basis - (10400 * R + 9500 * 9.024)) < 2.0,
          f"{basis:.2f} vs {10400 * R + 9500 * 9.024:.2f}")

    # ---- 门槛：4 位小数的量化噪声不许改写正常票的股数 ----
    fresh_db()
    sh = shadow_id()
    session = db.get_session()
    try:
        session.add(db.Position(
            user_id=sh, book=1, stock_code="562500.SH", stock_name="机器人ETF华夏",
            hold_qty=10400, available_qty=10400, frozen_qty=0,
            cost_price=0.962, updated_at=NOW,
        ))
        session.commit()
    finally:
        session.close()
    seed = [{"stock_code": "562500.SH", "hold_qty": 10400, "cost_price": 0.962}]
    _patch_raw({"562500.SH": {D1: 0.9100}})
    session = db.get_session()
    try:
        _got, notes = bt._rescale_copied_book(
            session, sh, D1, D1, D1, {"562500.SH": {D1: bar(0.908, 0.912, 0.906, 0.9102)}}, seed)
        session.commit()
    finally:
        session.close()
    _restore_raw()
    check(f"|f−1| < {bt._ADJ_EPS} 的票原样不动（f=1.0002 只是 4 位小数的量化噪声）",
          _book_map(sh)["562500.SH"] == (10400, 0.962), str(_book_map(sh)["562500.SH"]))
    check("阈值内不动就是不动，notes 也不出（不静默 ≠ 要刷屏）", notes == [], str(notes))

    # ---- 窗口内 f 变过 = 窗口里吃到除权/份额变动，必须报出来 ----
    fresh_db()
    sh = shadow_id()
    session = db.get_session()
    try:
        session.add(db.Position(
            user_id=sh, book=1, stock_code="159300.SZ", stock_name="沪深300ETF富国",
            hold_qty=10400, available_qty=10400, frozen_qty=0,
            cost_price=5.031, updated_at=NOW,
        ))
        session.commit()
    finally:
        session.close()
    seed = [{"stock_code": "159300.SZ", "hold_qty": 10400, "cost_price": 5.031}]
    _patch_raw({"159300.SZ": {D1: 4.971, D2: 3.900}})   # f 从 0.2788 跳到 0.3544
    session = db.get_session()
    try:
        _got, notes = bt._rescale_copied_book(
            session, sh, D1, D2, D1,
            {"159300.SZ": {D1: bar(1.384, 1.389, 1.379, 1.386),
                           D2: bar(1.382, 1.390, 1.378, 1.382)}}, seed)
        session.commit()
    finally:
        session.close()
    _restore_raw()
    check("★ 窗口内复权因子变过 ⇒ 报出「窗口内含除权/份额变动」（排查时的重要线索）",
          len(notes) == 1 and "复权因子有变动" in notes[0], str(notes))
    check("起始日的换算仍按 f(起始日)，不按末日（后面的除权与起始持仓的估值无关）",
          abs(_book_map(sh)["159300.SZ"][0] - 10400 / (1.386 / 4.971)) <= 1,
          str(_book_map(sh)["159300.SZ"]))

    # ---- 取不到不复权价 = 硬错误，绝不按 f=1 兜底 ----
    fresh_db()
    sh = shadow_id()
    session = db.get_session()
    try:
        session.add(db.Position(
            user_id=sh, book=1, stock_code="159300.SZ", stock_name="沪深300ETF富国",
            hold_qty=10400, available_qty=10400, frozen_qty=0,
            cost_price=5.031, updated_at=NOW,
        ))
        session.commit()
    finally:
        session.close()
    seed = [{"stock_code": "159300.SZ", "hold_qty": 10400, "cost_price": 5.031}]
    _patch_raw({})   # 不复权一条都取不到
    err = ""
    session = db.get_session()
    try:
        bt._rescale_copied_book(session, sh, D1, D1, D1,
                                {"159300.SZ": {D1: bar(1.384, 1.389, 1.379, 1.386)}}, seed)
    except ServiceError as e:
        err = str(e)
    finally:
        session.close()
    _restore_raw()
    check("★ 不复权价取不到 ⇒ 停下问人，**不按 f=1 照常跑**",
          "不复权" in err and "159300.SZ" in err, err[:80])
    check("错误信息给出两条可照做的出路（空仓起步 / 挪起始日期）",
          "空仓起步" in err and "起始日期" in err, err[:120])
    check("停下时影子账簿**没有被改过**（不能留下半截换算）",
          _book_map(sh)["159300.SZ"] == (10400, 5.031), str(_book_map(sh)["159300.SZ"]))

    # ---- 空仓起步：连取数都不该发生 ----
    fresh_db()
    sh = shadow_id()
    _RAW_CALLS.clear()
    _patch_raw({})
    session = db.get_session()
    try:
        got, notes = bt._rescale_copied_book(session, sh, D1, D1, D1, {}, [])
    finally:
        session.close()
    _restore_raw()
    check("★ 空仓起步（seed 为空）⇒ 一次不复权取数都不发（cash 模式零回归、零网络）",
          got == [] and notes == [] and _RAW_CALLS == [], str(_RAW_CALLS))


# ------------------------------------------------------------- 6. 等权基准


def baseline_section() -> None:
    section("6. 等权买入持有基准（与策略同源的手数取整与费用）")

    codes = ["600000.SH", "600001.SH", "600002.SH"]
    bars = {
        "600000.SH": {D1: bar(10.0, 10.5, 9.8, 10.2), DAYS5[4]: bar(10.2, 11.2, 10.1, 11.0)},
        "600001.SH": {D1: bar(20.0, 20.5, 19.5, 20.2), DAYS5[4]: bar(20.2, 21.2, 20.0, 21.0)},
        "600002.SH": {D1: bar(50.0, 50.5, 49.0, 50.2), DAYS5[4]: bar(50.2, 52.2, 50.0, 52.0)},
    }
    r = calc.equal_weight_baseline(codes, bars, DAYS5, 300000.0, FEE_CFG)
    qty = {h["stock_code"]: h["quantity"] for h in r["holdings"]}
    # 手算：每份预算 10 万。10.00 → 10000 股花 100000，佣金 25 超出预算 ⇒ 减一手到 9900；
    # 20.00 → 4900 股；50.00 → 1900 股。期末市值 9900×11 + 4900×21 + 1900×52 = 310600，
    # 加上没花掉的现金 7927 ⇒ 318527。
    check("起始日按**开盘价**成交，且每份预算把佣金也算在内（A股减到整手）",
          qty == {"600000.SH": 9900, "600001.SH": 4900, "600002.SH": 1900}, str(qty))
    check("期末总资产 == 手算的 318527", r["total_assets"] == 318527.0, str(r["total_assets"]))
    check("逐日曲线行数 == 交易日数", len(r["curve"]) == len(DAYS5), str(len(r["curve"])))
    check("曲线日期与交易日一一对应",
          [c["trade_date"] for c in r["curve"]] == DAYS5)

    r0 = calc.equal_weight_baseline(codes, bars, DAYS5, 300000.0, ZERO_FEE_CFG)
    check("零费用时三只各买满整数手（10000/5000/2000）",
          {h["stock_code"]: h["quantity"] for h in r0["holdings"]}
          == {"600000.SH": 10000, "600001.SH": 5000, "600002.SH": 2000},
          str(r0["holdings"]))
    check("零费用终值 319000 == 手算", r0["total_assets"] == 319000.0, str(r0["total_assets"]))
    check("**含费用后的终值低于无费用版**（318527 < 319000）——费用模型确实在起作用",
          r["total_assets"] < r0["total_assets"],
          f"{r['total_assets']} vs {r0['total_assets']}")

    # 费率调到 0 但最低 5 元佣金还在 ⇒ 仍然是收费的。这条守着上面那条断言的意义。
    cfg_no_min = SimpleNamespace(fee_commission_rate=0.0, fee_waive_min=False,
                                 fee_stamp_duty_rate=0.0)
    r1 = calc.equal_weight_baseline(codes, bars, DAYS5, 300000.0, cfg_no_min)
    check("费率归零但「不免五」仍在收佣金（所以零费用必须同时 waive_min）",
          r1["total_assets"] < r0["total_assets"], f"{r1['total_assets']} vs {r0['total_assets']}")

    r2 = calc.equal_weight_baseline(codes, bars, DAYS5, 300000.0, FEE_CFG)
    check("可重复（同输入同输出）", r2["total_assets"] == r["total_assets"])

    # 起始日无行情的票直接不买，它那份钱留在现金——**不重分**给其他票
    r3 = calc.equal_weight_baseline(
        codes + ["600003.SH"], bars, DAYS5, 300000.0, FEE_CFG
    )
    check("起始日无行情的标的被跳过（不买、也不重分它的那份）",
          len(r3["holdings"]) == 3 and r3["total_assets"] == r["total_assets"],
          f"{len(r3['holdings'])} / {r3['total_assets']}")

    r4 = calc.equal_weight_baseline(codes, bars, DAYS5, 0.0, FEE_CFG)
    check("零本金 ⇒ 空结果，不除零", r4["total_assets"] == 0.0 and r4["curve"] == [])


# ------------------------------------------------------------- 7. 指数基准


def index_section() -> None:
    section("7. 指数基准归一化")

    bench = {DAYS5[0]: 4000.0, DAYS5[1]: 4080.0, DAYS5[2]: 3920.0}
    r = calc.index_baseline(bench, DAYS5, 100000.0)
    check("首日归一到 init_basis 本身", r["curve"][0]["total_assets"] == 100000.0,
          str(r["curve"][0]))
    check("+2% ⇒ 102000", r["curve"][1]["total_assets"] == 102000.0, str(r["curve"][1]))
    check("-2% ⇒ 98000", r["curve"][2]["total_assets"] == 98000.0, str(r["curve"][2]))
    check("缺基准价的日子**不画**（宁可不画，也不能拿错的除数画一条看着正常的线）",
          len(r["curve"]) == 3, str(len(r["curve"])))

    r2 = calc.index_baseline({}, DAYS5, 100000.0)
    check("首日无基准价 ⇒ 空曲线（没有归一化除数）",
          r2["curve"] == [] and r2["total_return"] == 0.0)


# ------------------------------------------------------------- 8. 聚合


def aggregate_section() -> None:
    section("8. aggregate_steps：总收益率 / 最大回撤 / 累计费用")

    steps = [
        {"trade_date": D1, "cash": 100000.0, "market_value": 0.0, "total_assets": 100000.0,
         "day_pnl": 0.0, "realized_pnl": 0.0, "fees": 25.0, "trade_count": 1, "status": "ok"},
        {"trade_date": DAYS5[1], "cash": 0.0, "market_value": 120000.0,
         "total_assets": 120000.0, "day_pnl": 20000.0, "realized_pnl": 0.0, "fees": 25.0,
         "trade_count": 1, "status": "ok"},
        {"trade_date": DAYS5[2], "cash": 0.0, "market_value": 90000.0,
         "total_assets": 90000.0, "day_pnl": -30000.0, "realized_pnl": 3990.0, "fees": 30.0,
         "trade_count": 1, "status": "degraded", "error": "研究失败"},
    ]
    agg = calc.aggregate_steps(steps, 100000.0)
    check("交易日数", agg["day_count"] == 3)
    check("期末资产 == 最后一天的 total_assets", agg["final_assets"] == 90000.0)
    check("总盈亏 = 90000 − 100000 = -10000", agg["total_pnl"] == -10000.0, str(agg["total_pnl"]))
    check("总收益率 = -10%", agg["total_return"] == -0.1, str(agg["total_return"]))
    check("最大回撤 = (120000−90000)/120000 = 0.25", agg["max_drawdown"] == 0.25,
          str(agg["max_drawdown"]))
    check("累计费用 = 25+25+30 = 80", agg["total_fees"] == 80.0, str(agg["total_fees"]))
    check("已实现盈亏取最后一天的**累计**值", agg["realized_pnl"] == 3990.0,
          str(agg["realized_pnl"]))
    check("成交笔数 = 3", agg["trade_count"] == 3, str(agg["trade_count"]))
    check("曲线行数 == 天数", len(agg["equity_curve"]) == 3)
    check("降级的那些天要如实列出来（不能悄悄混进正常结果）",
          len(agg["degraded_days"]) == 1
          and agg["degraded_days"][0]["trade_date"] == DAYS5[2], str(agg["degraded_days"]))

    # init_basis 用错（拿初始现金当分母）在 copy 模式下差别很大——这里守着"分母是入参"
    agg2 = calc.aggregate_steps(steps, 200000.0)
    check("分母用传入的 init_basis（换成 20 万 ⇒ 收益率 -55%）",
          agg2["total_return"] == -0.55, str(agg2["total_return"]))

    empty = calc.aggregate_steps([], 100000.0)
    check("空 step 序列 ⇒ 全零，不抛", empty["day_count"] == 0 and empty["max_drawdown"] == 0.0)


# ------------------------------------------------------------- 9. 起跑否决


def validate_section() -> None:
    section("9. 起跑前的快速否决（宁可在这里拒，也不要跑十小时才发现什么都没做）")

    fresh_db()
    rid = make_run(init_mode="cash", init_cash=100000.0)
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == rid).first()
        # 判据是**执行层真正读的那份数据**——冻结的标的池，不是发起人托管设置里的
        # stock_scope。旧断言守的正是那个谁也读不到的键：校验说"不行"而执行层说"行"，
        # 反过来的组合也一样能过，两边从来没有对上过。
        row.universe_json = json.dumps([])
        # 单元里另一处也在改 config_json 时，这条断言依然要成立：判据不看它。
        row.config_json = json.dumps({"stock_scope": 1})
        try:
            bt._validate_startable(row)
            check("空仓 + 冻结池为空 ⇒ 起跑被拒", False, "没有抛")
        except ServiceError as e:
            check("空仓 + 冻结池为空 ⇒ 起跑被拒（提示选一个股票范围）",
                  "股票范围" in str(e), str(e))

        # 反过来：冻结池非空，即使 config_json 里的 scope 是「仅持仓」也要放行——
        # 这正是"校验读的键 = 执行读的键"的直接体现，也是本缺陷的回归门。
        row.universe_json = json.dumps(["600519.SH"])
        row.config_json = json.dumps({"stock_scope": 0})
        bt._validate_startable(row)
        check("空仓 + 冻结池非空（即便 scope=0）⇒ 放行", True)

        row.config_json = json.dumps({"stock_scope": 1})
        bt._validate_startable(row)
        check("空仓 + 仅自选 ⇒ 放行", True)

        row.init_cash = 0.0
        try:
            bt._validate_startable(row)
            check("空仓但初始资金为 0 ⇒ 起跑被拒", False, "没有抛")
        except ServiceError as e:
            check("空仓但初始资金为 0 ⇒ 起跑被拒", "初始资金" in str(e), str(e))

        row.init_cash = 100000.0
        row.init_mode = "copy"
        row.shadow_user_id = None
        try:
            bt._validate_startable(row)
            check("缺影子账户 ⇒ 起跑被拒", False, "没有抛")
        except ServiceError as e:
            check("缺影子账户 ⇒ 起跑被拒", "影子账户" in str(e), str(e))
    finally:
        session.close()


# ------------------------------------------------------------- 10. 编排端到端


class _Stubs:
    """把 ``_drive`` 的四个外部依赖（交易日历 / 行情 / 基准 / 研究层）换成确定性桩。

    唯一**不**打桩的是撮合本身：``run_execution`` 的桩内部调用**真实**的
    ``trade.place_order``，所以现金、持仓、委托、成交都是真库里长出来的。这样这一节验的是
    "编排 + 账务 + 断点续跑"，而不是我自己写的假账。
    """

    BUY_DAYS = {DAYS5[0], DAYS5[2]}     # 只在第 1、3 天买（第 2 天不交易，模拟"团队看空"）

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.crash_on: str | None = None
        self.crashed = False
        self._saved: list = []

    # ---- 桩 ----
    def fake_execution(self, sh, clock=None):
        self.calls.append(clock.date)
        bar = clock.bars(["600519.SH"]).get("600519.SH")
        if bar is None or not bar.tradable:
            return {"trades": [], "skipped": "no_bar"}
        # 当天已经买过就不再买：返回空成交会让 _drive 提前结束子 tick 循环
        if clock.date in self.BUY_DAYS and self.calls.count(clock.date) == 1:
            r = trade_mod.place_order(
                sh, "600519.SH", "贵州茅台", 0, 100,
                price=bar.open, ts=clock.ts, clock=clock,
            )
            # 崩在"委托已落库、step 还没写"的当口——这正是检查点要兜住的那一刻。
            # 用 BaseException 而不是 Exception：进程被杀时解释器**根本没机会**跑任何
            # except 分支，这是对"kill -9"唯一忠实的模拟（_drive 里按天兜的是 Exception，
            # 那是给单日行情/接口抖动用的，两者不是一回事）。
            if self.crash_on == clock.date and not self.crashed:
                self.crashed = True
                raise SystemExit("模拟进程被杀")
            return {"trades": [r], "skipped": None}
        return {"trades": [], "skipped": "done"}

    def __enter__(self):
        self._saved = [
            (bt.bd, "trading_days", bt.bd.trading_days),
            (bt.bd, "load_bars", bt.bd.load_bars),
            (bt.bd, "load_benchmark", bt.bd.load_benchmark),
            (trust, "run_execution", trust.run_execution),
            (trust, "run_plan_for_user", trust.run_plan_for_user),
        ]
        bars = {
            d: bar(10.0 + i, 10.6 + i, 9.5 + i, 10.2 + i, prev=10.1 + i)
            for i, d in enumerate(DAYS5)
        }
        bt.bd.trading_days = lambda s, e: list(DAYS5)
        bt.bd.load_bars = lambda codes, days, s, e, on_progress=None: (
            {"600519.SH": bars}, []
        )
        bt.bd.load_benchmark = lambda s, e: {d: 4000.0 + 10 * i for i, d in enumerate(DAYS5)}
        trust.run_execution = self.fake_execution
        trust.run_plan_for_user = lambda *a, **k: []
        from app import analysis_service as _as
        self._saved.append((_as, "run_portfolio_plan_for_user",
                            _as.run_portfolio_plan_for_user))
        # `_drive` 现在会**检查**这一层的返回值（空计划 = 失败日，见那里的注释）。本脚本验的是
        # 账务/续跑，计划层在这里是替身，所以必须回一份"有效方案"——回 None 会让每一天都被判成
        # 失败日，连错三天就中止整轮。要验「空计划会中止」请用 _drive_now 的专用用例。
        _as.run_portfolio_plan_for_user = lambda *a, **k: {
            "actions": [{"code": "600519.SH", "action": "hold"}]
        }
        return self

    def __exit__(self, *exc):
        for mod, name, fn in reversed(self._saved):
            setattr(mod, name, fn)
        return False


class _NoPlanStubs(_Stubs):
    """执行层每天都回「今天没有可执行的计划」——``_run_execution`` 在回测路径下的真实返回。

    它**不抛异常**：这正是危险之处。用 ``skipped`` 而不是用一个"返回空成交的假执行层"，
    是为了钉住真正的失效模式（替身返回空成交是"计划在、没触发"，两者的库内形状必须能分开）。
    """

    def fake_execution(self, sh, clock=None):
        self.calls.append(clock.date)
        return {"trades": [], "skipped": "no_plan_backtest"}


def _take_token(rid: int, token: str) -> None:
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == rid).first()
        row.worker_token = token
        session.commit()
    finally:
        session.close()


def _drive_now(rid: int, token: str = "t-test") -> None:
    _take_token(rid, token)
    bt._drive(rid, token, False)


def _worker_now(rid: int, token: str = "t-test") -> None:
    """同 :func:`_drive_now`，但走 **worker 外壳**：异常会被翻成 run 的终态。

    验「失败必须落到 failed、而不是留下一轮看起来跑完了的空转」时只能用这条——
    ``_drive`` 只负责抛，把异常翻译成 ``failed`` + 用户可读消息的是 ``_run_worker``
    （见那里的注释）。用 ``_drive_now`` 的话异常直接炸到调用方，看不到终态。
    """
    _take_token(rid, token)
    bt._run_worker(rid, token, False)


def _steps(rid: int) -> list[tuple[str, str, str]]:
    """该 run 的逐日 step：``(交易日, status, error)``，按日期排。"""
    session = db.get_session()
    try:
        rows = (
            session.query(db.BacktestStep)
            .filter(db.BacktestStep.run_id == rid)
            .order_by(db.BacktestStep.trade_date)
            .all()
        )
        return [(r.trade_date, r.status, r.error or "") for r in rows]
    finally:
        session.close()


def _equity(rid: int) -> list:
    """结果里的净值曲线，去掉时间戳后可直接比对。"""
    r = bt.get_run(rid)["result"]
    return [(p["trade_date"], p["total_assets"]) for p in r.get("equity_curve") or []]


def _book(sh: int) -> tuple:
    return (_cash(sh), _pos(sh, "600519.SH"), _count(db.Order), _count(db.Trade))


def orchestration_section() -> None:
    section("10. 编排端到端：跑一遍 / 崩在中途再续跑，两者的账必须**逐项相同**")

    # ---- 甲：一次跑完 ----
    fresh_db()
    sh_a = shadow_id()
    rid_a = make_run(init_mode="cash", init_cash=100000.0)
    with _Stubs() as st:
        _drive_now(rid_a)
    run_a = bt.get_run(rid_a)
    res_a = run_a["result"]
    check("状态置 done", run_a["status"] == "done", run_a["status"])
    check("5 个交易日各落一条 step", len(res_a["equity_curve"]) == 5,
          str(res_a["equity_curve"]))
    check("净值曲线的日期就是交易日历", [d for d, _ in _equity(rid_a)] == DAYS5,
          str(_equity(rid_a)))
    check("两天各买一手（第 2 天没交易 ⇒ 团队可以原地不动）",
          _count(db.Order) == 2 and _pos(sh_a, "600519.SH")["hold_qty"] == 200,
          f"orders={_count(db.Order)} hold={_pos(sh_a, '600519.SH')}")
    check("起始基准 = 现金 100000（copy 才可能非零持仓）",
          res_a["init_basis"] == 100000.0, str(res_a["init_basis"]))
    # 子 tick 的意义：买入日多推一格（第二格空手才 break），非买入日一格就停。
    # 这条断言守着"子 tick 真的在推进梯子"，而不是每天固定跑满 3 次白烧时间。
    check("买入日走 2 个子 tick、非买入日走 1 个",
          st.calls == [DAYS5[0], DAYS5[0], DAYS5[1], DAYS5[2], DAYS5[2], DAYS5[3], DAYS5[4]],
          str(st.calls))
    check("结果带等权基准与沪深300 基准", "baseline_equal_weight" in res_a
          and "baseline_index" in res_a)
    check("超额收益两个都算了", "excess_return" in res_a and "excess_return_index" in res_a)
    # 声明条数 = 固定那些 + 本次运行特有的那些（后者按 config 里的实际情况追加/不追加）。
    # 这里钉的是"固定那批一条都不能少"，而不是一个写死的总数——写死总数会让后端每加一条
    # 声明都得回来改这一行，改着改着就会变成"把数字改大让它过"。
    check("固定声明随结果一起下发（一条都不能少）",
          len(res_a["disclosures"]) >= len(calc.DISCLOSURES)
          and {d["key"] for d in res_a["disclosures"]} >= {d["key"] for d in calc.DISCLOSURES},
          str(len(res_a.get("disclosures") or [])))
    # 这次 run 没走范围选择（没冻 bt_merged）⇒ 不该凭空多出一条"自动并入"的声明。
    # 凭空出现的声明比没有更糟：它是一句不成立的免责。
    check("没发生自动并入时不编造并入声明",
          all(d["key"] != "range_merged" for d in res_a["disclosures"]),
          str([d["key"] for d in res_a["disclosures"]]))
    check("完成时间已落", run_a["finished_at"] is not None)
    check("收尾后 worker_token 归零（避免挡住下一次续跑）",
          db.get_session().query(db.BacktestRun.worker_token)
          .filter(db.BacktestRun.id == rid_a).scalar() is None)
    book_a = _book(sh_a)
    eq_a = _equity(rid_a)

    # ---- 乙：崩在第 3 天，再续跑 ----
    fresh_db()
    sh_b = shadow_id()
    rid_b = make_run(init_mode="cash", init_cash=100000.0)
    with _Stubs() as st:
        st.crash_on = DAYS5[2]
        try:
            _drive_now(rid_b)
            check("模拟崩溃确实抛出来了", False, "没有抛")
        except BaseException as e:  # noqa: BLE001 —— SystemExit 模拟的就是"没人接得住"
            check("进程被杀：run 停在 running（等 resume_orphan_runs 接上）",
                  "模拟进程被杀" in str(e), f"{type(e).__name__}: {e}")
    run_b = bt.get_run(rid_b)
    check("检查点停在崩溃的前一天（第 3 天没有落 step）",
          run_b["last_step_date"] == DAYS5[1], str(run_b["last_step_date"]))
    check("第 3 天的半截委托/成交**还在库里**（这正是要清的残迹）",
          _count(db.Order) == 2 and _count(db.Trade) == 2,
          f"orders={_count(db.Order)} trades={_count(db.Trade)}")
    check("半截的账务确实脏了（现金 != 检查点）",
          _cash(sh_b) != json.loads(run_b["checkpoint_json"])["cash"],
          f"{_cash(sh_b)} vs {run_b['checkpoint_json']}")

    with _Stubs() as st:
        _drive_now(rid_b, "t-resume")
    run_b2 = bt.get_run(rid_b)
    res_b = run_b2["result"]
    check("续跑后状态 done", run_b2["status"] == "done", run_b2["status"])
    check("续跑从第 3 天接上，第 1、2 天没有重跑",
          st.calls == [DAYS5[2], DAYS5[2], DAYS5[3], DAYS5[4]], str(st.calls))
    check("**续跑的净值曲线与一次跑完逐项相同**", _equity(rid_b) == eq_a,
          f"{_equity(rid_b)} vs {eq_a}")
    check("**续跑的账务与一次跑完逐项相同**（现金/持仓/委托/成交）",
          _book(sh_b) == book_a, f"{_book(sh_b)} vs {book_a}")
    check("续跑的结果总量也相同",
          (res_b["final_assets"], res_b["total_return"], res_b["total_fees"])
          == (res_a["final_assets"], res_a["total_return"], res_a["total_fees"]),
          f"{res_b['final_assets']} vs {res_a['final_assets']}")

    # ---- 丙：取消（在日界生效，账务停在最后一个检查点上，是一致的）----
    fresh_db()
    sh_c = shadow_id()
    rid_c = make_run(init_mode="cash", init_cash=100000.0)
    with _Stubs() as st:
        bt.cancel_run(rid_c, owner_id())
        try:
            _drive_now(rid_c)
            check("取消后 _drive 抛 PlanCancelled", False, "没有抛")
        except Exception as e:  # noqa: BLE001
            check("取消在日界生效：抛的是 PlanCancelled 而不是别的",
                  type(e).__name__ == "PlanCancelled", type(e).__name__)
    check("取消后一笔都没成交",
          _count(db.Order) == 0 and _cash(sh_c) == 100000.0, str(_book(sh_c)))
    check("取消不写 result（没跑完就没有结果）",
          bt.get_run(rid_c)["result"] == {}, str(bt.get_run(rid_c)["result"]))

    # ---- 丁：计划层空转**必须**判成失败日，不能悄悄变成"团队今天没动作" ----
    # 守的是一处**静默**断链：``run_portfolio_plan_for_user`` 在结构化输出失败时返回 None
    # 而**不抛**（见 ``analysis_service`` 里的 ``return None``），于是 TrustPlan 不落库、次日
    # 执行层无计划可依、整轮回测一天天零成交——而 step 还会被标成 ok，跑完还报 done。实盘
    # 入口对同一情形是 raise 的，只有回测此前把返回值丢掉了。"团队连续几天什么都没做"正是
    # 回测唯一要验的东西，所以这里钉住：空计划 = 失败日，且连错就中止整轮。
    #
    # 正例（计划层给得出方案 ⇒ 全绿 done）由本节甲段守着，不在这里重复。
    # 两个用例的**性质不同**，别混为一谈：
    # ① ``None`` 是真实会发生的那个——``run_portfolio_plan_for_user`` 拿不到可用方案时
    #    就是返回 None（两处 ``return None`` 都判 ``actions`` 为空），所以这是生产路径。
    # ② 空 ``actions`` 走的是 ``_drive`` 里 ``not plan.get("actions")`` 那条**防御性**分支。
    #    它在当前实现下**不可达**（函数返回的方案必有非空 actions），但照样要断：这段代码
    #    存在的意义就是"计划层将来改成返回空方案时别再静默一次"，那就得先证明它会响。
    for label, bad_plan in (
        ("返回 None（生产路径：计划层没产出）", None),
        ("返回空 actions（防御性分支：当前不可达）", {"actions": []}),
    ):
        fresh_db()
        shadow_id()
        rid_d = make_run(init_mode="cash", init_cash=100000.0)
        with _Stubs() as st:
            from app import analysis_service as _as
            _as.run_portfolio_plan_for_user = lambda *a, _p=bad_plan, **k: _p
            _worker_now(rid_d, "t-empty")
        run_d = bt.get_run(rid_d)
        states = _steps(rid_d)
        want_n = bt._MAX_CONSECUTIVE_ERRORS
        check(f"计划层{label} ⇒ 整轮落 failed（不是 done）",
              run_d["status"] == "failed", run_d["status"])
        check(f"计划层{label} ⇒ 连错 {want_n} 天即中止，不把 5 天跑满",
              len(states) == want_n, f"{len(states)} 天：{states}")
        check(f"计划层{label} ⇒ 每天都标 degraded（没被当成「团队今天没动作」）",
              bool(states) and all(s == "degraded" for _, s, _ in states), str(states))
        check(f"计划层{label} ⇒ 失败原因写明是计划层没产出方案",
              bool(states) and all("组合决策层未产出有效行动方案" in e for _, _, e in states),
              str(states))
        check(f"计划层{label} ⇒ 中止消息直接可读（含连续天数与根因）",
              f"连续 {want_n} 天无法生成计划" in (run_d["message"] or "")
              and "组合决策层未产出有效行动方案" in (run_d["message"] or ""),
              run_d["message"])
        # 中止发生在"计划已算错、账还没记岔"的位置：交出的是最后一天的净值快照，
        # 不是半截账簿——否则用户看到的曲线会在末尾断一天。
        check(f"计划层{label} ⇒ 中止前把当天净值落库（曲线不断尾）",
              [d for d, _, _ in states] == DAYS5[:want_n], str(states))
        # 中止是**真的停住**，不是"标个失败继续跑满 5 天"——第 4、5 天连执行层都没进。
        check(f"计划层{label} ⇒ 中止后不再往后跑（后两天根本没执行）",
              set(st.calls) == set(DAYS5[:want_n]), str(st.calls))
        # 中止发生在"计划已算错、账还没记岔"的位置：抛之前先存了检查点，所以实库与检查点
        # 应当**一致**（对比乙段：那里是进程被杀，没机会存，所以两者必须**不一致**）。
        check(f"计划层{label} ⇒ 中止时检查点与实库一致（续跑不会算岔）",
              json.loads(run_d["checkpoint_json"] or "{}").get("cash")
              == _cash(run_d["shadow_user_id"]),
              f"{run_d['checkpoint_json']} vs {_cash(run_d['shadow_user_id'])}")

    # ---- 戊：执行日**没有计划可执行**必须判成失败日（静默空转的读侧孪生）----
    # 丁段守的是生成侧（计划层没产出 ⇒ 响亮失败）。这一段守**读侧**：``_run_execution``
    # 在当天没有计划时返回 ``{"trades": [], "skipped": "no_plan_backtest"}`` 而**不抛异常**，
    # 而 ``_drive`` 早先只读 ``res["trades"]``，把 skipped 丢掉了。于是
    # 「计划丢了」与「计划在、一档都没触发」在库里**长得一模一样**：零成交、step 都是 ok、
    # 整轮都报"完成：5 个交易日"。用户看到的正是这个——报告页少两天，结果页却说跑完了。
    # 这里让它不可能再静默：缺计划日 = degraded + 可读原因，连错就中止。
    fresh_db()
    sh_e = shadow_id()
    rid_e = make_run(init_mode="cash", init_cash=100000.0)
    # 走 worker 外壳（与丁段同一理由）：``_drive`` 只负责抛，把异常翻成 ``failed`` +
    # 可读消息的是 ``_run_worker``。用 ``_drive_now`` 的话看不到终态，等于没验"不再报 done"。
    with _NoPlanStubs() as st_e:
        _worker_now(rid_e, "t-noplan")
    run_e = bt.get_run(rid_e)
    states_e = _steps(rid_e)
    want_n = bt._MAX_CONSECUTIVE_ERRORS
    check("★ 缺计划连错即中止整轮（不再跑满 5 天还报 done）",
          f"连续 {want_n} 天没有可执行的计划" in (run_e["message"] or ""), run_e["message"])
    check("★ 整轮落 failed，不是 done（这一条就是「静默空转」的反面）",
          run_e["status"] == "failed", run_e["status"])
    check("★ **首日豁免**：第一天没有计划是设计（没有前置研究日），仍是 ok 而不是 degraded",
          states_e and states_e[0][1] == "ok", str(states_e))
    check(f"★ 之后每天都标 degraded，且原因写明「当日没有可执行的计划」（不是「团队今天没动作」）",
          bool(states_e) and all(s == "degraded" for _, s, _ in states_e[1:])
          and all("当日没有可执行的计划" in e for _, _, e in states_e[1:]),
          str(states_e))
    check(f"⇒ 共 {want_n + 1} 天落 step（首日豁免 + 连错 {want_n} 天）",
          [d for d, _, _ in states_e] == DAYS5[:want_n + 1], str(states_e))
    check("⇒ 一天都没成交（无计划就真的什么都没做）",
          _count(db.Order) == 0 and _cash(sh_e) == 100000.0, str(_book(sh_e)))
    check("⇒ 中止时检查点与实库一致（续跑不会算岔）",
          json.loads(run_e["checkpoint_json"] or "{}").get("cash") == _cash(sh_e),
          f"{run_e['checkpoint_json']} vs {_cash(sh_e)}")
    # 中止点放在落 step 之后、(4) 之前：不该为"永远不会被执行的下一天"多写一份计划。
    check("⇒ 不为没跑到的日子留下孤儿计划（本周期的计划生成在缺计划日被跳过）",
          not _plans(run_e["shadow_user_id"]), str(_plans(run_e["shadow_user_id"])))

    # ---- 己之一：分类器本身（纯函数，不碰网络、不起库）----
    # 第一次跑 C4（run 5）就是在这里被误诊的：DeepSeek 402（余额不足）打在 Stage2 的三个
    # 节点上，``invoke_structured`` 按既有契约返回 None ⇒ 回测报「组合决策层未产出有效行动
    # 方案」，读起来像管线 bug（真实原因只在前一行的 structured-output 警告里），而且照着
    # 「连错 3 天」又白耗了 2 天墙钟。账户级失败**不可重试**：没有 LLM 就没有团队。
    from tradingagents.agents.utils.structured import (  # noqa: E402
        AccountLevelLLMError,
        account_level_reason,
        invoke_structured,
        invoke_structured_or_freetext,
    )

    class _FakeStatusError(Exception):
        """OpenAI SDK 的 APIStatusError 是鸭子类型（``status_code``），这里照它的形状造。"""

        def __init__(self, status: int, msg: str):
            super().__init__(msg)
            self.status_code = status

    check("402（余额耗尽）判为账户级",
          bool(account_level_reason(_FakeStatusError(402, "Insufficient Balance"))),
          str(account_level_reason(_FakeStatusError(402, "Insufficient Balance"))))
    check("401（key 失效）判为账户级",
          bool(account_level_reason(_FakeStatusError(401, "Unauthorized"))))
    check("★ 429（限流）**不**判为账户级——它是瞬时的，按致命处理会让一次抖动废掉几小时进度",
          account_level_reason(_FakeStatusError(429, "Rate limit exceeded")) is None)
    check("★ 403 **不**判为账户级（同一 provider 上常常只是某个端点的权限）",
          account_level_reason(_FakeStatusError(403, "Forbidden")) is None)
    check("★ 「结构化输出没解析出来」**不**判为账户级（那是模型不调工具，该重试/回落）",
          account_level_reason(ValueError("structured output returned no parsed result")) is None)

    class _BoomLLM:
        """invoke 一律抛指定异常，并记下被调用了几次（断言"没白试"用）。"""

        def __init__(self, exc: Exception):
            self.exc, self.n = exc, 0

        def invoke(self, prompt):
            self.n += 1
            raise self.exc

    llm_f = _BoomLLM(_FakeStatusError(402, "Insufficient Balance"))
    raised_f = None
    try:
        invoke_structured(llm_f, "p", "T")
    except AccountLevelLLMError as exc:
        raised_f = exc
    check("★ 402 时 invoke_structured 抛 AccountLevelLLMError（不再返回 None 让上层当普通失败日）",
          raised_f is not None and "402" in str(raised_f), str(raised_f))
    check("★ 而且**只试一次**：账户级失败重试没有意义（此前会试满两次再放弃）",
          llm_f.n == 1, f"{llm_f.n} 次")

    llm_g = _BoomLLM(_FakeStatusError(500, "boom"))
    check("普通故障仍守原契约：重试一次后返回 None（回落逻辑不受影响）",
          invoke_structured(llm_g, "p", "T") is None and llm_g.n == 2, f"{llm_g.n} 次")

    plain_h = _BoomLLM(_FakeStatusError(500, "boom"))
    raised_h = None
    try:
        invoke_structured_or_freetext(
            _BoomLLM(_FakeStatusError(402, "Insufficient Balance")),
            plain_h, "p", lambda r: "x", "T",
        )
    except AccountLevelLLMError as exc:
        raised_h = exc
    check("★ 账户级失败不回落 free-text（它会以同样的方式失败，只会多烧一次调用）",
          raised_h is not None and plain_h.n == 0, f"raised={raised_h} 回落调用={plain_h.n} 次")

    # ---- 己之二：真库上，账户级失败**立刻**中止整轮 ----
    # 与丁段（计划层空转）形状相似但判据不同：丁段要的是"连错 3 天才停"，这里要的是"第 1 天
    # 就停"。差别就是本轮修的东西——账户级失败是**不可重试**的，慢慢走完只是把同一个错误写得更慢。
    fresh_db()
    shadow_id()
    rid_f = make_run(init_mode="cash", init_cash=100000.0)

    def _account_boom(*a, **k):
        raise AccountLevelLLMError("HTTP 402: Insufficient Balance")

    with _Stubs() as st_f:
        from app import analysis_service as _as
        _as.run_portfolio_plan_for_user = _account_boom
        _worker_now(rid_f, "t-acct")
    run_f = bt.get_run(rid_f)
    states_f = _steps(rid_f)
    check("★ 账户级失败 ⇒ 整轮立刻落 failed（不是 done，也不是跑满 5 天）",
          run_f["status"] == "failed", run_f["status"])
    check("★ 只跑到第 1 天就停——不按「连错 3 天」的慢性路径走完",
          [d for d, _, _ in states_f] == DAYS5[:1], str(states_f))
    check("★ 中止消息写出**真实原因**（余额/key），而不是中性的「未产出有效行动方案」",
          "LLM 账户不可用" in (run_f["message"] or "")
          and "Insufficient Balance" in (run_f["message"] or "")
          and "未产出有效行动方案" not in (run_f["message"] or ""),
          run_f["message"])
    check("⇒ 中止后不再往后跑（后 4 天连执行层都没进）",
          set(st_f.calls) == set(DAYS5[:1]), str(st_f.calls))
    check("⇒ 当天净值已落库、检查点与实库一致（曲线不断尾，续跑不会算岔）",
          json.loads(run_f["checkpoint_json"] or "{}").get("cash") == _cash(run_f["shadow_user_id"]),
          f"{run_f['checkpoint_json']} vs {_cash(run_f['shadow_user_id'])}")
    check("⇒ 这一天的原因也写明是账户问题（step 层面同样读得出来，不靠 run.message 一个字段）",
          bool(states_f) and "LLM 账户不可用" in states_f[0][2], str(states_f))


# ------------------------------------------------------------- 11. API 层

STRANGER_PHONE = "13900000002"


def stranger_id() -> int:
    session = db.get_session()
    try:
        u = db.User(phone=STRANGER_PHONE, created_at=NOW, is_backtest=False)
        session.add(u)
        session.flush()
        session.add(db.TrustConfig(
            user_id=u.id, is_active=False, book_created=True, available_cash=0.0,
            stock_scope=1, updated_at=NOW,
        ))
        session.commit()
        return int(u.id)
    finally:
        session.close()


def _with_days(fn):
    """`preview` 要数交易日，而 `bd.trading_days` 会去连 Wind。"""
    saved = bt.bd.trading_days
    bt.bd.trading_days = lambda s, e: list(DAYS5)
    try:
        return fn()
    finally:
        bt.bd.trading_days = saved


def _raises(fn) -> str:
    """跑 fn，期望它抛 ServiceError；返回消息（没抛就返回空串）。"""
    try:
        fn()
    except ServiceError as e:
        return e.message
    except Exception as e:  # noqa: BLE001 —— 抛了别的类型也是失败
        return f"<{type(e).__name__}: {e}>"
    return ""


def _add_watchlist(uid: int, codes: list[str]) -> None:
    session = db.get_session()
    try:
        for c in codes:
            session.add(db.Watchlist(
                user_id=uid, stock_code=c, stock_name=c, source=0,
                group_name="默认", created_at=NOW,
            ))
        session.commit()
    finally:
        session.close()


def api_section() -> None:
    section("11. API 层：越权挡住 / 删除不留痕 / 预览的估算与告警")

    fresh_db()
    me, other = owner_id(), stranger_id()
    sh = shadow_id()
    rid = make_run(init_mode="cash", init_cash=100000.0)

    # ---- 甲：每一条读接口都必须按发起人过滤 ----
    check("本人能读自己的进度", bt.get_progress(rid, me)["run_id"] == rid)
    check("**别人读不到我的进度**", _raises(lambda: bt.get_progress(rid, other)) == "回测不存在",
          _raises(lambda: bt.get_progress(rid, other)))
    check("**别人读不到我的结果**", _raises(lambda: bt.get_result(rid, other)) == "回测不存在")
    check("**别人读不到我的成交流水**", _raises(lambda: bt.get_trades(rid, other)) == "回测不存在")
    check("**别人删不掉我的回测**", _raises(lambda: bt.delete_run(rid, other)) == "回测不存在")
    check("**别人取消不了我的回测**",
          _raises(lambda: bt.cancel_run(rid, other)) == "回测不存在",
          _raises(lambda: bt.cancel_run(rid, other)))
    check("列表只出自己的",
          [r["run_id"] for r in bt.list_runs(me)] == [rid]
          and bt.list_runs(other) == [],
          f"me={bt.list_runs(me)} other={bt.list_runs(other)}")
    check("越权尝试没有改动任何东西（run 还在）",
          bt.get_run(rid) is not None and bt.get_progress(rid, me)["status"] == "pending")

    # ---- 已跑时长（"这次到底跑了多久"）----
    # 界面上原本一个时间都不显示，用户只能去库里翻 created_at/finished_at。而这恰恰是
    # 决定"要不要再来一次"的那个数。口径只有一条：**跑完取 finished_at，在跑取现在**。
    def _elapsed(created, finished, updated=None):
        """直接喂时间戳算已跑时长，绕开"现在几点"——否则断言会随运行时刻漂。"""
        class _R:
            created_at = created
        r = _R()
        r.finished_at = finished
        r.updated_at = updated if updated is not None else created
        return bt._elapsed_seconds(r), bt.elapsed_text(r)

    secs, label = _elapsed(1000.0, 1000.0 + 3600 * 2.5)
    check("跑完：已跑时长 = finished_at - created_at，且给人话",
          secs == 9000.0 and label == "约 2.5 小时", f"{secs} / {label}")
    check("在跑：时长按**现在**算，不按 updated_at 算（否则卡住的 run 会把时长冻住）",
          _elapsed(1000.0, None, updated=1000.0)[0] > 3600.0,
          str(_elapsed(1000.0, None, updated=1000.0)[0]))
    check("★ created_at 缺失（口径升级前的老记录）→ 给不出就如实给不出，不凑 0",
          _elapsed(None, None) == (None, ""), str(_elapsed(None, None)))
    check("时钟回拨（finished_at < created_at）时夹到 0，不出现负数时长",
          _elapsed(2000.0, 1000.0)[0] == 0.0, str(_elapsed(2000.0, 1000.0)[0]))

    prog = bt.get_progress(rid, me)
    check("进度接口带上 elapsed_seconds 与 elapsed_text（前端不用自己算）",
          "elapsed_seconds" in prog and "elapsed_text" in prog, str(sorted(prog)))
    check("列表也带上 elapsed_text（一眼看出哪条还在跑、跑了多久）",
          all("elapsed_text" in r for r in bt.list_runs(me)), str(bt.list_runs(me)))

    # ---- 乙：删除 ----
    with _Stubs():
        _drive_now(rid)
    check("先跑完，有 5 条 step 与成交流水",
          len(bt.get_result(rid, me)["steps"]) == 5
          and bt.get_trades(rid, me)["trade_count"] == 2,
          str(bt.get_trades(rid, me)))

    session = db.get_session()
    try:
        # 影子账户的研究缓存（真实场景下是十小时量级的 LLM 调用）——删除回测**不该**动它
        session.add(db.EngineRun(
            user_id=sh, ticker="600519.SH", market="SH", trade_date=D1,
            rating="Buy", report_json="{}", created_at=NOW,
        ))
        session.commit()
    finally:
        session.close()

    running = make_run(init_mode="cash", init_cash=100000.0, status="running")
    with bt._ACTIVE_LOCK:
        bt._ACTIVE_RUNS.add(running)           # 模拟"本进程真的有 worker 在推它"
    check("运行中的回测不许删（先取消）",
          _raises(lambda: bt.delete_run(running, me)) == "回测正在运行，请先取消再删除",
          _raises(lambda: bt.delete_run(running, me)))
    with bt._ACTIVE_LOCK:
        bt._ACTIVE_RUNS.discard(running)       # 模拟 kill -9 之后没人来得及改状态
    check("**孤儿 run（状态还是 running，但没有活着的 worker）可以删掉**——"
          "否则这条记录会一直卡着，要等下次重启才被认领",
          bt.delete_run(running, me)["deleted"] is True)
    check("孤儿删除也清干净了", bt.get_run(running) is None)
    # 上面的孤儿删除会把影子账簿重置一次；把它再弄脏，好让后面的断言仍能验到"删完是干净的"
    session = db.get_session()
    try:
        session.query(db.TrustConfig).filter(db.TrustConfig.user_id == sh).update(
            {"available_cash": 1234.0}
        )
        session.commit()
    finally:
        session.close()

    bt.delete_run(rid, me)
    check("删除后 run 没了", bt.get_run(rid) is None)
    check("删除后逐日 step 也没了（不留孤儿行）",
          _count(db.BacktestStep) == 0, str(_count(db.BacktestStep)))
    check("删除后影子账簿被清干净（现金归零、无持仓/委托/成交）",
          (_cash(sh), _pos(sh, "600519.SH"), _count(db.Order), _count(db.Trade))
          == (0.0, None, 0, 0),
          str((_cash(sh), _pos(sh, "600519.SH"), _count(db.Order), _count(db.Trade))))
    check("**研究缓存被保留**（重跑一次不用再烧十小时）", _count(db.EngineRun) == 1)

    # ---- 丁：删回测时，计划只删「只有这个 run 覆盖的日期」 ----
    # 影子账户跨 run 复用，而 ``TrustPlan`` 的唯一键只有 ``(user_id, trade_date)``——
    # 同窗口的两个 run **共用同一批计划行**。阶段 C 的矩阵里 C1 与 C3 就是同一个 5 日窗口，
    # 删掉 C1 不该顺手清空 C3 的报告（那正是这一条要防的）。
    fresh_db()
    me = owner_id()
    r_a = make_run(init_mode="cash", init_cash=100000.0)
    r_b = make_run(init_mode="cash", init_cash=100000.0)
    sh_ab = bt.get_run(r_a)["shadow_user_id"]
    check("两个 run 共用一个影子账户（设计如此：EngineRun 研究缓存太贵）",
          bt.get_run(r_b)["shadow_user_id"] == sh_ab, str(bt.get_run(r_b)["shadow_user_id"]))
    in_win = [D1, D2]        # run 的窗口是 [D1, D2]
    out_win = "2026-03-30"   # 窗口外：谁都不该删它
    session = db.get_session()
    try:
        # 每天**只有一行**——``TrustPlan`` 的唯一键就是 ``(user_id, trade_date)``，这正是
        # "同窗口的两个 run 共用同一批计划行"的机制来源（不是两个 run 各存一份）。
        for d in in_win + [out_win]:
            session.add(db.TrustPlan(
                user_id=sh_ab, trade_date=d,
                plan_json=json.dumps({"actions": [{"code": "600519.SH", "action": "hold"}]}),
                created_at=NOW,
            ))
        session.commit()
    finally:
        session.close()
    check("（前提）影子名下有 2 天窗口内计划 + 1 天窗口外计划",
          _plans(sh_ab) == sorted(in_win + [out_win]), str(_plans(sh_ab)))

    out_a = bt.delete_run(r_a, me)
    check("★ 删掉 r_a：窗口内的计划**留给还在的 r_b**（同窗口的另一个 run）",
          _plans(sh_ab) == sorted(in_win + [out_win]), str(_plans(sh_ab)))
    check("回执如实给出删了几条计划（这里是 0，因为都被 r_b 覆盖）",
          out_a["plans"] == 0, str(out_a))

    out_b = bt.delete_run(r_b, me)
    check("★ 删掉最后一个覆盖该窗口的 run：计划这才跟着走",
          _plans(sh_ab) == [out_win], str(_plans(sh_ab)))
    check("回执计数正确", out_b["plans"] == len(in_win), str(out_b))
    check("★ 窗口外的计划不动（不是无脑按影子账户清空）",
          _plans(sh_ab) == [out_win], str(_plans(sh_ab)))

    # ---- 丙：预览 ----
    fresh_db()
    me = owner_id()
    check("标的池为空时明确告警（而不是让用户跑一场空转）",
          "标的池为空" in "".join(
              _with_days(lambda: bt.preview(me, D1, DAYS5[-1]))["warnings"]),
          str(_with_days(lambda: bt.preview(me, D1, DAYS5[-1]))["warnings"]))

    _add_watchlist(me, [f"6005{i:02d}.SH" for i in range(45)])
    pv = _with_days(lambda: bt.preview(me, D1, DAYS5[-1]))
    check("交易日数取自交易日历", pv["trading_days"] == 5, str(pv["trading_days"]))
    check("标的数取自当前托管标的池", pv["universe_size"] == 45, str(pv["universe_size"]))
    check("预计调用次数 = 天数 × 标的数 × 19",
          pv["llm_calls"] == 5 * 45 * 19, str(pv["llm_calls"]))
    check("预计耗时 = 天数 × ceil(标的/4) × 19 × 35s",
          pv["est_seconds"] == 5 * 12 * 19 * 35, str(pv["est_seconds"]))
    check("耗时给的是人话而不是秒数", pv["est_label"].startswith("约 ")
          and "小时" in pv["est_label"], pv["est_label"])
    check("量级过大时劝用户先跑试点",
          any("10 天试点" in w for w in pv["warnings"]), str(pv["warnings"]))

    future_msg = _raises(lambda: _with_days(lambda: bt.preview(me, "2099-01-01", "2099-02-01")))
    check("**结束日期晚于今天要被拒**（回测未来没有意义，只会静默给出误导结果）",
          "不能晚于今天" in future_msg, future_msg)
    check("起止颠倒要被拒",
          "晚于结束日期" in _raises(lambda: _with_days(lambda: bt.preview(me, DAYS5[-1], D1))),
          _raises(lambda: _with_days(lambda: bt.preview(me, DAYS5[-1], D1))))
    # `"2026-3-2"` 能通过字符串比较，却会在下游的 strptime / 交易日历里炸开，
    # 报出来的错跟用户填的东西毫无关系——所以要在入口就拦住。
    check("日期格式不对在入口就被拒（不是等下游炸）",
          "格式不对" in _raises(lambda: _with_days(lambda: bt.preview(me, "2026-3-2", DAYS5[-1]))),
          _raises(lambda: _with_days(lambda: bt.preview(me, "2026-3-2", DAYS5[-1]))))
    check("日期为空也被拒",
          _raises(lambda: _with_days(lambda: bt.preview(me, "", ""))) != "",
          _raises(lambda: _with_days(lambda: bt.preview(me, "", ""))))

    # ---- 探数：把"45 只里 40 只有数据"摆到**发起之前** ----
    # 不探数的话，缺失的票要等十小时之后才由结果页的告警栏揭晓。
    def probe_stub(available: int):
        """替掉 ``load_bars``：``universe`` 的前 N 只有行情，其余记一条告警。"""
        def _stub(codes, days, s, e, on_progress=None):
            got = {c: {days[0]: bar(10, 10, 10, 10)} for c in codes[:available]} if days else {}
            return got, [f"{c}：历史行情取不到，该标的全程不参与" for c in codes[available:]]
        return _stub

    def probe_stub_missing(gone: list[str]):
        """替掉 ``load_bars``：**点名的几只**取不到，其余都有行情。

        不能用 ``probe_stub(N)`` 的"前 N 只有行情"来造"持仓缺行情"——``get_analysis_universe``
        返回的是 ``持仓 ∪ 自选池``，**持仓排在最前面**。一旦给托管簿加了一只票，它就成了
        ``codes[0]``，必然落在前 N 里、必然"有行情"，这条断言就永远测不到东西。
        真实的触发场景是这只票**整段区间都没有 bar**（长期停牌 / 已退市 / 代码写错），
        所以按名字点掉它，才对得上要守的那件事。
        """
        gone_set = set(gone)

        def _stub(codes, days, s, e, on_progress=None):
            got = ({c: {days[0]: bar(10, 10, 10, 10)} for c in codes if c not in gone_set}
                   if days else {})
            return got, [f"{c}：历史行情取不到，该标的全程不参与" for c in codes if c in gone_set]
        return _stub

    saved_load = bt.bd.load_bars
    try:
        bt.bd.load_bars = probe_stub(40)
        pvp = _with_days(lambda: bt.preview(me, D1, DAYS5[-1], probe=True))
        check("probe=True 时真的去取了数", pvp["probe"] is True)
        check("★ 探数回报实际可用数（45 请求 → 40 可用 / 缺 5）",
              (pvp["data_available"], pvp["data_missing"]) == (40, 5),
              f"{pvp['data_available']}/{pvp['data_missing']}")
        check("缺失清单逐个给出（不是只说「有标的缺失」）",
              len(pvp["missing_symbols"]) == 5, str(pvp["missing_symbols"]))
        check("缺 5/45 未过阈值 → 不拦，但把实情摆出来",
              pvp["blocking"] is False and pvp["block_reason"] == "", pvp["block_reason"])

        bt.bd.load_bars = probe_stub(10)
        pvp2 = _with_days(lambda: bt.preview(me, D1, DAYS5[-1], probe=True))
        check("★ 缺 35/45（超过一半）→ blocking=True，且拒因就是起跑门禁那句",
              pvp2["blocking"] is True and "只剩 10 只可跑" in pvp2["block_reason"],
              pvp2["block_reason"])
        check("★ 拒因排在 warnings 最前面（不然会被读成又一条建议）",
              pvp2["warnings"][0] == pvp2["block_reason"], str(pvp2["warnings"][:2]))
    finally:
        bt.bd.load_bars = saved_load

    # 不探数时这些字段必须是"没查过"而不是"查了没问题"——两者在 UI 上长得一样，
    # 但后者是在替后端打保票。
    pvn = _with_days(lambda: bt.preview(me, D1, DAYS5[-1]))
    check("不探数时 probe=False 且可用数留空（不假装查过）",
          pvn["probe"] is False and pvn["data_available"] is None, str(pvn["data_available"]))
    check("不探数时不拦（拦不拦要有依据，没依据就不许在 UI 上禁用按钮）",
          pvn["blocking"] is False, str(pvn["blocking"]))

    # ---- 门禁二：起始持仓在起始日没有行情 → 起跑必然被拦，预览就该先说 ----
    session = db.get_session()
    try:
        session.add(db.Position(
            user_id=me, book=1, stock_code="300750.SZ", stock_name="宁德时代",
            hold_qty=200, available_qty=200, frozen_qty=0, cost_price=180.0, updated_at=NOW,
        ))
        session.commit()
    finally:
        session.close()
    try:
        # 300750.SZ 整段取不到行情 → copy 模式会在 _basis 那里退回成本价，把历史浮盈算进收益
        bt.bd.load_bars = probe_stub_missing(["300750.SZ"])
        pvc = _with_days(lambda: bt.preview(me, D1, DAYS5[-1], probe=True, init_mode="copy"))
        check("★ copy 模式下持仓缺起始日行情 → 预览就标 blocking",
              pvc["blocking"] is True and pvc["missing_held"] == ["300750.SZ"],
              str(pvc["missing_held"]))
        check("拒因说清是持仓缺行情、并给出去处（不是干说一句「不行」）",
              "起始持仓" in pvc["block_reason"] and "空仓起步" in pvc["block_reason"],
              pvc["block_reason"])
        # 空仓起步时同一批数据是能跑的：没有起始持仓，就没有分母失真的问题
        pvk = _with_days(lambda: bt.preview(me, D1, DAYS5[-1], probe=True, init_mode="cash"))
        check("★ 同一批数据在「空仓起步」下不拦（门禁二是持仓专属的，不该误伤）",
              pvk["missing_held"] == [], str(pvk["missing_held"]))
    finally:
        bt.bd.load_bars = saved_load


# ------------------------------------------------------------- 12. 数据门禁


def gate_section() -> None:
    """起跑前的完整性门禁。**这是"静默样本替换"的唯一防线**：

    ``load_bars`` 对单只失败是记条 warning 然后继续，这在取数层是对的，但没人收口的话，
    用户看到的永远是"标的 45 只"而实际只有 40 只在跑——那不是回测，是换了样本却不说。
    """
    section("12. 起跑前的数据完整性门禁（缺得太多就不许跑）")

    full = {"A": {"d": 1}, "B": {"d": 1}}
    check("全部有行情 → 放行", bt._data_gate(["A", "B"], full) == "")
    check("缺 1/5 → 放行（为一只停牌票拦下一次十小时长跑是过度设计）",
          bt._data_gate(list("ABCDE"), {c: {"d": 1} for c in "ABCD"}) == "")
    # 判据是"**超过**一半"，边界值不能被悄悄挪成 >=
    check("恰好缺一半 → 仍放行（阈值是「超过一半」，边界不能悄悄挪）",
          bt._data_gate(list("ABCD"), {"A": {"d": 1}, "B": {"d": 1}}) == "")

    over = bt._data_gate(list("ABCDE"), {"A": {"d": 1}, "B": {"d": 1}})
    check("★ 缺 3/5（超过一半）→ 拒绝起跑，并说清还剩几只",
          "只剩 2 只可跑" in over and "5 只标的" in over, over)
    # 「票在、但整段一个 bar 都没有」必须和「票不在」判成同一件事。用 3 只（而不是 2 只）
    # 是因为 2 只时缺 1 只恰好是一半，落在"放行"一侧——那样这条断言测的是阈值边界，
    # 不是"空 dict 算不算有数据"。
    empty_msg = bt._data_gate(["A", "B", "C"], {"A": {"d": 1}, "B": {}, "C": {}})
    check("★ 「票在但整段没有 bar」也算缺失（空 dict 不能被当成有数据）",
          "只剩 1 只可跑" in empty_msg
          and empty_msg == bt._data_gate(["A", "B", "C"], {"A": {"d": 1}}),
          empty_msg)

    none_msg = bt._data_gate(["A", "B"], {})
    check("★ 一只都没有 → 拒绝，且必须点明它和「团队判断该空仓」在结果页上长得一样",
          "均无行情" in none_msg and "该空仓" in none_msg, none_msg)
    check("all_bars 为 None 时不抛异常（按全缺处理，不把 500 抛给用户）",
          "均无行情" in bt._data_gate(["A"], None))
    check("空标的池 → 拒绝并指向自选股",
          "标的池为空" in bt._data_gate([], {}), bt._data_gate([], {}))

    # ---- 取数失败的翻译：厂商原文不能直接甩给用户 ----
    # Wind 的**每日额度**耗尽长得像限流，但它落在 ``VendorRejectedError`` 上——``_with_retry``
    # 只认 ``VendorRateLimitError``，所以它不会被退避重试（对**次日才重置**的额度，重试是白等）。
    # 用户看到的会是 run.message 那一行，所以必须是能照做的话。
    def _caught(fn) -> str:
        try:
            fn()
        except ServiceError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"<<未翻译:{type(e).__name__}>> {e}"
        return "<<没抛>>"

    quota = _caught(lambda: _boom_through(bt._data_errors_as_user_text,
                                          VendorRejectedError(
                                              "Wind index_data/get_index_kline 返回：单日请求次数超限")))
    check("★ Wind 每日额度耗尽 → 译成中文，且点明「等次日」",
          "额度已用完" in quota and "次日" in quota, quota)
    check("额度的厂商原文保留在括号里（排查要用，但不能是唯一信息）",
          "单日请求次数超限" in quota, quota)

    auth = _caught(lambda: _boom_through(bt._data_errors_as_user_text,
                                         VendorRejectedError("鉴权失败：API key 无效")))
    check("鉴权类拒绝 → 不猜原因，原样保留厂商原文（猜错比不猜更耽误人）",
          "鉴权失败" in auth and "<<未翻译" not in auth, auth)

    nomd = _caught(lambda: _boom_through(bt._data_errors_as_user_text,
                                         NoMarketDataError("000300.SH", "000300.SH", "no rows")))
    # 原文保留在括号里是**故意**的（排查要用），所以不能断言"没有英文"——
    # 要断言的是**中文结论在前、厂商原文只是附注**。
    check("★ 基准整段取不到 → 中文给出「换一段区间」，厂商原文退成附注",
          nomd.startswith("交易日历取不到行情：所选区间内没有可用的 K 线")
          and "请换一段区间再试" in nomd and "No market data" in nomd, nomd)


def _boom_through(cm_factory, exc: Exception) -> None:
    """进 ``cm_factory(what)`` 再抛 ``exc``，用来断言这层翻译。"""
    with cm_factory("交易日历"):
        raise exc


# ------------------------------------------------------------- 13. 回测股票范围


def _seed_range_fixture() -> int:
    """摆一副"2 个有票的组 + 1 个空组 + 1 只持仓"的形状，返回 owner id。

    空组是刻意造的：分组名册能持久存在空组（``WatchlistGroup``），而"选了空组"与
    "选了一个不存在的组"是两种不同的空——前者合法、后者要如实说明。
    """
    fresh_db()
    uid = owner_id()
    session = db.get_session()
    try:
        for g in ("科技", "医药", "空组"):
            session.add(db.WatchlistGroup(user_id=uid, name=g, created_at=NOW))
        rows = [
            ("000001.SZ", "平安银行", "科技"),
            ("600002.SH", "齐鲁石化", "科技"),
            ("600519.SH", "贵州茅台", "医药"),
        ]
        for code, name, grp in rows:
            session.add(db.Watchlist(
                user_id=uid, stock_code=code, stock_name=name, source=0,
                group_name=grp, created_at=NOW,
            ))
        # 真实托管簿的一只持仓，且它在自选股里（copy 模式要验"已在内就不重复并入"）
        session.add(db.Position(
            user_id=uid, book=1, stock_code="600519.SH", stock_name="贵州茅台",
            hold_qty=100, available_qty=100, frozen_qty=0, cost_price=1500.0, updated_at=NOW,
        ))
        # 一只**不在**任何自选分组里的持仓：copy 模式必须把它并进来
        session.add(db.Position(
            user_id=uid, book=1, stock_code="601398.SH", stock_name="工商银行",
            hold_qty=500, available_qty=500, frozen_qty=0, cost_price=5.0, updated_at=NOW,
        ))
        session.commit()
    finally:
        session.close()
    return uid


def range_section() -> None:
    section("13. 回测股票范围：解析、去重、自动并入、以及预览与起跑同源")

    uid = _seed_range_fixture()

    # ---- 甲：持仓股 ----
    r = bt.resolve_range(uid, bt.BT_SCOPE_HOLDINGS)
    check("范围=持仓股 ⇒ 只有托管簿的两只持仓",
          r["codes"] == ["600519.SH", "601398.SH"], str(r["codes"]))
    check("范围=持仓股 ⇒ 标签写明「持仓股」与只数",
          r["label"].startswith("持仓股（2 只）"), r["label"])

    # ---- 乙：自选股 · 不选分组 = 全部分组 ----
    r = bt.resolve_range(uid, bt.BT_SCOPE_WATCHLIST)
    check("范围=自选股且不选分组 ⇒ 全部分组（保序去重）",
          r["codes"] == ["000001.SZ", "600002.SH", "600519.SH"], str(r["codes"]))
    check("不选分组的标签是「全部分组」", "全部分组" in r["label"], r["label"])

    # ---- 丙：选单组 / 多组 ----
    r = bt.resolve_range(uid, bt.BT_SCOPE_WATCHLIST, ["科技"])
    check("选「科技」⇒ 只有科技组的 2 只",
          r["codes"] == ["000001.SZ", "600002.SH"], str(r["codes"]))
    check("单组标签直接列组名", "科技" in r["label"] and "2 只" in r["label"], r["label"])

    r = bt.resolve_range(uid, bt.BT_SCOPE_WATCHLIST, ["科技", "医药"])
    check("多组并集去重（600519.SH 只出现一次）",
          r["codes"] == ["000001.SZ", "600002.SH", "600519.SH"], str(r["codes"]))
    check("两组时标签把组名列全（不缩写）",
          r["label"] == "自选股 · 科技、医药（3 只）", r["label"])

    r = bt.resolve_range(uid, bt.BT_SCOPE_WATCHLIST, ["科技", "医药", "空组"])
    check("三个组以上才缩写，免得标签长到没人读",
          r["label"].startswith("自选股 · 科技、医药 等 3 组"), r["label"])

    r = bt.resolve_range(uid, bt.BT_SCOPE_WATCHLIST, ["空组"])
    check("选一个**存在但为空**的组 ⇒ 空池（合法，由调用方报错）",
          r["codes"] == [] and r["missing_groups"] == [], str(r))

    # 这一条是本节的要害：分组被删掉之后**不能悄悄退回全部分组**——那样用户以为只跑了
    # 科技组，实际跑的是整个自选股，而界面上没有任何异常。
    r = bt.resolve_range(uid, bt.BT_SCOPE_WATCHLIST, ["已删除的组"])
    check("★ 选了一个**已不存在**的组 ⇒ 空池，绝不退回全部分组",
          r["codes"] == [] and r["missing_groups"] == ["已删除的组"], str(r))
    check("★ 已删除的组在标签里如实说明",
          "已不存在" in r["label"] and "已跳过" in r["label"], r["label"])

    r = bt.resolve_range(uid, bt.BT_SCOPE_WATCHLIST, ["科技", "已删除的组"])
    check("部分组已删 ⇒ 留下的组照常出票，同时如实说明",
          r["codes"] == ["000001.SZ", "600002.SH"]
          and r["missing_groups"] == ["已删除的组"], str(r))

    # ---- 丁：copy 模式的自动并入 ----
    r = bt.resolve_range(uid, bt.BT_SCOPE_WATCHLIST, ["科技"], init_mode="copy")
    check("copy + 只选科技组 ⇒ 范围外的两只持仓被自动并入",
          r["codes"] == ["000001.SZ", "600002.SH", "600519.SH", "601398.SH"], str(r["codes"]))
    check("并入清单只列**范围外**的持仓（医药组的 600519.SH 与组外的 601398.SH）",
          r["merged"] == ["600519.SH", "601398.SH"], str(r["merged"]))
    check("并入数量写进标签，用户看得到",
          "自动并入 2 只" in r["label"], r["label"])

    r = bt.resolve_range(uid, bt.BT_SCOPE_HOLDINGS, init_mode="copy")
    check("copy + 范围=持仓股 ⇒ 没有需要并入的（范围本来就是全部持仓）",
          r["merged"] == [] and r["codes"] == ["600519.SH", "601398.SH"], str(r))

    r = bt.resolve_range(uid, bt.BT_SCOPE_WATCHLIST, ["医药"], init_mode="copy")
    check("★ 已在范围内的持仓不重复并入（600519.SH 在医药组里）",
          r["merged"] == ["601398.SH"] and r["codes"].count("600519.SH") == 1, str(r))

    check("cash 模式不做任何并入",
          bt.resolve_range(uid, bt.BT_SCOPE_WATCHLIST, ["科技"], "cash")["merged"] == [])

    # ---- 戊：预览与起跑同源 ----
    pv = _with_days(lambda: bt.preview(uid, D1, DAYS5[-1], bt_scope=bt.BT_SCOPE_WATCHLIST,
                                       bt_groups=["科技"]))
    check("★ 预览的 universe == resolve_range 的结果（预览说 2 只就真跑 2 只）",
          pv["universe"] == ["000001.SZ", "600002.SH"], str(pv["universe"]))
    check("预览回报 range_label", "科技" in pv["range_label"], pv["range_label"])
    check("预览回报 merged_holdings（供结果页如实标注）",
          pv["merged_holdings"] == [], str(pv["merged_holdings"]))

    pv_copy = _with_days(lambda: bt.preview(uid, D1, DAYS5[-1], init_mode="copy",
                                            bt_scope=bt.BT_SCOPE_WATCHLIST,
                                            bt_groups=["科技"]))
    check("★ copy 模式下预览的标的池也含被并入的持仓",
          pv_copy["universe"] == ["000001.SZ", "600002.SH", "600519.SH", "601398.SH"],
          str(pv_copy["universe"]))
    check("copy 模式的预览如实回报并入清单",
          pv_copy["merged_holdings"] == ["600519.SH", "601398.SH"],
          str(pv_copy["merged_holdings"]))

    # ---- 己：不传 bt_scope = 旧行为（零回归门） ----
    pv_old = _with_days(lambda: bt.preview(uid, D1, DAYS5[-1]))
    check("★ 不传 bt_scope ⇒ 沿用旧行为（在管全集），既有调用零回归",
          pv_old["universe"] == ["600519.SH", "601398.SH", "000001.SZ", "600002.SH"],
          str(pv_old["universe"]))
    check("旧路径不编造 range_label / merged",
          pv_old["range_label"] == "" and pv_old["merged_holdings"] == [],
          str((pv_old["range_label"], pv_old["merged_holdings"])))

    # ---- 庚：范围解析结果会冻进 run 的 config ----
    rid = bt.create_run(
        uid, start_date=D1, end_date=D2, init_mode="cash", init_cash=100000.0,
        universe=["000001.SZ", "600002.SH"],
        extra_config={"bt_scope": 1, "bt_groups": ["科技"],
                      "bt_names": {"000001.SZ": "平安银行"},
                      "bt_merged": [], "bt_range_label": "自选股 · 科技（2 只）"},
    )
    frozen = bt.get_run(rid)["config"]
    check("范围选择冻进了 config_json（bt_scope / bt_groups）",
          frozen["bt_scope"] == 1 and frozen["bt_groups"] == ["科技"], str(frozen))
    check("股票名冻结在 bt_names 里（_drive 取名字用）",
          frozen["bt_names"] == {"000001.SZ": "平安银行"}, str(frozen.get("bt_names")))

    # ---- 辛：自动并入必须**逐只**出现在声明里，而不是一句笼统的"部分持仓已并入" ----
    # 声明这种东西，含糊就等于没有：用户需要对得上账的恰恰是"哪几只"。
    plain = calc.disclosures_for(frozen)
    check("没有并入时不出这一条声明",
          all(d["key"] != "range_merged" for d in plain), str([d["key"] for d in plain]))
    merged_list = ["600519.SH", "601398.SH"]
    with_merged = calc.disclosures_for({**frozen, "bt_merged": merged_list})
    row = next((d for d in with_merged if d["key"] == "range_merged"), None)
    check("★ 有并入时逐只列出（不是只说'部分持仓'）",
          row is not None and all(c in row["text"] for c in merged_list)
          and str(len(merged_list)) in row["text"],
          (row or {}).get("text", "(没有这条)"))
    check("自动并入声明说的是本次真实选的范围（来自冻结的 bt_scope）",
          row is not None and "自选股" in row["text"], (row or {}).get("text", ""))


# ------------------------------------------------------------- main


def main() -> int:
    print("回测计算层与断点续跑冒烟：纯算术 + 内存 SQLite 上的真实检查点")
    realized_section()
    drawdown_section()
    checkpoint_json_section()
    restore_section()
    basis_section()
    rescale_section()
    baseline_section()
    index_section()
    aggregate_section()
    validate_section()
    orchestration_section()
    api_section()
    gate_section()
    range_section()

    print()
    if _FAILS:
        print(f"❌ 共 {_COUNT} 项，失败 {len(_FAILS)} 项：")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
