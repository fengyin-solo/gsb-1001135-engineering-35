"""发布闸门领域模型：阶段、批次会话、发布台账、路段待办与告警清单。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Stage(str, Enum):
    """发布闸门四阶段，顺序即允许推进的顺序。"""

    DEPENDENCY_CHECK = "dependency_check"
    SHADOW_ACCEPTANCE = "shadow_acceptance"
    DIFF_ADJUDICATION = "diff_adjudication"
    TRAFFIC_SWITCH = "traffic_switch"


STAGE_ORDER: list[Stage] = list(Stage)
STAGE_LABELS: dict[Stage, str] = {
    Stage.DEPENDENCY_CHECK: "依赖检查",
    Stage.SHADOW_ACCEPTANCE: "影子验收",
    Stage.DIFF_ADJUDICATION: "差异裁定",
    Stage.TRAFFIC_SWITCH: "切换流量",
}


class StageState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class SessionStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    SWITCHED = "switched"
    FAILED = "failed"
    REJECTED = "rejected"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    ROLLED_BACK = "rolled_back"


@dataclass
class StageConclusion:
    """单个闸门阶段的结论；失败时 detail 记录原因，旧版本保持不动。"""

    stage: str
    state: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "state": self.state, "detail": self.detail}


@dataclass
class ReleaseSession:
    """一次发布批次。events 为隔离环境要重放的巡查/病害/桥隧事件。"""

    session_id: str
    batch_no: str
    version_signature: str
    package_signature: str
    jurisdiction_code: str
    operator: str
    status: str
    current_stage: str
    events: list[dict[str, Any]]
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    replays: list[dict[str, Any]] = field(default_factory=list)
    lock_holder: str | None = None
    lock_token: str | None = None
    last_heartbeat: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LedgerEntry:
    """发布台账：只追加、不改写，历史记录保留当时批次与约定版本。"""

    seq: int
    batch_no: str
    version_signature: str
    stage: str
    result: str
    operator: str
    jurisdiction_code: str
    convention_version: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AlertItem:
    """告警清单项：验收结论（含失败拦截）都要回写。"""

    id: str
    batch_no: str
    level: str
    source_stage: str
    title: str
    content: str
    jurisdiction_code: str
    created_at: str
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RoadTodo:
    """路段待办：由差异裁定后的变更结论按路段生成。"""

    id: str
    batch_no: str
    road_ref: str
    kind: str
    title: str
    event_code: str
    decision: str
    rule_id: str | None
    jurisdiction_code: str
    created_at: str
    done: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
