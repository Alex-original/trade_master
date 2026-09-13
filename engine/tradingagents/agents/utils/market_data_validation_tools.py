from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.market_data_validator import build_verified_market_snapshot


@tool
def get_verified_market_snapshot(
    symbol: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[str, "the current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[
        int, "number of recent trading rows to include for sanity-checking"
    ] = 30,
) -> str:
    """Deterministic verification snapshot for exact market-data claims.

    Returns the latest OHLCV row on or before curr_date, common technical
    indicators, and recent closes. Call this before making exact claims about
    price levels, Bollinger bands, RSI, MACD, moving averages, support /
    resistance, or historical comparisons, and treat it as the source of truth.
    """
    try:
        return build_verified_market_snapshot(symbol, curr_date, look_back_days)
    except (VendorError, ValueError) as exc:
        # 单只标的取不到行情快照时，降级返回说明文本而非抛异常：tool_node 的默认
        # 错误处理会把异常一路 re-raise，令整只标的 10–15 分钟深析全部作废。返回
        # 文本让 market 分析师看到「无精确数据」，继续产出不做精确数值主张的报告，
        # 而不是拖垮整个 graph。
        return (
            f"Verified market data snapshot unavailable for {symbol}: "
            f"{type(exc).__name__}: {exc}. "
            f"Do not make any exact OHLCV, price-level, or indicator-value claims "
            f"for this ticker; state explicitly that market data was unavailable."
        )
