"""道桥养护态势总览发布闸门接口。

覆盖：发布批次创建（版本签名幂等）、四阶段单向推进、检查连接断开复位、
心跳、整批回退、发布台账/路段待办/告警清单，以及队列、地图服务、缓存的
连通性演练开关。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.release.dependencies import DEPENDENCY_NAMES
from app.release.errors import GateError, LockBusyError
from app.release.service import release_service

router = APIRouter(prefix="/api/release", tags=["态势总览发布闸门"])


class CreateSessionPayload(BaseModel):
    operator: str = Field(default="值班长")
    jurisdiction_code: str | None = None
    events: list[dict[str, Any]] | None = None


class AdvancePayload(BaseModel):
    operator: str = Field(default="值班长")


class DependencyFaultPayload(BaseModel):
    name: str
    down: bool = True


def _gate_conflict(exc: GateError) -> None:
    from fastapi import HTTPException

    raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)})


@router.get("/state")
def release_state() -> dict[str, Any]:
    """发布运行态总览：当前生效版本、编排锁、依赖连通性。"""
    return {
        "active": release_service.active_overview()["release"],
        "lock": release_service.lock_status(),
        "dependencies": release_service.dependency_status(),
        "sessions": len(release_service.list_sessions()),
    }


@router.get("/overview")
def released_overview() -> dict[str, Any]:
    """当前生效的态势总览（发布前为内置基线，任何失败都不会变成零）。"""
    return release_service.active_overview()


@router.post("/sessions")
def create_session(payload: CreateSessionPayload) -> dict[str, Any]:
    """创建发布批次；相同事件内容与辖区约定重复发布时按版本签名幂等返回。"""
    try:
        session, created = release_service.create_session(
            operator=payload.operator,
            jurisdiction_code=payload.jurisdiction_code,
            events=payload.events,
        )
    except GateError as exc:
        _gate_conflict(exc)
    return {"created": created, "session": session.to_dict()}


@router.get("/sessions")
def list_sessions() -> dict[str, Any]:
    return {"items": release_service.list_sessions()}


@router.get("/sessions/{session_id}")
def session_detail(session_id: str) -> dict[str, Any]:
    try:
        return release_service.session_detail(session_id)
    except GateError as exc:
        _gate_conflict(exc)


@router.post("/sessions/{session_id}/advance")
def advance_session(session_id: str, payload: AdvancePayload) -> dict[str, Any]:
    """单向推进当前闸门：依赖检查→影子验收→差异裁定→切换流量。

    跳级、倒序、依赖不可达、冲突未裁决都会被拒绝（409），旧版本保留。
    """
    try:
        session = release_service.advance(session_id, operator=payload.operator)
    except LockBusyError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=423, detail={"code": "lock_busy", "message": str(exc)})
    except GateError as exc:
        _gate_conflict(exc)
    return {"session": session.to_dict()}


@router.post("/sessions/{session_id}/resume")
def resume_session(session_id: str, payload: AdvancePayload) -> dict[str, Any]:
    """检查连接断开后从未完成项复位；已灌入隔离环境的样例不重复灌入。"""
    try:
        session = release_service.resume(session_id, operator=payload.operator)
    except GateError as exc:
        _gate_conflict(exc)
    return {"session": session.to_dict()}


@router.post("/sessions/{session_id}/heartbeat")
def session_heartbeat(session_id: str) -> dict[str, Any]:
    try:
        release_service.heartbeat(session_id)
    except GateError as exc:
        _gate_conflict(exc)
    return {"ok": True}


@router.post("/sessions/{session_id}/rollback")
def rollback_session(session_id: str, payload: AdvancePayload) -> dict[str, Any]:
    """整批回退当前生效批次；未切换流量或历史批次的倒序回滚一律拒绝。"""
    try:
        session = release_service.rollback(session_id, operator=payload.operator)
    except GateError as exc:
        _gate_conflict(exc)
    return {"session": session.to_dict()}


@router.get("/ledger")
def release_ledger(batch_no: str | None = None) -> dict[str, Any]:
    """发布台账：只追加不改写，历史记录保留当时批次与辖区约定版本。"""
    return {"items": release_service.ledger(batch_no)}


@router.get("/road_todos")
def road_todos(road_ref: str | None = None) -> dict[str, Any]:
    """路段待办：差异裁定变更结论按路段回写。"""
    return {"items": release_service.road_todos(road_ref)}


@router.get("/alerts")
def release_alerts(include_resolved: bool = True) -> dict[str, Any]:
    """告警清单：验收通过/拦截/切流/回退结论全部回写。"""
    return {"items": release_service.alerts(include_resolved)}


@router.post("/dependencies/fault")
def inject_dependency_fault(payload: DependencyFaultPayload) -> dict[str, Any]:
    """演练开关：模拟队列、地图服务或缓存不可达（不可达时禁止转换流量）。"""
    if payload.name not in DEPENDENCY_NAMES:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=f"未知依赖：{payload.name}，可选 {DEPENDENCY_NAMES}")
    release_service.set_dependency_fault(payload.name, payload.down)
    return {"dependencies": release_service.dependency_status()}
