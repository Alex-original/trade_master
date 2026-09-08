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
    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("user_id", "stock_code", name="uq_watchlist_user_code"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    stock_code = Column(String(20), nullable=False, index=True)
    stock_name = Column(String(50), nullable=False, default="")
    source = Column(Integer, nullable=False, default=0)  # 0 用户添加 / 1 Agent 候选
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
    stock_scope = Column(Integer, nullable=False, default=0)  # 0 仅持仓 / 1 仅自选 / 2 全市场
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


def init_db():
    Base.metadata.create_all(engine)


def get_session():
    return SessionLocal()
