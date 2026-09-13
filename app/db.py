"""数据库模型与会话管理（PostgreSQL + SQLAlchemy 2.x）。

「模拟交易 + AI 托管」App 的数据层。时间字段统一存 Unix 时间戳（float），
与 video-note 的记账风格一致（每笔资金/持仓变动走流水或即时更新）。

双簿模型（修订版 PRD §3.2/§8）：
- 镜像簿（book=0）：同步真实券商持仓快照；现金在 accounts.available_cash（初始 50 万，同步后 0）。
- 托管簿（book=1）：托管页粘贴快照建立；现金在 trust_configs.available_cash（固定 0，换仓式）。
"""
from __future__ import annotations

import os

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://trade_master:trade_master@localhost:5432/trade_master",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    phone = Column(String(20), unique=True, nullable=False, index=True)
    created_at = Column(Float, nullable=False)
    # 回测影子账户标记。**正确性一律读这一列，不解析手机号前缀**——前缀只是给人看的。
    # 实盘调度器（trust._iter_active_users）靠它把影子账户排除在外：一旦影子账户被
    # is_active=True 选中，调度器会在真实盘中拿实时价交易回测账簿。
    is_backtest = Column(Boolean, nullable=False, default=False)


class SmsCode(Base):
    __tablename__ = "sms_codes"

    id = Column(Integer, primary_key=True)
    phone = Column(String(20), nullable=False, index=True)
    code = Column(String(10), nullable=False)
    expires_at = Column(Float, nullable=False)
    used = Column(Boolean, nullable=False, default=False)


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(Float, nullable=False)
    expires_at = Column(Float, nullable=False)


class Account(Base):
    """镜像簿账户：现金 + 同步状态。托管簿现金见 TrustConfig.available_cash。"""

    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    available_cash = Column(Float, nullable=False, default=500000.0)  # 镜像簿现金（初始 50 万）
    has_synced = Column(Boolean, nullable=False, default=False)  # 是否已同步过真实持仓
    last_sync_at = Column(Float, nullable=True)
    created_at = Column(Float, nullable=False)
    # 券商快照口径（可选）：解析确认时用户手动校准的「总市值 / 浮动盈亏」；
    # 非空时作为镜像簿总览的权威口径展示（默认按持仓 × 实时价计算，见 account.get_account）。
    broker_mv = Column(Float, nullable=True)
    broker_pnl = Column(Float, nullable=True)


class Position(Base):
    """双簿持仓：book 0=镜像簿（同步真实持仓），1=托管簿（AI 托管）。"""

    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("user_id", "book", "stock_code", name="uq_position_book_code"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    book = Column(Integer, nullable=False, default=0)  # 0 镜像 / 1 托管
    stock_code = Column(String(20), nullable=False, index=True)  # 带市场后缀 600519.SH / 06862.HK
    stock_name = Column(String(50), nullable=False, default="")
    hold_qty = Column(Integer, nullable=False, default=0)
    available_qty = Column(Integer, nullable=False, default=0)
    frozen_qty = Column(Integer, nullable=False, default=0)  # 当日买入冻结（A股 T+1，次日释放）
    cost_price = Column(Float, nullable=False, default=0.0)
    updated_at = Column(Float, nullable=False)


class Watchlist(Base):
    """自选股。分组模型：同一标的可出现在多个命名分组，group_name 为分组名。"""

    __tablename__ = "watchlist"
    __table_args__ = (
        UniqueConstraint("user_id", "group_name", "stock_code", name="uq_watchlist_user_group_code"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    stock_code = Column(String(20), nullable=False, index=True)
    stock_name = Column(String(50), nullable=False, default="")
    source = Column(Integer, nullable=False, default=0)  # 0 用户添加 / 1 Agent 候选
    group_name = Column(String(50), nullable=False, default="默认")
    created_at = Column(Float, nullable=False)


class WatchlistMeta(Base):
    """用户自选股的分组偏好：当前激活分组（新添加/粘贴同步落在该组）。"""

    __tablename__ = "watchlist_meta"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    active_group = Column(String(50), nullable=False, default="默认")
    created_at = Column(Float, nullable=False)


class WatchlistGroup(Base):
    """自选股分组名册（含空组也能持久存在）：分组的「重命名/删除/选中」都落这里。"""

    __tablename__ = "watchlist_groups"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_watch_group_user_name"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(50), nullable=False)
    created_at = Column(Float, nullable=False)


class Order(Base):
    """委托单。source: 0 手动(预留) / 1 AI 托管。status: 0已报 1已成 2已撤 3废单。"""

    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    order_id = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    stock_code = Column(String(20), nullable=False)
    stock_name = Column(String(50), nullable=False, default="")
    direction = Column(Integer, nullable=False)  # 0 买入 / 1 卖出
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    status = Column(Integer, nullable=False, default=0)
    source = Column(Integer, nullable=False, default=1)  # 0 手动 / 1 AI
    fail_reason = Column(String(200), nullable=False, default="")
    created_at = Column(Float, nullable=False)


class Trade(Base):
    """成交记录。amount=成交金额（不含费），fee=佣金+印花税。"""

    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    trade_id = Column(String(64), unique=True, nullable=False, index=True)
    order_id = Column(String(64), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    stock_code = Column(String(20), nullable=False)
    stock_name = Column(String(50), nullable=False, default="")
    direction = Column(Integer, nullable=False)  # 0 买入 / 1 卖出
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    amount = Column(Float, nullable=False)
    fee = Column(Float, nullable=False, default=0.0)
    ai_reason = Column(Text, nullable=False, default="")
    traded_at = Column(Float, nullable=False)


class TrustConfig(Base):
    """托管配置。托管簿现金固定 0（换仓式：卖出解锁买入，买前须卖足）。"""

    __tablename__ = "trust_configs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    is_active = Column(Boolean, nullable=False, default=False)
    book_created = Column(Boolean, nullable=False, default=False)  # 是否已粘贴快照建簿
    available_cash = Column(Float, nullable=False, default=0.0)  # 托管簿现金（换仓式）
    # 建簿快照口径（可选）：解析确认时用户手动校准的总市值/浮动盈亏；非空时作托管总览权威口径。
    broker_mv = Column(Float, nullable=True)
    broker_pnl = Column(Float, nullable=True)
    # 本簿已实现盈亏（卖出时按成本价增量累计，见 trade.place_order 卖出分支）。
    # **刻意 nullable、默认 NULL**：老库里的簿建于本列存在之前，累计值不可知——
    # 留 NULL 让上层如实显示「暂未统计」，而不是拿 0 冒充一个看起来像结论的数。
    # 建簿/重贴快照时由 trust._clear_book_runtime 归零，故语义是「本簿累计」。
    realized_pnl = Column(Float, nullable=True)
    stock_scope = Column(Integer, nullable=False, default=0)  # 0 仅持仓 / 1 仅自选 / 2 全市场
    stock_scope_group = Column(String(50), nullable=True)  # scope=1 时仅自选里的某一组（NULL=全部分组）
    style = Column(Integer, nullable=False, default=1)  # 0 保守 / 1 均衡 / 2 进取
    risk_max_trades_day = Column(Integer, nullable=True)  # 可选：单日最大交易次数
    risk_max_position_pct = Column(Float, nullable=True)  # 可选：单票最大仓位
    risk_stop_loss_pct = Column(Float, nullable=True)  # 可选：单票止损比例
    fee_commission_rate = Column(Float, nullable=False, default=0.00025)  # 佣金万 2.5
    fee_waive_min = Column(Boolean, nullable=False, default=False)  # 是否免 5 元最低佣金
    fee_stamp_duty_rate = Column(Float, nullable=False, default=0.0005)  # 印花税（仅 A股卖出）
    agent_id = Column(String(50), nullable=False, default="consortium-1")
    updated_at = Column(Float, nullable=False)


class EngineRun(Base):
    """深度引擎决策缓存。同日同标的命中不重跑（唯一键 user+ticker+market+trade_date）。"""

    __tablename__ = "engine_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "ticker", "market", "trade_date", name="uq_engine_run"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    ticker = Column(String(20), nullable=False, index=True)
    market = Column(String(10), nullable=False, default="")
    trade_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    rating = Column(String(20), nullable=False, default="")
    report_json = Column(Text, nullable=False, default="{}")
    created_at = Column(Float, nullable=False)


class NotificationConfig(Base):
    __tablename__ = "notification_configs"
    __table_args__ = (UniqueConstraint("user_id", "channel", name="uq_notify_user_channel"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    channel = Column(String(20), nullable=False)  # feishu / wecom / email
    webhook_url = Column(String(1000), nullable=False, default="")
    is_enabled = Column(Boolean, nullable=False, default=False)


class TrustPlan(Base):
    """组合级次日行动计划（Stage 2 组合决策层落库）。唯一键 (user_id, trade_date)。"""

    __tablename__ = "trust_plans"
    __table_args__ = (UniqueConstraint("user_id", "trade_date", name="uq_trust_plan"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    trade_date = Column(String(10), nullable=False)  # 计划针对的下一交易日 YYYY-MM-DD
    plan_json = Column(Text, nullable=False, default="{}")
    created_at = Column(Float, nullable=False)


class BacktestRun(Base):
    """一次历史回测。**只存编排状态与结果，账务一律落在影子账户里**。

    账务为什么不放这张表：执行层（``trust._execute_plan``）、盯市（``account.get_positions``）、
    计划生成（``trust.run_plan_for_user``）全部按 ``user_id`` 取数。让它们改成读回测表，等于
    给回测再写一套执行逻辑——那正是本方案要避免的（见 ``app/market_clock.py`` 的说明）。
    于是反过来：给回测一个**影子 user**，所有既有代码原样复用，隔离由 user_id 天然完成。

    进度为什么入 DB：既有的 ``_plan_tasks`` / ``_tasks`` 是进程内 dict，回测动辄数小时，
    进程重启后必须能续跑。所以这张表本身就是进度表 + 检查点。
    """

    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)  # 发起人
    shadow_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)  # 账务承载

    status = Column(String(20), nullable=False, default="pending")  # pending/running/done/failed/cancelled
    stage = Column(String(40), nullable=False, default="")
    message = Column(String(500), nullable=False, default="")

    start_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    end_date = Column(String(10), nullable=False)
    init_mode = Column(String(10), nullable=False, default="cash")  # cash | copy
    init_cash = Column(Float, nullable=False, default=0.0)
    init_basis = Column(Float, nullable=True)  # 起始基准（市值口径，非成本价）
    # 起始持仓快照（copy 模式才有）。**单独一列而不是塞进检查点**：检查点每天被覆盖，
    # 而起始持仓要贯穿整轮——已实现盈亏靠它做加权平均成本的初值，续跑后也必须还在。
    init_positions_json = Column(Text, nullable=False, default="[]")

    universe_json = Column(Text, nullable=False, default="[]")  # 冻结的标的池
    config_json = Column(Text, nullable=False, default="{}")  # 费用/风控/scope/sub_ticks/复权快照

    done = Column(Integer, nullable=False, default=0)
    total = Column(Integer, nullable=False, default=0)
    cancel_requested = Column(Boolean, nullable=False, default=False)

    # 断点续跑三件套：最后完成的交易日 + 该日最大 order_id（截断半截残迹）+ 账务快照
    last_step_date = Column(String(10), nullable=True)
    last_order_id = Column(Integer, nullable=True)
    checkpoint_json = Column(Text, nullable=False, default="{}")

    result_json = Column(Text, nullable=False, default="{}")
    # 多进程 CAS 防双驱：起跑时写入随机 token，落检查点/收尾时校验，防止两个进程同时推同一个 run
    worker_token = Column(String(64), nullable=True)

    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
    finished_at = Column(Float, nullable=True)


class BacktestStep(Base):
    """回测的逐日净值与快照（唯一键 ``(run_id, trade_date)``）。

    既供结果页画净值曲线，也是断点续跑后**核对账务是否一致**的依据。
    """

    __tablename__ = "backtest_steps"
    __table_args__ = (UniqueConstraint("run_id", "trade_date", name="uq_backtest_step"),)

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("backtest_runs.id"), nullable=False, index=True)
    trade_date = Column(String(10), nullable=False)

    cash = Column(Float, nullable=False, default=0.0)
    market_value = Column(Float, nullable=False, default=0.0)
    total_assets = Column(Float, nullable=False, default=0.0)
    day_pnl = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)  # 加权平均成本法，含费用
    fees = Column(Float, nullable=False, default=0.0)

    positions_json = Column(Text, nullable=False, default="[]")
    trades_json = Column(Text, nullable=False, default="[]")
    plan_json = Column(Text, nullable=False, default="{}")  # 当日实际执行的计划（复盘用）

    status = Column(String(20), nullable=False, default="ok")  # ok | degraded | error
    error = Column(String(500), nullable=False, default="")
    created_at = Column(Float, nullable=False)


def init_db():
    Base.metadata.create_all(engine)
    _ensure_schema()


def _ensure_schema() -> None:
    """轻量迁移：create_all 只建新表、不会改已有表结构，这里给老库补列/换唯一约束。

    - watchlist 加 group_name 分组列；唯一约束由 (user, code) 放开为 (user, group, code)。
    - accounts / trust_configs 加 broker_mv / broker_pnl 券商快照口径列。
    - trust_configs 加 realized_pnl 本簿已实现盈亏列（无 DEFAULT，老库补出来是 NULL）。
    - users 加 is_backtest 回测影子账户标记列。
    任一步失败仅告警不中断（如临时库）。
    """
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE watchlist "
                    "ADD COLUMN IF NOT EXISTS group_name VARCHAR(50) NOT NULL DEFAULT '默认'"
                )
            )
            conn.execute(text("ALTER TABLE watchlist DROP CONSTRAINT IF EXISTS uq_watchlist_user_code"))
            exists = conn.execute(
                text("SELECT 1 FROM pg_constraint WHERE conname = 'uq_watchlist_user_group_code'")
            ).fetchone()
            if not exists:
                conn.execute(
                    text(
                        "ALTER TABLE watchlist ADD CONSTRAINT uq_watchlist_user_group_code "
                        "UNIQUE (user_id, group_name, stock_code)"
                    )
                )
            conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS broker_mv DOUBLE PRECISION"))
            conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS broker_pnl DOUBLE PRECISION"))
            conn.execute(
                text("ALTER TABLE trust_configs ADD COLUMN IF NOT EXISTS broker_mv DOUBLE PRECISION")
            )
            conn.execute(
                text("ALTER TABLE trust_configs ADD COLUMN IF NOT EXISTS broker_pnl DOUBLE PRECISION")
            )
            conn.execute(
                text("ALTER TABLE trust_configs ADD COLUMN IF NOT EXISTS stock_scope_group VARCHAR(50)")
            )
            # 本簿已实现盈亏。**不给 DEFAULT**——老库补列后为 NULL，正是想要的
            # 「口径升级前的簿，累计值不可知」（前台据此显示「暂未统计」）。
            conn.execute(
                text("ALTER TABLE trust_configs ADD COLUMN IF NOT EXISTS realized_pnl DOUBLE PRECISION")
            )
            # 回测影子账户标记。默认 FALSE：老库里的既有用户全部是真实账户。
            conn.execute(
                text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_backtest BOOLEAN NOT NULL DEFAULT FALSE")
            )
            # backtest_runs 由 create_all 新建时已含此列，这里只为补上本轮迭代早期建过的库。
            conn.execute(
                text(
                    "ALTER TABLE backtest_runs "
                    "ADD COLUMN IF NOT EXISTS init_positions_json TEXT NOT NULL DEFAULT '[]'"
                )
            )
    except Exception as e:  # noqa: BLE001
        print(f"[db] schema 迁移失败（忽略，相关功能可能受限）：{e}", flush=True)


def get_session():
    return SessionLocal()
