"""历史回测：取过去一段真实行情，让托管团队**重跑一遍**，看它当时能做成什么样。

**为什么账务落在影子账户里，而不是回测表里**

执行层（``trust._execute_plan``）、盯市（``account.get_positions``）、计划生成
（``trust.run_plan_for_user``）全部按 ``user_id`` 取数。让它们改成读回测表，等于给回测
再写一套执行逻辑——而「两套逻辑口径必然漂移」正是本方案从头到尾在躲的坑
（见 ``app/market_clock.py`` 的说明）。

于是反过来做：给回测一个**影子 user**。所有既有代码原样复用，隔离由 ``user_id`` 天然完成
——每张表都按 ``user_id`` 分区，``TrustPlan`` 唯一键 ``(user_id, trade_date)``，
``EngineRun`` ``(user_id, ticker, market, trade_date)``。``trust._clear_book_runtime`` 的
DELETE 全带 ``user_id`` 过滤，**永远够不到真实账簿**。

**必须堵的那个实盘漏洞**：``trust._iter_active_users`` 会捞走所有 ``is_active=True`` 的
``TrustConfig``，而 cron ``_sched_execution`` 周一到五 9:00-14:59 **每分钟**跑一次。影子账户
只要 ``is_active=True`` 就会被**实盘调度器拿实时价下单**。两道卡口见 ``db.User.is_backtest``
的列注释与 ``trust.toggle_trust``。

**一个必须声明的限制**：``EngineRun`` 缓存键**不含组合上下文**（``run_analysis_cached`` 的
缓存只看 ``(user_id, ticker, trade_date)``），所以同一天、不同起始资金的两次回测会复用同一份
研究报告。实盘也有同样隐患。要重跑就传 ``fresh_research=True``，起跑时删掉影子账户的缓存。
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from app import account as account_mod
from app import backtest_calc as calc
from app import backtest_data as bd
from app import db
from app import trade as trade_mod
from app.errors import PlanCancelled, ServiceError
from app.market_clock import HistoryClock
# build_ladders 是纯函数：把一批动作排成逐票的梯子。这里只用它**取票号**（监控卡片要按
# 当天计划涉及的票去读缓存），触发判据仍在 trust.get_monitor_conditions 里，只有那一份。
from app.plan_actions import build_ladders
from app import trust as trust_mod
from tradingagents.agents.utils.structured import AccountLevelLLMError
from tradingagents.asof import asof_scope
from tradingagents.dataflows.errors import NoMarketDataError, VendorError, VendorRejectedError

logger = logging.getLogger(__name__)

#: 影子账户的手机号前缀。**只是给人看的标记**——正确性一律靠 ``User.is_backtest`` 列，
#: 不靠前缀解析。``bt-`` + 12 位十六进制 = 15 字符，落在 ``String(20)`` 内且 unique；
#: 短信验证码登录不可达（没有这个号码）。
SHADOW_PHONE_PREFIX = "bt-"

#: 从真实托管配置里**冻结**到回测的快照字段。回测中途用户改了托管设置，不能影响已在跑的
#: 这次回测——否则同一份结果里的前半段和后半段用的规则不一样。
_SETTING_FIELDS = (
    "stock_scope",
    "stock_scope_group",
    "style",
    "risk_max_trades_day",
    "risk_max_position_pct",
    "risk_stop_loss_pct",
    "fee_commission_rate",
    "fee_waive_min",
    "fee_stamp_duty_rate",
    "agent_id",
)


# ---------------------------------------------------------------- 读取


def get_run(run_id: int) -> dict | None:
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        return _run_to_dict(row) if row else None
    finally:
        session.close()


def _run_to_dict(row) -> dict:
    return {
        "run_id": row.id,
        "user_id": row.user_id,
        "shadow_user_id": row.shadow_user_id,
        "status": row.status,
        "stage": row.stage,
        "message": row.message,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "init_mode": row.init_mode,
        "init_cash": row.init_cash,
        "init_basis": row.init_basis,
        "init_positions": json.loads(row.init_positions_json or "[]"),
        "universe": json.loads(row.universe_json or "[]"),
        "config": json.loads(row.config_json or "{}"),
        "done": row.done,
        "total": row.total,
        "cancel_requested": bool(row.cancel_requested),
        "last_step_date": row.last_step_date,
        "last_order_id": row.last_order_id,
        "checkpoint_json": row.checkpoint_json or "{}",
        "result": json.loads(row.result_json or "{}"),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "finished_at": row.finished_at,
    }


# ---------------------------------------------------------------- 影子账户


def ensure_shadow_user(session, owner_user_id: int) -> int:
    """该用户名下**稳定**的影子账户；没有就建一个。

    跨回测复用是刻意的：``EngineRun`` 缓存是最贵的成本项（30 天 × 5 只 ≈ 600 次 LLM 调用，
    实测约 10 小时量级），每次回测换新 user 会让这份缓存全部作废。所以一个新回测只重置
    **账务**（``reset_shadow_book``），研究报告留着。由此产生的限制见模块 docstring。

    调用方负责 commit。
    """
    row = (
        session.query(db.BacktestRun.shadow_user_id)
        .filter(
            db.BacktestRun.user_id == owner_user_id,
            db.BacktestRun.shadow_user_id.isnot(None),
        )
        .order_by(db.BacktestRun.id.desc())
        .first()
    )
    if row and row[0]:
        # 影子账户必须还在（可能被人工清理）。不在了就重建一个，而不是把新 run 挂到悬空 id 上。
        if session.query(db.User.id).filter(db.User.id == row[0]).first():
            return int(row[0])

    user = db.User(
        phone=SHADOW_PHONE_PREFIX + secrets.token_hex(6),
        created_at=time.time(),
        is_backtest=True,
    )
    session.add(user)
    session.flush()
    _ensure_shadow_config(session, int(user.id))
    return int(user.id)


def _ensure_shadow_config(session, shadow_user_id: int):
    """影子账户的托管配置。``is_active`` **硬性 False** —— 见模块 docstring 的漏洞说明。"""
    cfg = (
        session.query(db.TrustConfig)
        .filter(db.TrustConfig.user_id == shadow_user_id)
        .first()
    )
    if cfg is None:
        cfg = db.TrustConfig(
            user_id=shadow_user_id,
            is_active=False,
            book_created=True,
            available_cash=0.0,
            updated_at=time.time(),
        )
        session.add(cfg)
        session.flush()
    cfg.is_active = False
    return cfg


#: 影子账户自选股里的唯一分组名。冻结标的池被物化到这个名字下（见 ``_materialize_universe``）。
BACKTEST_GROUP = "回测标的池"

#: 冻结配置里**不**照搬到影子 cfg 上的字段。
#:
#: - ``stock_scope`` / ``stock_scope_group``：发起人托管设置的"选股范围"，与本次回测的
#:   股票范围是两件事。**影子一律用 scope=1 + group=NULL**，让"影子的自选股"精确等于
#:   冻结标的池（见 ``_materialize_universe``）。照抄的话，scope 抄到 0 会让执行层的
#:   ``empty_start``（``trust.run_execution``）不成立，空仓起步照样零成交——那正是本函数
#:   要修的那个缺陷的一半。
#: - ``agent_id``：全仓库无人读取（只有 ``db.py`` / 本模块 / ``trust.get_trust`` 出现），
#:   写不写都没有行为差异，不搬。
_MIRROR_SKIP_FIELDS = frozenset({"stock_scope", "stock_scope_group", "agent_id"})


def _apply_frozen_config(cfg, frozen: dict) -> None:
    """把**发起时冻结**的托管设置写到影子账户的 TrustConfig 上。**幂等**。

    ## 为什么必须做

    影子账户的 TrustConfig 此前是 ``_ensure_shadow_config`` 建的一个**默认值行**
    （``stock_scope=0``、费率与风控参数全是 db 默认），而执行层（``trust.run_execution``
    的 scope/风控闸门）、成交费（``trade.place_order`` 的 ``_calc_fee``）、Stage2 的风控
    文案读的都是它。于是"发起时冻结"这件事只冻结在了 ``BacktestRun.config_json`` 里，
    **从来没生效过**——用户设置的手续费率、单票上限、止损线、单日笔数额度，回测里用的
    全是默认值。这是静默的：结果照出，只是规则不是用户的那一套。

    ## 绝不碰的字段

    ``available_cash`` / ``book_created`` / ``broker_mv`` / ``broker_pnl`` **不在**
    ``_SETTING_FIELDS`` 里，本函数够不到它们——这是刻意的：现金与建簿状态属于**账务**，
    归 ``reset_shadow_book`` / ``apply_initial_state`` / ``_restore_checkpoint`` 管。
    将来若往 ``_SETTING_FIELDS`` 加金额型字段，会覆盖检查点恢复的现金，务必先读这段。
    """
    for k in _SETTING_FIELDS:
        if k in _MIRROR_SKIP_FIELDS:
            continue
        if k in frozen:
            setattr(cfg, k, frozen[k])
    # 影子账户的选股范围**恒为** 1（仅自选）+ 不限定分组：它的自选股就是冻结标的池，
    # 于是 get_managed_symbols / get_analysis_universe 取出来的正好是冻结池。
    cfg.stock_scope = 1
    cfg.stock_scope_group = None
    cfg.is_active = False  # 实盘调度器防护，任何路径下都不能被打开


def _materialize_universe(session, shadow_user_id: int, universe: list[dict]) -> int:
    """把**冻结标的池**写进影子账户的自选股，返回写入条数。**幂等**（先删后插）。

    覆盖式而不是增量：影子的自选股只该反映"这一次回测的标的池"。同一发起人的所有 run
    共用一个影子账户（``ensure_shadow_user``），残留上一个 run 的标的会让选池凭空多出票来。

    配 ``stock_scope=1`` + ``stock_scope_group=NULL``（见 ``_apply_frozen_config``），于是
    ``get_analysis_universe(影子)`` = 影子持仓 ∪ 影子自选 = 冻结池——因为冻结池在建 run 时
    已按"复制持仓模式下自动并入全部持仓"的规则**包含**了所有持仓。一处镜像，四处对齐：

    - ``trust.get_managed_symbols(sh)`` = 冻结池
    - ``trust.get_analysis_universe(sh)`` = 冻结池（Stage1 研究的标的集）
    - ``trust.run_execution`` 的 ``empty_start`` 对空仓起步恒成立
    - ``backtest._drive`` 取股票名不再为空

    ``universe`` 接受 ``[{"code","name"}]`` 或裸 code 列表（后者名字留空，由调用方兜底）。
    """
    session.query(db.Watchlist).filter(db.Watchlist.user_id == shadow_user_id).delete()
    session.query(db.WatchlistGroup).filter(
        db.WatchlistGroup.user_id == shadow_user_id
    ).delete()
    session.query(db.WatchlistMeta).filter(
        db.WatchlistMeta.user_id == shadow_user_id
    ).delete()

    now = time.time()
    session.add(db.WatchlistGroup(
        user_id=shadow_user_id, name=BACKTEST_GROUP, created_at=now,
    ))
    session.add(db.WatchlistMeta(
        user_id=shadow_user_id, active_group=BACKTEST_GROUP, created_at=now,
    ))
    seen: set[str] = set()
    n = 0
    for item in universe:
        code = item.get("code") if isinstance(item, dict) else item
        name = (item.get("name") or "") if isinstance(item, dict) else ""
        if not code or code in seen:
            continue
        seen.add(code)
        session.add(db.Watchlist(
            user_id=shadow_user_id, stock_code=code, stock_name=name,
            source=0, group_name=BACKTEST_GROUP, created_at=now,
        ))
        n += 1
    return n


def reset_shadow_book(session, shadow_user_id: int) -> dict:
    """把影子账户的账务重置到干净态：委托 / 成交 / 托管簿持仓全清，现金归零。

    一个 run 的账务就该只属于那个 run。**不 commit**，由调用方在同一事务里收尾。

    ``_clear_book_runtime`` 的 DELETE 全带 ``user_id`` 过滤——这是隔离的全部依据，
    也是本函数可以随便调而不会碰到真实账簿的原因。

    ## 这里**故意不删计划**（``plans=False``）

    影子账户跨 run 复用（``ensure_shadow_user``），所以「另一个 run 开始」这件事会落在
    同一个 user 上。计划是**那次回测唯一的历史记录**（报告页、每日监控条件都从它来），
    按运行痕迹删掉就等于让上一个 run 的报告凭空消失——这正是 09-08 那份计划消失的机制。

    与真实账簿（``plans=True``）的差别是刻意的：真实簿「重贴快照 / 重置托管」语义是这本
    簿重新开始，旧计划必须走；影子簿没有「用户重新开始」这个动作，只有「另一个 run 开始」。
    跨 run 的日期串味由 ``list_plan_reports`` / ``get_plan_report`` 按 run 的
    ``[start_date, end_date]`` 收口解决；``delete_run`` 删自己窗口内的计划。
    """
    cleared = trust_mod._clear_book_runtime(session, shadow_user_id, plans=False)
    session.query(db.Position).filter(
        db.Position.user_id == shadow_user_id, db.Position.book == 1
    ).delete()
    cfg = _ensure_shadow_config(session, shadow_user_id)
    cfg.book_created = True
    cfg.available_cash = 0.0
    cfg.broker_mv = None
    cfg.broker_pnl = None
    cfg.updated_at = time.time()
    return cleared


def apply_initial_state(session, run_row, owner_user_id: int,
                        frozen: dict | None = None,
                        frozen_universe: list | None = None) -> dict:
    """把 run 的起始状态写进影子账簿。**必须在 ``reset_shadow_book`` 之后调用。**

    - ``cash``：空仓 + ``init_cash`` 现金；
    - ``copy``：复制真实托管簿的当前持仓与现金。

    ## 冻结值必须在这里落到影子账户上

    ``frozen``（= ``run.config_json``）里的费率/风控/风格要镜像到影子的 TrustConfig，
    ``frozen_universe``（= 本次真正在用的标的池）要物化成影子的自选股。**不做这两件事，
    回测就会跑在影子账户的默认配置上**：选池为空 → Stage1 一条研究都跑不出来 →
    Stage2 看不到任何标的 → 全程零成交；费率与风控闸门也全是 db 默认值。而这一切
    **不报错**，结果页看起来只是"团队很保守"。这是本模块最贵的一个坑。

    ``frozen`` / ``frozen_universe`` 都允许为 ``None``：那时从 ``run_row`` 的
    ``config_json`` / ``universe_json`` 读。但**能显式传就显式传**——``_drive`` 解析出来的
    标的池可能来自回退（``universe_json`` 为空的旧 run），只读 ``run_row`` 会物化出一个空池。

    复制持仓时 ``available_qty`` 直接取 ``hold_qty``、``frozen_qty`` 归零：回测起始日一定是
    过去的交易日，那批股票是"窗口之前就持有的"，不存在当日买入的 T+1 冻结。照搬真实簿里
    当天的冻结数会让起始持仓在第一天莫名其妙卖不掉。

    **``init_basis`` 不在这里算**：它必须用起始日**市价**而非这里的 ``cost_price``
    （成本价会把起始日之前的历史浮盈算进回测收益，是最容易写错的一条）。
    """
    shadow_user_id = int(run_row.shadow_user_id)
    cfg = _ensure_shadow_config(session, shadow_user_id)
    # 先把发起时冻结的托管设置镜像过来（费率/风控/风格），再把冻结标的池物化成影子的自选股。
    # 顺序不能颠倒：``_ensure_shadow_config`` 可能在 cfg 不存在时新建一行全默认值。
    if frozen is None:
        frozen = json.loads(run_row.config_json or "{}")
    if frozen_universe is None:
        frozen_universe = json.loads(run_row.universe_json or "[]")
    _apply_frozen_config(cfg, frozen)
    _materialize_universe(session, shadow_user_id, frozen_universe)
    cfg.book_created = True
    copied: list[str] = []
    #: 起始持仓的**完整**快照（含数量与成本价）。已实现盈亏要靠它做加权平均成本的初值，
    #: 所以它必须随 run 持久化，而不是每天被检查点覆盖掉——调用方把它写进
    #: ``BacktestRun.init_positions_json``。
    seed: list[dict] = []

    if run_row.init_mode == "copy":
        src_cfg = (
            session.query(db.TrustConfig)
            .filter(db.TrustConfig.user_id == owner_user_id)
            .first()
        )
        if src_cfg is None:
            raise ServiceError("请先创建托管配置再发起回测")
        now = time.time()
        rows = (
            session.query(db.Position)
            .filter(db.Position.user_id == owner_user_id, db.Position.book == 1)
            .all()
        )
        for p in rows:
            if p.hold_qty <= 0:
                continue
            session.add(db.Position(
                user_id=shadow_user_id, book=1,
                stock_code=p.stock_code, stock_name=p.stock_name,
                hold_qty=p.hold_qty, available_qty=p.hold_qty, frozen_qty=0,
                cost_price=p.cost_price, updated_at=now,
            ))
            copied.append(p.stock_code)
            seed.append({
                "stock_code": p.stock_code,
                "hold_qty": int(p.hold_qty),
                "cost_price": float(p.cost_price or 0.0),
            })
        cfg.available_cash = float(src_cfg.available_cash or 0.0)
    else:
        cfg.available_cash = float(run_row.init_cash or 0.0)

    cfg.updated_at = time.time()
    return {"cash": cfg.available_cash, "positions": copied, "seed": seed}


#: 复权因子换算的生效门槛。复权因子来自真实除权事件，量级是分红/送转级别（通常 ≥1%）；
#: 而 Wind 取数只有 4 位小数，价格 ~1 元时的相对量化误差 ≤~2e-4。拿 1e-3 当界，既不会
#: 漏掉真事件，也不会让正常票（f=1）的股数被取整改动。见 ``_rescale_copied_book``。
_ADJ_EPS = 1e-3


def _rescale_copied_book(session, shadow_user_id: int, start_date: str, end_date: str,
                         day: str, all_bars: dict, seed: list[dict]
                         ) -> tuple[list[dict], list[str]]:
    """把复制来的起始持仓换算进**后复权口径**。返回 ``(seed, notes)``，原地改影子账簿。

    ## 为什么必须换

    回测全空间是后复权（``backtest_data`` 的口径 1），而真实托管簿的成本价是**不复权**盘面价。
    原样复制会让同一个持仓行里 ``cost``（不复权）与 ``price``（后复权）**不同口径**，于是
    任何"现价 vs 成本"的比较都失去意义。实测（run 4 的 159300.SZ 沪深300ETF富国，
    复权因子 **0.279**）：

    - 止损判据 ``bar.low < cost × (1−pct)`` 变成 ``1.379 < 5.031 × 0.92`` ⇒ **恒为真**；
    - 09-08 该持仓被无条件清仓：卖 10400 股只换回 **14414.40 元**，而真实市值 **51043 元**
      ⇒ 回测**凭空少掉 36629 元（−71.8%）**，且这笔钱立刻变成现金污染其后每一天；
    - ``calc.replay_realized`` 拿 seed 的成本价当加权平均成本初值 ⇒ 该票已实现盈亏同样错；
    - ``init_basis`` 变成混合口径（现金不复权 + 持仓后复权）：430876.14 对真实 464273.74。

    换算方式（``f = 后复权收盘 / 不复权收盘``，取**起始日**的值）：

    - **数量** ``Q_h = Q_r / f`` ⇒ ``Q_h × P_h(day) == Q_r × P_r(day)``，起始市值不变。
      股数取整到整股，所以实盘上差**不足一股**的后复权价（实测 159300.SZ：51697.80 对
      51698.40，约 1e-5），这是"股票只能整股持有"的必然代价，不是换算误差；
    - **成本** ``C_h = f × C_r`` ⇒ 浮亏比例与真实一致（``P_h/C_h == P_r/C_r``，**恒等式**）。

    于是止损判据化为 ``f·low_r < f·C_r·(1−pct)``，**与不复权口径完全等价**；全清时的
    已实现盈亏 ``Q_h·P_h − Q_h·C_h == Q_r·P_r − Q_r·C_r``，也逐分相等。
    ``init_basis`` 由调用方紧接着用换算后的持仓计算，因此起始净资产回到真实值。

    ## 三个刻意的取舍

    - **``|f−1| < _ADJ_EPS`` 的票原样不动**（走 ``notes`` 报出来，不静默）。没有复权因子的票
      （本例 5/6 只）**逐字节不变**——"不需要换算的票不碰"比"统一换算再取整"更安全，
      后者会把 36600 股改写成 36596 股这种无意义的抖动。
    - **取不到不复权价 = 硬错误，绝不按 f=1 兜底**。f 错了与"没换"同级：一只真实市值 5 万的
      持仓会变成 1.4 万，而结果页上**完全看不出来**。所以在这里停下来问人（与 ``_basis``
      那条 unpriced 门禁同一个立场）。
    - **股数变成"后复权等值股数"**（本例 10400 → 37302）。这是 hfq 空间里唯一自洽的表示；
      结果页必须声明，否则用户会以为账户真的多了两万股 —— 所以换算过的票都进 ``notes``。

    只在 ``init_mode == "copy"`` 且**全新起跑**时调用一次：续跑走检查点，影子里已经是
    换算后的数，再乘一次会把数量除两遍。
    """
    copied = [s for s in seed if int(s.get("hold_qty") or 0) > 0]
    if not copied:
        return seed, []
    codes = [str(s["stock_code"]) for s in copied]
    raw, notes = bd.load_unadjusted_closes(codes, start_date, end_date)
    missing = [c for c in codes if not (raw.get(c) or {}).get(day)]
    if missing:
        raise ServiceError(
            f"复制起始持仓时有 {len(missing)} 只在 {day} 取不到**不复权**行情："
            f"{'、'.join(missing)}。回测的行情是后复权、真实簿的成本价是不复权，不换算就会把"
            "「现价 vs 成本」比成两个口径——实测会把一只浮亏 2% 的健康持仓按后复权价清仓，"
            "凭空吃掉七成市值。所以这里停下而不是按 f=1 照常跑。两个办法：改用「空仓起步」、"
            "或把起始日期挪到这几只有不复权行情的那段。"
        )
    for s in copied:
        code = str(s["stock_code"])
        bar = (all_bars.get(code) or {}).get(day)
        hfq_close = float(bar.close) if bar is not None else 0.0
        raw_close = float((raw.get(code) or {}).get(day) or 0.0)
        if hfq_close <= 0 or raw_close <= 0:
            # 后复权价也缺 ⇒ 这只票整段没行情，交给调用方的 unpriced 门禁去拦（那里会停下）
            continue
        f = hfq_close / raw_close
        if abs(f - 1.0) < _ADJ_EPS:
            continue
        qty_r = int(s["hold_qty"])
        qty_h = int(round(qty_r / f))
        cost_r = float(s.get("cost_price") or 0.0)
        # 成本价**不取整**：取到 4 位会让 ``cost_h/price_h == cost_r/price_r`` 这条等价关系
        # 差出 ~3.6e-5 的相对量（对 5 万的持仓约 1.8 元）——换算本来就是为了让口径分毫不差，
        # 在最后一步引入一个四舍五入是没有理由的。
        cost_h = cost_r * f
        if qty_h <= 0 or cost_h <= 0:
            continue
        pos = (
            session.query(db.Position)
            .filter(db.Position.user_id == shadow_user_id, db.Position.book == 1,
                    db.Position.stock_code == code)
            .first()
        )
        if pos is None:
            continue
        pos.hold_qty = qty_h
        pos.available_qty = qty_h
        pos.cost_price = cost_h
        pos.updated_at = time.time()
        s["hold_qty"] = qty_h
        s["cost_price"] = cost_h
        # 窗口内 f 是否变过：变了说明窗口里吃到了除权/份额变动。后复权口径本身是连续的，
        # 起始日的换算用 f(day) 仍然正确；但这件事必须报出来——它是"为什么股数与真实不符"
        # 的一个独立原因，排查时不知道就会怀疑到换算本身上。
        drift = _adj_drift(all_bars.get(code) or {}, raw.get(code) or {}, day)
        notes.append(
            f"复制持仓已按后复权口径换算：{code} 复权因子 {f:.4f}，"
            f"{qty_r} 股 → {qty_h} 股、成本 {cost_r:.4f} → {cost_h:.4f}"
            f"（真实市值不变；这是后复权等值股数，不是账户里的真实股数）"
            + (f"；该票窗口内复权因子有变动（{drift}），说明窗口内含除权/份额变动" if drift else "")
        )
    return seed, notes


def _adj_drift(hfq: dict, raw: dict, day: str) -> str:
    """窗口内（``day`` 及之后）复权因子是否变过。变了返回一句可读的说明，否则空串。"""
    fs: list[tuple[str, float]] = []
    for d in sorted(set(hfq) & set(raw)):
        if d < day:
            continue
        try:
            c_h, c_r = float(hfq[d].close), float(raw[d])
        except Exception:  # noqa: BLE001
            continue
        if c_h > 0 and c_r > 0:
            fs.append((d, c_h / c_r))
    if len(fs) < 2:
        return ""
    lo = min(v for _, v in fs)
    hi = max(v for _, v in fs)
    if hi - lo < _ADJ_EPS:
        return ""
    return f"{fs[0][0]} {fs[0][1]:.4f} → {fs[-1][0]} {fs[-1][1]:.4f}"


# ---------------------------------------------------------------- 回测股票范围

#: 回测股票范围：持仓股 / 自选股。**二选一**（用户决定），不是可以同时勾的组合。
BT_SCOPE_HOLDINGS = 0
BT_SCOPE_WATCHLIST = 1


def resolve_range(owner_user_id: int, bt_scope: int, bt_groups: list[str] | None = None,
                  init_mode: str = "cash") -> dict:
    """把「回测股票范围」解析成**冻结标的池**。返回:

    ``{"codes": [...], "names": {code: name}, "merged": [...],
       "missing_groups": [...], "label": "自选股 · 科技 等 2 组（8 只）"}``

    - ``bt_scope == 0``：发起人托管簿的全部持仓（``hold_qty > 0``）
    - ``bt_scope == 1``：发起人自选股；``bt_groups`` 非空时只取这几组，
      传空/不传 = 全部分组（与托管设置里「全部自选分组」的语义一致）
    - ``init_mode == "copy"``：**自动并入**发起人全部持仓——起始状态要复制持仓，
      而范围里没有的持仓是复制不过去的，会让回测的起点与实际账户对不上。
      被并入的 code 放在 ``merged`` 里，供预览如实标注（用户决定：自动并入并标注）

    去重保序；``bt_groups`` 里已经不存在（被删掉）的组会被跳过并记进 ``missing_groups``，
    由调用方在 label / 预览里如实说明——**静默少几只票**是这类选择器最容易出的错。

    刻意**不复用** ``stock_scope`` / ``stock_scope_group``：那两个键是**发起人托管设置**
    里的选股范围，与"这次回测拿哪些票跑"是两件事。把它们混起来正是本模块修掉的那个缺陷的
    形状——校验读的键与执行读的键不是同一个。
    """
    codes: list[str] = []
    names: dict[str, str] = {}
    seen: set[str] = set()
    held: list[tuple[str, str]] = []

    def _add(code: str, name: str | None) -> None:
        code = (code or "").strip()
        if not code or code in seen:
            return
        seen.add(code)
        codes.append(code)
        names[code] = (name or "").strip()

    session = db.get_session()
    try:
        held = [
            (p.stock_code, p.stock_name or "")
            for p in session.query(db.Position).filter(
                db.Position.user_id == owner_user_id,
                db.Position.book == 1,
                db.Position.hold_qty > 0,
            ).all()
        ]
        missing_groups: list[str] = []
        if bt_scope == BT_SCOPE_WATCHLIST:
            # ``requested`` 区分了"一个分组都没选"（= 全部分组）与"选的分组全都不存在"
            # （= 空池）。少了这层区分，请求一个已删除的分组会**悄悄退回全部分组**——
            # 用户以为只跑了科技组，实际跑的是整个自选股，且不报错。
            requested = [g for g in (bt_groups or []) if g and g.strip()]
            wanted = list(requested)
            if requested:
                existing = {
                    r[0] for r in session.query(db.WatchlistGroup.name)
                    .filter(db.WatchlistGroup.user_id == owner_user_id).all()
                }
                missing_groups = [g for g in requested if g not in existing]
                wanted = [g for g in requested if g in existing]
            q = session.query(db.Watchlist).filter(db.Watchlist.user_id == owner_user_id)
            if requested:
                # 选的分组一个都不存在 ⇒ **空池**，而不是"没筛选"的全量池
                q = q.filter(db.Watchlist.group_name.in_(wanted)) if wanted else None
            if q is not None:
                for w in q.order_by(db.Watchlist.id).all():
                    _add(w.stock_code, w.stock_name)
        else:
            for code, name in held:
                _add(code, name)
    finally:
        session.close()

    # copy 模式把范围外的持仓并进来。**必须在自选股的组过滤之后做**——并入的是持仓，
    # 与选了哪几个分组无关。
    merged: list[str] = []
    if init_mode == "copy":
        for code, name in held:
            if code and code not in seen:
                _add(code, name)
                merged.append(code)

    return {
        "codes": codes,
        "names": names,
        "merged": merged,
        "missing_groups": missing_groups,
        "label": _range_label(bt_scope, bt_groups, len(codes), missing_groups, len(merged)),
    }


def _range_label(bt_scope: int, bt_groups: list[str] | None, n: int,
                 missing_groups: list[str], n_merged: int) -> str:
    """给人看的一句话描述。**如实**是关键：少了几只、并了几只都要说。"""
    if bt_scope == BT_SCOPE_WATCHLIST:
        wanted = [g for g in (bt_groups or []) if g and g.strip()]
        if not wanted:
            head = "自选股 · 全部分组"
        elif len(wanted) <= 2:
            head = "自选股 · " + "、".join(wanted)
        else:
            head = f"自选股 · {wanted[0]}、{wanted[1]} 等 {len(wanted)} 组"
    else:
        head = "持仓股"
    label = f"{head}（{n} 只）"
    if missing_groups:
        label += f"；分组 {'、'.join(missing_groups)} 已不存在，已跳过"
    if n_merged:
        label += f"；另自动并入 {n_merged} 只持仓股"
    return label


# ---------------------------------------------------------------- 创建 run


def create_run(
    owner_user_id: int,
    *,
    start_date: str,
    end_date: str,
    init_mode: str = "cash",
    init_cash: float = 0.0,
    universe: list[str] | None = None,
    total: int = 0,
    extra_config: dict | None = None,
) -> int:
    """建一个 ``BacktestRun`` 行并挂上影子账户，返回 run_id。**不起跑**（起跑见 ``start_run``）。

    ``config`` 在**这一刻**从真实托管配置冻结下来，之后用户在界面上改托管设置不影响本次
    回测——否则同一份结果里的前半段与后半段会跑在不同规则下，结果无法解释。
    """
    if init_mode not in ("cash", "copy"):
        raise ServiceError("起始状态只能是 cash 或 copy")
    if not start_date or not end_date or start_date > end_date:
        raise ServiceError("回测日期区间不合法")

    session = db.get_session()
    try:
        src = (
            session.query(db.TrustConfig)
            .filter(db.TrustConfig.user_id == owner_user_id)
            .first()
        )
        if src is None:
            raise ServiceError("请先创建托管配置再发起回测")

        config = {k: getattr(src, k) for k in _SETTING_FIELDS}
        config.update(extra_config or {})

        now = time.time()
        run = db.BacktestRun(
            user_id=owner_user_id,
            status="pending",
            start_date=start_date,
            end_date=end_date,
            init_mode=init_mode,
            init_cash=float(init_cash or 0.0),
            universe_json=json.dumps(list(universe or []), ensure_ascii=False),
            config_json=json.dumps(config, ensure_ascii=False),
            total=int(total or 0),
            created_at=now,
            updated_at=now,
        )
        session.add(run)
        session.flush()
        # 注意顺序：先 flush 出 id，再挂影子账户。ensure_shadow_user 查的是**本用户名下
        # 已有的 run**，刚 flush 的这条 shadow_user_id 还是空，所以不会被自己选中。
        run.shadow_user_id = ensure_shadow_user(session, owner_user_id)
        session.commit()
        return int(run.id)
    finally:
        session.close()


# ---------------------------------------------------------------- 主循环

#: 同一模拟日内最多推进几档梯子。``_pick_tier`` 是**无状态**的（"当前持仓占比本身就是进度"），
#: 所以同一天内重复调用 ``run_execution`` 就会自动逐档推进——这正是实盘"每分钟走一步"的等价物。
#: 3 档覆盖了"一次跳空穿越多档"的常见量级；再多只是把同一天来回跑，收益递减。
DEFAULT_SUB_TICKS = 3

#: 连续这么多天连计划都生成不出来就判定为系统性故障（配额/鉴权/网络），中止整个 run。
#: 单日偶发失败只记 degraded 继续跑——十小时的长跑不该被一次 LLM 抖动废掉，但也**绝不能**
#: 悄悄退化成"团队连续几天什么都没做"（那正是回测唯一要验的东西）。
_MAX_CONSECUTIVE_ERRORS = 3

#: 本进程正在推的 run。只用于挡住"同一个进程里点两次起跑"，跨进程的防双驱靠 ``worker_token``。
_ACTIVE_RUNS: set[int] = set()
_ACTIVE_LOCK = threading.Lock()


def start_run(run_id: int, *, fresh_research: bool = False) -> bool:
    """起跑或续跑一个 run。返回 False = 已有别的 worker 在推它。

    **为什么进度入 DB 而不是进程内 dict**：既有的 ``trust._plan_tasks`` /
    ``analysis_service._tasks`` 是进程内 dict（注释自认"多进程部署需换 Redis/DB"），
    而回测动辄数小时、必然经历重启。所以 ``BacktestRun`` 自己就是进度表 + 检查点，
    进程重启后 ``resume_orphan_runs`` 接着跑。

    ``fresh_research=True``：起跑前删掉影子账户的 ``EngineRun`` 缓存，强制重跑深度研究。
    默认复用（缓存键不含组合上下文，见模块 docstring 的限制说明）。
    """
    token = secrets.token_hex(16)
    session = db.get_session()
    try:
        run = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        if run is None:
            raise ServiceError("回测不存在")
        if run.status in ("done", "cancelled"):
            raise ServiceError("该回测已结束")
        if run.status == "running" and run.worker_token:
            raise ServiceError("该回测已在运行中")
        _validate_startable(run)

        if fresh_research and run.shadow_user_id:
            session.query(db.EngineRun).filter(
                db.EngineRun.user_id == int(run.shadow_user_id)
            ).delete(synchronize_session=False)

        run.worker_token = token
        run.status = "running"
        run.stage = "loading"
        run.message = "正在准备"
        run.cancel_requested = False
        run.finished_at = None
        run.updated_at = time.time()
        session.commit()
    finally:
        session.close()

    with _ACTIVE_LOCK:
        _ACTIVE_RUNS.add(run_id)
    threading.Thread(
        target=_run_worker,
        args=(run_id, token, bool(fresh_research)),
        name=f"backtest-{run_id}",
        daemon=True,
    ).start()
    return True


def _validate_startable(run) -> None:
    """起跑前的快速否决。**宁可在这里拒掉，也不要跑十小时才发现什么都没做。**

    判据必须读**执行层真正读的那份数据**，也就是冻结的 ``universe_json``。

    这里曾经读的是 ``config_json["stock_scope"]``（发起人托管设置里的选股范围），而执行层
    读的是**影子账户**的 ``stock_scope``。两者此前从不相干——影子账户的配置是一行默认值，
    ``stock_scope=0``。于是校验放行、执行空转：空仓起步时 ``run_execution`` 每天都返回
    ``no_positions``，结果页上看起来只是"团队很保守"。**校验读的键与执行读的键不是同一个，
    这个坑就是这么来的。** 现在两者同源：影子池已被物化成冻结池，校验也读冻结池。
    """
    if not run.shadow_user_id:
        raise ServiceError("回测缺少影子账户，请删除后重新发起")
    if run.start_date > run.end_date:
        raise ServiceError("回测日期区间不合法")
    if run.init_mode == "cash":
        if float(run.init_cash or 0.0) <= 0:
            raise ServiceError("空仓起步必须给一个大于 0 的初始资金")
        if not json.loads(run.universe_json or "[]"):
            raise ServiceError(
                "空仓起步需要一个非空的回测股票范围——否则团队没有可买的标的，"
                "整轮回测会变成一场「每天什么都不做」的空转。"
                "请把回测股票范围选成「自选股」，或先把持仓股备好再发起。"
            )


def resume_orphan_runs() -> list[int]:
    """进程启动时把孤儿 run 重新拉起来，返回已重启的 run_id 列表。

    **判据是"进程刚启动"**：本仓库是单进程部署（``deploy.sh`` 起单个 uvicorn），所以启动
    那一刻任何 ``status='running'`` 的行都必然是**上一个进程**留下的——它背后没有活着的
    worker。多进程部署下这个判据不成立，那时需要真正的心跳列，别照抄这里的假设。
    """
    session = db.get_session()
    try:
        rows = (
            session.query(db.BacktestRun.id)
            .filter(db.BacktestRun.status == "running")
            .all()
        )
        ids = [int(r[0]) for r in rows]
        if ids:
            session.query(db.BacktestRun).filter(db.BacktestRun.id.in_(ids)).update(
                {"worker_token": None, "status": "pending"},
                synchronize_session=False,
            )
            session.commit()
    finally:
        session.close()

    resumed = []
    for rid in ids:
        try:
            if start_run(rid):
                resumed.append(rid)
        except Exception as e:  # noqa: BLE001 —— 一个 run 起不来不该拖住启动
            logger.warning("回测 %s 续跑失败：%s", rid, e)
    return resumed


def _run_worker(run_id: int, token: str, fresh_research: bool) -> None:
    """worker 外壳：翻译异常、收尾。**任何路径都必须让 run 落到终态**，否则它会永远停在
    ``running`` 上，而 ``resume_orphan_runs`` 每次重启都会把它再拉起来跑一遍。"""
    try:
        _drive(run_id, token, fresh_research)
    except PlanCancelled:
        _finish(run_id, token, "cancelled", "已取消")
    except Exception as e:  # noqa: BLE001
        logger.exception("回测 %s 失败", run_id)
        # ``ServiceError`` 的 message 本来就是写给用户看的（门禁拒因、日期非法、区间无行情…），
        # 进度条上那行字会原样显示给用户，加个 "ServiceError: " 前缀只是让它更难读。
        # 其余异常保留类名——那是给排查用的，用户读不懂也无妨。
        msg = str(e) if isinstance(e, ServiceError) else f"{type(e).__name__}: {e}"
        _finish(run_id, token, "failed", msg)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_RUNS.discard(run_id)


def _drive(run_id: int, token: str, fresh_research: bool) -> None:
    run = get_run(run_id)
    if run is None:
        return
    # 局部导入：``analysis_service`` 反过来会拉进 trust / account 一大串，放在模块顶层会把
    # 这条依赖提前到 import 期（仓库里 ``trust.run_plan_for_user`` 也是这么处理的）。
    from app import analysis_service as run_analysis_service

    sh = int(run["shadow_user_id"])
    cfg = run["config"] or {}
    sub_ticks = max(1, int(cfg.get("sub_ticks") or DEFAULT_SUB_TICKS))

    # ---- 标的池：发起时冻结；没冻结就回退到**发起人**当下的在管全集 ----
    # 回退要读发起人（``run["user_id"]``）而不是影子：影子池是上一次回测留下的，
    # 用它会让本次回测悄悄跑在别的池子上。测试直接造 run 时会走到这条回退。
    owner_id = int(run["user_id"])
    universe = list(run["universe"] or [])
    if not universe:
        universe = [s["code"] for s in trust_mod.get_analysis_universe(owner_id) if s.get("code")]
    if not universe:
        raise ServiceError("标的池为空，请检查回测股票范围与自选股")

    # ---- 取数。as-of 必须封顶到区间末日：get_wind_ohlcv / get_index_ohlcv 在
    #      as-of 作用域内才默认后复权，实时口径下默认前复权（会随未来分红重算历史价）。----
    _say(run_id, token, "loading", f"正在取 {len(universe)} 只标的的历史行情…")
    with asof_scope(run["end_date"]), _data_errors_as_user_text("历史行情"):
        days = bd.trading_days(run["start_date"], run["end_date"])
        if not days:
            raise ServiceError("所选区间内没有交易日，请重新选择日期")
        all_bars, warnings = bd.load_bars(
            universe, days, run["start_date"], run["end_date"],
            on_progress=lambda i, n, code: _say(
                run_id, token, "loading", f"取历史行情 {i}/{n}：{code}"
            ),
        )
        bench = bd.load_benchmark(run["start_date"], run["end_date"])

    # ---- 股票名：优先用发起时冻结的 ``bt_names``，缺失的再向**发起人**的在管全集补齐 ----
    # 不能读影子池：物化发生在下面 ``apply_initial_state`` 里，此刻影子的自选股还是上一次
    # 回测留下的残迹（全新起跑）或已被重置（续跑），取出来的名字会是空的——成交记录里
    # 股票名为空就是这么来的。
    names: dict[str, str] = dict(cfg.get("bt_names") or {})
    universe_named: list[dict] = [{"code": c, "name": names.get(c, "")} for c in universe]
    if any(not s["name"] for s in universe_named):
        for s in trust_mod.get_analysis_universe(owner_id):
            for item in universe_named:
                if not item["name"] and item["code"] == s["code"]:
                    item["name"] = s.get("name") or ""
                    names[item["code"]] = item["name"]
    for c in universe:
        names.setdefault(c, "")

    # ---- 断点续跑：有检查点就从下一天接上，否则重置账簿、从头起跑 ----
    total = len(days)
    start_idx = 0
    init_basis = float(run["init_basis"] or 0.0)
    resume_from = run["last_step_date"]
    resuming = bool(resume_from and resume_from in days and _checkpoint_usable(run))

    #: 起始簿复权换算的说明（含"哪些票被换算过"）。续跑时为空间量：换算在写检查点之前
    #: 已经做过一次，影子账簿里就是换算后的数，再乘一遍会把数量除两遍。
    scale_notes: list[str] = []

    # ---- 数据完整性门禁：**在任何 LLM 调用之前** ----
    # 取数刚做完，判断用的就是本次真正会喂给撮合的那份 bars，不是另取一次的近似。
    # 只在全新起跑时硬卡：续跑的数据在两次尝试之间变化更可能是一次瞬时抖动，
    # 为它废掉已经烧掉几小时的进度不划算（那时候按警告放行，结果页如实标注）。
    if not resuming:
        blocked = _data_gate(universe, all_bars)
        if blocked:
            raise ServiceError(blocked)

    if resuming:
        start_idx = days.index(resume_from) + 1
        session = db.get_session()
        try:
            _restore_checkpoint(session, run, sh, frozen=cfg, frozen_universe=universe_named)
            session.commit()
        finally:
            session.close()
        if not init_basis:
            raise ServiceError("检查点缺少起始基准，请删除本回测后重跑")
        _say(run_id, token, "running",
             f"从检查点续跑：已完成 {start_idx}/{total} 天")
    else:
        session = db.get_session()
        try:
            row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
            reset_shadow_book(session, sh)
            state = apply_initial_state(
                session, row, owner_id, frozen=cfg, frozen_universe=universe_named,
            )
            # **必须在算 init_basis 与落 init_positions_json 之前**换算：复制来的持仓是
            # 不复权口径，回测全空间是后复权。放晚一步，一个混合口径的起始簿会同时污染
            # 起始基准（分母）与已实现盈亏的初值（分子）。空仓起步没有复制持仓 ⇒ no-op。
            #
            # ⚠️ **必须自己再开一次 asof_scope**：上面那个 with 块在 ``load_benchmark``
            # 就结束了，此处已经在作用域之外。没有作用域 ⇒ ``_fetch_cached`` 完全不碰缓存
            # （它的承重墙就是"没有 as-of 不读缓存"），每一轮 copy 回测都会重新打 Wind，
            # 预热与断网验收全部落空 —— 而且**不会报错**，只是慢。
            with asof_scope(run["end_date"]):
                state["seed"], scale_notes = _rescale_copied_book(
                    session, sh, run["start_date"], run["end_date"], days[0],
                    all_bars, state["seed"],
                )
            session.commit()
        finally:
            session.close()
        # 门禁二：起始持仓必须**在起始日有行情**。没有行情时 ``_basis`` 会退到 cost_price，
        # 而 cost_price 是"今天"的口径 —— 它会把起始日之前累积的历史浮盈算成回测收益，
        # 也就是把一个错误的分子除以一个错误的分母（§6 那条红线）。这种失真在结果页上
        # **完全看不出来**，所以宁可在这里停下来问人。空仓起步没有起始持仓，天然不受影响。
        unpriced: list[str] = []
        init_basis = _basis(sh, days[0], all_bars, unpriced=unpriced)
        if unpriced:
            raise ServiceError(
                f"起始持仓里有 {len(unpriced)} 只在 {days[0]} 取不到行情："
                f"{'、'.join(unpriced)}。用成本价兜底会把起始日之前的历史浮盈算进回测收益，"
                "所以这里停下而不是照常跑。三个办法：把起始日期往后挪到它们有行情的那段、"
                "改用「空仓起步」、或先把这几只调出托管簿。"
            )
        session = db.get_session()
        try:
            row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
            row.init_basis = init_basis
            row.init_positions_json = json.dumps(state["seed"], ensure_ascii=False)
            row.checkpoint_json = "{}"
            row.last_step_date = None
            row.last_order_id = None
            row.done = 0
            row.updated_at = time.time()
            session.commit()
        finally:
            session.close()
        run["init_positions"] = state["seed"]

    _set_total(run_id, token, total)
    result_warnings = list(warnings) + scale_notes
    consecutive_errors = 0

    for i in range(start_idx, total):
        if _cancelled(run_id):
            raise PlanCancelled()
        d = days[i]
        bars = bd.bars_on(all_bars, d)
        clock = HistoryClock(d, bars, names=names)
        day_err = ""
        day_status = "ok"

        # (1) 执行 d 日的计划 —— 子 tick 逐档推进。实盘要几分钟走完的梯子，这里一天走完，
        #     这就是"路径歧义按不利方向假设"在数量维度上的落实。
        trades_before = _max_trade_id(sh)
        trades: list[dict] = []
        # ``run_execution`` 在"当天没有可执行的计划"时返回
        # ``{"trades": [], "skipped": "no_plan_backtest"}`` 而**不抛异常**（``trust._run_execution``）。
        # 此前这里只看 ``trades``，于是「计划丢了」与「计划在、但一档都没触发」在库里长得
        # 一模一样：都是零成交、step 都是 ok、整轮都报"完成：N 个交易日"。
        # 这是"静默空转"的**读侧**孪生——生成侧已经在下面 (4) 修过（计划层返回 None 会 raise），
        # 读侧漏了。回测唯一要回答的问题就是"团队这几天做了什么"，所以这一天必须被标出来。
        skipped = ""
        try:
            for k in range(sub_ticks):
                res = trust_mod.run_execution(sh, clock=clock.with_offset(k))
                skipped = res.get("skipped") or skipped
                got = res.get("trades") or []
                trades.extend(got)
                if not got:
                    break
        except PlanCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            # 单日异常不掀桌子：十小时的长跑不该被一次行情抖动废掉。但连错三天就是系统性
            # 故障（鉴权/配额/代码错误），继续跑只会产出一段"团队什么都没做"的假历史。
            consecutive_errors += 1
            day_status, day_err = "error", f"执行层异常：{type(e).__name__}: {e}"
            logger.exception("回测 %s 在 %s 执行失败", run_id, d)
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                # 不写 step：账务停在上一日的检查点上，是自洽的。写一条"错误日"反而会让
                # 续跑时的 T+1 / 成本价接在一个没执行完的日子上。
                raise ServiceError(
                    f"连续 {consecutive_errors} 天执行失败（{day_err}），已中止回测"
                )

        # 「今天没有计划可执行」按与执行层异常**同一口径**记 degraded。
        #
        # **首日（i == 0）豁免**：计划是「研究日日终产出、次日执行」的，回测第一天没有前置
        # 研究日，天然无计划。这是设计，不是缺陷——把它算成错误日会让每次回测的第一天都
        # degraded，还会让"连着几天都没计划"提前中止整轮（该中止的是真的丢了计划）。
        if skipped == "no_plan_backtest" and i > 0 and day_status == "ok":
            consecutive_errors += 1
            day_status = "degraded"
            day_err = f"当日没有可执行的计划（{d}）——计划行缺失，团队这一天不可能做任何事"
            logger.warning("回测 %s 在 %s 无计划可执行", run_id, d)

        # (2) T+1 释放：**只在日终一次**。子 tick 期间不释放，与实盘一致。
        try:
            trade_mod.release_t1(sh)
        except Exception as e:  # noqa: BLE001
            logger.warning("回测 %s 在 %s 释放 T+1 失败：%s", run_id, d, e)

        # (3) 日终盯市 + 落 step
        step = _mark_to_market(run_id, sh, d, clock, run, init_basis, day_status, day_err)

        # 缺计划的连错在这里中止——**放在落 step 之后**：这一天的净值已经进库，曲线不断尾，
        # 检查点也停在最后一个完整执行过的日子上（与下面计划层失败那条同一口径，测试
        # 钉的就是"中止时实库与检查点一致"）。再往后放就要先为"永远不会被执行的下一天"
        # 写一份计划，然后还得解释它为什么没人执行。
        if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            _save_step(run_id, step)
            _checkpoint(run_id, token, d, i + 1, sh)
            raise ServiceError(
                f"连续 {consecutive_errors} 天没有可执行的计划，已中止回测（{day_err}）"
            )

        # (4) 出下一交易日的计划：Stage1 as-of d + Stage2 组合决策。
        #     顺序**不能**颠倒——Stage1 注入的组合上下文必须是"d 日执行完、按 d 日收盘价计"的
        #     持仓，否则团队会拿着今天的仓位去分析半年前的行情。
        if i + 1 < total:
            try:
                _say(run_id, token, "research",
                     f"{d} 收盘：正在生成 {days[i + 1]} 的计划（第 {i + 1}/{total} 天）")
                trust_mod.run_plan_for_user(
                    sh, date=d, clock=clock,
                    should_cancel=lambda: _cancelled(run_id),
                )
                # 组合决策层的返回值**必须看**。它在结构化输出失败时返回 None 而**不抛异常**
                # （见 ``analysis_service.run_portfolio_plan_for_user``），于是 TrustPlan 不落库、
                # 次日执行层无计划可依、整轮回测一天天"什么都没做"——而 step 还会被标成 ok。
                # 实盘路径（``trust.py`` 的手动/定时入口）对同一情形是 raise 的，回测这边此前
                # 把返回值丢掉了，于是只有回测**静默**。而"团队连续几天什么都没做"正是回测
                # 唯一要验的东西（见 ``_MAX_CONSECUTIVE_ERRORS``），绝不能悄悄退化成它。
                # 所以按与写 step 同一口径当作失败日：抛出去由下面的 except 标 degraded、
                # 计入 consecutive_errors，连错 _MAX_CONSECUTIVE_ERRORS 天就中止整轮。
                plan = run_analysis_service.run_portfolio_plan_for_user(
                    sh, days[i + 1], clock=clock, research_date=d
                )
                if not plan or not plan.get("actions"):
                    raise ServiceError(
                        f"组合决策层未产出有效行动方案（{days[i + 1]}）"
                    )
                # 只在**这一天整体没问题**时才清零，而不是无条件清零。此前是无条件，于是
                # "执行层失败一天、计划生成成功一天"交替出现时计数器永远涨不到 3，连错中止
                # 形同虚设。判据取 day_status 而不是"计划这次成了"——计划成不成只说明研究层
                # 还活着，证明不了这一天团队真的做了事。
                if day_status == "ok":
                    consecutive_errors = 0
            except PlanCancelled:
                raise
            except AccountLevelLLMError as fatal:
                # 账户级失败（余额耗尽 / key 失效）**不走「连错三天」的慢性路径**：
                # 没有 LLM 就没有团队，后面每一天都会以同样的方式失败，多耗的每一天都是白烧的
                # 墙钟；更要紧的是那句中性化的「组合决策层未产出有效行动方案」根本读不出真相
                # ——第一次跑 C4 时它被当成了"API 接口中断"，查了一轮才落到 402 上。
                # 当天执行结果照常入库、检查点停在最后一个完整日，所以续跑只会多 1 个无计划日，
                # 而不是 3 个。这里沿用普通失败日的形状（step 标 degraded + 写明原因），
                # 只是**不再等**——用户可见的 run.message 由下面这个 ServiceError 决定。
                if day_status == "ok":
                    day_status = "degraded"
                day_err = f"LLM 账户不可用：{fatal}"
                step["status"], step["error"] = day_status, day_err
                logger.error("回测 %s 在 %s 中止：%s", run_id, d, day_err)
                _save_step(run_id, step)
                _checkpoint(run_id, token, d, i + 1, sh)
                raise ServiceError(
                    f"LLM 账户不可用，已立即中止回测（不是连错三天判定）：{fatal}。"
                    "请充值或更换 API key 后从检查点续跑。"
                ) from fatal
            except Exception as e:  # noqa: BLE001
                consecutive_errors += 1
                if day_status == "ok":
                    day_status = "degraded"
                day_err = f"研究/计划失败：{type(e).__name__}: {e}"
                step["status"], step["error"] = day_status, day_err
                logger.exception("回测 %s 在 %s 生成计划失败", run_id, d)
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    _save_step(run_id, step)
                    _checkpoint(run_id, token, d, i + 1, sh)
                    raise ServiceError(
                        f"连续 {consecutive_errors} 天无法生成计划（{day_err}），已中止回测"
                    )

        _save_step(run_id, step)
        _checkpoint(run_id, token, d, i + 1, sh)

    _finalize(run_id, token, init_basis, days, all_bars, bench, result_warnings)


def _checkpoint_usable(run: dict) -> bool:
    """检查点是否可用。``last_step_date`` 有值但检查点坏掉时**退回全新起跑**，而不是拿一个
    半截账簿硬续——后者会算出与前十几天不连续的收益，而结果页上看不出来。"""
    return bool(calc.parse_checkpoint(run.get("checkpoint_json")))


# ---------------------------------------------------------------- 进度 / 取消 / 收尾


def _say(run_id: int, token: str, stage: str, message: str) -> None:
    """上报进度。**带 token 校验**：一个被接管的 run 不该再被旧 worker 写进度。"""
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        if row is None or row.worker_token != token:
            return
        row.stage = stage
        row.message = message[:500]
        row.updated_at = time.time()
        session.commit()
    finally:
        session.close()


def _set_total(run_id: int, token: str, total: int) -> None:
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        if row is not None and row.worker_token == token:
            row.total = int(total)
            session.commit()
    finally:
        session.close()


def _cancelled(run_id: int) -> bool:
    session = db.get_session()
    try:
        row = (
            session.query(db.BacktestRun.cancel_requested)
            .filter(db.BacktestRun.id == run_id)
            .first()
        )
        return bool(row and row[0])
    finally:
        session.close()


def cancel_run(run_id: int, owner_user_id: int) -> bool:
    """请求取消。**不杀线程**——只在日界与计划生成的间隙生效（正在跑的 LLM 调用中断不了）。

    取消是"下一站停车"而不是"急刹"：账务停在最后一个已落的检查点上，是**一致的**。
    """
    session = db.get_session()
    try:
        row = (
            session.query(db.BacktestRun)
            .filter(db.BacktestRun.id == run_id, db.BacktestRun.user_id == owner_user_id)
            .first()
        )
        if row is None:
            raise ServiceError("回测不存在")
        if row.status in ("done", "cancelled", "failed"):
            return False
        row.cancel_requested = True
        row.updated_at = time.time()
        session.commit()
        return True
    finally:
        session.close()


def _finish(run_id: int, token: str, status: str, message: str) -> None:
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        if row is None:
            return
        # token 不匹配 = 本 run 已被别的 worker 接管，旧 worker 收尾会把状态写坏。
        if token and row.worker_token and row.worker_token != token:
            return
        row.status = status
        row.stage = status
        row.message = (message or "")[:500]
        row.finished_at = time.time()
        row.updated_at = time.time()
        if status in ("done", "cancelled"):
            row.worker_token = None
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------- 起始基准


def _basis(shadow_user_id: int, date: str, all_bars: dict,
           unpriced: list[str] | None = None) -> float:
    """起始基准 = 起始日现金 + Σ(起始持仓 × **起始日收盘价**)。

    **必须用市价，不能用 ``cost_price``**（§6）。copy 模式下的成本价是"今天"的口径，拿它当
    分母会把起始日之前累积的历史浮盈算成回测收益——这是全流程最容易写错的一条，
    也是 ``smoke_backtest_calc.py`` 专门盯的那条回归。

    取不到起始日行情的持仓按 ``cost_price`` 兜底：那说明这只票的数据整段缺失，用成本价是
    **下策里唯一不制造虚假收益的下策**（市值口径在这里不存在）。但兜底本身会让分母失真，
    所以把"兜了哪些票"通过 ``unpriced`` 回传给调用方——**由调用方决定这是可接受的降级还是
    必须停下来问人**。这里不自己抛异常：同一次遍历里既算账又判生死，会让这个纯计算函数
    的调用方（测试）也得去构造一整套策略上下文。
    """
    clock = HistoryClock(date, bd.bars_on(all_bars, date))
    total = _shadow_cash(shadow_user_id)
    for p in account_mod.get_positions(shadow_user_id, 1, clock=clock):
        qty = int(p.get("hold_qty") or 0)
        mv = p.get("market_value") or 0.0
        if not mv and qty:
            # 用**同一个** position 列表判定，而不是另起一次查询：两处各写一份
            # "哪些算持仓"的过滤条件，迟早会漂移，而漂移的后果是分母悄悄出错。
            # ``qty`` 为 0 的空仓不算——它本来就不该有市值，兜不兜底都是 0，
            # 报出来只会凭空拦下一次合法起跑。
            if unpriced is not None:
                unpriced.append(str(p.get("stock_code") or ""))
            mv = float(p.get("cost_price") or 0.0) * qty
        total += mv
    return round(total, 2)


#: 缺失比例超过这个值就**拒绝起跑**。不是精算出来的阈值，是一条"多数标的不在，跑出来的
#: 还是不是这个策略"的分界：缺一两只可以如实标注后跑，缺一半以上换来的是另一个策略的结果，
#: 却顶着同一张标的池的名字。
_MAX_MISSING_RATIO = 0.5


def _data_gate(universe: list[str], all_bars: dict) -> str:
    """全新起跑前的标的池门禁：放行返回 ``""``，否则返回拒绝的理由。

    **为什么必须有这道门**：``load_bars`` 对单只失败是"记 warning 然后继续"——这在取数层
    是对的（一只票取不到不该废掉十小时的长跑），但**如果没人把 warnings 收口**，用户看到的
    就永远是"标的 45 只"，而实际只有 40 只在跑。那不是回测，是静默的样本替换。

    拦两条：

    1. **一只都没有**：跑下去会得到一份"团队十个交易日什么都没做"的历史，而它看起来
       和"团队判断该空仓"**一模一样**——这是最坏的一种错，因为它不可辨。
    2. **缺超过一半**：剩下的池子已经不是用户选的那个池子了（结果页也会画出来）。
       缺一两只则放行：如实标注实际参与数即可，为一只停牌票拦下一次长跑是过度设计。

    **只在全新起跑时调用**。续跑已经烧掉几小时的 LLM，数据在两次尝试之间变化更可能是一次
    瞬时抖动，为它废掉已完成的十天不划算——那时候按警告放行，由结果页如实标注。
    """
    if not universe:
        return "标的池为空，请检查自选股与选股范围"
    available = [c for c in universe if (all_bars or {}).get(c)]
    missing = len(universe) - len(available)
    if not available:
        return (
            f"{len(universe)} 只标的在所选区间内均无行情。跑下去只会得到一份"
            "「团队全程没交易」的空结果，而它和「团队判断该空仓」在结果页上长得一样。"
            "请换一段区间或换一批标的。"
        )
    if missing / len(universe) > _MAX_MISSING_RATIO:
        return (
            f"{len(universe)} 只标的里有 {missing} 只在所选区间内取不到行情，"
            f"只剩 {len(available)} 只可跑——这已经不是你所选的那个标的池了。"
            "请换一段区间，或先清理自选股里长期停牌 / 已退市的票。"
        )
    return ""


#: Wind 的**每日额度**拒绝原文。它长得像限流，但**不是** ``VendorRateLimitError``：
#: 等两秒再试毫无意义，额度要到次日才重置。所以它落在 ``VendorRejectedError``（服务级拒绝）
#: 里，``backtest_data._with_retry`` 不会去退避重试——那只会把失败拖慢三次，然后照样失败。
_WIND_QUOTA_MARKS = ("请求次数超限", "次数超限", "超出每日", "每日调用次数")


@contextmanager
def _data_errors_as_user_text(what: str):
    """把厂商的取数错误翻译成**用户能照做**的中文。认不出来就包一层、保留原类名。

    **为什么需要这层**：取数失败在 ``_drive`` 里会落到 ``run.status="failed"``，那行 message
    是直接显示给用户的。厂商原文 ``VendorRejectedError: Wind index_data/get_index_kline
    返回：单日请求次数超限`` 用户读不懂——他不会知道"这是额度不是 bug"、更不会知道"要等明天"，
    最可能的反应是反复重试，把额度信息刷得更脏。

    **为什么额度耗尽必须当场停**：额度没了，交易日历（``trading_days``）第一个就取不到，
    连回测的时间轴都没有；即便绕过它，后面每只票的日线、以及分析时用的行情工具也全会失败——
    那种"跑完了但团队全程没数据"的结果页，比直接失败危险得多。所以不留降级路径。

    **只包起始取数那一段**，不包整轮：分析阶段的工具调用**必须**能失败——那是设计好的降级
    （见 ``wind.py`` 的哨兵），包进来会把"某个工具取不到数"升级成整轮回测失败。
    """
    try:
        yield
    except VendorRejectedError as e:
        text = str(e)
        if any(m in text for m in _WIND_QUOTA_MARKS):
            raise ServiceError(
                f"{what}取不到行情：**Wind 行情接口今日额度已用完**，要等次日额度重置后才能重试。"
                "回测对额度很敏感——交易日历要 1 次指数调用，标的池每只票各 1 次日线调用，"
                "所以额度一空，回测连第一步都迈不出去。这不是代码问题、也不是你的配置有问题；"
                "已跑过的交易日有研究缓存，额度恢复后重跑不会白费。"
                f"（厂商原文：{text}）"
            ) from e
        raise ServiceError(f"{what}取不到：{text}") from e
    except NoMarketDataError as e:
        # 走到这里只可能是基准指数——``load_bars`` 对单只的 NoMarketDataError 是内部消化的。
        raise ServiceError(
            f"{what}取不到行情：所选区间内没有可用的 K 线（{e}）。"
            "多半是区间太早、整段休市，或结束日还没收盘。请换一段区间再试。"
        ) from e
    except VendorError as e:
        # 兜底：限流（``_with_retry`` 已退避三次仍失败）、鉴权失败等。一律保留类名，
        # 那是给排查用的——**不要**在这里猜原因，猜错比不猜更耽误人。
        raise ServiceError(f"{what}取不到：{type(e).__name__}: {e}") from e


# ---------------------------------------------------------------- 逐日盯市


def _mark_to_market(
    run_id: int, sh: int, date: str, clock: HistoryClock, run: dict,
    init_basis: float, status: str, error: str,
) -> dict:
    """日终盯市：按当日 ``bar.close`` 计市值（停牌顺延最后有效收盘），并算出当日与累计口径。

    ``day_pnl`` = 本日总资产 − 上一日总资产；首日的上一日是 ``init_basis``。
    ``realized_pnl`` = 从 run 起点**逐笔重放**到本日的累计已实现盈亏——重放而不是逐日累加，
    是为了让续跑后的结果与一次跑完完全一致（重放只依赖库里的成交明细与起始持仓）。
    """
    positions = account_mod.get_positions(sh, 1, clock=clock)
    cash = _shadow_cash(sh)
    mv = round(sum((p.get("market_value") or 0.0) for p in positions), 2)
    total = round(cash + mv, 2)

    session = db.get_session()
    try:
        prev = (
            session.query(db.BacktestStep.total_assets)
            .filter(db.BacktestStep.run_id == run_id, db.BacktestStep.trade_date < date)
            .order_by(db.BacktestStep.trade_date.desc())
            .first()
        )
    finally:
        session.close()
    prev_total = float(prev[0]) if prev else float(init_basis or 0.0)

    day_trades = _trades_on(sh, date)
    realized = calc.replay_realized(run["init_positions"] or [], _all_trades(sh))
    return {
        "trade_date": date,
        "cash": cash,
        "market_value": mv,
        "total_assets": total,
        "day_pnl": round(total - prev_total, 2),
        "realized_pnl": realized,
        "fees": round(sum(float(t.get("fee") or 0.0) for t in day_trades), 2),
        "trade_count": len(day_trades),
        "positions": [
            {
                "stock_code": p["stock_code"], "stock_name": p.get("stock_name") or "",
                "hold_qty": p["hold_qty"], "available_qty": p["available_qty"],
                "cost_price": p["cost_price"], "price": p.get("price"),
                "market_value": p.get("market_value") or 0.0, "pnl": p.get("pnl"),
            }
            for p in positions
        ],
        "trades": day_trades,
        "plan": trust_mod._get_today_plan(sh, date=date) or {},
        "status": status,
        "error": error,
    }


def _trades_on(sh: int, date: str) -> list[dict]:
    """当日成交。用 ``traded_at`` 落在模拟日当天来划——子 tick 只改分钟，仍属同一天，
    而实盘的 ``traded_at`` 又是真实时间，所以这条筛选在两种模式下都成立。"""
    lo = datetime.strptime(date, "%Y-%m-%d").timestamp()
    hi = lo + 86400
    session = db.get_session()
    try:
        rows = (
            session.query(db.Trade)
            .filter(
                db.Trade.user_id == sh,
                db.Trade.traded_at >= lo,
                db.Trade.traded_at < hi,
            )
            .order_by(db.Trade.id)
            .all()
        )
        return [
            {
                "trade_id": t.trade_id, "order_id": t.order_id,
                "stock_code": t.stock_code, "stock_name": t.stock_name,
                "direction": t.direction, "price": t.price, "quantity": t.quantity,
                "amount": t.amount, "fee": t.fee, "ai_reason": t.ai_reason,
                "traded_at": t.traded_at,
            }
            for t in rows
        ]
    finally:
        session.close()


def _all_trades(sh: int) -> list[dict]:
    session = db.get_session()
    try:
        rows = (
            session.query(db.Trade)
            .filter(db.Trade.user_id == sh)
            .order_by(db.Trade.id)
            .all()
        )
        return [
            {"stock_code": t.stock_code, "direction": t.direction,
             "price": t.price, "quantity": t.quantity, "fee": t.fee}
            for t in rows
        ]
    finally:
        session.close()


def _shadow_cash(sh: int) -> float:
    session = db.get_session()
    try:
        row = (
            session.query(db.TrustConfig.available_cash)
            .filter(db.TrustConfig.user_id == sh)
            .first()
        )
        return round(float(row[0]), 2) if row else 0.0
    finally:
        session.close()


# ---------------------------------------------------------------- 落库：step 与检查点


def _save_step(run_id: int, step: dict) -> None:
    """写一天的净值快照（唯一键 ``(run_id, trade_date)``，重复写即覆盖）。"""
    session = db.get_session()
    try:
        row = (
            session.query(db.BacktestStep)
            .filter(
                db.BacktestStep.run_id == run_id,
                db.BacktestStep.trade_date == step["trade_date"],
            )
            .first()
        )
        if row is None:
            row = db.BacktestStep(
                run_id=run_id, trade_date=step["trade_date"], created_at=time.time()
            )
            session.add(row)
        row.cash = step["cash"]
        row.market_value = step["market_value"]
        row.total_assets = step["total_assets"]
        row.day_pnl = step["day_pnl"]
        row.realized_pnl = step["realized_pnl"]
        row.fees = step["fees"]
        row.positions_json = json.dumps(step["positions"], ensure_ascii=False)
        row.trades_json = json.dumps(step["trades"], ensure_ascii=False)
        row.plan_json = json.dumps(step["plan"], ensure_ascii=False)
        row.status = step["status"]
        row.error = (step.get("error") or "")[:500]
        session.commit()
    finally:
        session.close()


def _checkpoint(run_id: int, token: str, date: str, done: int, sh: int) -> None:
    """日终记检查点：``{cash, positions}`` + ``max(orders.id)``。

    ``positions`` 必须连 ``cost_price`` / ``available_qty`` / ``frozen_qty`` 一起存——
    少任何一个，续跑后的定仓、T+1 与已实现盈亏都会与一次跑完的结果不同。
    """
    session = db.get_session()
    try:
        row = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        if row is None or (token and row.worker_token and row.worker_token != token):
            return
        # 持仓与现金都**在本 session 内**读，不再开第二个 session：SQLite 下写事务里再开
        # 一个连接会撞锁，而这段代码是要在内存 SQLite 的冒烟里跑的。
        positions = [
            {
                "stock_code": p.stock_code, "stock_name": p.stock_name,
                "hold_qty": p.hold_qty, "available_qty": p.available_qty,
                "frozen_qty": p.frozen_qty, "cost_price": p.cost_price,
            }
            for p in session.query(db.Position)
            .filter(db.Position.user_id == sh, db.Position.book == 1)
            .all()
        ]
        cash_row = (
            session.query(db.TrustConfig.available_cash)
            .filter(db.TrustConfig.user_id == sh)
            .first()
        )
        row.last_step_date = date
        row.last_order_id = _max_order_id(session, sh)
        row.checkpoint_json = calc.serialize_checkpoint(
            float(cash_row[0]) if cash_row else 0.0, positions
        )
        row.done = int(done)
        row.updated_at = time.time()
        session.commit()
    finally:
        session.close()


def _restore_checkpoint(session, run: dict, sh: int,
                        frozen: dict | None = None,
                        frozen_universe: list | None = None) -> None:
    """把账簿恢复到检查点。**幂等**——重复调用结果相同。

    三步：① 按 ``last_order_id`` 截断半截残迹（``place_order`` 每笔独立事务提交，
    失败时会留下"委托已落库、成交没落库"的半截）；② 用快照覆盖现金与持仓；
    ③ 重新镜像冻结配置与冻结标的池。

    第 ③ 步在续跑时**也必须做**：同一发起人的所有 run 共用同一个影子账户，中途并发起
    另一个 run、或删掉另一个 run，都会把影子的自选股与配置改掉。重新物化一次（幂等、
    几十行 DB）就能自愈，否则续跑会不动声色地跑在别人的池子上。

    只删 ``id > last_order_id`` 的行、只覆盖**影子账户**的行——两道 ``user_id`` 过滤
    让这个函数够不到任何真实账簿。
    """
    blob = calc.parse_checkpoint(run.get("checkpoint_json"))
    if not blob:
        raise ServiceError("检查点已损坏，请删除本回测后重跑")

    last_oid = int(run.get("last_order_id") or 0)
    dead = (
        session.query(db.Order.id, db.Order.order_id)
        .filter(db.Order.user_id == sh, db.Order.id > last_oid)
        .all()
    )
    if dead:
        session.query(db.Trade).filter(
            db.Trade.user_id == sh,
            db.Trade.order_id.in_([d[1] for d in dead]),
        ).delete(synchronize_session=False)
        session.query(db.Order).filter(
            db.Order.user_id == sh, db.Order.id.in_([d[0] for d in dead])
        ).delete(synchronize_session=False)

    last_day = run.get("last_step_date")
    if last_day:
        # 半截的当日 step（写完 step 但还没落检查点就崩了）一并清掉，否则它会以错误的
        # 净值留在曲线上，而检查点又把账务退回了前一天。
        session.query(db.BacktestStep).filter(
            db.BacktestStep.run_id == int(run["run_id"]),
            db.BacktestStep.trade_date > last_day,
        ).delete(synchronize_session=False)

    cfg = _ensure_shadow_config(session, sh)
    # 顺序：先镜像**配置**与**标的池**，最后才写现金。现金属于账务，检查点永远权威；
    # 反过来写的话，将来若往 ``_SETTING_FIELDS`` 加了金额型字段就会覆盖掉检查点。
    _apply_frozen_config(cfg, frozen if frozen is not None else (run.get("config") or {}))
    _materialize_universe(
        session, sh, frozen_universe if frozen_universe is not None else (run.get("universe") or []),
    )
    cfg.book_created = True
    cfg.available_cash = float(blob.get("cash") or 0.0)
    cfg.updated_at = time.time()
    session.query(db.Position).filter(
        db.Position.user_id == sh, db.Position.book == 1
    ).delete(synchronize_session=False)
    now = time.time()
    for p in blob["positions"]:
        session.add(db.Position(
            user_id=sh, book=1,
            stock_code=p.get("stock_code"), stock_name=p.get("stock_name") or "",
            hold_qty=int(p.get("hold_qty") or 0),
            available_qty=int(p.get("available_qty") or 0),
            frozen_qty=int(p.get("frozen_qty") or 0),
            cost_price=float(p.get("cost_price") or 0.0),
            updated_at=now,
        ))


def _max_order_id(session, sh: int) -> int:
    row = (
        session.query(db.Order.id)
        .filter(db.Order.user_id == sh)
        .order_by(db.Order.id.desc())
        .first()
    )
    return int(row[0]) if row else 0


def _max_trade_id(sh: int) -> int:
    session = db.get_session()
    try:
        row = (
            session.query(db.Trade.id)
            .filter(db.Trade.user_id == sh)
            .order_by(db.Trade.id.desc())
            .first()
        )
        return int(row[0]) if row else 0
    finally:
        session.close()


# ---------------------------------------------------------------- 收尾


def _finalize(run_id: int, token: str, init_basis: float, days: list[str],
              all_bars: dict, bench: dict, warnings: list[str]) -> None:
    """聚合逐日 step → 结果 + 基准 + 声明，落 ``result_json`` 并把 run 置 done。"""
    session = db.get_session()
    try:
        rows = (
            session.query(db.BacktestStep)
            .filter(db.BacktestStep.run_id == run_id)
            .order_by(db.BacktestStep.trade_date)
            .all()
        )
        steps = [
            {
                "trade_date": r.trade_date, "cash": r.cash, "market_value": r.market_value,
                "total_assets": r.total_assets, "day_pnl": r.day_pnl,
                "realized_pnl": r.realized_pnl, "fees": r.fees,
                "trade_count": len(json.loads(r.trades_json or "[]")),
                "status": r.status, "error": r.error,
            }
            for r in rows
        ]
        run = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        if run is None:
            return
        if token and run.worker_token and run.worker_token != token:
            return
        config = json.loads(run.config_json or "{}")
        universe = json.loads(run.universe_json or "[]")
        done_days = [s["trade_date"] for s in steps]

        agg = calc.aggregate_steps(steps, init_basis)
        eq = calc.equal_weight_baseline(
            universe, all_bars, done_days, init_basis, _FeeCfg(config)
        )
        idx = calc.index_baseline(bench, done_days, init_basis)
        # 实际参与数**单独成一列**，不让前端从 warnings 里正则抠。缺失是"标的池被悄悄换掉"
        # 这件事的唯一凭证，它必须和 universe_size 一样是个一等字段，不能是文案。
        available = [c for c in universe if (all_bars or {}).get(c)]
        agg.update({
            "baseline_equal_weight": eq,
            "baseline_index": idx,
            "excess_return": round(agg["total_return"] - eq.get("total_return", 0.0), 4),
            "excess_return_index": round(agg["total_return"] - idx.get("total_return", 0.0), 4),
            "universe_requested": len(universe),
            "universe_loaded": len(available),
            "missing_symbols": [c for c in universe if c not in set(available)],
            "warnings": list(warnings or []),
            "disclosures": calc.disclosures_for(config),
            "start_date": run.start_date,
            "end_date": run.end_date,
        })
        run.result_json = json.dumps(agg, ensure_ascii=False)
        run.status = "done"
        run.stage = "done"
        run.done = len(steps)
        run.message = f"完成：{len(steps)} 个交易日"
        run.finished_at = time.time()
        run.updated_at = time.time()
        run.worker_token = None
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------- 查询（API 层用）


#: 第 0 步实测：一次深度分析约 35 秒、约 19 次 LLM 调用（``scripts/probe_llm_latency.py``）。
#: 用来把"预计耗时"从一个凭空的数字变成一个**可复核的估算**——结果页与预览页都按它说话。
SECONDS_PER_ANALYSIS = 35.0
CALLS_PER_ANALYSIS = 19
STAGE1_WORKERS = 4


def list_runs(owner_user_id: int, limit: int = 20) -> list[dict]:
    """该用户发起过的回测，新的在前。**不带完整结果**（只给总收益率这类摘要）。"""
    session = db.get_session()
    try:
        rows = (
            session.query(db.BacktestRun)
            .filter(db.BacktestRun.user_id == owner_user_id)
            .order_by(db.BacktestRun.id.desc())
            .limit(int(limit))
            .all()
        )
        out = []
        for r in rows:
            res = json.loads(r.result_json or "{}")
            out.append({
                "run_id": r.id, "status": r.status, "stage": r.stage, "message": r.message,
                "start_date": r.start_date, "end_date": r.end_date,
                "init_mode": r.init_mode, "init_cash": r.init_cash,
                "init_basis": r.init_basis,
                "done": r.done, "total": r.total,
                "total_return": res.get("total_return"),
                "max_drawdown": res.get("max_drawdown"),
                "created_at": r.created_at, "finished_at": r.finished_at,
                "elapsed_text": elapsed_text(r),
            })
        return out
    finally:
        session.close()


def _require_own(session, run_id: int, owner_user_id: int):
    """取本用户名下的 run。**每条 API 都必须过这里**——回测表按 user_id 分区，越权读别人的
    回测等于把别人的持仓与决策过程全抖出来。"""
    row = (
        session.query(db.BacktestRun)
        .filter(db.BacktestRun.id == run_id, db.BacktestRun.user_id == owner_user_id)
        .first()
    )
    if row is None:
        raise ServiceError("回测不存在")
    return row


def _elapsed_seconds(r):
    """已跑秒数。**跑完取 ``finished_at``，在跑取"现在"**——不是 ``updated_at``。

    差别很实在：``updated_at`` 是心跳时间，一个卡住十分钟的 run 会把已跑时长**冻在十分钟前**，
    界面看着像"十分钟没动"，实际它还在跑；反过来若拿它当"现在"，时长就永远追着心跳走。
    在跑的 run 只能拿墙上时钟算，这样 UI 每轮询一次就往前跳一格，才是"正在跑"的体感。
    """
    if not r.created_at:
        return None
    end = r.finished_at if r.finished_at else time.time()
    return max(0.0, float(end) - float(r.created_at))


def elapsed_text(r) -> str:
    """这条回测**实际跑了多久**（人话：``约 2.3 小时``）。

    此前只有 ``created_at`` / ``finished_at`` 两个裸时间戳，界面上一个都不显示——
    于是"这次跑了多久"只能靠人去库里翻，而这恰恰是决定"要不要再来一次"的那个数。
    ``_human_duration`` 的措辞与小节取数处的**预估**是同一套：预估和实际用两种说法，
    用户就没法把它们对起来。
    """
    secs = _elapsed_seconds(r)
    return "" if secs is None else _human_duration(secs)


def get_progress(run_id: int, owner_user_id: int) -> dict:
    """进度：状态 / 阶段 / 文案 / 已完成天数 / 总天数 / 已耗时。前端轮询这一个口。"""
    session = db.get_session()
    try:
        r = _require_own(session, run_id, owner_user_id)
        res = json.loads(r.result_json or "{}")
        return {
            "run_id": r.id, "status": r.status, "stage": r.stage, "message": r.message,
            "done": r.done, "total": r.total,
            "cancel_requested": bool(r.cancel_requested),
            "start_date": r.start_date, "end_date": r.end_date,
            "init_mode": r.init_mode, "init_basis": r.init_basis,
            "has_result": bool(res),
            "total_return": res.get("total_return"),
            "created_at": r.created_at, "updated_at": r.updated_at,
            "finished_at": r.finished_at,
            "elapsed_seconds": _elapsed_seconds(r),
            "elapsed_text": elapsed_text(r),
        }
    finally:
        session.close()


def get_result(run_id: int, owner_user_id: int) -> dict:
    """聚合结果 + 基准 + 声明。没跑完就只给进度，不给半截结果。"""
    session = db.get_session()
    try:
        r = _require_own(session, run_id, owner_user_id)
        steps = (
            session.query(db.BacktestStep)
            .filter(db.BacktestStep.run_id == run_id)
            .order_by(db.BacktestStep.trade_date)
            .all()
        )
        return {
            "run_id": r.id,
            "status": r.status,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "init_mode": r.init_mode,
            "init_basis": r.init_basis,
            "result": json.loads(r.result_json or "{}"),
            "steps": [
                {
                    "trade_date": s.trade_date, "cash": s.cash,
                    "market_value": s.market_value, "total_assets": s.total_assets,
                    "day_pnl": s.day_pnl, "realized_pnl": s.realized_pnl, "fees": s.fees,
                    "trade_count": len(json.loads(s.trades_json or "[]")),
                    "status": s.status, "error": s.error,
                }
                for s in steps
            ],
        }
    finally:
        session.close()


def get_trades(run_id: int, owner_user_id: int) -> dict:
    """逐日成交流水（含 AI 理由）。取 ``BacktestStep.trades_json`` 而不是再查一次 Trade 表：
    那正是"当天实际发生了什么"的快照，也是结果页与回测过程**唯一**的一致来源。"""
    session = db.get_session()
    try:
        _require_own(session, run_id, owner_user_id)
        rows = (
            session.query(db.BacktestStep)
            .filter(db.BacktestStep.run_id == run_id)
            .order_by(db.BacktestStep.trade_date)
            .all()
        )
        days = []
        total = 0
        for s in rows:
            trades = json.loads(s.trades_json or "[]")
            if not trades:
                continue
            total += len(trades)
            days.append({"trade_date": s.trade_date, "trades": trades})
        return {"run_id": run_id, "trade_count": total, "days": days}
    finally:
        session.close()


#: 「每日监控条件」的进程内缓存：``{(run_id, trade_date): (expire_at, payload)}``。
#: 每次切日期都要按当天计划涉及的票把缓存文件读出来、重建 bars；用户在日期条上会来回点，
#: 不缓存就是每次百毫秒级的重复劳动。照着 ``account._quote_cache`` 那个 60s TTL 的样子做。
_MONITOR_CACHE: dict[tuple[int, str], tuple[float, dict]] = {}
_MONITOR_CACHE_TTL = 60.0


def _invalidate_monitor_cache(run_id: int) -> None:
    """清掉某个 run 的监控条件缓存（删 run 时调：它的计划要跟着走，缓存不能留着）。"""
    for k in [k for k in _MONITOR_CACHE if k[0] == run_id]:
        _MONITOR_CACHE.pop(k, None)


def get_day_monitor_conditions(run_id: int, owner_user_id: int, trade_date: str) -> dict:
    """回测里**某一天**的监控条件卡片——按**执行口径**判定，与撮合同一个判据。

    实盘那张卡片问的是"现在该不该动手"（实时价）；回测这张问的是"那天为什么动了/没动"。
    所以价位必须是**当天真实行情**（卖/减看当日最低、买/建看当日最高、其余看收盘），
    而不是拿回测跑到此刻的价去套——后者和当天的成交根本对不上，卡片就失去了意义。

    ## 行情来源是**缓存**，不打 Wind

    按当天计划涉及的票去 ``load_bars``：窗口就是 run 自己的 ``[start_date, end_date]``，
    与起跑时 ``_drive`` 用的是同一对参数，所以预热过的窗口必然命中磁盘缓存（用
    ``scripts/verify_bars_cache.py`` 断网验过）。这里**只读计划里出现的票**而不是整个
    标的池——监控卡片用不到没被决策的标的。

    ## 没有计划就如实说没有

    返回 ``has_plan=False`` + ``plan_missing_reason``，**不返回一张"价格全空、一档都不触发"
    的假卡片**：那张卡片看起来像"团队看了但没动"，而事实是这天团队根本没有可执行的东西。
    原因词表与 ``list_plan_reports`` 一致（``first_day`` / ``missing``）。
    """
    key = (run_id, trade_date)
    hit = _MONITOR_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    session = db.get_session()
    try:
        r = _require_own(session, run_id, owner_user_id)
        if not (r.start_date <= trade_date <= r.end_date):
            raise ServiceError(f"{trade_date} 不在这次回测的区间内（{r.start_date} ~ {r.end_date}）")
        sh = int(r.shadow_user_id)
        start, end = r.start_date, r.end_date
        row = trust_mod._get_plan_row_for_date(sh, trade_date)
        first_step = (
            session.query(db.BacktestStep)
            .filter(db.BacktestStep.run_id == run_id)
            .order_by(db.BacktestStep.trade_date)
            .first()
        )
        first_day = first_step.trade_date if first_step else None
    finally:
        session.close()

    if row is None or not (row.get("plan") or {}).get("actions"):
        payload = {
            "run_id": run_id, "trade_date": trade_date, "has_plan": False,
            "plan_missing_reason": "first_day" if trade_date == first_day else "missing",
            "price_basis": "execution", "code_count": 0, "actions": [],
        }
        _MONITOR_CACHE[key] = (time.time() + _MONITOR_CACHE_TTL, payload)
        return payload

    # 只取当天计划真正涉及的票。``build_ladders`` 是纯函数，与 ``get_monitor_conditions``
    # 内部用的是同一个——这里调一次只为拿票号，判据仍在那边。
    ladders, _lw = build_ladders((row.get("plan") or {}).get("actions") or [])
    codes = list(ladders.keys())
    if not codes:
        # 计划行在、action 也在，但没有一条带 code：此时一行卡片都渲染不出来，却要为此去拿
        # 交易日历（``trading_days`` 会打 Wind）。直接给空卡片，别为一张空表跑一趟网络。
        payload = {
            "run_id": run_id, "trade_date": trade_date, "has_plan": True,
            "plan_missing_reason": "", "price_basis": "execution",
            "code_count": 0, "actions": [],
        }
        _MONITOR_CACHE[key] = (time.time() + _MONITOR_CACHE_TTL, payload)
        return payload
    with asof_scope(end), _data_errors_as_user_text("历史行情"):
        days = bd.trading_days(start, end)
        all_bars, _warnings = bd.load_bars(codes, days, start, end)
    bars = bd.bars_on(all_bars, trade_date)
    # ``now`` 取模拟日盘中（``HistoryClock`` 固定 10:00）：既过了 09:30 的时间闸门，
    # 又不会因为真实墙钟在盘前把整片档位判成"未触发"。
    now = HistoryClock(trade_date, bars).now

    payload = trust_mod.get_monitor_conditions(sh, plan_row=row, bars=bars, now=now)
    payload.update({
        "run_id": run_id,
        "has_plan": True,
        "plan_missing_reason": "",
        "price_basis": "execution",
    })
    _MONITOR_CACHE[key] = (time.time() + _MONITOR_CACHE_TTL, payload)
    return payload


# ------------------------------------------------------------ 回测流程报告
#
# 「流程报告」= 与实盘「财团托管 → 次日行动报告」**同一个渲染器**产出的那份 HTML
# （``plan_report.render_plan_report``）。回测跨多个交易日、每天一份计划，所以比实盘
# 多出来的只有一件事：**按日期取**。
#
# 两个容易写错的地方，都在这两个函数里：
# ① 计划存在**影子 user** 名下（``BacktestRun.shadow_user_id``），不是发起人；用
#    ``owner_user_id`` 去查计划会一条都查不到。所有权校验走 ``_require_own``，
#    查数据用影子 id——两件事不能混。
# ② 报告里的「账户现状」（现金/持仓）必须是**计划生成时那一刻**的账，不是回测结束时
#    的账。计划是研究日 D-1 盘后生成的，所以取 ``trade_date < D`` 的最后一个 step。
#    直接抄 ``get_plan_download_data`` 那套（读当前持仓）会让报告显示最终持仓——
#    对 09-09 那份计划来说就是"0 持仓"配 38 条建仓动作，一份自相矛盾的报告。


def list_plan_reports(run_id: int, owner_user_id: int) -> dict:
    """回测里可查看流程报告的日期清单。

    ## 骨架是 ``BacktestStep``（这个 run 真实跑过的交易日），不是 ``TrustPlan``

    早先只列「有计划行」的日期，于是**没计划的那天整个从列表里消失**：用户看到的是报告页
    凭空少了两天，而不是"这两天没有报告"。而"没计划"和"有计划但一档都没触发"是**两回事**
    ——前者说明团队那天根本不可能做任何事，后者说明它做了判断但没动手。列表必须把前者
    显式画出来，否则报告页越完整越像是在骗人。

    所以取 **step 日期 ∪ 窗口内计划日期**：
    - step 有、计划无 ⇒ ``has_plan=False`` + ``plan_missing_reason``（首日 / 真的缺）；
    - 计划有、step 无 ⇒ ``executed=False``，那一天没跑到（中止/失败），别让一份没执行过的
      计划看起来像执行过了。

    ## 计划查询必须按 run 窗口收口

    影子账户跨 run 复用，``TrustPlan`` 的唯一键只有 ``(user_id, trade_date)``。不带窗口
    过滤会把**别的回测**的计划混进这次的结果里；而回测的整个价值就在于"这一段时间里团队
    看到了什么"，串味等于污染结论。

    ``has_plan`` 判的是 ``actions`` 非空，不是"行存在"——一份 ``{}`` 的空计划落库过也
    等于没计划，执行层拿它同样什么都做不了。
    """
    session = db.get_session()
    try:
        r = _require_own(session, run_id, owner_user_id)
        steps = {
            s.trade_date: s
            for s in session.query(db.BacktestStep)
            .filter(db.BacktestStep.run_id == run_id)
            .all()
        }
        plans = (
            session.query(db.TrustPlan)
            .filter(
                db.TrustPlan.user_id == r.shadow_user_id,
                db.TrustPlan.trade_date >= r.start_date,
                db.TrustPlan.trade_date <= r.end_date,
            )
            .order_by(db.TrustPlan.trade_date, db.TrustPlan.id)
            .all()
        )
        plan_by_date: dict[str, object] = {}
        for p in plans:
            # 同一天可能有多份（跨 run 复用 + 重复生成），后写的覆盖先写的——与 ``get_plan_report``
            # 的 ``order_by(id.desc()).first()`` 同一口径，两处不能分家。
            plan_by_date[p.trade_date] = p
        # 首日 = 这个 run 真正跑过的第一个交易日（计划是「研究日日终产出、次日执行」，
        # 首日没有前置研究日，天然无计划——这是设计，不是缺陷）。
        first_day = min(steps) if steps else None

        dates = []
        for d in sorted(set(steps) | set(plan_by_date)):
            p = plan_by_date.get(d)
            try:
                blob = json.loads(p.plan_json or "{}") if p else {}
            except Exception:
                blob = {}
            process = blob.get("process") or {}
            actions = blob.get("actions") or []
            st = steps.get(d)
            reason = ""
            if not actions:
                reason = "first_day" if d == first_day else "missing"
            dates.append({
                "trade_date": d,
                "created_at": (p.created_at or 0.0) if p else 0.0,
                "action_count": len(actions),
                "research_count": len(process.get("research") or {}),
                "research_missing": list(process.get("research_missing") or []),
                "has_plan": bool(actions),
                "plan_missing_reason": reason,
                "executed": st is not None,
                "trade_count": len(json.loads(st.trades_json or "[]")) if st else 0,
                "status": (st.status if st else "") or "",
                "error": (st.error if st else "") or "",
            })
        return {"run_id": run_id, "dates": dates}
    finally:
        session.close()


def get_plan_report(run_id: int, owner_user_id: int, trade_date: str) -> dict | None:
    """某一天的流程报告数据：计划全文 + **研究日当时**的账户快照。该日无计划返回 None。

    返回值与 ``trust.get_plan_download_data`` 同形（``plan`` / ``positions`` /
    ``research`` / ``research_missing`` / ``research_frozen`` / ``cash`` / ``trade_date``
    / ``created_at``），好让路由能直接喂给 ``plan_report.render_plan_report``——
    格式一致是靠"同一个渲染器 + 同形的入参"保证的，不是靠两边各写一遍。
    """
    session = db.get_session()
    try:
        r = _require_own(session, run_id, owner_user_id)
        # 窗口收口：影子账户跨 run 复用，计划行的唯一键只有 (user_id, trade_date)。
        # 不限窗口的话，同一天里**别的回测**写的计划会被当成本次回测的计划渲染出来——
        # 而那份计划可能来自更晚的运行，正是本函数下面刻意躲开的未来函数。
        row = (
            session.query(db.TrustPlan)
            .filter(
                db.TrustPlan.user_id == r.shadow_user_id,
                db.TrustPlan.trade_date == trade_date,
                db.TrustPlan.trade_date >= r.start_date,
                db.TrustPlan.trade_date <= r.end_date,
            )
            .order_by(db.TrustPlan.id.desc())
            .first()
        )
        if row is None:
            return None
        try:
            plan = json.loads(row.plan_json or "{}")
        except Exception:
            plan = {}
        created_at = row.created_at or 0.0

        prev = (
            session.query(db.BacktestStep)
            .filter(
                db.BacktestStep.run_id == run_id,
                db.BacktestStep.trade_date < trade_date,
            )
            .order_by(db.BacktestStep.trade_date.desc())
            .first()
        )
        if prev is not None:
            cash = float(prev.cash or 0.0)
            positions = json.loads(prev.positions_json or "[]")
            as_of, as_of_source = prev.trade_date, "step"
        else:
            # 只有"该计划生效日之前一天都没跑过"才会走到这——正常回测里计划至少落在
            # 第二个交易日，所以前一日必有 step，这一支是**防御性**的。真走到这，用 run 的
            # 起始状态，并如实把来源标出来（前端会显示"起始状态"而不是假装是某天的收盘账）。
            #
            # ``init_basis`` 要等起跑第一步才由 ``_basis`` 算出来（未起跑是 0），
            # 所以这里再退一档到 ``init_cash``——否则一个还没起跑的 run 会显示总资产 0。
            cash = float(r.init_basis or r.init_cash or 0.0)
            positions = json.loads(r.init_positions_json or "[]")
            as_of, as_of_source = "", "initial"
    finally:
        session.close()

    total_assets = round(cash + sum(float(p.get("market_value") or 0.0) for p in positions), 2)
    for p in positions:
        # 渲染器按 assets_ratio 出「占比」列；step 的快照里没有这个字段（它是账户层
        # 算的），这里按同一口径补上：占比 = 市值 / 总资产。
        p.setdefault("market_value", 0.0)
        p["assets_ratio"] = round(float(p["market_value"]) / total_assets, 6) if total_assets else 0.0

    process = plan.get("process") or {}
    research = process.get("research") or {}
    if research:
        missing = list(process.get("research_missing") or [])
        frozen = True
    else:
        # **不回落到实时查库**。``collect_research_snapshot`` 不带研究日时会取"最近一次"
        # EngineRun，而影子 user 的研究结果跨多个研究日累积——那样取到的可能是**更晚**
        # 的日期，即未来函数。宁可如实报"这份计划没有研究依据"，也不给一份日期对不上的
        # 研究报告：报告的用途正是判断"团队当时凭什么下的这个决定"。
        missing = list(dict.fromkeys(a.get("code") for a in (plan.get("actions") or []) if a.get("code")))
        frozen = False

    return {
        "run_id": run_id,
        "plan": plan,
        "positions": positions,
        "research": research,
        "research_missing": missing,
        "research_frozen": frozen,
        "cash": cash,
        "total_assets": total_assets,
        "trade_date": trade_date,
        "created_at": created_at,
        "as_of": as_of,
        "as_of_source": as_of_source,
    }


def _delete_run_plans(session, run_row) -> int:
    """删掉这个 run 窗口内的次日行动计划，**但留给同窗口的其它 run**，返回条数。

    为什么要删：「删掉这次回测，它的报告就该一起消失」——报告页与每日监控条件都是
    ``TrustPlan`` 的渲染，行留着它就会出现在同窗口另一个 run 的报告页里，而那份计划可能
    来自更晚的运行，正是 ``get_plan_report`` 一直在躲的未来函数。

    为什么不能无脑按窗口删：影子账户跨 run 复用，``TrustPlan`` 的唯一键是
    ``(user_id, trade_date)``——**同窗口的两个 run 共用同一批计划行**（后跑的覆盖先跑的）。
    阶段 C 的矩阵里 C1 与 C3 就是同一个 5 日窗口，删掉 C1 不该顺手清空 C3 的报告。

    用字符串区间比较而不是 ``bd.trading_days``：后者要读基准指数缓存，未命中时**会真打
    Wind**（``delete_run`` 不在 as-of 作用域里，缓存直接穿透）。删一条记录不该产生网络调用。
    ``trade_date`` 是定长零填充的 ``YYYY-MM-DD``，字典序即日期序。
    """
    sh = run_row.shadow_user_id
    if not sh:
        return 0
    others = (
        session.query(db.BacktestRun)
        .filter(
            db.BacktestRun.shadow_user_id == sh,
            db.BacktestRun.id != run_row.id,
            db.BacktestRun.start_date <= run_row.end_date,
            db.BacktestRun.end_date >= run_row.start_date,
        )
        .all()
    )
    q = session.query(db.TrustPlan).filter(
        db.TrustPlan.user_id == int(sh),
        db.TrustPlan.trade_date >= run_row.start_date,
        db.TrustPlan.trade_date <= run_row.end_date,
    )
    for o in others:
        q = q.filter(~db.TrustPlan.trade_date.between(o.start_date, o.end_date))
    return int(q.delete(synchronize_session=False) or 0)


def delete_run(run_id: int, owner_user_id: int) -> dict:
    """删除一个回测：结果 + 逐日快照 + 影子账簿的运行痕迹。

    **影子账户本身留着**——它承载 ``EngineRun`` 研究缓存（30 天 × 5 只 ≈ 600 次 LLM 调用，
    十小时量级）。删掉它等于把最贵的东西扔了，下一次回测要从头烧一遍。

    影子账簿的运行痕迹走 ``reset_shadow_book``（它不碰计划），所以计划在这里**显式**按本
    run 窗口删，见 ``_delete_run_plans``。两件事分开做，是为了让「重置影子账务」与「删掉
    这次回测的报告」各自有可解释的语义，而不是一个动作顺手把两样都干了。
    """
    session = db.get_session()
    try:
        r = _require_own(session, run_id, owner_user_id)
        with _ACTIVE_LOCK:
            alive = run_id in _ACTIVE_RUNS
        if r.status == "running" and alive:
            raise ServiceError("回测正在运行，请先取消再删除")
        if r.status == "running":
            # 状态是 running 但本进程没有它的 worker：上一个进程留下的孤儿（`kill -9` 之后
            # 谁都来不及改状态）。**单进程部署下 `_ACTIVE_RUNS` 就是权威判据**，不放行的话
            # 这条记录会一直卡着删不掉，得等到下次重启才被 resume_orphan_runs 认领。
            logger.warning("回测 %s 状态为 running 但没有活着的 worker，按孤儿删除", run_id)
        sh = r.shadow_user_id
        session.query(db.BacktestStep).filter(db.BacktestStep.run_id == run_id).delete(
            synchronize_session=False
        )
        session.delete(r)
        n_plans = _delete_run_plans(session, r)
        if sh:
            reset_shadow_book(session, int(sh))
        session.commit()
        # 监控卡片缓存里存的是**计划行**的渲染结果，而计划刚被删——不清的话 60 秒内回看
        # 这个 run 还能看到已经不存在的卡片。
        _invalidate_monitor_cache(run_id)
        return {"run_id": run_id, "deleted": True, "plans": n_plans}
    finally:
        session.close()


def preview(owner_user_id: int, start_date: str, end_date: str,
            probe: bool = False, init_mode: str = "cash",
            bt_scope: int | None = None, bt_groups: list[str] | None = None) -> dict:
    """发起前的预览：交易日数、标的数、预计调用次数与耗时、以及各种硬性校验。

    **耗时是估算而不是实测**：按第 0 步标定的单次分析 35 秒 / 19 次 LLM 调用，
    ``ceil(标的数 / 4 并发) × 单只耗时 × 天数``。真实值只会更高（还要加 Stage2 与工具轮次），
    所以文案里说的是"至少"。

    ``probe=True`` 时**真去取一次数**，把"45 只里 40 只有数据"摆在发起之前，而不是让用户
    在十小时之后才发现。代价是 N 次 Wind 调用（外加几十秒），所以它是个显式选项、不是默认
    行为——预览按钮不该突然变慢。取回的数据会落进 ``backtest_data`` 的磁盘缓存，所以
    **探数同时也是给正式起跑预热**：点过「检查数据」之后再起跑，取数那一步是零网络调用。

    ``bt_scope``：回测股票范围（见 ``resolve_range``）。**``None`` = 沿用老行为**
    （``get_analysis_universe`` 的在管全集），既有调用与既有断言一字不动；给了才按范围解析。
    预览与起跑**必须用同一套解析**——``_data_gate`` 的注释写得很清楚："预览说能跑而实际被
    拦下比不预览更糟"，范围解析同理：预览说"8 只"而实际跑 12 只，用户看到的就是假账。
    """
    # 先验格式再比大小：``"2026-3-2"`` 能通过字符串比较，却会在下游的交易日历里错位。
    # **注意 ``strptime`` 对补零是宽容的**（``%m`` / ``%d`` 都收 "3" / "2"），所以光 parse
    # 不够——必须再 format 回去比对，才能确认字符串本身就是规范写法。
    for label, v in (("起始日期", start_date), ("结束日期", end_date)):
        try:
            ok = datetime.strptime(v, "%Y-%m-%d").strftime("%Y-%m-%d") == v
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise ServiceError(f"{label}格式不对，应为 YYYY-MM-DD")
    if start_date > end_date:
        raise ServiceError("回测日期区间不合法（起始日期晚于结束日期）")
    today = datetime.now().strftime("%Y-%m-%d")
    if end_date > today:
        raise ServiceError("回测只能取已经发生的行情，结束日期不能晚于今天")

    range_label = ""
    merged_holdings: list[str] = []
    if bt_scope is None:
        universe = [
            s["code"] for s in trust_mod.get_analysis_universe(owner_user_id) if s.get("code")
        ]
    else:
        rr = resolve_range(owner_user_id, bt_scope, bt_groups, init_mode)
        universe = rr["codes"]
        range_label = rr["label"]
        merged_holdings = rr["merged"]
    with asof_scope(end_date), _data_errors_as_user_text("交易日历"):
        days = bd.trading_days(start_date, end_date)
        probe_result = _probe_availability(universe, days, start_date, end_date) if probe else None
    n = len(universe)
    waves = (n + STAGE1_WORKERS - 1) // STAGE1_WORKERS if n else 0
    est_seconds = len(days) * waves * CALLS_PER_ANALYSIS * SECONDS_PER_ANALYSIS

    # 探数才有的两列：标的池层面的门禁（与 ``_drive`` 用**同一个** ``_data_gate``，
    # 不复制一份判据——预览说"能跑"而实际被拦下，比不预览更糟）。
    gate = ""
    if probe_result is not None:
        gate = _data_gate(universe, probe_result["all_bars"])
    held_unpriced = _held_without_start_bar(
        owner_user_id, days[0] if days else "", probe_result["all_bars"] if probe_result else None
    ) if (probe_result is not None and init_mode == "copy" and days) else []

    return {
        "start_date": start_date,
        "end_date": end_date,
        "trading_days": len(days),
        "universe": universe,
        "universe_size": n,
        "stage1_runs": len(days) * n,
        "llm_calls": len(days) * n * CALLS_PER_ANALYSIS,
        "est_seconds": int(est_seconds),
        "est_label": _human_duration(est_seconds),
        "probe": probe_result is not None,
        "data_available": probe_result["available"] if probe_result else None,
        "data_missing": probe_result["missing"] if probe_result else None,
        "missing_symbols": probe_result["missing_symbols"] if probe_result else [],
        "missing_held": held_unpriced,
        "range_label": range_label,
        "merged_holdings": merged_holdings,
        "blocking": bool(gate) or bool(held_unpriced),
        "block_reason": gate or (
            f"起始持仓里有 {len(held_unpriced)} 只在起始日没有行情：{'、'.join(held_unpriced)}。"
            "用成本价兜底会把历史浮盈算进回测收益，所以会被拦下——请把起始日期往后挪、"
            "改用「空仓起步」，或先把这几只调出托管簿。" if held_unpriced else ""
        ),
        "warnings": _preview_warnings(
            len(days), n, start_date, end_date, today, gate=gate, held_unpriced=held_unpriced
        ),
    }


def _probe_availability(universe: list[str], days: list[str],
                        start_date: str, end_date: str) -> dict:
    """真取一次数，看**实际**有多少标的能参与。必须整个跑在 ``asof_scope`` 里。"""
    all_bars, warn = bd.load_bars(universe, days, start_date, end_date)
    available = [c for c in universe if all_bars.get(c)]
    return {
        "all_bars": all_bars,
        "available": len(available),
        "missing": len(universe) - len(available),
        # 缺失清单封顶 20 只：这是给人看的提示，不是数据接口。几百只全列出来会把
        # 面板撑爆，而第一屏之后的信息没人会读。
        "missing_symbols": [c for c in universe if not all_bars.get(c)][:20],
        "warnings": warn,
    }


def _held_without_start_bar(owner_user_id: int, day0: str, all_bars: dict | None) -> list[str]:
    """copy 模式下**会被门禁二拦下**的持仓清单：起始日取不到行情的那些。

    只在探数且 ``init_mode == "copy"`` 时调用——预览阶段还没有影子账簿，这里读的是
    **真实托管簿**的持仓，它和起跑时复制过去的那份是同一批（``apply_initial_state``
    复制 ``hold_qty > 0`` 的行）。目的是把"起跑后必被拦下"提前到点按钮之前。
    """
    if not day0 or all_bars is None:
        return []
    bars_today = bd.bars_on(all_bars, day0)
    out: list[str] = []
    for p in account_mod.get_positions(owner_user_id, 1):
        code = str(p.get("stock_code") or "")
        if int(p.get("hold_qty") or 0) > 0 and code and code not in bars_today:
            out.append(code)
    return out


def _preview_warnings(day_count: int, universe_size: int, start: str, end: str,
                      today: str, gate: str = "", held_unpriced: list[str] | None = None) -> list[str]:
    warn: list[str] = []
    # 拦下的原因放在**最前面**：它是唯一一条会让「开始回测」按不下去的东西，
    # 排在"建议先跑试点"后面会被读成又一条建议。
    if gate:
        warn.append(gate)
    if held_unpriced:
        warn.append(
            f"起始持仓里有 {len(held_unpriced)} 只在起始日没有行情："
            f"{'、'.join(held_unpriced)}——「复制当前持仓」会被拦下。"
        )
    if not universe_size:
        warn.append("标的池为空：请先在自选股里加票，或把选股范围改成「全市场」。")
    if not day_count:
        warn.append("所选区间内没有交易日（用沪深300的K线日期当日历），请重新选择。")
    if day_count * universe_size > 200:
        warn.append(
            f"这次要跑 {day_count} 天 × {universe_size} 只 = {day_count * universe_size} 次深度分析，"
            "是小时级的长任务。建议先跑 10 天试点——研究报告有缓存，续跑到 30 天不会重跑已跑过的部分。"
        )
    if (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(end, "%Y-%m-%d")).days < 1:
        warn.append("结束日期就在今天：当日行情未必完整（Wind 日线要收盘后才有），建议往前挪一天。")
    if start < "2020-01-01":
        warn.append("区间跨度过长，数据接口可能分段限流，取数会明显变慢。")
    return warn


def _human_duration(seconds: float) -> str:
    """秒 → 人话。**宁可说"约 11 小时"也不要说 39900 秒**——用户要据此决定跑不跑。"""
    s = int(max(0, seconds))
    if s < 90:
        return f"约 {s} 秒"
    if s < 3600:
        return f"约 {s // 60} 分钟"
    if s < 86400:
        h = s / 3600
        label = f"{h:.1f}" if h < 10 else f"{h:.0f}"
        if label.endswith(".0"):
            label = label[:-2]
        return f"约 {label} 小时"
    return f"约 {s / 86400:.1f} 天"


class _FeeCfg:
    """只给 ``trade._calc_fee`` 用的费率视图。

    等权基准要用**与策略同一套**费用模型（否则"团队比躺着不动强吗"里混进一个口径差），
    但费用参数存在冻结的 ``config_json`` 里而不是一个 ORM 行上，所以这里做个最小的
    属性适配，而不是把费率再抄一遍。
    """

    def __init__(self, config: dict) -> None:
        self.fee_commission_rate = float(config.get("fee_commission_rate") or 0.0)
        self.fee_waive_min = bool(config.get("fee_waive_min"))
        self.fee_stamp_duty_rate = float(config.get("fee_stamp_duty_rate") or 0.0)
