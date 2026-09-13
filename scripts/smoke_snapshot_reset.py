"""重贴持仓快照 = 重建托管簿（清空流水/计划 + 覆盖持仓 + 关托管开关）的离线回归。

不连 Postgres、不连 Wind、不跑 LLM：把 ``app.db`` 的 SessionLocal 换成绑在**内存 SQLite** 上的
sessionmaker，用真实的 ``snapshot_trust_book`` / ``reset_trust`` 打真实的库。

为什么能这么干：``app/db.py`` 的模型只用 Integer/String/Float/Text/Boolean/ForeignKey，没有
PG 专有类型，``Base.metadata.create_all`` 在 SQLite 上直接可用。**不要**调 ``db.init_db()``
——里面的 ``_ensure_schema`` 是 PG 方言（ADD COLUMN IF NOT EXISTS / pg_constraint）。

用法（在 product/trade_master 下）：
    .venv/bin/python scripts/smoke_snapshot_reset.py
"""
import os
import sys

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
# get_session() 在**调用时**查模块属性 SessionLocal，所以这里 patch 立即生效
db.SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)  # type: ignore[assignment]

from app import trust  # noqa: E402  （必须在 patch 之后）

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


# ---------------------------------------------------------------- 夹具
UID = 1


def wipe() -> None:
    """清空全库，回到干净态（每个场景之间调用）。"""
    s = db.get_session()
    try:
        for model in (db.Trade, db.Order, db.TrustPlan, db.Position, db.TrustConfig, db.User):
            s.query(model).delete()
        s.commit()
    finally:
        s.close()


def seed_config(**kw) -> None:
    s = db.get_session()
    try:
        s.add(db.User(id=UID, phone="13800000000", created_at=0.0))
        cfg = db.TrustConfig(
            user_id=UID,
            is_active=kw.get("is_active", True),
            book_created=kw.get("book_created", True),
            available_cash=kw.get("available_cash", 0.0),
            broker_mv=kw.get("broker_mv"),
            broker_pnl=kw.get("broker_pnl"),
            updated_at=0.0,
        )
        s.add(cfg)
        s.commit()
    finally:
        s.close()


def seed_position(code: str, name: str, qty: int, cost: float) -> None:
    s = db.get_session()
    try:
        s.add(db.Position(
            user_id=UID, book=1, stock_code=code, stock_name=name,
            hold_qty=qty, available_qty=qty, frozen_qty=0, cost_price=cost, updated_at=0.0,
        ))
        s.commit()
    finally:
        s.close()


def seed_trade(n: int) -> None:
    s = db.get_session()
    try:
        for i in range(n):
            s.add(db.Trade(
                trade_id=f"T{i}", order_id=f"O{i}", user_id=UID, stock_code="600519.SH",
                stock_name="贵州茅台", direction=1, price=1700.0, quantity=100,
                amount=170000.0, fee=51.0, ai_reason="测试", traded_at=0.0,
            ))
        s.commit()
    finally:
        s.close()


def seed_order(n: int) -> None:
    s = db.get_session()
    try:
        for i in range(n):
            s.add(db.Order(
                order_id=f"O{i}", user_id=UID, stock_code="600519.SH", stock_name="贵州茅台",
                direction=1, price=1700.0, quantity=100, status=1, created_at=0.0,
            ))
        s.commit()
    finally:
        s.close()


def seed_plan(n: int) -> None:
    s = db.get_session()
    try:
        for i in range(n):
            s.add(db.TrustPlan(
                user_id=UID, trade_date=f"2026-09-{10 + i:02d}",
                plan_json='{"actions": []}', created_at=0.0,
            ))
        s.commit()
    finally:
        s.close()


def counts() -> dict:
    """统计四张表的行数 + 托管簿持仓代码。"""
    s = db.get_session()
    try:
        cfg = s.query(db.TrustConfig).filter(db.TrustConfig.user_id == UID).first()
        return {
            "trades": s.query(db.Trade).filter(db.Trade.user_id == UID).count(),
            "orders": s.query(db.Order).filter(db.Order.user_id == UID).count(),
            "plans": s.query(db.TrustPlan).filter(db.TrustPlan.user_id == UID).count(),
            "positions": sorted(
                p.stock_code
                for p in s.query(db.Position).filter(
                    db.Position.user_id == UID, db.Position.book == 1
                ).all()
            ),
            "book_created": bool(cfg.book_created),
            "is_active": bool(cfg.is_active),
            "available_cash": cfg.available_cash,
            "broker_mv": cfg.broker_mv,
            "broker_pnl": cfg.broker_pnl,
        }
    finally:
        s.close()


NEW_ROWS = [
    {"code": "518880", "name": "黄金ETF华安", "qty": 9500, "cost_price": 9.0},
    {"code": "600519", "name": "贵州茅台", "qty": 100, "cost_price": 1750.0},
    {"code": "300750", "name": "宁德时代", "qty": 500, "cost_price": 215.0},
]


def main() -> int:  # noqa: C901 —— 冒烟脚本，线性罗列各场景
    # ---------------------------------------------------------------- 1. 重贴
    section("1. 重贴快照：自动清空流水/计划 + 覆盖持仓")
    wipe()
    seed_config(is_active=True, book_created=True, available_cash=0.0)
    seed_position("000001.SZ", "平安银行", 1000, 11.0)
    seed_position("601318.SH", "中国平安", 200, 45.0)
    seed_trade(2)
    seed_order(2)
    seed_plan(1)

    cleared = trust.snapshot_trust_book(UID, NEW_ROWS, cash=123456.78, reset_snapshot=True)
    c = counts()
    check("不再报「已有成交记录」", True)
    check("回执计数正确", cleared == {"orders": 2, "trades": 2, "plans": 1}, str(cleared))
    check("成交记录已清空", c["trades"] == 0, str(c["trades"]))
    check("委托记录已清空", c["orders"] == 0, str(c["orders"]))
    check("次日行动计划已清空（监控条件随之清空）", c["plans"] == 0, str(c["plans"]))
    check("持仓被新快照覆盖（旧票不留、只留新的 3 只）",
          c["positions"] == ["300750.SZ", "518880.SH", "600519.SH"], str(c["positions"]))
    check("可用资金按新快照写入", c["available_cash"] == 123456.78, str(c["available_cash"]))
    check("仍处于已建簿状态", c["book_created"] is True)

    # ---------------------------------------------------------------- 2. 开关
    section("2. 重贴会关掉托管开关（堵住按旧评级对新仓位下单）")
    check("is_active 被关闭", c["is_active"] is False, str(c["is_active"]))
    res = trust.run_execution(UID)
    check("执行层随即停手（skipped=not_active）",
          res.get("skipped") == "not_active", str(res))

    # ---------------------------------------------------------------- 3. 快照口径
    section("3. 券商快照口径")
    check("reset_snapshot=True 清空快照口径",
          c["broker_mv"] is None and c["broker_pnl"] is None,
          f"{c['broker_mv']}/{c['broker_pnl']}")

    wipe()
    seed_config(is_active=True, book_created=True, broker_mv=1.0, broker_pnl=2.0)
    trust.snapshot_trust_book(UID, NEW_ROWS, cash=1000.0, mv=88888.0, pnl=-1234.5)
    c = counts()
    check("手动校准的快照口径被写入",
          c["broker_mv"] == 88888.0 and c["broker_pnl"] == -1234.5,
          f"{c['broker_mv']}/{c['broker_pnl']}")

    # ---------------------------------------------------------------- 4. 首次建簿
    section("4. 首次建簿：零回归（没有东西可清）")
    wipe()
    seed_config(is_active=False, book_created=False, available_cash=0.0)
    cleared = trust.snapshot_trust_book(UID, NEW_ROWS, cash=5000.0)
    c = counts()
    check("计数全 0、不报错", cleared == {"orders": 0, "trades": 0, "plans": 0}, str(cleared))
    check("持仓写入 3 只", len(c["positions"]) == 3, str(c["positions"]))
    check("book_created 置真", c["book_created"] is True)

    # ---------------------------------------------------------------- 5. 空仓启动
    section("5. 空仓启动（50 万）")
    wipe()
    seed_config(is_active=True, book_created=True, available_cash=0.0)
    seed_position("600519.SH", "贵州茅台", 100, 1750.0)
    seed_trade(1)
    seed_plan(1)
    cleared = trust.snapshot_trust_book(UID, [], cash=500000.0)
    c = counts()
    check("持仓被清空（空仓启动）", c["positions"] == [], str(c["positions"]))
    check("现金 = 50 万", c["available_cash"] == 500000.0, str(c["available_cash"]))
    check("流水与计划一并清空",
          cleared == {"orders": 0, "trades": 1, "plans": 1}, str(cleared))
    check("开关被关闭", c["is_active"] is False)

    # ---------------------------------------------------------------- 6. reset
    section("6. reset_trust：补上原先漏掉的计划清空")
    wipe()
    seed_config(is_active=True, book_created=True, available_cash=0.0, broker_mv=1.0, broker_pnl=2.0)
    seed_position("600519.SH", "贵州茅台", 100, 1750.0)
    seed_trade(3)
    seed_order(4)
    seed_plan(2)
    cleared = trust.reset_trust(UID)
    c = counts()
    check("回执计数正确", cleared == {"orders": 4, "trades": 3, "plans": 2}, str(cleared))
    check("次日行动计划被清空（原先的缺口）", c["plans"] == 0, str(c["plans"]))
    check("成交/委托被清空", c["trades"] == 0 and c["orders"] == 0)
    check("回到未建簿态", c["book_created"] is False and c["positions"] == [])
    check("快照口径被清掉", c["broker_mv"] is None and c["broker_pnl"] is None)
    check("开关被关闭", c["is_active"] is False)

    # ---------------------------------------------------------------- 7. 边界
    section("7. 边界：脏行沿用旧行为")
    wipe()
    seed_config(is_active=False, book_created=False)
    trust.snapshot_trust_book(UID, [
        {"code": "600519", "name": "贵州茅台", "qty": 100, "cost_price": 1750.0},
        {"code": "600519", "name": "贵州茅台", "qty": 100, "cost_price": 1750.0},  # 重复代码
        {"code": "000001", "name": "平安银行", "qty": 0, "cost_price": 11.0},      # 股数 0
        {"code": "300750", "name": "宁德时代", "qty": -5, "cost_price": 215.0},    # 负数
    ], cash=100.0)
    c = counts()
    check("重复代码去重、非法股数跳过 → 只留 1 只", c["positions"] == ["600519.SH"], str(c["positions"]))

    # ---------------------------------------------------------------- 汇总
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
