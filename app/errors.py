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
