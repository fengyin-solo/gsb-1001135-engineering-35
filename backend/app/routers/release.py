"""道桥养护态势总览发布接口。

闸门按「依赖检查、样例回放、差异裁定、切换流量」单向推进，路径里带闸门序号
只是为了可读，服务端以发布当前 gate_index 为准，跳级/倒序一律 409 拒绝。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.release.errors import ReleaseError
from app.release.manager import GATES, GATE_LABELS, release_manager

router = APIRouter(prefix="/api/release", tags=["总览发布"])


class CreatePayload(BaseModel):
    operator: str = "值班员"
    revision: str = "总览例行发布"


class AdvancePayload(BaseModel):
    operator: str = "值班员"
    unknown_conflict: bool = False


class InjectPayload(BaseModel):
    stage: str


class InfraPayload(BaseModel):
    reachable: bool


def _raise(exc: ReleaseError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/status")
def release_status() -> dict[str, Any]:
    """发布总览：当前现网版本、编排锁、活跃发布、闸门、基础设施。"""
    return release_manager.status()


@router.post("/shadow-preview")
def shadow_preview(payload: AdvancePayload) -> dict[str, Any]:
    """发布前“影子验收”：隔离环境重放巡查、病害、桥隧事件并逐项对照（只读）。"""
    return release_manager.preview_shadow(unknown_conflict=payload.unknown_conflict)


@router.post("/releases")
def create_release(payload: CreatePayload) -> dict[str, Any]:
    """创建发布：构建包 + 占用编排锁；相同版本签名重复创建按幂等返回。"""
    try:
        return release_manager.create_release(operator=payload.operator, revision=payload.revision)
    except ReleaseError as exc:
        _raise(exc)


@router.get("/releases")
def list_releases() -> dict[str, Any]:
    return {"items": release_manager.list_releases(), "total": len(release_manager.list_releases())}


@router.get("/releases/{batch_id}")
def get_release(batch_id: str) -> dict[str, Any]:
    try:
        return release_manager.get_release(batch_id)
    except ReleaseError as exc:
        _raise(exc)


@router.post("/releases/{batch_id}/gates/{gate}")
def advance_gate(batch_id: str, gate: str, payload: AdvancePayload) -> dict[str, Any]:
    """推进单个闸门；跳级、倒序、重复推进已完成闸门都被拒绝。"""
    if gate not in GATES:
        raise HTTPException(
            status_code=400,
            detail=f"未知闸门：{gate}，合法闸门为 {'、'.join(GATE_LABELS[code] for code in GATES)}",
        )
    try:
        return release_manager.advance(
            batch_id, gate,
            operator=payload.operator,
            unknown_conflict=payload.unknown_conflict,
        )
    except ReleaseError as exc:
        _raise(exc)


@router.post("/releases/{batch_id}/rollback/{gate}")
def rollback_gate(batch_id: str, gate: str, payload: AdvancePayload) -> dict[str, Any]:
    """倒序回滚入口：必须拒绝；失败回退由系统在同一事务内整批完成。"""
    try:
        release_manager.rollback_gate(batch_id, gate, operator=payload.operator)
    except ReleaseError as exc:
        _raise(exc)
    raise HTTPException(status_code=500, detail="不可达分支")


@router.post("/releases/{batch_id}/resume")
def resume_replay(batch_id: str) -> dict[str, Any]:
    """连接断开后从未完成项复位：查看检查点，随后继续推进样例回放闸门。"""
    try:
        return release_manager.resume_replay(batch_id)
    except ReleaseError as exc:
        _raise(exc)


@router.post("/releases/{batch_id}/inject")
def inject_failure(batch_id: str, payload: InjectPayload) -> dict[str, Any]:
    """故障演练：在下一次执行某闸门时注入一次性故障。"""
    try:
        return release_manager.inject_failure(batch_id, payload.stage)
    except ReleaseError as exc:
        _raise(exc)


@router.get("/ledger")
def release_ledger() -> dict[str, Any]:
    items = release_manager.list_ledger()
    return {"items": items, "total": len(items)}


@router.get("/road-todos")
def road_todos() -> dict[str, Any]:
    items = release_manager.list_road_todos()
    return {"items": items, "total": len(items)}


@router.get("/alerts")
def alerts() -> dict[str, Any]:
    items = release_manager.list_alerts()
    return {"items": items, "total": len(items)}


@router.get("/infra")
def infra_status() -> dict[str, Any]:
    return {"items": release_manager.infra.status()}


@router.post("/infra/{name}")
def set_infra(name: str, payload: InfraPayload) -> dict[str, Any]:
    """演练用：设置队列/地图/缓存可达性；不可达时依赖检查与流量切换都被拒。"""
    try:
        return release_manager.infra.set_reachable(name, payload.reachable)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"未知基础设施：{name}（可选 queue、map、cache）")
