"""发布闸门领域错误：路由层据此翻译成 409（闸门规则冲突）。"""
from __future__ import annotations


class GateError(Exception):
    """闸门单向推进规则被违反，或当前闸门未通过。"""

    def __init__(self, message: str, *, code: str = "gate_rejected") -> None:
        super().__init__(message)
        self.code = code


class LockBusyError(Exception):
    """编排锁已被其他发布批次持有，当前只允许一个发布者。"""
