"""托管风控：style 风格标签 + 交易日工具。

供计划层（组合决策图的风控上下文）与执行层共用，避免 trust / analysis_service
互相 import 形成循环依赖。

注意：历史上这里还有一套 ``style_risk_band`` 仓位带（总仓位/单票/现金缓冲百分比），
曾作为**执行层硬上限**使用。现按产品决策改为「默认不设硬性限制、完全交由团队决定」，
故仓位带不再参与任何校验；用户若要限制，只能通过 TrustConfig 的三个显式可选项
（单票上限 / 止损比例 / 单日笔数）——留空即不限制。
"""
from __future__ import annotations

import datetime as _dt

#: style → 中文标签（仅用于描述投资风格倾向，不代表任何仓位上限）
STYLE_ZH: dict[int, str] = {0: "保守", 1: "均衡", 2: "进取"}


def style_label(style: int | None) -> str:
    """style → 中文标签；未知值回落「均衡」。"""
    return STYLE_ZH.get(style if style is not None else 1, "均衡")


def style_risk_band(style: int | None) -> dict:
    """style(0保守/1均衡/2进取) → {max_equity, max_single, min_cash}。

    - max_equity：总仓位（持仓市值/总资产）上限
    - max_single：单票市值占总资产上限
    - min_cash：现金缓冲下限（占总资产）
    未知 style 回落均衡档。
    """
    bands = {
        0: {"max_equity": 0.60, "max_single": 0.20, "min_cash": 0.40},
        1: {"max_equity": 0.80, "max_single": 0.30, "min_cash": 0.20},
        2: {"max_equity": 1.00, "max_single": 0.40, "min_cash": 0.05},
    }
    return bands.get(style, bands[1])


def next_trading_day(from_date: _dt.date | None = None) -> str:
    """下一工作日（周末顺延；忽略法定节假日，与 latest_trading_day 同口径）。"""
    d = (from_date or _dt.date.today()) + _dt.timedelta(days=1)
    while d.weekday() >= 5:  # 六/日顺延
        d += _dt.timedelta(days=1)
    return d.isoformat()
