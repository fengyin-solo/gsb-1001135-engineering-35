"""道桥养护态势总览发布闸门。

闸门顺序（单向推进，不可跳级、不可倒序）：
    依赖检查 dependency_check → 影子验收 shadow_acceptance
    → 差异裁定 diff_adjudication → 切换流量 traffic_switch

任一步失败都保留旧版本，总览永远不会被写成零；重复发布按版本签名幂等。
"""
from __future__ import annotations

from app.release.dependencies import DependencyRegistry
from app.release.errors import GateError, LockBusyError
from app.release.jurisdiction import CONVENTIONS, OperatingConvention, get_convention
from app.release.service import release_service

__all__ = [
    "GateError",
    "LockBusyError",
    "OperatingConvention",
    "CONVENTIONS",
    "get_convention",
    "DependencyRegistry",
    "release_service",
]
