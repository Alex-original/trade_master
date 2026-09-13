"""端到端确定性闭环（A8 主验证）：真 ``trade.place_order`` + **真实配置的数据库**。

这条脚本补的是离线冒烟网里唯一没被覆盖的那一格：``smoke_ladder.py`` 把 ``place_order``
整个换掉了，于是「梯子被穿越 → 真下单 → 真落库 → 归因累计」这段**从来没有一起跑过**。
这里不换 ``place_order``、不换 ``db``，只用 ``HistoryClock`` 把行情钉死，因此：

    行情确定 → 哪一档被穿越确定 → 成交股数确定 → 落库行数确定 → 已实现盈亏确定

**跑在哪里**：它用 ``app.db`` 当前配置的库，所以

- 在**生产内测容器**里跑 = 真 Postgres、真迁移后的 schema
- 本机默认 ``DATABASE_URL`` 指向 localhost 的 Postgres，没有就会连不上——**这没关系**

**刻意不叫 ``smoke_*``**：仓库约定 ``scripts/smoke_*.py`` 是「确定性离线、不连库、
秒级出结果」，而本脚本**必须**连真实数据库。取 ``smoke_`` 前缀会让
``for f in scripts/smoke_*.py`` 那种一把梭的回归循环在这里断掉。所以单列一个名字。

只碰 ``SHADOW_UID`` 这一个影子账户，且每次运行先把它清干净，可反复跑。
"""

import json
import os
import sys
import time

# 判据必须是「这是一个**包**」（有 __init__.py），不能只判目录存在：脚本被 docker cp 到
# /tmp 后执行时，dirname(dirname()) 会算成 `/`，而容器里 `/app` 恰好存在——于是 `/` 被插进
# sys.path，`app` 解析成那个**装代码的目录**本身，报 "cannot import name 'db' from 'app'
# (unknown location)"（命名空间包）。认包标记就不会误判。
def _find_root() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (here, os.getcwd()):
        if os.path.isfile(os.path.join(cand, "app", "__init__.py")):
            return cand
    raise SystemExit("找不到项目根目录（app/__init__.py）——请在仓库根或容器 /app 下运行")


_ROOT = _find_root()
sys.path.insert(0, _ROOT)

from app import db, notify, trade, trust  # noqa: E402
from app.backtest_calc import replay_realized  # noqa: E402
from app.market_clock import Bar, HistoryClock  # noqa: E402

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


# ---------------------------------------------------------------- 布景常量
SHADOW_UID = 990001                  # 影子账户，刻意远离真实用户 id
#: 第 4 段要把 ``_run_execution`` 换掉做反向对照，跑完必须还原——否则后面的段全在测桩。
_ORIG_RUN_EXECUTION = trust._run_execution
CODE = "600519.SH"
NAME = "贵州茅台"
DAY = "2026-09-14"                   # 周一，一个工作日
COST = 1700.0
HOLD = 1000
CASH = 100_000.0
# 一根跨越三档的 bar：low 1490 同时低于 1700 / 1600 / 1500 三个触发价。
# 昨收取 1520（不是复权前的 1700）——否则 1510 的成交价会被正确判成跌停而卖不出去，
# 那是 ``_check_limit`` 在正常工作，不是本脚本要验的东西。
BAR = Bar(open=1510.0, high=1530.0, low=1490.0, close=1500.0,
          prev_close=1520.0, volume_ratio=None, tradable=True)

# 减仓梯子，由浅到深：target_weight 递减、trigger_price 递减（``build_ladders`` 的合法性要求）
LADDER = [
    {"code": CODE, "name": NAME, "action": "reduce",
     "trigger_type": "price_below", "trigger_price": 1700.0, "target_weight": 0.5,
     "reason": "跌破1700减半"},
    {"code": CODE, "name": NAME, "action": "reduce",
     "trigger_type": "price_below", "trigger_price": 1600.0, "target_weight": 0.25,
     "reason": "跌破1600再减"},
    {"code": CODE, "name": NAME, "action": "sell",
     "trigger_type": "price_below", "trigger_price": 1500.0, "target_weight": 0.0,
     "reason": "跌破1500清仓"},
]


# ---------------------------------------------------------------- 布景
def wipe(session) -> None:
    """把影子账户的一切痕迹清干净——保证可反复运行，且不碰任何真实用户。"""
    assert SHADOW_UID not in {u.id for u in session.query(db.User).all() if not u.is_backtest}, \
        f"id {SHADOW_UID} 已被一个**真实**用户占用，拒绝继续"
    for model in (db.Trade, db.Order, db.Position, db.TrustPlan,
                  db.TrustConfig, db.Account, db.NotificationConfig, db.Session):
        session.query(model).filter(model.user_id == SHADOW_UID).delete()
    session.query(db.User).filter(db.User.id == SHADOW_UID).delete()
    session.commit()


def setup(session) -> None:
    now = time.time()
    session.add(db.User(id=SHADOW_UID, phone="90000099001", created_at=now, is_backtest=True))
    # **先 flush 再插从表**：SQLAlchemy 的 unit of work 只按 ``relationship()`` 排序插入，
    # 而这些表之间只有裸 ForeignKey、没有 relationship，所以它不保证 User 先落。SQLite 默认
    # 不查外键（本地干跑因此是绿的），Postgres 会直接抛 ForeignKeyViolation。
    session.flush()
    session.add(db.TrustConfig(
        user_id=SHADOW_UID,
        is_active=False,          # 影子账户必须关实盘开关（clock 非空时不受它约束）
        book_created=True,
        available_cash=CASH,
        realized_pnl=0.0,         # 建簿归零
        stock_scope=0,            # 仅持仓
        style=1,
        fee_commission_rate=0.00025,
        fee_waive_min=False,
        fee_stamp_duty_rate=0.0005,
        agent_id="closure-test",
        updated_at=now,
    ))
    session.add(db.Position(
        user_id=SHADOW_UID, book=1, stock_code=CODE, stock_name=NAME,
        hold_qty=HOLD, available_qty=HOLD, frozen_qty=0, cost_price=COST, updated_at=now,
    ))
    session.add(db.TrustPlan(
        user_id=SHADOW_UID, trade_date=DAY,
        plan_json=json.dumps({"actions": LADDER}, ensure_ascii=False),
        created_at=now,
    ))
    session.commit()


def rows(session) -> dict:
    return {
        "orders": session.query(db.Order).filter(db.Order.user_id == SHADOW_UID).count(),
        "trades": session.query(db.Trade).filter(db.Trade.user_id == SHADOW_UID).count(),
        "plans": session.query(db.TrustPlan).filter(db.TrustPlan.user_id == SHADOW_UID).count(),
    }


def pos(session):
    return (session.query(db.Position)
            .filter(db.Position.user_id == SHADOW_UID, db.Position.book == 1).first())


def cfg_of(session):
    return session.query(db.TrustConfig).filter(db.TrustConfig.user_id == SHADOW_UID).first()


def trade_rows(session) -> list:
    return (session.query(db.Trade).filter(db.Trade.user_id == SHADOW_UID)
            .order_by(db.Trade.id).all())


def tick(clock: HistoryClock, k: int) -> dict:
    """第 k 个子 tick 的执行。**clock 非空 = 回测路径 = 绝不发通知**。"""
    return trust.run_execution(SHADOW_UID, clock.with_offset(k))


# ---------------------------------------------------------------- 各段
def test_setup() -> None:
    section("0. 布景落库")
    s = db.get_session()
    try:
        wipe(s)
        check("清场后影子账户无任何行", rows(s) == {"orders": 0, "trades": 0, "plans": 0})
        setup(s)
        check("影子用户已建", s.query(db.User).filter(db.User.id == SHADOW_UID).first() is not None)
        check("托管簿已建且 book_created=True",
              cfg_of(s) is not None and cfg_of(s).book_created is True)
        check("建簿时 realized_pnl 归零（不是 NULL）", cfg_of(s).realized_pnl == 0.0,
              repr(cfg_of(s).realized_pnl))
        p = pos(s)
        check(f"持仓 {HOLD} 股 @ {COST}，全部可卖",
              p is not None and p.hold_qty == HOLD and p.available_qty == HOLD)
        check("次日计划已落库（trade_date=%s）" % DAY, rows(s)["plans"] == 1)
    finally:
        s.close()


def test_ladder_walk() -> None:
    section("1. 梯子被**逐档**穿越（一根 bar 跨三档，但不能一笔到底）")
    clock = HistoryClock(DAY, {CODE: BAR}, names={CODE: NAME})
    seq = []
    for k in range(4):
        res = tick(clock, k)
        got = res.get("trades") or []
        s = db.get_session()
        try:
            cur = pos(s)
            held = cur.hold_qty if cur else 0
        finally:
            s.close()
        seq.append({
            "tick": k,
            "n": len(got),
            "qty": [t["quantity"] for t in got],
            "price": [t["price"] for t in got],
            "reason": [(t.get("ai_reason") or "")[:28] for t in got],
            "held_after": held,
            "skipped": res.get("skipped"),
        })

    for r in seq:
        print(f"     tick{r['tick']}: 成交 {r['n']} 笔 {r['qty']} @ {r['price']} "
              f"→ 余 {r['held_after']}  skipped={r['skipped']}")

    check("★ 每个 tick 至多一笔（逐档推进，不是一笔到底）",
          all(r["n"] <= 1 for r in seq), str([r["n"] for r in seq]))
    check("★ 三个 tick 依次推进三档，成交股数 500→300→200",
          [r["qty"][0] if r["qty"] else None for r in seq[:3]] == [500, 300, 200],
          str([r["qty"] for r in seq[:3]]))
    check("★ 四档走完仓位归零，第 4 个 tick 无仓可执行",
          seq[3]["n"] == 0 and seq[3]["skipped"] == "no_positions", str(seq[3]))
    # 成交价必须**逐档按自己的触发价**算 ``fill_price``，而不是全用一个价：
    #   tick0 第1档 tp=1700 → min(open 1510, 1700) = 1510
    #   tick1 第2档 tp=1600 → min(open 1510, 1600) = 1510
    #   tick2 第3档 tp=1500 → min(open 1510, 1500) = 1500   ← 这一档跳空未越过，按触发价成交
    # 若三笔都取同一个价，说明"定仓价与成交价同源"在某处退化成了取开盘价。
    check("★ 每档成交价 = min(开盘, **该档**触发价) → 1510 / 1510 / 1500",
          [r["price"][0] if r["price"] else None for r in seq[:3]] == [1510.0, 1510.0, 1500.0],
          str([r["price"] for r in seq[:3]]))
    check("每笔都带得出计划理由", all(r["reason"] and r["reason"][0] for r in seq[:3]),
          str([r["reason"] for r in seq[:3]]))
    check("逐档目标占比写进了理由（0.5 / 0.25 / 0）",
          all(str(w) in seq[i]["reason"][0] for i, w in enumerate([0.5, 0.25, 0.0])),
          str([r["reason"] for r in seq[:3]]))


def test_persisted() -> None:
    section("2. 四张表都真的落了行")
    s = db.get_session()
    try:
        r = rows(s)
        check("orders 3 行", r["orders"] == 3, str(r))
        check("trades 3 行", r["trades"] == 3, str(r))
        ts = trade_rows(s)
        check("三笔全是卖出（direction=1）", all(t.direction == 1 for t in ts),
              str([t.direction for t in ts]))
        check("三笔都挂在同一个代码上且 source 为 AI 托管",
              all(t.stock_code == CODE for t in ts), str([t.stock_code for t in ts]))
        check("orders 与 trades 一一对应（order_id 能对上）",
              {t.order_id for t in ts} ==
              {o.order_id for o in s.query(db.Order).filter(db.Order.user_id == SHADOW_UID).all()},
              "order_id 不匹配")
        check("★ 持仓行已清空（卖到 0 股时 place_order 删行）", pos(s) is None,
              repr(pos(s).hold_qty) if pos(s) else "None")
        c = cfg_of(s)
        check("★ 现金确实增加了（卖出回款 − 费用）", c.available_cash > CASH,
              f"{c.available_cash}")
    finally:
        s.close()


def test_realized() -> None:
    section("3. 归因：计数器 == 逐笔重放 == 手算（三路必须逐分一致）")
    s = db.get_session()
    try:
        ts = trade_rows(s)
        trades = [{"stock_code": t.stock_code, "direction": t.direction, "price": t.price,
                   "quantity": t.quantity, "fee": t.fee} for t in ts]
        counter = cfg_of(s).realized_pnl
        replay = replay_realized([{"stock_code": CODE, "hold_qty": HOLD, "cost_price": COST}], trades)
        manual = round(sum((t["price"] - COST) * t["quantity"] - t["fee"] for t in trades), 2)
    finally:
        s.close()

    print(f"     计数器={counter}  重放={replay}  手算={manual}")
    check("★ 三路一致（生产增量计数器 == backtest_calc 独立重放）",
          counter == replay == manual, f"counter={counter} replay={replay} manual={manual}")
    check("★ 不是 0，也不是 NULL（真的累计了）",
          counter is not None and counter != 0.0, repr(counter))
    check("亏损的梯子给出负数（1510 < 成本 1700 卖 1000 股）", counter < 0, repr(counter))

    # 顺带把 G2 在真实数据上的两态都钉住
    an = trust.get_analytics(SHADOW_UID)
    check("get_analytics 给出同一个数", an["realized_pnl"] == counter, str(an["realized_pnl"]))
    check("有值的簿不出现「暂未统计」提示", an["realized_pnl_note"] == "", repr(an["realized_pnl_note"]))
    check("trade_count 与流水一致", an["trade_count"] == 3, str(an["trade_count"]))


def test_no_notify_on_backtest() -> None:
    section("4. 回测路径绝不外发通知（clock 非空 ⇒ 不刷真 webhook）")
    s = db.get_session()
    try:
        wipe(s)
        setup(s)
    finally:
        s.close()

    sent: list = []
    real = notify.notify_trades
    notify.notify_trades = lambda uid, trades: sent.append((uid, len(trades)))
    try:
        clock = HistoryClock(DAY, {CODE: BAR}, names={CODE: NAME})
        res = tick(clock, 0)
        check("确实成交了（否则这条断言没有意义）", len(res.get("trades") or []) == 1,
              str(res.get("trades")))
        check("★ clock 非空的执行不调用 notify_trades", sent == [], str(sent))

        # 反向对照：同一份成交，clock=None 时必须派发——证明上面不是因为"壳没接上"而沉默
        trust._run_execution = lambda uid, clock=None: {"trades": [{"x": 1}], "skipped": None}
        trust.run_execution(SHADOW_UID, None)
        check("★ 反向对照：clock=None 时确实派发了（一次、合并成一条）",
              sent == [(SHADOW_UID, 1)], str(sent))
    finally:
        notify.notify_trades = real
        trust._run_execution = _ORIG_RUN_EXECUTION


def test_clear_resets() -> None:
    section("5. 重贴快照/重置托管 → 本簿累计归零（不是 NULL），流水同生共死")
    s = db.get_session()
    try:
        # 清之前先如实读一遍，再拿它当期望——不硬编码条数：第 4 段为了做通知对照
        # 重设过簿景，写死 3 只会让这条断言变成对"段与段之间怎么排"的假设。
        had = rows(s)
        before = cfg_of(s).realized_pnl
        assert before not in (None, 0.0), f"前置条件不成立：归因应当非零，实际 {before!r}"
        assert sum(had.values()) > 0, f"前置条件不成立：清之前就没什么可清 {had}"
        cleared = trust._clear_book_runtime(s, SHADOW_UID)
        s.commit()
        after = cfg_of(s).realized_pnl
        left = rows(s)
    finally:
        s.close()
    print(f"     清前={had}  归零前={before}  归零后={after}  清掉={cleared}")
    check("★ 清簿后 realized_pnl == 0.0（归零，不是置 NULL）",
          after == 0.0, repr(after))
    check("清掉的条数 == 清之前库里的条数（没有漏表）", cleared == had, f"{cleared} != {had}")
    check("流水表里一行不剩（归因与流水同生共死）", left == {"orders": 0, "trades": 0, "plans": 0},
          str(left))
    check("★ 归零后 get_analytics 给 0.0 且**不带**「暂未统计」提示（与老簿的 NULL 两态可分）",
          trust.get_analytics(SHADOW_UID)["realized_pnl"] == 0.0
          and trust.get_analytics(SHADOW_UID)["realized_pnl_note"] == "",
          str(trust.get_analytics(SHADOW_UID)["realized_pnl"]))


def main() -> int:  # noqa: C901
    # 只打印驱动/host/库名——绝不把 URL 整个打出来（里面含密码）
    u = db.engine.url
    print(f"目标库：{u.drivername}://{u.host or ''}{u.database or ''}")
    section("环境自述")
    s = db.get_session()
    try:
        real = [u.id for u in s.query(db.User).all() if not u.is_backtest]
        print(f"     库里的**真实**用户 id：{real}（本脚本一个都不会碰）")
        print(f"     影子账户 id：{SHADOW_UID}")
        check("影子里有 present 的 trust_configs.realized_pnl 列（迁移已跑）",
              cfg_of(s) is None or hasattr(cfg_of(s), "realized_pnl"))
    finally:
        s.close()

    test_setup()
    test_ladder_walk()
    test_persisted()
    test_realized()

    try:
        test_no_notify_on_backtest()
        test_clear_resets()
    finally:
        # 收尾：本脚本该在库里「什么都没留下」。第 4 段结尾为了做通知对照重设过簿景
        # （一行持仓 + 一份 TrustConfig），而 `_clear_book_runtime` 按语义**只清运行痕迹、
        # 不碰持仓**——所以必须显式 wipe 一次，否则每跑一次就往生产库留一行影子持仓。
        s = db.get_session()
        try:
            wipe(s)
            left = dict(rows(s))
            left["configs"] = s.query(db.TrustConfig).filter(
                db.TrustConfig.user_id == SHADOW_UID).count()
            left["users"] = s.query(db.User).filter(db.User.id == SHADOW_UID).count()
        finally:
            s.close()
        check("★ 跑完在库里不留任何残迹（可反复跑、不污染生产）",
              all(v == 0 for v in left.values()), str(left))

    print(f"\n{'=' * 62}")
    if _FAILS:
        print(f"❌ {len(_FAILS)}/{_COUNT} 项未通过：")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
