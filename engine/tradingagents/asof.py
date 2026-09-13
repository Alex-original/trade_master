"""as-of 作用域：告诉数据层的日期盲工具「现在是哪一天」。

**为什么需要这个模块**：回测里最致命的一类错误是未来函数——模型读到了它当时读不到的信息。
日期可寻址的接口（K 线、指数线）靠参数就能封顶，但 ``wind.py`` 里有一批工具**根本没有
日期参数**（新闻 / 公告 / 三大报表 / 估值 / 公司事件 / 宏观 / 风险指标 / 时点快照），
它们永远返回"最新"。对这些，"按日期查"在结构上不可能，只能显式禁用（见 ``wind.py`` 的
``_asof_unavailable``）。

**为什么用 contextvar 而不是全局 dict**：``TradingAgentsGraph.__init__`` 会
``set_config(self.config)`` 写**进程级全局**。回测与实盘 15:05 的计划生成会并发，
全局变量会互相覆盖 —— 那是"回测污染实盘"级别的 bug。contextvar 跟着执行上下文走，
天然隔离。

**为什么必须在 worker 线程里设**：``run_plan`` 用的是裸
``concurrent.futures.ThreadPoolExecutor``（``app/analysis_service.py``），
不是 ``ContextThreadPoolExecutor``。在 ``run_plan`` 外面设 contextvar，worker 线程
**看不到**。所以作用域必须开在 ``run_analysis_cached`` 里包住 ``run_analysis``。

**实测已验证**：contextvar 能穿透 ``ToolNode`` 抵达工具函数（工具跑在非主线程，仍继承到值）；
裸 ``ThreadPoolExecutor`` 下 4 个并发 worker 各看到自己的值、零串扰，且外层 context 未被污染。
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager

#: 当前 as-of 日期（``YYYY-MM-DD``）。None = 实时，数据层按"最新"取数。
ASOF: contextvars.ContextVar[str | None] = contextvars.ContextVar("backtest_asof", default=None)


def current_asof() -> str | None:
    """当前 as-of 日期；None 表示实时（非回测）。"""
    return ASOF.get()


@contextmanager
def asof_scope(date: str | None):
    """在作用域内把 as-of 钉到 ``date``；退出时**恢复**原值（可嵌套）。

    ``date`` 为 None 时不改变现状但仍会正确恢复，调用方不必分支。
    """
    token = ASOF.set(date)
    try:
        yield
    finally:
        ASOF.reset(token)
