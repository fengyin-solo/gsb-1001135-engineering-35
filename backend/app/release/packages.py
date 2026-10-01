"""发布构建包：版本签名、候选总览快照、结构迁移与整批回退计划。

- 构建包、结构迁移、整批回退由编排器放在同一事务边界内执行（见 manager.py）；
- 版本签名只由内容决定：相同内容重复构建得到同一签名，重复发布据此幂等；
- 候选总览为空（零）时拒绝构建，任一步失败都只能保留旧版本；
- 结构迁移同时给出 up/down，整批回退即按 down 反向执行。
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from app.release.errors import PackageRejected

ROAD_BATCH_COLUMN = "发布批次"


@dataclass(frozen=True)
class MigrationStep:
    """一条结构迁移：up 应用、down 回退。"""

    key: str
    target_module: str
    default_value: object = None

    def up(self, store: Any) -> None:
        for row in store.rows(self.target_module):
            row.setdefault(self.key, self.default_value)

    def down(self, store: Any) -> None:
        for row in store.rows(self.target_module):
            row.pop(self.key, None)

    def describe(self) -> dict[str, str]:
        return {
            "key": self.key,
            "module": self.target_module,
            "up": f"为{self.target_module}补齐结构字段「{self.key}」（默认{self.default_value}）",
            "down": f"移除{self.target_module}结构字段「{self.key}」，整批回退",
        }


@dataclass
class ReleasePackage:
    batch_id: str
    revision: str
    signature: str
    snapshot: dict[str, Any]
    migrations: list[MigrationStep] = field(default_factory=list)
    built_at: str = ""

    def apply_migrations(self, store: Any) -> None:
        for step in self.migrations:
            step.up(store)

    def rollback_migrations(self, store: Any) -> None:
        # 整批回退：迁移按相反顺序 down
        for step in reversed(self.migrations):
            step.down(store)

    def rollback_plan(self) -> list[dict[str, str]]:
        return [step.describe() for step in reversed(self.migrations)]

    def as_meta(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "revision": self.revision,
            "signature": self.signature,
            "built_at": self.built_at,
            "migrations": [step.describe() for step in self.migrations],
            "rollback_plan": self.rollback_plan(),
        }


def compute_signature(snapshot: dict[str, Any], migration_keys: list[str], revision: str) -> str:
    """版本签名：只对内容（总览统计、迁移结构、版本说明）取摘要。"""
    content = {
        "revision": revision,
        "modules": [
            {"name": item["name"], "created": item["created"], "pending": item["pending"],
             "abnormal": item["abnormal"]}
            for item in snapshot.get("modules", [])
        ],
        "migrations": migration_keys,
    }
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def build_package(store: Any, *, batch_id: str, revision: str, previous_batch: str | None) -> ReleasePackage:
    """从当前仓库构建候选发布包；候选总览为零则拒绝。"""
    snapshot = store.overview()
    modules = snapshot.get("modules", [])
    total_created = sum(int(item.get("created", 0)) for item in modules)
    if not modules or total_created == 0:
        # 绝不允许把总览写成零：宁可保留旧版本
        raise PackageRejected("候选总览为空（业务总量为零），拒绝构建，保留当前现网版本")

    migrations = [
        MigrationStep(ROAD_BATCH_COLUMN, "road_section", default_value=previous_batch or "基线批次"),
    ]
    signature = compute_signature(snapshot, [f"{step.target_module}.{step.key}" for step in migrations], revision)
    package = ReleasePackage(
        batch_id=batch_id,
        revision=revision,
        signature=signature,
        snapshot=copy.deepcopy(snapshot),
        migrations=migrations,
        built_at=_now_text(),
    )
    return package


def _now_text() -> str:
    from datetime import datetime, timezone, timedelta

    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")
