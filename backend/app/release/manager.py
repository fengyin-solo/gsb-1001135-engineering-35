"""总览发布编排：闸门状态机、编排锁、事务边界与结论回写。

闸门严格单向推进：依赖检查 → 样例回放 → 差异裁定 → 切换流量。
跳级、倒序、对已完成/已放弃的发布重复操作一律拒绝；任一步失败保留旧版本，
不把总览写成零。发布结论回写发布台账、路段待办和告警清单；构建包、结构迁移
与整批回退使用同一事务边界。重复发布按版本签名幂等。
"""
from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.release.errors import (
    ConflictUnresolved,
    InfraUnreachable,
    MigrationFailed,
    OrchestrationLocked,
    PackageRejected,
    ReleaseOrderError,
    ReplayDisconnected,
)
from app.release.infra import Infrastructure
from app.release.packages import ROAD_BATCH_COLUMN, ReleasePackage, build_package
from app.release.samples import ShadowRunner

GATES = ["dependency", "replay", "adjudication", "traffic"]
GATE_LABELS = {
    "dependency": "依赖检查",
    "replay": "样例回放",
    "adjudication": "差异裁定",
    "traffic": "切换流量",
}
# 终态：只有切换流量成功才发布；其余终态都保留旧版本
RELEASE_DONE = "已发布"
RELEASE_BLOCKED = "差异阻断"
RELEASE_ABANDONED = "已放弃"
ACTIVE_STATUSES = ("待发布", RELEASE_BLOCKED)

CST = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")


class ReleaseManager:
    def __init__(self, store: Any, infra: Infrastructure | None = None) -> None:
        self._lock = threading.RLock()
        self.store = store
        self.infra = infra or Infrastructure()
        # 编排锁：同一时刻只允许一个活跃发布（唯一发布者）
        self._owner_token: str | None = None
        self.releases: dict[str, dict[str, Any]] = {}
        self.ledger: list[dict[str, Any]] = []
        self.road_todos: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []
        # 样例→首次灌入批次：旧样例维持此前批次
        self.sample_registry: dict[str, str] = {}
        # 发布者注入的故障演练开关（一次性消费）
        self.fail_injections: dict[str, set[str]] = {}
        # 事务暂存区
        self._tx_store_snapshot: dict[str, list[dict[str, Any]]] | None = None
        self._tx_side_snapshot: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None
        self.active_version = {
            "version": "v0",
            "revision": "基线版本",
            "batch_id": "baseline",
            "signature": "baseline",
            "released_at": None,
        }
        # 基线快照：任何失败都退回到这里，总览永不落空
        self.active_snapshot: dict[str, Any] = copy.deepcopy(store.overview())

    # ================= 公共只读视图 =================

    def status(self) -> dict[str, Any]:
        with self._lock:
            active = self._active_release_locked()
            return {
                "active_version": dict(self.active_version),
                "orchestration_lock": {
                    "locked": self._owner_token is not None,
                    "holder": active["batch_id"] if active else None,
                    "operator": active["operator"] if active else None,
                },
                "active_release": self._public_release(active) if active else None,
                "gates": [{"code": code, "label": GATE_LABELS[code]} for code in GATES],
                "infra": self.infra.status(),
                "counts": {
                    "releases": len(self.releases),
                    "ledger": len(self.ledger),
                    "road_todos": len(self.road_todos),
                    "alerts": len(self.alerts),
                },
            }

    def list_releases(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public_release(item) for item in self.releases.values()]

    def get_release(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            release = self.releases.get(batch_id)
            if release is None:
                raise ReleaseOrderError(f"发布批次 {batch_id} 不存在")
            return self._public_release(release)

    def list_ledger(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.ledger)

    def list_road_todos(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.road_todos)

    def list_alerts(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self.alerts)

    def overview(self) -> dict[str, Any]:
        """对外总览：始终返回已发布快照（发布失败保留旧版本，绝不返回零）。"""
        with self._lock:
            payload = copy.deepcopy(self.active_snapshot)
            payload["release"] = dict(self.active_version)
            return payload

    # ================= 创建发布（含幂等） =================

    def create_release(self, *, operator: str = "值班员", revision: str = "总览例行发布") -> dict[str, Any]:
        with self._lock:
            seq = len(self.releases) + 1
            version = f"v{seq}"
            batch_id = f"BATCH-{datetime.now(CST).strftime('%Y%m%d')}-{seq:03d}"
            package = self._try_build(batch_id, revision)
            duplicate = self._find_by_signature_locked(package.signature)

            # 编排锁优先：保证只有一个发布者。持锁人是自己时才允许按签名幂等重试，
            # 其他发布者即使内容相同也必须等待。
            if self._owner_token is not None:
                if duplicate is not None and duplicate["operator"] == operator \
                        and duplicate["lock_token"] == self._owner_token:
                    result = self._public_release(duplicate)
                    result["idempotent"] = True
                    result["message"] = (
                        f"版本签名 {package.signature} 已由批次 {duplicate['batch_id']} 持有，"
                        "重复请求幂等返回")
                    return result
                current = self._active_release_locked()
                label = current["batch_id"] if current else "其他发布者"
                raise OrchestrationLocked(f"编排锁已被批次 {label} 持有，请等待其结束后再发布")

            # 无持锁时命中相同签名（如对已发布版本重复发布），幂等返回既有批次
            if duplicate is not None:
                result = self._public_release(duplicate)
                result["idempotent"] = True
                result["message"] = (
                    f"版本签名 {package.signature} 已由批次 {duplicate['batch_id']} 发布，"
                    "重复请求幂等返回")
                return result

            token = uuid.uuid4().hex
            self._owner_token = token

            release = {
                "batch_id": batch_id,
                "version": version,
                "revision": revision,
                "operator": operator,
                "status": "待发布",
                "gate_index": 0,
                "package": package,
                "gate_states": {code: {"state": "待执行", "at": None, "detail": None} for code in GATES},
                "checkpoint": [],
                "replay": None,
                "shadow_report": None,
                "lock_token": token,
                "created_at": _now(),
                "updated_at": _now(),
            }
            self.releases[batch_id] = release
            self.fail_injections.setdefault(batch_id, set())
            self._append_ledger(batch_id, version, "创建发布", f"修订说明：{revision}；版本签名 {package.signature}")
            return self._public_release(release)

    def _try_build(self, batch_id: str, revision: str) -> ReleasePackage:
        """构建包（事务外预构建）；候选总览为零直接拒绝，不占编排锁。"""
        previous_batch = self.active_version["batch_id"]
        return build_package(self.store, batch_id=batch_id, revision=revision,
                             previous_batch=previous_batch)

    def _find_by_signature_locked(self, signature: str) -> dict[str, Any] | None:
        for release in self.releases.values():
            if release["package"].signature == signature and release["status"] != RELEASE_ABANDONED:
                return release
        return None

    # ================= 闸门推进（单向状态机） =================

    def advance(self, batch_id: str, gate: str, *, operator: str = "值班员",
                unknown_conflict: bool = False) -> dict[str, Any]:
        with self._lock:
            release = self._require_release_locked(batch_id)
            self._require_owner_locked(release, operator)
            if gate not in GATES:
                raise ReleaseOrderError(f"未知闸门：{gate}")
            expected = GATES[release["gate_index"]]
            if gate != expected:
                if GATES.index(gate) < release["gate_index"]:
                    raise ReleaseOrderError(
                        f"发布闸门只允许单向推进：{GATE_LABELS[gate]} 已完成，禁止倒序回滚")
                raise ReleaseOrderError(
                    f"禁止跳级发布：当前应执行「{GATE_LABELS[expected]}」，不能直接执行「{GATE_LABELS[gate]}」")

            handler = {
                "dependency": self._gate_dependency,
                "replay": self._gate_replay,
                "adjudication": lambda item: self._gate_adjudication(item, unknown_conflict),
                "traffic": self._gate_traffic,
            }[gate]
            return handler(release)

    # ---- 闸门一：依赖检查 ----

    def _gate_dependency(self, release: dict[str, Any]) -> dict[str, Any]:
        gate_state = release["gate_states"]["dependency"]
        if gate_state["state"] == "通过":
            raise ReleaseOrderError("依赖检查已通过，禁止重复执行（闸门单向推进）")
        if self._consume_failure(release["batch_id"], "dependency"):
            self._mark_gate(gate_state, "失败", "依赖检查失败，保留旧版本")
            self._append_ledger(release["batch_id"], release["version"], "依赖检查",
                                "依赖检查失败，未进入样例回放，保留旧版本", ok=False)
            self._abandon_locked(release, "依赖检查失败，已放弃本批次，现网版本不变")
            raise InfraUnreachable("队列、地图服务或缓存存在不可达项，依赖检查失败，保留旧版本")

        unreachable = self.infra.unreachable_names()
        if unreachable:
            self._mark_gate(gate_state, "失败", f"不可达：{'、'.join(unreachable)}")
            self._abandon_locked(release, "依赖检查失败，已放弃本批次，现网版本不变")
            raise InfraUnreachable(f"队列、地图服务或缓存不可达（{'、'.join(unreachable)}），禁止继续发布")

        self._mark_gate(gate_state, "通过", "队列、地图服务、缓存均可达")
        release["gate_index"] += 1
        release["updated_at"] = _now()
        self._append_ledger(release["batch_id"], release["version"], "依赖检查",
                            "队列、地图服务、缓存均可达，闸门单向进入样例回放")
        return self._public_release(release)

    # ---- 闸门二：样例回放（影子环境 + 断点续跑） ----

    def _gate_replay(self, release: dict[str, Any]) -> dict[str, Any]:
        gate_state = release["gate_states"]["replay"]
        runner = ShadowRunner(
            self.store,
            batch_id=release["batch_id"],
            sample_registry=self.sample_registry,
            on_progress=lambda ingested: self._persist_checkpoint(release, ingested),
        )
        # 从未完成项复位：从检查点续跑，已灌入的样例不重复灌入
        checkpoint = list(release["checkpoint"])
        resume_from = len(checkpoint)
        if checkpoint:
            runner.restore_checkpoint(checkpoint)
        try:
            replay = runner.replay(checkpoint)
        except Exception:  # pragma: no cover - 防御性：隔离环境异常按断连处理
            self._mark_gate(gate_state, "中断", f"连接断开，已完成 {len(release['checkpoint'])} 项，待续跑")
            self._append_ledger(release["batch_id"], release["version"], "样例回放",
                                "回放连接断开，检查点保留，从未完成项复位", ok=False)
            raise ReplayDisconnected("影子回放连接断开，检查点已保留，请从未完成项续跑")

        # 故障注入：回放中途断连（检查点已落，已灌入样例不会重复）
        if self._consume_failure(release["batch_id"], "replay"):
            partial = len(release["checkpoint"]) or max(resume_from, 1)
            release["checkpoint"] = list(replay["event_ids"])[:partial]
            self._mark_gate(gate_state, "中断", f"连接断开，检查点保留 {len(release['checkpoint'])} 项")
            self._append_ledger(release["batch_id"], release["version"], "样例回放",
                                f"回放连接断开（注入），检查点 {len(release['checkpoint'])} 项，从未完成项复位",
                                ok=False)
            raise ReplayDisconnected("影子回放连接断开，检查点已保留，请从未完成项续跑")

        release["replay"] = replay
        release["shadow_snapshot"] = runner.shadow_snapshot
        self._mark_gate(gate_state, "通过",
                        f"隔离环境重放 {replay['ingested']} 项"
                        + (f"，断点续跑 {replay['resumed_from']} 项（未重复灌入）" if replay["resumed_from"] else ""))
        release["gate_index"] += 1
        release["updated_at"] = _now()
        self._append_ledger(release["batch_id"], release["version"], "样例回放",
                            gate_state["detail"])
        return self._public_release(release)

    def _persist_checkpoint(self, release: dict[str, Any], ingested: list[str]) -> None:
        release["checkpoint"] = list(ingested)
        release["updated_at"] = _now()

    # ---- 闸门三：差异裁定（影子验收结论回写，同事务） ----

    def _gate_adjudication(self, release: dict[str, Any], unknown_conflict: bool) -> dict[str, Any]:
        gate_state = release["gate_states"]["adjudication"]
        # 每次裁定在隔离环境重新完整重放（旧样例维持此前批次，结果幂等）
        runner = ShadowRunner(
            self.store,
            batch_id=release["batch_id"],
            sample_registry=self.sample_registry,
            unknown_conflict=unknown_conflict,
        )
        runner.replay()
        live = runner.sample_live()
        report = runner.adjudicate(live)
        release["shadow_report"] = report

        # 结论回写台账、路段待办、告警清单：与闸门结论同一事务边界
        snapshot = self._transactional_snapshot()
        try:
            self._writeback_conclusions(release, report, passed=report["verdict"] == "通过")
            self._transactional_commit(snapshot)
        except Exception:
            self._transactional_rollback(snapshot)
            raise

        if report["verdict"] != "通过":
            self._mark_gate(gate_state, "阻断",
                            f"影子验收不通过：阻断 {report['summary']['blocked']} 项，保留旧版本")
            release["status"] = RELEASE_BLOCKED
            release["updated_at"] = _now()
            self._append_ledger(release["batch_id"], release["version"], "差异裁定",
                                f"影子验收不通过，阻断 {report['summary']['blocked']} 项，保留旧版本",
                                ok=False)
            raise ConflictUnresolved(
                f"差异裁定存在无法按辖区运营约定裁决的样例 {report['summary']['blocked']} 项，保留旧版本")

        self._mark_gate(gate_state, "通过",
                        f"影子验收通过：对照差异 {report['summary']['diff_count']} 项，"
                        f"按辖区约定接受/和解 {report['summary']['accepted']} 项，"
                        f"待现网核实 {report['summary']['suspended']} 项")
        release["gate_index"] += 1
        release["updated_at"] = _now()
        self._append_ledger(release["batch_id"], release["version"], "差异裁定", gate_state["detail"])
        return self._public_release(release)

    def _writeback_conclusions(self, release: dict[str, Any], report: dict[str, Any], *, passed: bool) -> None:
        """验收变更结论回写：发布台账（append）、路段待办、告警清单。"""
        batch_id = release["batch_id"]
        for diff in report["diffs"]:
            if diff.get("need_todo"):
                self._add_todo(
                    section=diff["section"],
                    title=f"影子验收差异待办：{diff['event_id']}（{diff['kind']}）",
                    source="影子验收",
                    batch_id=batch_id,
                    status="待核实" if diff["result"] == "suspend" else "待处理",
                    detail=diff["basis"],
                )
            if diff.get("need_alert"):
                self._add_alert(
                    title=f"影子验收告警：{diff['event_id']}（{diff['kind']}）",
                    level="中" if diff["kind"] == "载荷不一致" else "高",
                    source="影子验收",
                    batch_id=batch_id,
                    detail=diff["basis"],
                )
        verdict = report["verdict"]
        self._add_alert(
            title=f"发布批次 {batch_id} 影子验收结论：{verdict}",
            level="低" if passed else "高",
            source="差异裁定",
            batch_id=batch_id,
            detail=(f"对照 {report['summary']['diff_count']} 项差异；"
                    + ("全部已按辖区运营约定裁决" if passed else "存在阻断项，现网版本保持不变")),
            dedupe_key=f"{batch_id}:verdict",
        )

    # ---- 闸门四：切换流量（构建包+迁移+切换+整批回退 同一事务） ----

    def _gate_traffic(self, release: dict[str, Any]) -> dict[str, Any]:
        gate_state = release["gate_states"]["traffic"]
        # 切换前最后一次复查：队列、地图服务或缓存不可达时禁止转换流量
        unreachable = self.infra.unreachable_names()
        if unreachable or self._consume_failure(release["batch_id"], "traffic"):
            names = unreachable or ["注入故障"]
            self._mark_gate(gate_state, "失败", f"前置依赖不可达：{'、'.join(names)}，禁止转换流量")
            self._append_ledger(release["batch_id"], release["version"], "切换流量",
                                f"队列、地图服务或缓存不可达（{'、'.join(names)}），禁止转换流量，保留旧版本",
                                ok=False)
            self._abandon_locked(release, "切换流量前置检查失败，已放弃，现网版本不变")
            raise InfraUnreachable(f"队列、地图服务或缓存不可达（{'、'.join(names)}），禁止转换流量")

        package: ReleasePackage = release["package"]
        snapshot = self._transactional_snapshot()
        try:
            # 同一事务边界：构建包 → 结构迁移 → 流量切换 → 结论回写
            package.apply_migrations(self.store)

            if self._consume_failure(release["batch_id"], "migration"):
                raise MigrationFailed("结构迁移失败（注入）")

            candidate = self.store.overview()
            if not candidate.get("modules") or not any(int(m.get("created", 0)) for m in candidate["modules"]):
                # 绝不把总览写成零
                raise PackageRejected("切换前校验发现候选总览为空，拒绝发布")

            # 路段清单写入发布批次（迁移补齐的默认列在此更新为本批次）
            for row in self.store.rows("road_section"):
                row[ROAD_BATCH_COLUMN] = release["batch_id"]

            self.active_snapshot = copy.deepcopy(candidate)
            self.active_version = {
                "version": release["version"],
                "revision": release["revision"],
                "batch_id": release["batch_id"],
                "signature": package.signature,
                "released_at": _now(),
            }

            self._writeback_release_conclusions(release, package)
            self._transactional_commit(snapshot)
        except Exception:
            # 整批回退：结构迁移 down + 快照还原 + 旧版本总览，同一事务边界
            try:
                package.rollback_migrations(self.store)
            finally:
                self._transactional_rollback(snapshot)
            self._mark_gate(gate_state, "回退", "切换失败，构建包与结构迁移已整批回退，保留旧版本")
            self._abandon_locked(release, "切换流量失败，已整批回退，现网版本不变")
            self._append_ledger(release["batch_id"], release["version"], "整批回退",
                                "构建包、结构迁移与流量切换在同一事务边界整批回退，总览保留旧版本，未写成零",
                                ok=False)
            self._add_alert(
                title=f"发布批次 {release['batch_id']} 整批回退",
                level="高",
                source="切换流量",
                batch_id=release["batch_id"],
                detail="切换流量阶段失败，构建包与结构迁移已整批回退，现网版本不变",
            )
            raise MigrationFailed("切换流量失败，构建包与结构迁移已整批回退，保留旧版本")

        self._mark_gate(gate_state, "通过",
                        f"构建包、结构迁移与流量切换同一事务完成；签名 {package.signature}")
        release["status"] = RELEASE_DONE
        release["gate_index"] = len(GATES)
        release["updated_at"] = _now()
        # 唯一发布者：发布完成释放编排锁
        self._owner_token = None
        self._append_ledger(release["batch_id"], release["version"], "切换流量",
                            f"新版本 {release['version']} 已承接现网流量，编排锁释放")
        return self._public_release(release)

    def _writeback_release_conclusions(self, release: dict[str, Any], package: ReleasePackage) -> None:
        """发布结论回写：路段待办关闭、发布告警、路段清单批次列已在事务中更新。"""
        batch_id = release["batch_id"]
        for todo in self.road_todos:
            if todo["batch_id"] == batch_id and todo["status"] in ("待处理", "待核实"):
                todo["status"] = "已随发布关闭"
                todo["closed_at"] = _now()
        self._add_alert(
            title=f"发布批次 {batch_id} 已上线（{release['version']}）",
            level="低",
            source="切换流量",
            batch_id=batch_id,
            detail=f"构建包签名 {package.signature}；结构迁移 {len(package.migrations)} 项同一事务提交",
            dedupe_key=f"{batch_id}:released",
        )

    # ================= 倒序回滚入口（必须拒绝） =================

    def rollback_gate(self, batch_id: str, gate: str, *, operator: str = "值班员") -> None:
        with self._lock:
            release = self._require_release_locked(batch_id)
            self._require_owner_locked(release, operator)
            raise ReleaseOrderError(
                "发布闸门只允许「依赖检查 → 样例回放 → 差异裁定 → 切换流量」单向推进，"
                "倒序回滚必须被拒绝；切换失败已由系统在同一事务边界内整批回退并保留旧版本")

    # ================= 断点续跑（连接断开复位） =================

    def resume_replay(self, batch_id: str) -> dict[str, Any]:
        """检查连接断开后从未完成项复位：只返回续跑信息，推进仍走 advance 闸门。"""
        with self._lock:
            release = self._require_release_locked(batch_id)
            if release["status"] not in ACTIVE_STATUSES:
                raise ReleaseOrderError(f"批次 {batch_id} 已结束（{release['status']}），无需续跑")
            gate_state = release["gate_states"]["replay"]
            if gate_state["state"] != "中断":
                raise ReleaseOrderError("样例回放未处于中断状态，无需从未完成项复位")
            return {
                "batch_id": batch_id,
                "checkpoint": list(release["checkpoint"]),
                "completed": len(release["checkpoint"]),
                "message": "从未完成项复位：已灌入样例不会重复灌入，调用闸门推进即可续跑",
            }

    # ================= 发布前影子验收（只读预览） =================

    def preview_shadow(self, *, unknown_conflict: bool = False) -> dict[str, Any]:
        """发布前“影子验收”：隔离环境重放并对照，不占编排锁、不写现网。"""
        with self._lock:
            batch_id = "PREVIEW"
            # 预览使用独立注册表，不污染正式批次的样例批次记录
            runner = ShadowRunner(
                self.store,
                batch_id=batch_id,
                sample_registry=dict(self.sample_registry),
                unknown_conflict=unknown_conflict,
            )
            replay = runner.replay()
            live = runner.sample_live()
            report = runner.adjudicate(live)
            return {
                "convention": {
                    "name": runner.convention.name,
                    "timezone": runner.convention.timezone_name,
                    "business_day_cutoff": f"{runner.convention.business_day_cutoff_hour:02d}:00",
                    "device_clock_tolerance_minutes": int(
                        runner.convention.device_clock_tolerance.total_seconds() // 60),
                },
                "replay": replay,
                "report": report,
            }

    # ================= 故障演练 =================

    def inject_failure(self, batch_id: str, stage: str) -> dict[str, Any]:
        with self._lock:
            allowed = {"dependency", "replay", "adjudication", "traffic", "migration"}
            if stage not in allowed:
                raise ReleaseOrderError(f"可注入的故障阶段：{'、'.join(sorted(allowed))}")
            self._require_release_locked(batch_id)
            self.fail_injections.setdefault(batch_id, set()).add(stage)
            return {"batch_id": batch_id, "stage": stage, "armed": True}

    # ================= 事务边界 =================

    def _transactional_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """开启事务：深拷贝现网仓库，并暂存回写类数据，失败时整体还原。"""
        self._tx_store_snapshot = self.store.snapshot()
        self._tx_side_snapshot = (
            copy.deepcopy(self.road_todos),
            copy.deepcopy(self.alerts),
        )
        return self._tx_store_snapshot

    def _transactional_commit(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        self._tx_store_snapshot = None
        self._tx_side_snapshot = None

    def _transactional_rollback(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        self.store.restore(snapshot)
        todos, alerts = self._tx_side_snapshot
        self.road_todos = todos
        self.alerts = alerts
        self._tx_store_snapshot = None
        self._tx_side_snapshot = None

    # ================= 内部工具 =================

    def _active_release_locked(self) -> dict[str, Any] | None:
        for release in self.releases.values():
            if release["status"] in ACTIVE_STATUSES:
                return release
        return None

    def _require_release_locked(self, batch_id: str) -> dict[str, Any]:
        release = self.releases.get(batch_id)
        if release is None:
            raise ReleaseOrderError(f"发布批次 {batch_id} 不存在")
        if release["status"] == RELEASE_ABANDONED:
            raise ReleaseOrderError(f"批次 {batch_id} 已放弃，禁止再操作；请创建新发布")
        if release["status"] == RELEASE_DONE:
            raise ReleaseOrderError(f"批次 {batch_id} 已发布，禁止重复或倒序操作")
        return release

    def _require_owner_locked(self, release: dict[str, Any], operator: str) -> None:
        if self._owner_token != release["lock_token"]:
            raise OrchestrationLocked("编排锁不属于当前发布者，禁止操作该批次")
        if release["operator"] != operator:
            raise OrchestrationLocked(
                f"批次 {release['batch_id']} 由发布者「{release['operator']}」持有，"
                f"「{operator}」不能推进（编排锁保证唯一发布者）")

    def _consume_failure(self, batch_id: str, stage: str) -> bool:
        armed = self.fail_injections.get(batch_id, set())
        if stage in armed:
            armed.discard(stage)
            return True
        return False

    def _abandon_locked(self, release: dict[str, Any], reason: str) -> None:
        release["status"] = RELEASE_ABANDONED
        release["abandon_reason"] = reason
        release["updated_at"] = _now()
        if self._owner_token == release["lock_token"]:
            self._owner_token = None
        self._add_alert(
            title=f"发布批次 {release['batch_id']} 已放弃",
            level="高",
            source="编排器",
            batch_id=release["batch_id"],
            detail=reason,
            dedupe_key=f"{release['batch_id']}:abandon",
        )

    def _mark_gate(self, gate_state: dict[str, Any], state: str, detail: str) -> None:
        gate_state["state"] = state
        gate_state["at"] = _now()
        gate_state["detail"] = detail

    def _append_ledger(self, batch_id: str, version: str, gate: str, conclusion: str, *, ok: bool = True) -> None:
        # 发布台账 append-only：历史记录保留当时批次，不改写
        self.ledger.append({
            "id": len(self.ledger) + 1,
            "batch_id": batch_id,
            "version": version,
            "gate": gate,
            "ok": ok,
            "conclusion": conclusion,
            "recorded_at": _now(),
        })

    def _add_todo(self, *, section: str, title: str, source: str, batch_id: str,
                  status: str, detail: str) -> None:
        key = f"{batch_id}:{title}"
        if any(item["dedup_key"] == key for item in self.road_todos):
            return
        self.road_todos.append({
            "id": len(self.road_todos) + 1,
            "section": section,
            "title": title,
            "source": source,
            "batch_id": batch_id,
            "status": status,
            "detail": detail,
            "created_at": _now(),
            "dedup_key": key,
        })

    def _add_alert(self, *, title: str, level: str, source: str, batch_id: str,
                   detail: str, dedupe_key: str | None = None) -> None:
        key = dedupe_key or f"{batch_id}:{title}"
        if any(item["dedup_key"] == key for item in self.alerts):
            return
        self.alerts.append({
            "id": len(self.alerts) + 1,
            "title": title,
            "level": level,
            "source": source,
            "batch_id": batch_id,
            "detail": detail,
            "status": "未销警",
            "created_at": _now(),
            "dedup_key": key,
        })

    def _public_release(self, release: dict[str, Any] | None) -> dict[str, Any] | None:
        if release is None:
            return None
        package: ReleasePackage = release["package"]
        return {
            "batch_id": release["batch_id"],
            "version": release["version"],
            "revision": release["revision"],
            "operator": release["operator"],
            "status": release["status"],
            "gate_index": release["gate_index"],
            "next_gate": GATES[release["gate_index"]] if release["gate_index"] < len(GATES) else None,
            "gates": [
                {"code": code, "label": GATE_LABELS[code],
                 **release["gate_states"][code]}
                for code in GATES
            ],
            "checkpoint": list(release["checkpoint"]),
            "replay": release.get("replay"),
            "shadow_report": release.get("shadow_report"),
            "package": package.as_meta(),
            "created_at": release["created_at"],
            "updated_at": release["updated_at"],
            "abandon_reason": release.get("abandon_reason"),
        }


# 单例编排器：与全局内存仓库绑定，路由层统一使用它
from app.store import store as _store  # noqa: E402

release_manager = ReleaseManager(_store)
