"""本簿已实现盈亏（``TrustConfig.realized_pnl``）的离线回归与**交叉核对**。

不连 Postgres、不连 Wind、不跑 LLM：把 ``app.db`` 的 SessionLocal 换成绑在**内存 SQLite** 上的
sessionmaker，用真实的 ``trade.place_order`` 打真实的库。

**这个脚本的价值在"对账"**：已实现盈亏有两条互相独立的算法——

  1. 生产路径：``trade.place_order`` 卖出分支里的**增量计数器**（信任它会漏记/多记才需要测）；
  2. 独立参照：``backtest_calc.replay_realized`` 的**逐笔重放**（回测在用，与 1 无共享代码）。

两条路必须给出同一个数。任何一处口径漂移（忘了扣费、卖了没动成本、清仓时把成本清零的
时机不对）都会让它们分家——这是单看任何一边都发现不了的。

覆盖：
  1. 单笔清仓 / 部分卖出 / 加权平均成本 / 多次累积 / 亏损 —— 每条都与 replay_realized 对账
  2. 带**快照基座成本**的簿：计数器用持仓成本价，重放用 initial_positions，两者必须一致
  3. 重贴快照 / 重置托管 → 归零（``_clear_book_runtime`` 是唯一收敛点）
  4. ``get_analytics``：有值返回数字、**老簿返回 None 而不是 0**（两种"没有数字"含义不同）

用法（在 product/trade_master 下）：
    .venv/bin/python scripts/smoke_realized_pnl.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
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
# get_session() 在**调用时**查模块属性 SessionLocal，所以这里 patch 立即生效
db.SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)  # type: ignore[assignment]

from app import backtest_calc, trade, trust  # noqa: E402  （必须在 patch 之后）

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


# ---------------------------------------------------------------- 假 Wind
# place_order 只在 price=None 时才取行情；本脚本恒显式传 price，所以这里只为
# to_wind_code 与 _check_limit 的 K 线调用存在。
#
# **昨收必须跟着成交价走**：_check_limit 判的是「成交价是否到板价」，昨收写死 100 而
# 成交价 12 等于「跌了 88%」，会被正确地判成跌停。买卖前把昨收设成与成交价相同
# （涨跌 0%），本脚本要测的是盈亏记账，不是涨跌停——那条在 smoke_limit_rules.py 里测。
LIVE: dict[str, float] = {"prev_close": 100.0}


class FakeWind:
    def to_wind_code(self, code: str) -> str:
        return code

    def get_company_name(self, code: str) -> str:
        return ""

    def get_wind_ohlcv(self, code, start, end, period="10"):
        p = LIVE["prev_close"]
        return pd.DataFrame({"Close": [p, p]})


trade._wind = FakeWind()  # type: ignore[assignment]

# ---------------------------------------------------------------- 夹具
UID = 1


def wipe() -> None:
    s = db.get_session()
    try:
        for model in (db.Trade, db.Order, db.TrustPlan, db.Position, db.TrustConfig, db.User):
            s.query(model).delete()
        s.commit()
    finally:
        s.close()


def seed(cash: float = 100000.0, **cfg_kw) -> None:
    """建用户 + 托管配置。``realized_pnl`` 默认不传 → NULL（模拟"老簿"）。"""
    s = db.get_session()
    try:
        s.add(db.User(id=UID, phone="13800000000", created_at=0.0))
        kwargs = {
            "user_id": UID, "is_active": True, "book_created": True,
            "available_cash": cash, "updated_at": 0.0,
        }
        kwargs.update(cfg_kw)
        s.add(db.TrustConfig(**kwargs))
        s.commit()
    finally:
        s.close()


def cfg_field(name: str):
    s = db.get_session()
    try:
        return getattr(s.query(db.TrustConfig).filter(db.TrustConfig.user_id == UID).first(), name)
    finally:
        s.close()


def set_cfg(**kw) -> None:
    s = db.get_session()
    try:
        cfg = s.query(db.TrustConfig).filter(db.TrustConfig.user_id == UID).first()
        for k, v in kw.items():
            setattr(cfg, k, v)
        s.commit()
    finally:
        s.close()


def feed() -> list[dict]:
    """按成交先后（id 升序）取出成交流水，喂给 replay_realized 的形状。"""
    s = db.get_session()
    try:
        rows = s.query(db.Trade).filter(db.Trade.user_id == UID).order_by(db.Trade.id).all()
        return [
            {"stock_code": r.stock_code, "direction": r.direction, "price": r.price,
             "quantity": r.quantity, "fee": r.fee}
            for r in rows
        ]
    finally:
        s.close()


def buy(code: str, qty: int, price: float, name: str = "测试股") -> dict:
    LIVE["prev_close"] = price      # 行情与成交价同步 → 涨跌 0%，不撞涨跌停
    return trade.place_order(UID, code, name, 0, qty, price=price)


def sell(code: str, qty: int, price: float, name: str = "测试股") -> dict:
    LIVE["prev_close"] = price
    return trade.place_order(UID, code, name, 1, qty, price=price)


def both(code_base: str = "600519.SH", initial: list[dict] | None = None):
    """返回 (计数器值, 重放值)，供逐条对账。"""
    return cfg_field("realized_pnl"), backtest_calc.replay_realized(initial or [], feed())


def assert_agree(label: str, initial: list[dict] | None = None) -> None:
    counter, replay = both(initial=initial)
    check(f"{label}：计数器 == 重放（{counter} / {replay}）",
          counter == replay, f"counter={counter} replay={replay}")


# ---------------------------------------------------------------- 1. 空仓起步
def test_from_empty_book() -> None:
    section("空仓起步：计数器与逐笔重放逐条对账")

    # ---- 单笔清仓 ----
    wipe(); seed(cash=100000.0)
    buy("600519.SH", 100, 10.0)
    check("纯买入不动已实现盈亏（首卖前是 NULL，不是被写成一个数）",
          cfg_field("realized_pnl") is None, str(cfg_field("realized_pnl")))
    trade.release_t1(UID)                      # 否则 T+1 冻结卖不出去
    r = sell("600519.SH", 100, 12.0)
    # 卖 1200：佣金 0.3 → 不足 5 元取 5；印花税 1200*0.0005=0.6 → fee=5.6
    check("单笔清仓：fee == 5.6（免五关闭时最低 5 元 + 印花税）", r["fee"] == 5.6, str(r["fee"]))
    check("单笔清仓：(12-10)*100 - 5.6 == 194.4",
          cfg_field("realized_pnl") == 194.4, str(cfg_field("realized_pnl")))
    assert_agree("单笔清仓")
    check("清仓后持仓行被删（但不影响已实现盈亏留存）",
          _pos_qty("600519.SH") == 0)

    # ---- 多次卖出累积 ----
    wipe(); seed(cash=100000.0)
    buy("600519.SH", 200, 10.0)
    trade.release_t1(UID)
    sell("600519.SH", 100, 12.0)          # 盈：(12-10)*100 - 5.6 = 194.4
    first = cfg_field("realized_pnl")
    r2 = sell("600519.SH", 100, 9.0)      # 亏：(9-10)*100 - 5.45 = -105.45
    second = cfg_field("realized_pnl")
    # 第二笔选**亏本**卖，是为了让这条断言真的能区分「累加」与「覆盖」：
    # 若写成覆盖，second 会是 -105.45 而不是 194.4-105.45。
    check("第二次卖出是在第一次基础上累加（不是覆盖，且亏损会抵减）",
          second == round(first + (9.0 - 10.0) * 100 - r2["fee"], 2) and second < first,
          f"first={first} second={second} fee={r2['fee']}")
    check("累加值 == 194.4 - 105.45 == 88.95", second == 88.95, str(second))
    assert_agree("多次卖出累积")

    # ---- 亏损为负 ----
    wipe(); seed(cash=100000.0)
    buy("600519.SH", 100, 20.0)
    trade.release_t1(UID)
    r = sell("600519.SH", 100, 15.0)
    check("亏损记成负数（(15-20)*100 - fee == -505.75）",
          cfg_field("realized_pnl") == round(-500 - r["fee"], 2), str(cfg_field("realized_pnl")))
    assert_agree("亏损")


def _pos_qty(code: str) -> int:
    s = db.get_session()
    try:
        p = (s.query(db.Position)
             .filter(db.Position.user_id == UID, db.Position.book == 1,
                     db.Position.stock_code == code).first())
        return p.hold_qty if p else 0
    finally:
        s.close()


# ---------------------------------------------------------------- 2. 加权成本 / 基座
def test_weighted_and_base() -> None:
    section("加权平均成本 + 快照基座成本：计数器与重放必须同源")

    # ---- 两笔买入 → 加权成本，再部分卖出 ----
    wipe(); seed(cash=100000.0)
    buy("600519.SH", 100, 10.0)
    trade.release_t1(UID)
    buy("600519.SH", 100, 14.0)     # 成本 → (1000+1400)/200 = 12.0
    trade.release_t1(UID)
    check("加权成本价 == 12.0", _pos_cost("600519.SH") == 12.0, str(_pos_cost("600519.SH")))
    sell("600519.SH", 200, 13.0)
    # (13-12)*200 = 200；卖 2600：佣金 0.65→5，印花 1.3 → fee 6.3 → 193.7
    check("加权成本下 (13-12)*200 - 6.3 == 193.7",
          cfg_field("realized_pnl") == 193.7, str(cfg_field("realized_pnl")))
    assert_agree("加权成本部分卖出")

    # ---- 快照基座：簿不是从 0 建起的，计数器用持仓成本价 ----
    wipe(); seed(cash=50000.0)
    trust.snapshot_trust_book(UID, [
        {"code": "600519.SH", "name": "贵州茅台", "qty": 100, "cost_price": 8.0},
    ], cash=50000.0)
    check("快照建簿后已实现盈亏归零（新簿的累计确实是 0）",
          cfg_field("realized_pnl") == 0.0, str(cfg_field("realized_pnl")))
    sell("600519.SH", 100, 12.0)
    # (12-8)*100 = 400；卖 1200 → fee 5.6 → 394.4。参照侧必须给同样的基座。
    base = [{"stock_code": "600519.SH", "hold_qty": 100, "cost_price": 8.0}]
    check("带基座成本 (12-8)*100 - 5.6 == 394.4",
          cfg_field("realized_pnl") == 394.4, str(cfg_field("realized_pnl")))
    assert_agree("快照基座卖出一半", initial=base)

    # 基座只卖一半：另 100 股保留
    wipe(); seed(cash=50000.0)
    trust.snapshot_trust_book(UID, [
        {"code": "600519.SH", "name": "贵州茅台", "qty": 200, "cost_price": 8.0},
    ], cash=50000.0)
    sell("600519.SH", 100, 12.0)
    assert_agree("快照基座部分卖出（剩余持仓的成本价不被卖出改动）",
                 initial=[{"stock_code": "600519.SH", "hold_qty": 200, "cost_price": 8.0}])


def _pos_cost(code: str) -> float | None:
    s = db.get_session()
    try:
        p = (s.query(db.Position)
             .filter(db.Position.user_id == UID, db.Position.book == 1,
                     db.Position.stock_code == code).first())
        return p.cost_price if p else None
    finally:
        s.close()


# ---------------------------------------------------------------- 3. 归零
def test_zeroing() -> None:
    section("重贴快照 / 重置托管 → 归零（_clear_book_runtime 是唯一收敛点）")
    for label, act in (
        ("重贴快照", lambda: trust.snapshot_trust_book(
            UID, [{"code": "600519.SH", "name": "贵州茅台", "qty": 100, "cost_price": 8.0}],
            cash=50000.0)),
        ("重置托管", lambda: trust.reset_trust(UID)),
    ):
        wipe(); seed(cash=100000.0)
        buy("600519.SH", 100, 10.0)
        trade.release_t1(UID)
        sell("600519.SH", 100, 12.0)
        before = cfg_field("realized_pnl")
        check(f"{label}前确实有累计值（{before}）", before == 194.4, str(before))
        act()
        check(f"{label}后归零 == 0.0（**不是** NULL）",
              cfg_field("realized_pnl") == 0.0, str(cfg_field("realized_pnl")))


# ---------------------------------------------------------------- 4. get_analytics
def test_get_analytics() -> None:
    section("get_analytics：数字 / NULL 两种状态如实透出")

    # ---- 有值 ----
    wipe(); seed(cash=100000.0)
    buy("600519.SH", 100, 10.0)
    trade.release_t1(UID)
    sell("600519.SH", 100, 12.0)
    a = trust.get_analytics(UID)
    check("有累计值时 realized_pnl 是数字", a["realized_pnl"] == 194.4, str(a["realized_pnl"]))
    check("有数值时 note 为空串（不该再挂「暂未统计」）", a["realized_pnl_note"] == "",
          repr(a["realized_pnl_note"]))
    check("trade_count 与流水条数一致", a["trade_count"] == len(feed()), str(a["trade_count"]))

    # ---- 老簿（本列上线前建的 → NULL）----
    wipe(); seed(cash=100000.0)
    set_cfg(realized_pnl=None)
    a = trust.get_analytics(UID)
    check("★ 老簿返回 None，**不是 0**（0 会被读成「确实没赚没亏」）",
          a["realized_pnl"] is None, repr(a["realized_pnl"]))
    check("老簿的 note 说清为什么、以及怎么启用", "重贴" in a["realized_pnl_note"],
          a["realized_pnl_note"])

    # ---- 空簿（真的是 0）与老簿必须区分 ----
    wipe(); seed(cash=100000.0)
    trust.snapshot_trust_book(UID, [], cash=100000.0)
    a = trust.get_analytics(UID)
    check("★ 新建的空簿是 0.0 且 note 为空——与上面的 NULL 明确不同",
          a["realized_pnl"] == 0.0 and a["realized_pnl_note"] == "",
          f'{a["realized_pnl"]!r} / {a["realized_pnl_note"]!r}')


def main() -> int:
    test_from_empty_book()
    test_weighted_and_base()
    test_zeroing()
    test_get_analytics()

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
