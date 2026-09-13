"""业务异常。main.py 里注册全局 handler 转成可读的 HTTP 响应。

对齐 video-note 的 ServiceError 约定：响应体统一为 {"detail": msg}。
"""
from __future__ import annotations


class ServiceError(Exception):
    """业务异常，携带 message 与 HTTP status_code（默认 400）。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PlanNeedsConfirm(Exception):
    """盘中已有当日计划：须用户确认后才覆盖重跑（旧计划作废、按最新计划执行）。"""

    def __init__(self, message: str, trade_date: str = ""):
        super().__init__(message)
        self.message = message
        self.trade_date = trade_date


class PlanCancelled(Exception):
    """用户主动暂停计划生成：丢弃已完成的阶段性结果，不落库、不出计划。

    定义在 errors.py（而不是 trust.py）是因为 analysis_service 的 Stage1 线程池
    也要抛它，放中立场可避免 trust ↔ analysis_service 的循环导入。
    """
