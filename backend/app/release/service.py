"""道桥养护态势总览发布编排服务。

闸门按 依赖检查 → 影子验收 → 差异裁定 → 切换流量 单向推进：
* 跳级发布会被拒绝（只能推进 current_stage）；
* 倒序回滚会被拒绝（advance 不接受已完成阶段），唯一允许的回退是对已切换
  批次的「整批回退」，且整批回退与构建包、结构迁移共用同一事务边界；
* 任一步失败都保留旧版本，总览永远不会被写成零；
* 重复发布按版本签名幂等，直接返回首次批次，不重复灌入样例；
* 结论同时回写发布台账、路段待办和告警清单；台账只追加、不改写，
  历史记录保留当时批次与辖区约定版本。
"""
from __future__ import annotations

import copy
import itertools
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.release.build import (
    BATCH_COLUMN,
    BuildPackage,
    build_transaction,
    make_road_batch_migration,
    version_signature,
)
from app.release.dependencies import DependencyRegistry
from app.release.errors import GateError, LockBusyError
from app.release.jurisdiction import get_convention
from app.release.lock import OrchestratorLock
from app.release.models import (
    STAGE_LABELS,
    STAGE_ORDER,
    AlertItem,
    LedgerEntry,
    ReleaseSession,
    RoadTodo,
    SessionStatus,
    Stage,
    StageConclusion,
    StageState,
)
from app.release.samples import DEFAULT_EVENTS
from app.release.shadow import (
    KIND_BRIDGE_TUNNEL,
    KIND_DISEASE,
    KIND_PATROL,
    ShadowEngine,
)
from app.store import store

KIND_LABELS = {
    KIND_PATROL: "巡查",
    KIND_DISEASE: "病害",
    KIND_BRIDGE_TUNNEL: "桥隧事件",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReleaseService:
    def __init__(self, dependencies: DependencyRegistry | None = None) -> None:
        self._deps = dependencies or DependencyRegistry()
        self._lock = OrchestratorLock()
        self._guard = threading.RLock()
        self._seq = itertools.count(1)
        self._sessions: dict[str, ReleaseSession] = {}
        self._ledger: list[LedgerEntry] = []
        self._alerts: list[AlertItem] = []
        self._todos: list[RoadTodo] = []
        # 版本签名 → 首次批次（重复发布幂等）
        self._signature_index: dict[str, str] = {}
        # 当前生效版本；初始基线保证平台一启动总览就非空，绝不展示零
        self._active: dict[str, Any] = {
            "overview": store.overview(),
            "batch_no": "baseline",
            "version_signature": "baseline",
            "package_signature": "baseline",
            "jurisdiction_code": None,
            "activated_at": _now(),
        }
        self._previous_bundle: dict[str, list[dict[str, Any]]] | None = None
        self._previous_active: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 工具
    def reset(self) -> None:
        """清空全部发布运行态（测试用，不重置业务表，保留外部依赖故障设置）。"""
        deps = self._deps
        with self._guard:
            self._deps = deps
            self._lock = OrchestratorLock()
            self._seq = itertools.count(1)
            self._sessions = {}
            self._ledger = []
            self._alerts = []
            self._todos = []
            self._signature_index = {}
            self._active = {
                "overview": store.overview(),
                "batch_no": "baseline",
                "version_signature": "baseline",
                "package_signature": "baseline",
                "jurisdiction_code": None,
                "activated_at": _now(),
            }
            self._previous_bundle = None
            self._previous_active = None

    @staticmethod
    def _find_stage(stage_name: str) -> Stage:
        try:
            return Stage(stage_name)
        except ValueError as exc:
            raise GateError(f"未知闸门阶段：{stage_name}") from exc

    def _get_session(self, session_id: str) -> ReleaseSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise GateError(f"发布批次 {session_id} 不存在", code="session_not_found")
        return session

    def _append_ledger(
        self,
        session: ReleaseSession,
        stage: Stage,
        result: str,
        message: str,
        detail: dict[str, Any],
    ) -> LedgerEntry:
        convention = get_convention(session.jurisdiction_code)
        entry = LedgerEntry(
            seq=next(self._seq),
            batch_no=session.batch_no,
            version_signature=session.version_signature,
            stage=stage.value,
            result=result,
            operator=session.operator,
            jurisdiction_code=session.jurisdiction_code,
            convention_version=convention.convention_version,
            message=message,
            detail=detail,
            recorded_at=_now(),
        )
        self._ledger.append(entry)
        return entry

    def _add_alert(
        self,
        session: ReleaseSession,
        level: str,
        stage: Stage,
        title: str,
        content: str,
    ) -> AlertItem:
        item = AlertItem(
            id=f"ALT-{uuid.uuid4().hex[:10]}",
            batch_no=session.batch_no,
            level=level,
            source_stage=stage.value,
            title=title,
            content=content,
            jurisdiction_code=session.jurisdiction_code,
            created_at=_now(),
        )
        self._alerts.append(item)
        return item

    # ------------------------------------------------------------ 创建批次
    def create_session(
        self,
        *,
        operator: str,
        jurisdiction_code: str | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> tuple[ReleaseSession, bool]:
        """创建发布批次；同版本签名重复发布按幂等返回首次批次。

        幂等返回时不会重新灌入样例（隔离重放只跑一次），状态标为 idempotent_replay。
        """
        jurisdiction_code = jurisdiction_code or get_convention(None).jurisdiction_code
        convention = get_convention(jurisdiction_code)
        payload_events = events if events is not None else copy.deepcopy(DEFAULT_EVENTS)
        signature = version_signature(
            payload_events, jurisdiction_code, convention.convention_version
        )
        with self._guard:
            existing_id = self._signature_index.get(signature)
            if existing_id:
                existing = self._sessions[existing_id]
                return existing, False

            batch_no = f"REL-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{next(self._seq):03d}"
            session = ReleaseSession(
                session_id=uuid.uuid4().hex,
                batch_no=batch_no,
                version_signature=signature,
                package_signature="",
                jurisdiction_code=jurisdiction_code,
                operator=operator,
                status="created",
                current_stage=Stage.DEPENDENCY_CHECK.value,
                events=payload_events,
                created_at=_now(),
            )
            self._sessions[session.session_id] = session
            self._signature_index[signature] = session.session_id
            self._append_ledger(
                session,
                Stage.DEPENDENCY_CHECK,
                "created",
                f"批次 {batch_no} 已创建，版本签名 {signature}，等待依赖检查",
                {"event_count": len(payload_events)},
            )
            return session, True

    # ------------------------------------------------------------ 闸门推进
    def advance(self, session_id: str, *, operator: str) -> ReleaseSession:
        """单向推进当前闸门。跳级、倒序、依赖未达都会被拒绝。"""
        with self._guard:
            session = self._get_session(session_id)
            stage = self._find_stage(session.current_stage)
            existing = session.stages.get(stage.value)

            # 倒序保护：已经通过的阶段不允许再次“推进”
            if existing and existing.get("state") == StageState.PASSED.value:
                raise GateError(
                    f"{STAGE_LABELS[stage]}已通过，闸门只允许单向推进，倒序回滚必须走整批回退",
                    code="reverse_rejected",
                )
            if session.status == SessionStatus.SWITCHED.value:
                raise GateError("该批次已切换流量，禁止重复切换")

            # 跳级保护：必须前一闸门已经通过，禁止跳级发布
            stage_idx = STAGE_ORDER.index(stage)
            if stage_idx > 0:
                prev = STAGE_ORDER[stage_idx - 1]
                if session.stages.get(prev.value, {}).get("state") != StageState.PASSED.value:
                    raise GateError(
                        f"跳级发布被拒绝：{STAGE_LABELS[prev]}尚未通过，闸门必须单向逐级推进",
                        code="skip_rejected",
                    )

            # 编排锁：同一时刻只有一个发布者；失败后重试重新获锁
            try:
                token = self._lock.acquire(session.batch_no)
            except LockBusyError:
                raise
            session.lock_holder = session.batch_no
            session.lock_token = token
            session.last_heartbeat = _now()
            session.status = SessionStatus.RUNNING.value

            handlers = {
                Stage.DEPENDENCY_CHECK: self._run_dependency_check,
                Stage.SHADOW_ACCEPTANCE: self._run_shadow_acceptance,
                Stage.DIFF_ADJUDICATION: self._run_diff_adjudication,
                Stage.TRAFFIC_SWITCH: self._run_traffic_switch,
            }
            try:
                handlers[stage](session)
            finally:
                # 成功切流后锁随批次完成释放；失败时保留锁，只有同批次能复位
                if session.status != SessionStatus.RUNNING.value:
                    self._safe_release(session)

            return session

    def _pass_stage(
        self,
        session: ReleaseSession,
        stage: Stage,
        detail: dict[str, Any],
        message: str,
    ) -> None:
        conclusion = StageConclusion(stage=stage.value, state=StageState.PASSED.value, detail=detail)
        session.stages[stage.value] = conclusion.to_dict()
        idx = STAGE_ORDER.index(stage)
        if idx + 1 < len(STAGE_ORDER):
            session.current_stage = STAGE_ORDER[idx + 1].value
        else:
            session.current_stage = ""
        self._append_ledger(session, stage, "passed", message, detail)

    def _fail_stage(
        self,
        session: ReleaseSession,
        stage: Stage,
        detail: dict[str, Any],
        message: str,
    ) -> None:
        """记录失败结论：旧版本保留、总览不写成零；台账与告警正常回写。"""
        conclusion = StageConclusion(stage=stage.value, state=StageState.FAILED.value, detail=detail)
        session.stages[stage.value] = conclusion.to_dict()
        session.status = SessionStatus.FAILED.value
        self._append_ledger(session, stage, "failed", message, detail)
        self._add_alert(
            session,
            level="error",
            stage=stage,
            title=f"{STAGE_LABELS[stage]}未通过，保留旧版本",
            content=message,
        )

    # 阶段 1：依赖检查
    def _run_dependency_check(self, session: ReleaseSession) -> None:
        stage = Stage.DEPENDENCY_CHECK
        statuses = [item.to_dict() for item in self._deps.statuses()]
        unreachable = [item["label"] for item in statuses if not item["reachable"]]
        detail = {"dependencies": statuses, "unreachable": unreachable}
        if unreachable:
            self._fail_stage(
                session,
                stage,
                detail,
                "依赖检查未通过，不可达：" + "、".join(unreachable) + "，禁止继续发布",
            )
            raise GateError("依赖检查未通过：" + "、".join(unreachable))
        self._pass_stage(session, stage, detail, "队列、地图服务、缓存均可达，依赖检查通过")

    # 阶段 2：影子验收
    def _run_shadow_acceptance(self, session: ReleaseSession) -> None:
        stage = Stage.SHADOW_ACCEPTANCE
        # 检查连接断开后复位：已灌入隔离环境的样例按 (批次, 类型:编号) 去重，
        # 不重复灌入样例
        ingested = {item["key"] for item in session.replays}
        engine = ShadowEngine(get_convention(session.jurisdiction_code))
        for event in session.events:
            key = f"{event['kind']}:{event['code']}"
            if key in ingested:
                continue
            session.replays.append({"key": key, "ingested_at": _now()})
            ingested.add(key)

        comparisons = engine.replay_and_compare(session.events)
        report = engine.acceptance_report(session.events, comparisons)
        detail = {"report": report, "ingested": len(session.replays)}
        if not report["shadow_passed"]:
            self._fail_stage(
                session,
                stage,
                detail,
                "影子验收未通过：" + "；".join(report["blocked_reasons"]),
            )
            raise GateError("影子验收未通过")
        message = (
            f"影子验收重放 {report['replayed']} 条：一致 {report['counts']['match']}、"
            f"差异 {report['counts']['diff']}、冲突 {report['counts']['conflict']}、"
            f"新增 {report['counts']['new']}"
        )
        self._pass_stage(session, stage, detail, message)

    # 阶段 3：差异裁定
    def _run_diff_adjudication(self, session: ReleaseSession) -> None:
        stage = Stage.DIFF_ADJUDICATION
        shadow_detail = session.stages.get(Stage.SHADOW_ACCEPTANCE.value, {}).get("detail", {})
        comparisons = shadow_detail.get("report", {}).get("comparisons", [])
        convention = get_convention(session.jurisdiction_code)

        rulings: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        todos: list[dict[str, Any]] = []
        accepted_changes = 0
        for item in comparisons:
            if item["verdict"] == "match":
                continue
            verdicts: list[dict[str, Any]] = []
            for conflict in item["conflicts"]:
                verdicts.append(convention.adjudicate(conflict, item["production"] or {}, item["shadow"]))
            ruling = {
                "kind": item["kind"],
                "code": item["code"],
                "road_ref": item["road_ref"],
                "verdict": item["verdict"],
                "verdicts": verdicts,
            }
            if item["verdict"] == "conflict":
                if any(v["decision"] == "unresolved" for v in verdicts):
                    unresolved.append(ruling)
                    continue
                if all(v["decision"] == "accept" for v in verdicts):
                    accepted_changes += 1
                    self._record_road_todo(session, item, ruling)
                    todos.append(ruling["code"])
                else:
                    # 部分裁决维持现网：登记差异说明，不作为待办变更
                    accepted_changes += 0
            elif item["verdict"] in ("diff", "new"):
                accepted_changes += 1
                self._record_road_todo(session, item, None)
                todos.append(item["code"])
            rulings.append(ruling)

        detail = {
            "rulings": rulings,
            "unresolved": unresolved,
            "accepted_changes": accepted_changes,
            "todos": todos,
            "convention_version": convention.convention_version,
        }
        if unresolved:
            codes = "、".join(f"{r['code']}({'/'.join(v['conflict'] for v in r['verdicts'] if v['decision'] == 'unresolved')})" for r in unresolved)
            message = f"冲突样例超出辖区运营约定覆盖范围，无法裁决：{codes}，差异裁定未通过"
            self._fail_stage(session, stage, detail, message)
            raise GateError(message)

        # 构建包在裁定通过后定稿：业务表快照 + 结构迁移（同事务边界，切流时执行）
        package = self._build_package(session, detail)
        session.package_signature = package.package_signature
        detail["package_signature"] = package.package_signature
        self._pass_stage(
            session,
            stage,
            detail,
            f"差异裁定完成：采纳变更 {accepted_changes} 条，路段待办 {len(todos)} 条，构建包 {package.package_signature}",
        )
        self._add_alert(
            session,
            level="info",
            stage=stage,
            title="影子验收变更结论已裁定",
            content=f"批次 {session.batch_no} 采纳变更 {accepted_changes} 条，待办已回写路段清单",
        )

    def _record_road_todo(
        self, session: ReleaseSession, item: dict[str, Any], ruling: dict[str, Any] | None
    ) -> None:
        rule_ids = [v.get("rule_id") for v in (ruling or {}).get("verdicts", []) if v.get("rule_id")]
        decision = "conflict_adjudicated" if ruling else item["verdict"]
        title = f"{KIND_LABELS.get(item['kind'], item['kind'])} {item['code']} 影子验收变更待处置"
        todo = RoadTodo(
            id=f"TODO-{uuid.uuid4().hex[:10]}",
            batch_no=session.batch_no,
            road_ref=item["road_ref"] or "未归属路段",
            kind=item["kind"],
            title=title,
            event_code=item["code"],
            decision=decision,
            rule_id="、".join(rule_ids) if rule_ids else None,
            jurisdiction_code=session.jurisdiction_code,
            created_at=_now(),
        )
        self._todos.append(todo)

    def _new_overview(self, session: ReleaseSession, detail: dict[str, Any]) -> dict[str, Any]:
        """以裁定后的变更结论构建新批次总览（基线总览 + 影子变更，不允许为零）。"""
        base = store.overview()
        accepted = int(detail.get("accepted_changes", 0))
        cards = [dict(card) for card in base["cards"]]
        for card in cards:
            if card["label"] == "待处理":
                card["value"] = int(card["value"]) + len(detail.get("todos", []))
        cards = cards + [
            {"label": "影子验收采纳变更", "value": accepted},
            {"label": "影子重放样例", "value": len(session.events)},
        ]
        return {
            "cards": cards,
            "modules": base["modules"],
            "batch_no": session.batch_no,
            "version_signature": session.version_signature,
            "jurisdiction_code": session.jurisdiction_code,
        }

    def _build_package(self, session: ReleaseSession, detail: dict[str, Any]) -> BuildPackage:
        overview = self._new_overview(session, detail)
        table_bundle = {
            name: copy.deepcopy(rows) for name, rows in store.business_tables().items()
        }
        # 预制结构迁移后的表结构：路段表带「发布批次」列，已有历史批次标记的保留
        for row in table_bundle.get("road_section", []):
            row.setdefault("发布批次", session.batch_no)
        forward, backward = make_road_batch_migration(session.batch_no)
        return BuildPackage(
            batch_no=session.batch_no,
            version_signature=session.version_signature,
            overview=overview,
            table_bundle=table_bundle,
            migrations=[forward],
            rollback_migrations=[backward],
        )

    # 阶段 4：切换流量
    def _run_traffic_switch(self, session: ReleaseSession) -> None:
        stage = Stage.TRAFFIC_SWITCH
        # 切流前再次复核外部依赖：队列、地图服务或缓存不可达时禁止转换流量
        statuses = [item.to_dict() for item in self._deps.statuses()]
        unreachable = [item["label"] for item in statuses if not item["reachable"]]
        if unreachable:
            detail = {"dependencies": statuses, "unreachable": unreachable}
            self._fail_stage(
                session,
                stage,
                detail,
                "队列/地图服务/缓存不可达，禁止转换流量，保留旧版本：" + "、".join(unreachable),
            )
            raise GateError("外部依赖不可达，禁止转换流量")

        adj_detail = session.stages.get(Stage.DIFF_ADJUDICATION.value, {}).get("detail", {})
        package = self._build_package(session, adj_detail)

        # 构建包、结构迁移、整批回退同一事务边界：保存旧版快照，失败整批恢复
        previous_bundle = {
            name: copy.deepcopy(rows) for name, rows in store.business_tables().items()
        }
        previous_active = copy.deepcopy(self._active)
        try:
            # 进事务前先校验新总览合法：绝不把总览写成零（事务内还有同样防御）
            self._guard_overview_not_zero(package.overview)
            with build_transaction(package, mode="apply") as tx:
                new_overview = tx.apply()
                self._guard_overview_not_zero(new_overview)
        except Exception as exc:  # noqa: BLE001 - 任何失败都保留旧版本
            detail = {"error": str(exc), "retained_batch": self._active["batch_no"]}
            self._fail_stage(
                session,
                stage,
                detail,
                f"切换流量失败，已整批回退、保留旧版本 {self._active['batch_no']}：{exc}",
            )
            raise GateError("切换流量失败，旧版本已保留") from exc

        # 事务提交成功后再切换生效指针（台账只追加，历史批次不受影响）
        self._previous_bundle = previous_bundle
        self._previous_active = previous_active
        self._active = {
            "overview": package.overview,
            "batch_no": session.batch_no,
            "version_signature": session.version_signature,
            "package_signature": package.package_signature,
            "jurisdiction_code": session.jurisdiction_code,
            "activated_at": _now(),
        }
        session.status = SessionStatus.SWITCHED.value
        detail = {
            "dependencies": statuses,
            "package_signature": package.package_signature,
            "active_batch": session.batch_no,
            "retained_previous_batch": previous_active["batch_no"],
        }
        self._pass_stage(session, stage, detail, f"流量已切换到批次 {session.batch_no}，旧版本快照保留可整批回退")
        self._add_alert(
            session,
            level="info",
            stage=stage,
            title="态势总览发布完成",
            content=f"批次 {session.batch_no} 已生效，版本签名 {session.version_signature}",
        )

    @staticmethod
    def _guard_overview_not_zero(overview: dict[str, Any]) -> None:
        """绝不让总览落成零：无卡片、无模块或全部指标为 0 都视为失败。"""
        cards = overview.get("cards") or []
        modules = overview.get("modules") or []
        if not cards or not modules:
            raise ValueError("新总览为空，拒绝写入（不得把总览写成零）")
        if all(int(card.get("value", 0)) == 0 for card in cards):
            raise ValueError("新总览全部指标为 0，拒绝写入（不得把总览写成零）")

    # ------------------------------------------------------------ 复位/回退
    def resume(self, session_id: str, *, operator: str) -> ReleaseSession:
        """检查连接断开后从未完成项复位：重新认锁并续跑当前阶段。

        已灌入隔离环境的样例有去重，复位不会重复灌入样例。
        """
        with self._guard:
            session = self._get_session(session_id)
            if session.status not in (SessionStatus.FAILED.value, SessionStatus.RUNNING.value):
                raise GateError("只有未完成（运行中或失败待重试）的批次可以复位")
            try:
                token = self._lock.acquire(session.batch_no)
            except LockBusyError as exc:
                raise GateError(str(exc), code="lock_busy") from exc
            session.lock_token = token
            session.lock_holder = session.batch_no
            session.last_heartbeat = _now()
            session.status = SessionStatus.RUNNING.value
            self._append_ledger(
                session,
                self._find_stage(session.current_stage or Stage.TRAFFIC_SWITCH.value),
                "resumed",
                f"检查连接断开后由 {operator} 从未完成项复位，已灌入样例不重灌",
                {"ingested": len(session.replays)},
            )
            return session

    def heartbeat(self, session_id: str) -> None:
        with self._guard:
            session = self._get_session(session_id)
            if not session.lock_token:
                raise GateError("批次未持有编排锁")
            try:
                self._lock.heartbeat(session.batch_no, session.lock_token)
            except LockBusyError as exc:
                raise GateError(str(exc), code="lock_busy") from exc
            session.last_heartbeat = _now()

    def rollback(self, session_id: str, *, operator: str) -> ReleaseSession:
        """对已切换批次做整批回退。

        倒序回滚被拒绝：只有当前生效批次可以整批回退；构建包回退迁移与旧版表
        快照恢复使用同一事务边界，失败则保持当前版本不动。
        """
        with self._guard:
            session = self._get_session(session_id)
            if session.status != SessionStatus.SWITCHED.value:
                raise GateError(
                    "倒序回滚被拒绝：该批次尚未切换流量，闸门只允许单向推进",
                    code="reverse_rejected",
                )
            if self._active["batch_no"] != session.batch_no:
                raise GateError(
                    f"倒序回滚被拒绝：批次 {session.batch_no} 不是当前生效版本，"
                    "历史批次保留当时记录，不允许回退",
                    code="reverse_rejected",
                )
            package = self._build_package(
                session, session.stages[Stage.DIFF_ADJUDICATION.value]["detail"]
            )
            current_snapshot = {
                name: copy.deepcopy(rows) for name, rows in store.business_tables().items()
            }
            try:
                with build_transaction(package, mode="rollback") as tx:
                    tx.set_rollback_bundle(self._previous_bundle or {})
                    tx.apply()
            except Exception as exc:  # noqa: BLE001
                self._add_alert(
                    session,
                    level="error",
                    stage=Stage.TRAFFIC_SWITCH,
                    title="整批回退失败，当前版本保留",
                    content=str(exc),
                )
                raise GateError(f"整批回退失败：{exc}") from exc

            # 事务成功：生效指针退回上一版；台账只追加，历史记录保留
            restored_active = self._previous_active
            self._previous_active = copy.deepcopy(self._active)
            self._previous_bundle = current_snapshot
            if restored_active is not None:
                self._active = restored_active
            session.status = SessionStatus.ROLLED_BACK.value
            # 撤回本批次写入的路段待办与告警结论
            for todo in self._todos:
                if todo.batch_no == session.batch_no:
                    todo.done = True
            self._safe_release(session)
            self._append_ledger(
                session,
                Stage.TRAFFIC_SWITCH,
                "rolled_back",
                f"{operator} 对批次 {session.batch_no} 执行整批回退，已恢复版本 {self._active['batch_no']}",
                {"restored_batch": self._active["batch_no"]},
            )
            self._add_alert(
                session,
                level="warning",
                stage=Stage.TRAFFIC_SWITCH,
                title="态势总览已整批回退",
                content=f"批次 {session.batch_no} 已回退，恢复版本 {self._active['batch_no']}",
            )
            return session

    def _safe_release(self, session: ReleaseSession) -> None:
        if session.lock_token:
            try:
                self._lock.release(session.batch_no, session.lock_token)
            except LockBusyError:
                pass
        session.lock_token = None
        session.lock_holder = None

    # --------------------------------------------------------------- 查询面
    def active_overview(self) -> dict[str, Any]:
        overview = copy.deepcopy(self._active["overview"])
        overview["release"] = {
            "batch_no": self._active["batch_no"],
            "version_signature": self._active["version_signature"],
            "package_signature": self._active["package_signature"],
            "jurisdiction_code": self._active["jurisdiction_code"],
            "activated_at": self._active["activated_at"],
        }
        return overview

    def list_sessions(self) -> list[dict[str, Any]]:
        return [session.to_dict() for session in self._sessions.values()]

    def session_detail(self, session_id: str) -> dict[str, Any]:
        return self._get_session(session_id).to_dict()

    def ledger(self, batch_no: str | None = None) -> list[dict[str, Any]]:
        entries = self._ledger
        if batch_no:
            entries = [entry for entry in entries if entry.batch_no == batch_no]
        return [entry.to_dict() for entry in entries]

    def alerts(self, include_resolved: bool = True) -> list[dict[str, Any]]:
        items = self._alerts if include_resolved else [item for item in self._alerts if not item.resolved]
        return [item.to_dict() for item in items]

    def road_todos(self, road_ref: str | None = None) -> list[dict[str, Any]]:
        items = self._todos
        if road_ref:
            items = [item for item in items if item.road_ref == road_ref]
        return [item.to_dict() for item in items]

    def lock_status(self) -> dict[str, Any] | None:
        return self._lock.holder()

    def dependency_status(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._deps.statuses()]

    def set_dependency_fault(self, name: str, down: bool) -> None:
        if down:
            self._deps.fail(name)
        else:
            self._deps.recover(name)


release_service = ReleaseService()
