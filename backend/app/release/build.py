"""构建包、结构迁移与事务边界。

构建包（BuildPackage）携带：版本签名、业务表快照（新批次总览数据）、
结构迁移清单、回退迁移清单与包签名。应用构建包、执行结构迁移、整批回退
必须使用同一个事务边界——见 build_transaction：中途任何一步抛错都会把
业务数据恢复到事务开始前，绝不会把总览写成零。
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from app.store import store

# 需要随发布扩列并打批次标记的业务表（路段清单/待办要能回溯发布批次）
ROAD_TABLE = "road_section"
BATCH_COLUMN = "发布批次"

Migration = Callable[[], None]


def canonical_payload(payload: Any) -> str:
    """把事件/表数据规范化成稳定字符串，保证同一内容签名一致（幂等基础）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def version_signature(
    events: list[dict[str, Any]],
    jurisdiction_code: str,
    convention_version: str,
) -> str:
    """版本签名：事件内容 + 辖区约定版本。约定变更会产生新签名，旧批次不受影响。"""
    digest = hashlib.sha256(
        canonical_payload(
            {
                "events": events,
                "jurisdiction": jurisdiction_code,
                "convention_version": convention_version,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"sig-{digest[:16]}"


@dataclass
class BuildPackage:
    """待发布构建包：新批次总览数据表 + 结构/回退迁移。"""

    batch_no: str
    version_signature: str
    overview: dict[str, Any]
    table_bundle: dict[str, list[dict[str, Any]]]
    migrations: list[str] = field(default_factory=list)
    rollback_migrations: list[str] = field(default_factory=list)
    package_signature: str = ""

    def __post_init__(self) -> None:
        if not self.package_signature:
            body = canonical_payload(
                {
                    "batch_no": self.batch_no,
                    "version_signature": self.version_signature,
                    "overview": self.overview,
                    "tables": self.table_bundle,
                    "migrations": self.migrations,
                }
            )
            self.package_signature = f"pkg-{hashlib.sha256(body.encode('utf-8')).hexdigest()[:16]}"


def make_road_batch_migration(batch_no: str) -> tuple[str, str]:
    """结构迁移：给路段表增加「发布批次」列并给本批次记录打标。

    返回 (正向迁移名, 回退迁移名)。迁移本身在 build_transaction 内执行，
    失败随事务一起回退。
    """
    forward = f"road_section.add_batch_column@{batch_no}"
    backward = f"road_section.drop_batch_column@{batch_no}"
    return forward, backward


def apply_migration(name: str, batch_no: str) -> None:
    """按迁移名执行结构迁移（幂等：列已存在时不重复加）。

    新增「发布批次」列：已经带历史批次标记的行保持不变（历史记录保留当时批次），
    未标记的行归入本批次。换表快照在构建包内已按同一口径预制，二者在同一事务内
    保持结构一致。
    """
    if name.startswith("road_section.add_batch_column@"):
        for row in store.rows(ROAD_TABLE):
            row.setdefault(BATCH_COLUMN, batch_no)
        return
    raise ValueError(f"未知结构迁移：{name}")


def apply_rollback_migration(name: str, batch_no: str) -> None:
    """按迁移名执行回退迁移：移除本批次新增的列，恢复迁移前表结构。"""
    if name.startswith("road_section.drop_batch_column@"):
        for row in store.rows(ROAD_TABLE):
            row.pop(BATCH_COLUMN, None)
        return
    raise ValueError(f"未知回退迁移：{name}")


def build_transaction(
    package: BuildPackage,
    *,
    mode: str,
):
    """同一事务边界：应用构建包 + 结构迁移（或整批回退）。

    进入时对业务数据做快照，退出时任一步失败就整批恢复，保证总览不会被写成零。
    mode='apply' 发布；mode='rollback' 整批回退（回退迁移 + 恢复旧表快照）。

    台账/会话/告警是追加型审计记录，不在业务事务回退范围内，由调用方在
    事务外记录失败结论。
    """
    return _BuildTransaction(package, mode)


class _BuildTransaction:
    def __init__(self, package: BuildPackage, mode: str) -> None:
        self._package = package
        self._mode = mode
        self._snapshot: dict[str, list[dict[str, Any]]] = {}
        self._restored_bundle: dict[str, list[dict[str, Any]]] | None = None

    def __enter__(self) -> "_BuildTransaction":
        # 事务开始：冻结业务表快照
        self._snapshot = {
            name: copy.deepcopy(rows) for name, rows in store.business_tables().items()
        }
        return self

    def apply(self) -> dict[str, Any]:
        package = self._package
        if self._mode == "apply":
            # 1) 结构迁移（与构建包同事务）
            for migration in package.migrations:
                apply_migration(migration, package.batch_no)
            # 2) 应用构建包业务表
            for name, rows in package.table_bundle.items():
                store.replace_table(name, copy.deepcopy(rows))
            return package.overview
        # 整批回退：先执行回退迁移，再恢复上一版表快照
        for migration in package.rollback_migrations:
            apply_rollback_migration(migration, package.batch_no)
        if self._restored_bundle is not None:
            for name, rows in self._restored_bundle.items():
                store.replace_table(name, copy.deepcopy(rows))
        return {}

    def set_rollback_bundle(self, bundle: dict[str, list[dict[str, Any]]]) -> None:
        """整批回退时要恢复的上一版业务表快照。"""
        self._restored_bundle = bundle

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            # 任一步失败：整批回退到事务开始前，旧版本完整保留
            for name, rows in self._snapshot.items():
                store.replace_table(name, copy.deepcopy(rows))
        # 不吞异常：交由上层把失败结论写台账/告警
        return False
