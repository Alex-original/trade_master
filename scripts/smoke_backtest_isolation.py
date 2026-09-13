"""回测影子账户隔离的离线回归（真库、真代码，不连 Postgres）。

**为什么这个脚本最重要**：回测能不能信，先看它有没有可能污染实盘账簿。回测的账务全部落在
一个影子 user 上，隔离的依据是"每张表都按 user_id 分区"。这条依据值得用真库打一遍，而不是
在注释里相信它。

不连 Postgres、不连 Wind、不跑 LLM：把 ``app.db`` 的 SessionLocal 换成绑在**内存 SQLite** 上的
sessionmaker，用真实的 ``create_run`` / ``reset_shadow_book`` / ``apply_initial_state`` /
``trust._iter_active_users`` / ``trust.toggle_trust`` 打真实的库。

覆盖：
  1. 影子账户的建法：is_backtest=True、手机号落在 bt- 前缀（短信登录不可达）
  2. 跨回测复用同一个影子账户（EngineRun 缓存不白烧）
  3. **实盘调度器看不见影子账户**——即使它的 is_active 被强行置 True
  4. toggle_trust 拒绝激活影子账户（唯一的激活入口）
  5. users.is_backtest 列不存在时，调度器退回旧查询而**不瘫痪**
  6. reset_shadow_book 只清影子账户，真实用户的委托/成交/计划/持仓分毫不动
  7. 起始状态：cash 模式 / copy 模式（含冻结池物化与 T+1 冻结归零）
  8. 配置与标的池在发起时冻结，之后改真实托管配置不影响本次回测
  9. **冻结值真的落到了影子账户上**——配置镜像 + 标的池物化 + 研究标的集非空
 10. **Stage2 上下文里团队看得见候选池**（实盘与回测共用的一段，本次唯一动实盘的地方）

第 9 节是本模块最贵那个坑的栅栏：冻结值只躺在 ``config_json`` / ``universe_json`` 里、
从没写到影子账户上的话，Stage1 一条研究都跑不出来（``EngineRun`` 恒为 0），Stage2 看不到
任何标的，全程零成交——**不报错，只是结果页上看起来"团队很保守"**。
真正"跑完一轮后 ``EngineRun > 0``"的端到端指纹在 ``/tmp/bt_e2e.py`` 那类实跑脚本里；
离线脚本能守的是它的**必要条件**：影子的研究标的集非空且恰好等于冻结池。

用法（在 product/trade_master 下）：
    .venv/bin/python scripts/smoke_backtest_isolation.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402
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
_MAIN_SESSIONMAKER = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
db.SessionLocal = _MAIN_SESSIONMAKER  # type: ignore[assignment]

from app import backtest as bt  # noqa: E402  （必须在 patch 之后）
from app import backtest_calc as calc  # noqa: E402
from app import trust  # noqa: E402
from app.errors import ServiceError  # noqa: E402

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
OWNER_PHONE = "13800000001"

#: 第 1 节造的 run 显式带上一个标的池。**必须显式给**：不给她的话 ``universe_json`` 是
#: ``[]``，影子账户物化出来的自选股会是 0 条，第 7、9 节就断言不出"冻结池真的是这个"。
#: 2 只票，其中 ``600519.SH`` 与真实托管簿的持仓重合——复制模式下"持仓 ⊂ 冻结池"这条
#: 不变式需要这个重合才能被验证。
RUN_UNIVERSE = ["600519.SH", "000001.SZ"]


def fresh_db() -> None:
    """清空所有表并建一个真实用户（含托管配置与真实账簿痕迹）。"""
    session = db.get_session()
    try:
        for table in reversed(db.Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()

        owner = db.User(phone=OWNER_PHONE, created_at=NOW, is_backtest=False)
        session.add(owner)
        session.flush()
        session.add(db.TrustConfig(
            user_id=owner.id, is_active=True, book_created=True, available_cash=12345.0,
            stock_scope=1, style=2, risk_stop_loss_pct=0.08,
            fee_commission_rate=0.0003, updated_at=NOW,
        ))
        # 真实账簿的运行痕迹——第 6 节要证明它们**一条都不会少**
        session.add(db.Position(
            user_id=owner.id, book=1, stock_code="600519.SH", stock_name="贵州茅台",
            hold_qty=200, available_qty=200, frozen_qty=0, cost_price=1500.0, updated_at=NOW,
        ))
        session.add(db.Order(
            order_id="real-o1", user_id=owner.id, stock_code="600519.SH", stock_name="贵州茅台",
            direction=1, price=1600.0, quantity=100, status=1, source=1, created_at=NOW,
        ))
        session.add(db.Trade(
            trade_id="real-t1", order_id="real-o1", user_id=owner.id, stock_code="600519.SH",
            stock_name="贵州茅台", direction=1, price=1600.0, quantity=100, amount=160000.0,
            fee=96.0, traded_at=NOW,
        ))
        session.add(db.TrustPlan(
            user_id=owner.id, trade_date="2026-03-06",
            plan_json=json.dumps({"actions": [{"code": "600519.SH", "action": "hold"}]}),
            created_at=NOW,
        ))
        session.add(db.EngineRun(
            user_id=owner.id, ticker="600519.SH", market="", trade_date="2026-03-05",
            rating="Buy", report_json="{}", created_at=NOW,
        ))
        # 真实自选股：copy 模式要把它镜像给影子账户，否则 scope=1 选不出池子
        session.add(db.Watchlist(
            user_id=owner.id, stock_code="600519.SH", stock_name="贵州茅台",
            source=0, group_name="默认", created_at=NOW,
        ))
        session.add(db.WatchlistGroup(user_id=owner.id, name="科技", created_at=NOW))
        session.add(db.WatchlistMeta(user_id=owner.id, active_group="默认", created_at=NOW))
        session.commit()
    finally:
        session.close()


def owner_id() -> int:
    session = db.get_session()
    try:
        return int(session.query(db.User.id).filter(db.User.phone == OWNER_PHONE).first()[0])
    finally:
        session.close()


def make_run(**kw) -> tuple[int, int]:
    """建一个 run，返回 (run_id, shadow_user_id)。"""
    kw.setdefault("start_date", "2026-03-02")
    kw.setdefault("end_date", "2026-03-31")
    run_id = bt.create_run(owner_id(), **kw)
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        return run_id, int(row.shadow_user_id)
    finally:
        session.close()


def real_trace_counts() -> dict:
    session = db.get_session()
    try:
        uid = owner_id()
        return {
            "orders": session.query(db.Order).filter(db.Order.user_id == uid).count(),
            "trades": session.query(db.Trade).filter(db.Trade.user_id == uid).count(),
            "plans": session.query(db.TrustPlan).filter(db.TrustPlan.user_id == uid).count(),
            "positions": session.query(db.Position).filter(
                db.Position.user_id == uid, db.Position.book == 1).count(),
            "watch": session.query(db.Watchlist).filter(db.Watchlist.user_id == uid).count(),
        }
    finally:
        session.close()


def main() -> int:  # noqa: C901 —— 冒烟脚本，线性罗列各场景
    fresh_db()
    run_id, shadow_id = make_run(init_mode="cash", init_cash=100000.0,
                                 universe=list(RUN_UNIVERSE))

    # ------------------------------------------------------------- 1
    section("1. 影子账户的建法")
    session = db.get_session()
    try:
        sh = session.query(db.User).filter(db.User.id == shadow_id).first()
        user_is_bt = bool(sh.is_backtest)
        phone = sh.phone
    finally:
        session.close()
    check("影子账户标记 is_backtest=True", user_is_bt)
    check("手机号带 bt- 前缀（短信登录不可达）", phone.startswith(bt.SHADOW_PHONE_PREFIX), phone)
    check("手机号长度落在 String(20) 内", len(phone) <= 20, str(len(phone)))
    check("影子账户与真实账户是两个 user", shadow_id != owner_id())
    check("影子账户自带托管配置", _has_config(shadow_id))

    # ------------------------------------------------------------- 2
    section("2. 跨回测复用同一个影子账户（EngineRun 缓存不白烧）")
    run_id2, shadow_id2 = make_run(init_mode="cash", init_cash=50000.0)
    check("第二次回测复用同一个影子账户", shadow_id2 == shadow_id,
          f"{shadow_id2} vs {shadow_id}")
    check("两个 run 是不同的 run", run_id2 != run_id)
    session = db.get_session()
    try:
        n = session.query(db.BacktestRun).filter(db.BacktestRun.user_id == owner_id()).count()
    finally:
        session.close()
    check("发起人名下有两个 run", n == 2, str(n))

    # ------------------------------------------------------------- 3
    section("3. 实盘调度器看不见影子账户（§8.5 第 1 条）")
    check("正常状态下调度器只看到真实用户", trust._iter_active_users() == [owner_id()],
          str(trust._iter_active_users()))

    # 模拟"未来某处代码把影子账户激活了"这个 bug：直接写库绕过 toggle_trust 的守卫。
    # 选择侧的 join 必须独立地把这一行挡住——两道卡口各自都要成立。
    session = db.get_session()
    try:
        session.query(db.TrustConfig).filter(
            db.TrustConfig.user_id == shadow_id
        ).update({"is_active": True})
        session.commit()
        forced = session.query(db.TrustConfig).filter(
            db.TrustConfig.user_id == shadow_id).first().is_active
    finally:
        session.close()
    check("（前提）影子账户的 is_active 已被强行置 True", bool(forced))
    got = trust._iter_active_users()
    check("调度器仍然看不见影子账户", shadow_id not in got, str(got))
    check("真实用户仍在调度列表里", owner_id() in got, str(got))

    # ------------------------------------------------------------- 4
    section("4. toggle_trust 拒绝激活影子账户（§8.5 第 1 条的另一半）")
    try:
        trust.toggle_trust(shadow_id, True)
        check("激活影子账户被拒", False, "没有抛 ServiceError")
    except ServiceError as e:
        check("激活影子账户被拒", True)
        check("拒绝理由是给用户看的文案", "回测" in str(e), str(e))
    check("真实用户仍可正常开关托管", _toggle_ok(owner_id()))

    # ------------------------------------------------------------- 5
    section("5. users.is_backtest 列不存在时，调度器不瘫痪（§8.5 第 3 条）")
    _legacy_schema_fallback()

    # 把内存库换回来（第 5 节临时借用了 SessionLocal）
    db.SessionLocal = _MAIN_SESSIONMAKER  # type: ignore[assignment]

    # ------------------------------------------------------------- 6
    section("6. reset_shadow_book 只清影子账户，真实账簿分毫不动")
    # 先给影子名下落一份计划——它是那次回测**唯一的历史记录**（报告页 / 每日监控条件都从它
    # 渲染）。影子账户跨 run 复用，所以"另一个 run 开始"这件事也落在同一个 user 上：若按
    # 运行痕迹把计划一起删了，上一个 run 的报告就会凭空消失（生产上真丢过一天）。
    session = db.get_session()
    try:
        session.add(db.TrustPlan(
            user_id=shadow_id, trade_date="2026-07-09",
            plan_json=json.dumps({"actions": [{"code": "600519.SH", "action": "hold"}]}),
            created_at=1.0,
        ))
        session.commit()
    finally:
        session.close()
    before = real_trace_counts()
    session = db.get_session()
    try:
        bt.reset_shadow_book(session, shadow_id)
        session.commit()
    finally:
        session.close()
    check("★ 影子账户的**计划留着**（它是那次回测的历史记录，不是运行痕迹）",
          _shadow_plans(shadow_id) == ["2026-07-09"], str(_shadow_plans(shadow_id)))
    check("★ 影子账户的委托/成交/持仓**照旧清干净**（计划是唯一被豁免的那一样）",
          _shadow_orders(shadow_id) == 0 and _shadow_positions(shadow_id) == 0,
          f"orders={_shadow_orders(shadow_id)} positions={_shadow_positions(shadow_id)}")
    after = real_trace_counts()
    check("真实用户的委托数不变", before["orders"] == after["orders"] == 1, str(after))
    check("真实用户的成交数不变", before["trades"] == after["trades"] == 1, str(after))
    check("真实用户的计划数不变", before["plans"] == after["plans"] == 1, str(after))
    check("真实用户的托管持仓不变", before["positions"] == after["positions"] == 1, str(after))
    check("真实用户的自选股不变", before["watch"] == after["watch"] == 1, str(after))
    check("影子账户被清空（现金归零）", _cash(shadow_id) == 0.0, str(_cash(shadow_id)))

    # ------------------------------------------------------------- 7
    section("7. 起始状态：cash / copy")
    # ``uid`` 先求值再开 session——见第 9 节那段关于 StaticPool 共连接的说明。
    uid = owner_id()
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        row.init_mode = "cash"
        row.init_cash = 100000.0
        bt.reset_shadow_book(session, shadow_id)
        out = bt.apply_initial_state(session, row, uid)
        session.commit()
    finally:
        session.close()
    check("cash 模式：现金 == init_cash", out["cash"] == 100000.0, str(out))
    check("cash 模式：空仓起步", out["positions"] == [], str(out))

    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        row.init_mode = "copy"
        bt.reset_shadow_book(session, shadow_id)
        out = bt.apply_initial_state(session, row, uid)
        session.commit()
    finally:
        session.close()
    check("copy 模式：复制了真实托管簿的持仓", out["positions"] == ["600519.SH"], str(out))
    check("copy 模式：现金取真实托管簿的现金", out["cash"] == 12345.0, str(out))
    # 旧断言守的是"把**发起人的自选股**镜像给影子"；新语义下这件事已经不成立——影子物化的
    # 是**本次回测的冻结标的池**。发起人的自选股只有 1 只（600519.SH），冻结池有 2 只，
    # 所以这两个数字必须区分得开，否则这条断言就退化成"随便写点什么都能过"。
    n_watch = _watch_count(shadow_id)
    check("copy 模式：影子的自选股 == 冻结标的池（2 只），而不是发起人的自选股（1 只）",
          n_watch == len(RUN_UNIVERSE), f"{n_watch} vs {len(RUN_UNIVERSE)}")
    check("copy 模式：只物化了一个分组", _group_count(shadow_id) == 1,
          str(_group_count(shadow_id)))
    check("copy 模式：分组名就是回测标的池", _group_names(shadow_id) == [bt.BACKTEST_GROUP],
          str(_group_names(shadow_id)))
    check("copy 模式：可用量 == 持仓量（起始日不存在当日买入冻结）",
          _avail(shadow_id) == 200, str(_avail(shadow_id)))
    check("copy 模式：冻结量为 0", _frozen(shadow_id) == 0, str(_frozen(shadow_id)))
    check("copy 模式：成本价一并复制（止损判定要用）", _cost(shadow_id) == 1500.0,
          str(_cost(shadow_id)))
    check("copy 模式：影子账户仍不是激活态", _is_active(shadow_id) is False,
          str(_is_active(shadow_id)))

    # 影子的选股范围必须被**强制**成 scope=1 + 不限分组：它的自选股就是冻结池，
    # 于是 get_managed_symbols / get_analysis_universe 取出来的正好是冻结池。
    check("影子账户的 stock_scope 被强制为 1（仅自选）",
          _cfg_field(shadow_id, "stock_scope") == 1, str(_cfg_field(shadow_id, "stock_scope")))
    check("影子账户的 stock_scope_group 被清成 None（不限定分组）",
          _cfg_field(shadow_id, "stock_scope_group") is None,
          str(_cfg_field(shadow_id, "stock_scope_group")))
    # 这一条就是"Stage1 会去研究这些票"的必要条件。它是空的，EngineRun 就恒为 0——
    # 本缺陷的指纹。持仓 600519.SH ⊂ 冻结池，所以并集恰好等于冻结池。
    au = _analysis_universe_codes(shadow_id)
    check("影子的研究标的集 == 冻结标的池（Stage1 有票可研究的必要条件）",
          set(au) == set(RUN_UNIVERSE), f"{sorted(au)} vs {sorted(RUN_UNIVERSE)}")
    check("影子的研究标的集非空（EngineRun 恒为 0 的反面）", len(au) > 0, str(len(au)))

    # ------------------------------------------------------------- 8
    section("8. 配置与标的池在发起时冻结")
    run_id3, _ = make_run(init_mode="cash", init_cash=1.0,
                          universe=["600519.SH", "000001.SZ"],
                          extra_config={"sub_ticks": 3, "fresh_research": False})
    cfg = bt.get_run(run_id3)["config"]
    check("冻结了真实托管配置的风险参数", cfg["risk_stop_loss_pct"] == 0.08, str(cfg))
    check("冻结了真实托管配置的费用参数", cfg["fee_commission_rate"] == 0.0003, str(cfg))
    check("冻结了回测专属参数", cfg["sub_ticks"] == 3 and cfg["fresh_research"] is False, str(cfg))
    check("冻结了标的池",
          bt.get_run(run_id3)["universe"] == ["600519.SH", "000001.SZ"],
          str(bt.get_run(run_id3)["universe"]))

    # 改真实配置 → 已发起的 run 不受影响
    session = db.get_session()
    try:
        session.query(db.TrustConfig).filter(db.TrustConfig.user_id == owner_id()).update(
            {"risk_stop_loss_pct": 0.5, "fee_commission_rate": 0.009}
        )
        session.commit()
    finally:
        session.close()
    frozen = bt.get_run(run_id3)["config"]
    check("之后改真实托管配置，已发起的 run 不受影响",
          frozen["risk_stop_loss_pct"] == 0.08 and frozen["fee_commission_rate"] == 0.0003,
          str(frozen))

    # ------------------------------------------------------------- 9
    section("9. 冻结值真的落到了影子账户上（配置镜像 + 标的池物化）")
    # 先把真实配置改成一组**好认的数字**。上一节已把它改成 0.5 / 0.009，这组值既不同于
    # db 默认、也不同于上一节——影子上读到 0.25 就只可能来自"本次冻结"，
    # 不可能是残留值或默认值蒙对的。
    _set_real_config(risk_stop_loss_pct=0.25, fee_commission_rate=0.0011, style=3,
                     risk_max_position_pct=0.17)

    run_id4, _ = make_run(
        init_mode="cash", init_cash=100000.0,
        universe=["000001.SZ", "600000.SH"],
        extra_config={"sub_ticks": 2},
    )
    # 注意：``uid`` 必须在**开 session 之前**求值。本脚本用 StaticPool，所有 session 共用
    # 同一条 DBAPI 连接，于是内层 session 关闭时的 ROLLBACK 会把外层**尚未提交**的事务一并
    # 回滚掉——``bt.reset_shadow_book`` 里的 DELETE 会因此白做。生产环境每个 session 拿独立
    # 连接，不存在这个现象；这里只是脚手架的形状要摆对。
    uid = owner_id()
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id4).first()
        bt.reset_shadow_book(session, shadow_id)
        bt.apply_initial_state(session, row, uid)
        session.commit()
    finally:
        session.close()

    check("影子 cfg 拿到了冻结的止损线", _cfg_field(shadow_id, "risk_stop_loss_pct") == 0.25,
          str(_cfg_field(shadow_id, "risk_stop_loss_pct")))
    check("影子 cfg 拿到了冻结的佣金率", _cfg_field(shadow_id, "fee_commission_rate") == 0.0011,
          str(_cfg_field(shadow_id, "fee_commission_rate")))
    check("影子 cfg 拿到了冻结的风格", _cfg_field(shadow_id, "style") == 3,
          str(_cfg_field(shadow_id, "style")))
    check("影子 cfg 拿到了冻结的单票上限",
          _cfg_field(shadow_id, "risk_max_position_pct") == 0.17,
          str(_cfg_field(shadow_id, "risk_max_position_pct")))
    # 这一条是上一条的**反面**：冻结值里明明有 stock_scope=1，影子也必须恒为 1——
    # 但那是靠强制、不是靠照抄（照抄会在发起人 scope=0 时把回测跑成零成交）。
    check("冻结的 stock_scope 没有被照抄（影子恒为 1）",
          _cfg_field(shadow_id, "stock_scope") == 1, str(_cfg_field(shadow_id, "stock_scope")))

    # 标的池是**覆盖式**物化：上一节那个 run 的 600519.SH 必须消失。增量写会留残留，
    # 让"这次回测的池子"凭空多出票来。
    pooled = _watch_codes(shadow_id)
    check("冻结池是覆盖式的：上一个 run 的标的没有残留",
          set(pooled) == {"000001.SZ", "600000.SH"}, str(sorted(pooled)))
    check("物化后影子研究标的集 == 本 run 的冻结池",
          set(_analysis_universe_codes(shadow_id)) == {"000001.SZ", "600000.SH"},
          str(sorted(_analysis_universe_codes(shadow_id))))

    # 再改一次真实配置：已经写进影子的冻结值**不能**跟着动。
    _set_real_config(risk_stop_loss_pct=0.99, fee_commission_rate=0.05, style=1)
    check("之后改真实托管配置，影子的费率/风控不受影响",
          _cfg_field(shadow_id, "risk_stop_loss_pct") == 0.25
          and _cfg_field(shadow_id, "fee_commission_rate") == 0.0011
          and _cfg_field(shadow_id, "style") == 3,
          f"{_cfg_field(shadow_id, 'risk_stop_loss_pct')} / "
          f"{_cfg_field(shadow_id, 'fee_commission_rate')} / {_cfg_field(shadow_id, 'style')}")
    check("改真实托管配置也不会动影子的选股范围",
          _cfg_field(shadow_id, "stock_scope") == 1
          and _cfg_field(shadow_id, "stock_scope_group") is None,
          str(_cfg_field(shadow_id, "stock_scope")))

    # 续跑路径（``_restore_checkpoint``）也必须重新镜像 + 重新物化。同一发起人的所有 run
    # 共用一个影子账户，中途并发起另一个 run、或删掉另一个 run，都会把影子的配置与池子
    # 改掉。这里手工把它改脏，再看续跑能不能自愈——不自愈的话，续跑的后半段会不动声色地
    # 跑在别人的配置和别人的池子上。
    session = db.get_session()
    try:
        session.query(db.TrustConfig).filter(db.TrustConfig.user_id == shadow_id).update(
            {"stock_scope": 0, "risk_stop_loss_pct": 0.01}
        )
        session.query(db.Watchlist).filter(db.Watchlist.user_id == shadow_id).delete()
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id4).first()
        row.checkpoint_json = calc.serialize_checkpoint(54321.0, [])
        row.last_step_date = "2026-03-10"
        row.last_order_id = 0
        session.commit()
    finally:
        session.close()

    snap = bt.get_run(run_id4)  # 同样：先求值，再开 session（理由见上）
    session = db.get_session()
    try:
        bt._restore_checkpoint(session, snap, shadow_id)
        session.commit()
    finally:
        session.close()

    check("续跑：被改脏的 stock_scope 被重新镜像回 1",
          _cfg_field(shadow_id, "stock_scope") == 1, str(_cfg_field(shadow_id, "stock_scope")))
    check("续跑：被改脏的止损线被重新镜像回冻结值",
          _cfg_field(shadow_id, "risk_stop_loss_pct") == 0.25,
          str(_cfg_field(shadow_id, "risk_stop_loss_pct")))
    check("续跑：被删空的冻结池被重新物化",
          set(_watch_codes(shadow_id)) == {"000001.SZ", "600000.SH"},
          str(sorted(_watch_codes(shadow_id))))
    check("续跑：研究标的集重新等于冻结池",
          set(_analysis_universe_codes(shadow_id)) == {"000001.SZ", "600000.SH"},
          str(sorted(_analysis_universe_codes(shadow_id))))
    # 现金属于账务，检查点永远权威——镜像**必须**在写现金之前，否则将来往
    # ``_SETTING_FIELDS`` 加金额型字段就会把恢复出来的现金覆盖掉。
    check("续跑：现金取自检查点（镜像没把它冲掉）", _cash(shadow_id) == 54321.0,
          str(_cash(shadow_id)))

    # ------------------------------------------------------------- 10
    section("10. Stage2 上下文：候选池真的进了团队的视野（本次唯一动实盘的一段）")
    # 这一段守的是**实盘**行为。改动前 ``build_portfolio_plan_context`` 只把**持仓**的
    # 研究摘要喂给团队，于是自选股被 Stage1 花钱研究了一遍之后，没有任何环节把它交给决策
    # 团队——「选股范围＝自选股」在实盘里几乎不生效，而设置页照常显示着「自选股」。
    uid = owner_id()
    CAND = ["000001.SZ", "600002.SH"]
    session = db.get_session()
    try:
        # 标准形状：1 只持仓（600519.SH，fresh_db 造的）+ 2 只自选 + scope=1
        session.query(db.TrustConfig).filter(db.TrustConfig.user_id == uid).update(
            {"stock_scope": 1, "stock_scope_group": None}
        )
        for c in CAND:
            session.add(db.Watchlist(
                user_id=uid, stock_code=c, stock_name="", source=0,
                group_name="默认", created_at=NOW,
            ))
            # 三只票都得有研究结论，否则测的是"缺研究"而不是"候选进不进上下文"
            session.add(db.EngineRun(
                user_id=uid, ticker=c, market="", trade_date="2026-03-05",
                rating="Buy", report_json="{}", created_at=NOW,
            ))
        session.commit()
    finally:
        session.close()

    rng = _range_lines(_plan_context(uid))
    check("上下文里有「本次可操作的标的范围」两段", "持仓" in rng and "候选" in rng, str(rng)[:160])
    check("【持仓】段里有已持有的 600519.SH", "600519.SH" in rng.get("持仓", ""),
          rng.get("持仓", ""))
    check("【持仓】段里没有候选标的（只有真持仓才算持仓）",
          all(c not in rng.get("持仓", "") for c in CAND), rng.get("持仓", ""))
    check("【候选】段里有两只自选股（它们此前到不了团队眼前）",
          all(c in rng.get("候选", "") for c in CAND), rng.get("候选", ""))
    check("【候选】段里没有已持仓的标的（持仓不算候选）",
          "600519.SH" not in rng.get("候选", ""), rng.get("候选", ""))
    check("文案明确要求把动作限定在该范围内",
          "范围外的标的一律不要提交任何动作" in _plan_context(uid))

    # 止血手段：把选股范围改回「仅持仓股」→ 候选段自然为空，行为回到改动前。
    # 这一条就是"用户不需要等发版就能把实盘恢复原状"的证明。
    session = db.get_session()
    try:
        session.query(db.TrustConfig).filter(db.TrustConfig.user_id == uid).update(
            {"stock_scope": 0}
        )
        session.commit()
    finally:
        session.close()
    rng0 = _range_lines(_plan_context(uid))
    check("scope=0（仅持仓）时候选段为空——止血手段有效的证明",
          rng0.get("候选", "").endswith("（无）"), rng0.get("候选", ""))
    check("scope=0 时持仓仍在范围内", "600519.SH" in rng0.get("持仓", ""), rng0.get("持仓", ""))

    session = db.get_session()
    try:
        session.query(db.TrustConfig).filter(db.TrustConfig.user_id == uid).update(
            {"stock_scope": 1}
        )
        session.commit()
    finally:
        session.close()

    # 回测侧：影子账户的候选 == 冻结池 − 影子持仓。**不需要任何回测专用参数**——
    # 影子池在起跑时已被物化成冻结池，所以同一段代码自动读到对的东西。
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        row.init_mode = "copy"
        bt.reset_shadow_book(session, shadow_id)
        bt.apply_initial_state(session, row, uid)
        session.commit()
    finally:
        session.close()

    srng = _range_lines(_plan_context(shadow_id))
    check("回测侧：【持仓】段只有复制过来的 600519.SH",
          "600519.SH" in srng.get("持仓", "") and "000001.SZ" not in srng.get("持仓", ""),
          srng.get("持仓", ""))
    check("回测侧：【候选】段 == 冻结池 − 持仓（只剩 000001.SZ）",
          "000001.SZ" in srng.get("候选", "") and "600519.SH" not in srng.get("候选", ""),
          srng.get("候选", ""))

    # ------------------------------------------------------------- 收尾
    print(f"\n{'=' * 60}")
    if _FAILS:
        print(f"❌ {len(_FAILS)}/{_COUNT} 项未通过：")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


# ---------------------------------------------------------------- 辅助


def _has_config(uid: int) -> bool:
    session = db.get_session()
    try:
        return session.query(db.TrustConfig).filter(db.TrustConfig.user_id == uid).count() == 1
    finally:
        session.close()


def _cash(uid: int) -> float:
    session = db.get_session()
    try:
        return float(session.query(db.TrustConfig).filter(
            db.TrustConfig.user_id == uid).first().available_cash)
    finally:
        session.close()


def _shadow_plans(uid: int) -> list[str]:
    session = db.get_session()
    try:
        return [
            p.trade_date
            for p in session.query(db.TrustPlan)
            .filter(db.TrustPlan.user_id == uid)
            .order_by(db.TrustPlan.trade_date)
            .all()
        ]
    finally:
        session.close()


def _shadow_orders(uid: int) -> int:
    session = db.get_session()
    try:
        return int(session.query(db.Order).filter(db.Order.user_id == uid).count())
    finally:
        session.close()


def _shadow_positions(uid: int) -> int:
    session = db.get_session()
    try:
        return int(session.query(db.Position).filter(
            db.Position.user_id == uid, db.Position.book == 1).count())
    finally:
        session.close()


def _is_active(uid: int) -> bool:
    session = db.get_session()
    try:
        return bool(session.query(db.TrustConfig).filter(
            db.TrustConfig.user_id == uid).first().is_active)
    finally:
        session.close()


def _pos_field(uid: int, field: str):
    session = db.get_session()
    try:
        p = session.query(db.Position).filter(
            db.Position.user_id == uid, db.Position.book == 1).first()
        return getattr(p, field) if p else None
    finally:
        session.close()


def _avail(uid: int):
    return _pos_field(uid, "available_qty")


def _frozen(uid: int):
    return _pos_field(uid, "frozen_qty")


def _cost(uid: int):
    return _pos_field(uid, "cost_price")


def _watch_count(uid: int) -> int:
    session = db.get_session()
    try:
        return session.query(db.Watchlist).filter(db.Watchlist.user_id == uid).count()
    finally:
        session.close()


def _group_count(uid: int) -> int:
    session = db.get_session()
    try:
        return session.query(db.WatchlistGroup).filter(db.WatchlistGroup.user_id == uid).count()
    finally:
        session.close()


def _group_names(uid: int) -> list[str]:
    session = db.get_session()
    try:
        return sorted(
            r[0] for r in session.query(db.WatchlistGroup.name)
            .filter(db.WatchlistGroup.user_id == uid).all()
        )
    finally:
        session.close()


def _watch_codes(uid: int) -> list[str]:
    session = db.get_session()
    try:
        return sorted(
            r[0] for r in session.query(db.Watchlist.stock_code)
            .filter(db.Watchlist.user_id == uid).all()
        )
    finally:
        session.close()


def _analysis_universe_codes(uid: int) -> list[str]:
    """走**真实的** ``trust.get_analysis_universe``，不自己拼 SQL。

    这里必须调真函数：本模块要守的正是"影子账户被镜像之后，研究层真正取到的标的集是不是
    冻结池"。自己拼一个等价查询只是在测自己的 SQL。
    """
    return [s["code"] for s in trust.get_analysis_universe(uid)]


def _cfg_field(uid: int, field: str):
    session = db.get_session()
    try:
        cfg = session.query(db.TrustConfig).filter(db.TrustConfig.user_id == uid).first()
        return getattr(cfg, field) if cfg else None
    finally:
        session.close()


def _plan_context(uid: int) -> str:
    """调**真实的** ``build_portfolio_plan_context``，只把「账户快照」那一段换成桩。

    桩掉的 ``build_portfolio_context`` 要联网取实时价；本次改动的范围研究、候选池、
    【持仓】/【候选】分列与文案约束，全部在**真代码路径**上。桩的边界写清楚，
    免得这条断言变成"测桩"。
    """
    from app import analysis_service as A

    orig = A.build_portfolio_context
    A.build_portfolio_context = lambda user_id, clock=None: "账户快照（测试桩）"
    try:
        return A.build_portfolio_plan_context(uid)
    finally:
        A.build_portfolio_context = orig


def _range_lines(text: str) -> dict:
    """从上下文里抠出「本次可操作的标的范围」那两行，返回 ``{"持仓": 行, "候选": 行}``。

    用两个前导空格区分：范围段是 ``"  【持仓】…"``，而研究摘要段的组标题是不带缩进的
    ``"【持仓】"``。取**第一次**出现（范围段在文案里靠前）。
    """
    out: dict = {}
    for line in text.splitlines():
        if line.startswith("  【持仓】"):
            out.setdefault("持仓", line)
        elif line.startswith("  【候选】"):
            out.setdefault("候选", line)
    return out


def _set_real_config(**fields) -> None:
    """改**真实**用户的托管配置。用来验证"改真实配置不影响已冻结的影子"。"""
    session = db.get_session()
    try:
        session.query(db.TrustConfig).filter(
            db.TrustConfig.user_id == owner_id()
        ).update(fields)
        session.commit()
    finally:
        session.close()


def _toggle_ok(uid: int) -> bool:
    try:
        trust.toggle_trust(uid, True)
        trust.toggle_trust(uid, False)
        return True
    except ServiceError:
        return False


def _legacy_schema_fallback() -> None:
    """模拟 ``_ensure_schema`` 还没跑的老库：``users`` 表没有 ``is_backtest`` 列。

    期望：``_iter_active_users`` 退回旧查询并告警，**调度器正常工作**（真实用户照跑）。
    安全性由"影子账户的 is_active 恒为 False"保证——那一条在第 3、4 节已单独验证。
    """
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    db.Base.metadata.create_all(eng)
    with eng.begin() as conn:
        # 用原始 SQL 重建一个没有 is_backtest 列的 users 表。不用 ALTER TABLE DROP COLUMN：
        # 那个语法要 SQLite 3.35+，而这里对版本没有必要的要求。
        conn.execute(text("DROP TABLE users"))
        conn.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, "
            "phone VARCHAR(20) NOT NULL UNIQUE, created_at FLOAT NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO users (id, phone, created_at) VALUES (1, '13800000009', 0.0)"
        ))
        conn.execute(text(
            "INSERT INTO users (id, phone, created_at) VALUES (2, 'bt-legacy', 0.0)"
        ))
        conn.execute(text(
            "INSERT INTO trust_configs (user_id, is_active, book_created, available_cash, "
            "stock_scope, style, fee_commission_rate, fee_waive_min, fee_stamp_duty_rate, "
            "agent_id, updated_at) VALUES "
            "(1, 1, 1, 0.0, 0, 1, 0.00025, 0, 0.0005, 'consortium-1', 0.0)"
        ))
        conn.execute(text(
            "INSERT INTO trust_configs (user_id, is_active, book_created, available_cash, "
            "stock_scope, style, fee_commission_rate, fee_waive_min, fee_stamp_duty_rate, "
            "agent_id, updated_at) VALUES "
            "(2, 1, 1, 0.0, 0, 1, 0.00025, 0, 0.0005, 'consortium-1', 0.0)"
        ))
    db.SessionLocal = sessionmaker(bind=eng, autoflush=False, autocommit=False)  # type: ignore

    trust._SCHEMA_FALLBACK_WARNED = False  # 让告警重新可打印，好断言"只打一次"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = trust._iter_active_users()
        got2 = trust._iter_active_users()
    logged = buf.getvalue()

    check("列不存在时不抛异常（调度器不瘫痪）", isinstance(got, list), str(got))
    check("真实用户照常被选中", 1 in got, str(got))
    check("兜底分支告警了", "is_backtest 不可用" in logged, logged[:120])
    check("告警只打一次（调度器每分钟跑一次，否则刷爆日志）",
          logged.count("is_backtest 不可用") == 1, str(logged.count("is_backtest 不可用")))
    # 只打异常类型不打 SQLAlchemy 全文：后者会把整条 SQL 和参数铺开几十行。
    check("告警不打整条 SQL", "SELECT" not in logged, logged[:200])
    check("第二次调用结果稳定", got2 == got, f"{got2} vs {got}")


if __name__ == "__main__":
    sys.exit(main())
