"""发布流程业务异常。

路由层只负责把异常翻译成 HTTP 状态码，业务语义全部在这一层声明。
"""
from __future__ import annotations


class ReleaseError(Exception):
    """发布流程异常基类。"""

    status_code = 400


class ReleaseOrderError(ReleaseError):
    """跳级推进、倒序执行或对已结束发布重复操作。"""

    status_code = 409


class OrchestrationLocked(ReleaseError):
    """编排锁已被其他发布者持有。"""

    status_code = 409


class InfraUnreachable(ReleaseError):
    """队列、地图服务或缓存不可达，禁止转换流量。"""

    status_code = 503


class PackageRejected(ReleaseError):
    """构建包不合法，例如候选总览为空。"""

    status_code = 400


class ReplayDisconnected(ReleaseError):
    """影子回放过程中连接断开，需要从断点续跑。"""

    status_code = 503


class ConflictUnresolved(ReleaseError):
    """差异裁定存在无法按辖区约定裁决的样例。"""

    status_code = 422


class MigrationFailed(ReleaseError):
    """结构迁移失败，整批回退。"""

    status_code = 500
