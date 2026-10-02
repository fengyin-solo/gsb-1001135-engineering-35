"""道桥养护态势总览发布闸门的单元测试（仅依赖标准库，直接 python3 -m unittest）。

覆盖：
* 四阶段单向推进、跳级/倒序拒绝；
* 依赖检查失败保留旧版、不写成零；切流前依赖复核；
* 影子验收隔离重放、逐项对照、业务日/时钟/严重程度/状态四类冲突裁决；
* 辖区约定缺失时差异裁定拦截；
* 构建包+结构迁移+整批回退同一事务边界，切流失败旧版完整保留；
* 版本签名幂等、编排锁单发布者、连接断开复位不重复灌样例；
* 台账只追加、历史批次保留、旧样例维持此前批次。
"""
from __future__ import annotations

import unittest

from app.release.dependencies import CACHE, DependencyRegistry
from app.release.errors import GateError, LockBusyError
from app.release.jurisdiction import JURISDICTION_CITY_EAST
from app.release.models import SessionStatus, Stage, StageState
from app.release.service import ReleaseService
from app.release.shadow import ShadowEngine
from app.store import store


def run_all_gates(svc: ReleaseService, session_id: str) -> object:
    session = None
    for _ in Stage:
        session = svc.advance(session_id, operator="测试员")
    return session


class GateOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        store.reset_to_seed()
        self.deps = DependencyRegistry()
        self.svc = ReleaseService(self.deps)

    def test_full_pipeline_switches_traffic(self) -> None:
        session, created = self.svc.create_session(operator="测试员")
        self.assertTrue(created)
        run_all_gates(self.svc, session.session_id)
        self.assertEqual(self.svc.active_overview()["release"]["batch_no"], session.batch_no)
        stage_states = list(session.stages.values())
        self.assertEqual([s["state"] for s in stage_states], [StageState.PASSED.value] * 4)

    def test_repeat_publish_is_idempotent_by_signature(self) -> None:
        first, _ = self.svc.create_session(operator="甲")
        again, created = self.svc.create_session(operator="乙")
        self.assertFalse(created)
        self.assertEqual(first.session_id, again.session_id)
        self.assertEqual(first.batch_no, again.batch_no)
        # 台账只追加一条 created
        created_entries = [e for e in self.svc.ledger() if e["result"] == "created"]
        self.assertEqual(len(created_entries), 1)

    def test_skipping_stage_is_rejected(self) -> None:
        session, _ = self.svc.create_session(operator="测试员")
        # 手工把当前阶段拨到切换流量（模拟跳级）必须被拒绝：前置闸门未通过
        session.current_stage = Stage.TRAFFIC_SWITCH.value
        with self.assertRaises(GateError):
            self.svc.advance(session.session_id, operator="测试员")

    def test_reverse_advance_rejected_and_rollback_only_for_switched(self) -> None:
        session, _ = self.svc.create_session(operator="测试员")
        self.svc.advance(session.session_id, operator="测试员")  # 依赖检查通过
        # 已经通过的阶段不能倒序重推
        session.current_stage = Stage.DEPENDENCY_CHECK.value
        with self.assertRaises(GateError) as ctx:
            self.svc.advance(session.session_id, operator="测试员")
        self.assertEqual(ctx.exception.code, "reverse_rejected")
        # 未切换流量的批次禁止整批回退
        session.current_stage = Stage.SHADOW_ACCEPTANCE.value
        with self.assertRaises(GateError) as ctx:
            self.svc.rollback(session.session_id, operator="测试员")
        self.assertEqual(ctx.exception.code, "reverse_rejected")


class DependencyGateTests(unittest.TestCase):
    def setUp(self) -> None:
        store.reset_to_seed()
        self.deps = DependencyRegistry()
        self.svc = ReleaseService(self.deps)

    def test_dependency_down_blocks_gate_and_keeps_old_version(self) -> None:
        self.deps.fail(CACHE)
        session, _ = self.svc.create_session(operator="测试员")
        with self.assertRaises(GateError):
            self.svc.advance(session.session_id, operator="测试员")
        self.assertEqual(session.status, SessionStatus.FAILED.value)
        self.assertEqual(session.stages[Stage.DEPENDENCY_CHECK.value]["state"], StageState.FAILED.value)
        # 旧版本（基线）保留，总览非零
        active = self.svc.active_overview()
        self.assertEqual(active["release"]["batch_no"], "baseline")
        self.assertTrue(any(int(c["value"]) > 0 for c in active["cards"]))
        # 失败结论写入台账和告警清单
        self.assertIn("failed", [e["result"] for e in self.svc.ledger(session.batch_no)])
        self.assertTrue(self.svc.alerts())

    def test_switch_requires_dependencies_reachable(self) -> None:
        session, _ = self.svc.create_session(operator="测试员")
        for stage in list(Stage)[:3]:
            self.svc.advance(session.session_id, operator="测试员")
        # 前三关通过后，切流前缓存宕机：禁止转换流量
        self.deps.fail(CACHE)
        with self.assertRaises(GateError):
            self.svc.advance(session.session_id, operator="测试员")
        self.assertEqual(self.svc.active_overview()["release"]["batch_no"], "baseline")
        self.assertEqual(session.status, SessionStatus.FAILED.value)
        # 依赖恢复后从未完成项复位，可以继续切流
        self.deps.recover(CACHE)
        self.svc.resume(session.session_id, operator="测试员")
        self.svc.advance(session.session_id, operator="测试员")
        self.assertEqual(session.status, SessionStatus.SWITCHED.value)


class ShadowAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        store.reset_to_seed()
        self.svc = ReleaseService(DependencyRegistry())

    def test_default_events_cover_three_kinds_and_conflicts_counted(self) -> None:
        engine = ShadowEngine(JURISDICTION_CITY_EAST)
        from app.release.samples import DEFAULT_EVENTS

        comparisons = engine.replay_and_compare(DEFAULT_EVENTS)
        report = engine.acceptance_report(DEFAULT_EVENTS, comparisons)
        self.assertTrue(report["shadow_passed"])
        self.assertEqual(set(report["missing_kinds"]), set())
        self.assertGreaterEqual(report["counts"]["conflict"], 2)
        self.assertGreaterEqual(report["counts"]["new"], 1)

    def test_business_day_follows_jurisdiction_convention(self) -> None:
        # 08:00 切换点：07:59 归前一业务日，08:00 归当日
        from datetime import datetime

        from app.release.jurisdiction import JURISDICTION_CITY_EAST as conv

        early = conv.business_day(datetime.fromisoformat("2026-09-03T07:59:00+08:00"))
        on_time = conv.business_day(datetime.fromisoformat("2026-09-03T08:00:00+08:00"))
        self.assertEqual(early, "2026-09-02")
        self.assertEqual(on_time, "2026-09-03")

    def test_missing_event_kind_blocks_shadow_gate(self) -> None:
        # 只有巡查，缺病害和桥隧：影子验收必须拦截
        events = [
            {
                "kind": "patrol",
                "code": "PATR-0001",
                "road_ref": "路段甲",
                "occurred_at": "2026-09-01T09:00:00+08:00",
                "status": "待巡查",
            }
        ]
        session, _ = self.svc.create_session(operator="测试员", events=events)
        self.svc.advance(session.session_id, operator="测试员")
        with self.assertRaises(GateError):
            self.svc.advance(session.session_id, operator="测试员")
        self.assertEqual(
            session.stages[Stage.SHADOW_ACCEPTANCE.value]["state"], StageState.FAILED.value
        )

    def test_unresolved_conflict_blocks_adjudication(self) -> None:
        # 远郊辖区没有时钟超差/状态裁决约定：冲突样例无法裁决
        events = [
            {
                "kind": "patrol",
                "code": "PATR-0001",
                "road_ref": "路段甲",
                "occurred_at": "2026-09-01T09:00:00+08:00",
                "device_offset_minutes": 10,
                "status": "待巡查",
            },
            {
                "kind": "disease",
                "code": "PAVE-0001",
                "road_ref": "路段甲",
                "occurred_at": "2026-09-01T09:00:00+08:00",
                "device_offset_minutes": 10,
                "status": "修复中",
            },
            {
                "kind": "bridge_tunnel",
                "code": "BRID-0001",
                "road_ref": "桥甲",
                "occurred_at": "2026-09-01T09:00:00+08:00",
                "device_offset_minutes": 10,
                "status": "待检测",
            },
        ]
        session, _ = self.svc.create_session(
            operator="测试员", jurisdiction_code="J-REMOTE", events=events
        )
        for stage in list(Stage)[:2]:
            self.svc.advance(session.session_id, operator="测试员")
        with self.assertRaises(GateError):
            self.svc.advance(session.session_id, operator="测试员")
        self.assertEqual(
            session.stages[Stage.DIFF_ADJUDICATION.value]["state"], StageState.FAILED.value
        )
        # 未通过差异裁定，流量没有切换
        self.assertEqual(self.svc.active_overview()["release"]["batch_no"], "baseline")

    def test_shadow_replay_is_isolated_from_production(self) -> None:
        # 影子重放不允许修改现网业务表
        before = {name: len(rows) for name, rows in store.business_tables().items()}
        engine = ShadowEngine(JURISDICTION_CITY_EAST)
        from app.release.samples import DEFAULT_EVENTS

        engine.replay_and_compare(DEFAULT_EVENTS)
        after = {name: len(rows) for name, rows in store.business_tables().items()}
        self.assertEqual(before, after)


class TransactionAndLockTests(unittest.TestCase):
    def setUp(self) -> None:
        store.reset_to_seed()
        self.deps = DependencyRegistry()
        self.svc = ReleaseService(self.deps)

    def test_switch_failure_rolls_back_and_never_writes_zero(self) -> None:
        session, _ = self.svc.create_session(operator="测试员")
        for stage in list(Stage)[:3]:
            self.svc.advance(session.session_id, operator="测试员")

        # 场景一：结构迁移执行到一半失败，同事务边界必须整批回退
        import app.release.build as build_mod

        original_apply = build_mod.apply_migration

        def _broken_migration(name: str, batch_no: str) -> None:
            # 先落一列再抛错，制造“迁移半完成”现场，验证 __exit__ 整批恢复
            store.rows("road_section")[0]["发布批次"] = "half-applied"
            raise RuntimeError("结构迁移中途失败")

        build_mod.apply_migration = _broken_migration  # type: ignore[assignment]
        try:
            with self.assertRaises(GateError):
                self.svc.advance(session.session_id, operator="测试员")
        finally:
            build_mod.apply_migration = original_apply  # type: ignore[assignment]
        self.assertEqual(self.svc.active_overview()["release"]["batch_no"], "baseline")
        # 半完成的迁移现场被事务快照恢复，不留残列
        self.assertTrue(all("发布批次" not in row for row in store.rows("road_section")))

        # 场景二：故障恢复后复位重推，但若新总览为零仍被拒绝（不写成零）
        self.svc.resume(session.session_id, operator="测试员")
        original_guard = type(self.svc)._guard_overview_not_zero

        def _strict(_svc: object, _overview: object) -> None:
            raise ValueError("新总览为零，拒绝写入")

        type(self.svc)._guard_overview_not_zero = _strict  # type: ignore[method-assign]
        try:
            with self.assertRaises(GateError):
                self.svc.advance(session.session_id, operator="测试员")
        finally:
            type(self.svc)._guard_overview_not_zero = original_guard  # type: ignore[method-assign]
        self.assertEqual(self.svc.active_overview()["release"]["batch_no"], "baseline")
        self.assertTrue(all("发布批次" not in row for row in store.rows("road_section")))

    def test_orchestrator_lock_allows_single_publisher_only(self) -> None:
        first, _ = self.svc.create_session(operator="甲")
        second, _ = self.svc.create_session(
            operator="乙",
            events=[
                {
                    "kind": "patrol",
                    "code": "PATR-OTHER-1",
                    "road_ref": "其他路段",
                    "occurred_at": "2026-09-01T09:00:00+08:00",
                    "status": "待巡查",
                },
                {
                    "kind": "disease",
                    "code": "PAVE-OTHER-1",
                    "road_ref": "其他路段",
                    "occurred_at": "2026-09-01T09:00:00+08:00",
                    "status": "待修复",
                },
                {
                    "kind": "bridge_tunnel",
                    "code": "BRID-OTHER-1",
                    "road_ref": "其他桥",
                    "occurred_at": "2026-09-01T09:00:00+08:00",
                    "status": "待检测",
                },
            ],
        )
        self.svc.advance(first.session_id, operator="甲")
        # 第一批次处于运行中（第二关未推进），编排锁被其持有，第二个发布者被拒
        with self.assertRaises((LockBusyError, GateError)):
            self.svc.advance(second.session_id, operator="乙")

    def test_resume_does_not_reingest_samples(self) -> None:
        session, _ = self.svc.create_session(operator="测试员")
        self.svc.advance(session.session_id, operator="测试员")
        self.svc.advance(session.session_id, operator="测试员")
        ingested_after_shadow = len(session.replays)
        self.assertGreater(ingested_after_shadow, 0)
        # 连接断开后复位：重放记录保持不变，不重复灌入样例
        self.svc.resume(session.session_id, operator="测试员")
        self.svc.advance(session.session_id, operator="测试员")
        self.assertEqual(len(session.replays), ingested_after_shadow)

    def test_rollback_restores_previous_version_in_one_transaction(self) -> None:
        session, _ = self.svc.create_session(operator="测试员")
        run_all_gates(self.svc, session.session_id)
        self.assertEqual(self.svc.active_overview()["release"]["batch_no"], session.batch_no)
        # 发布迁移生效：路段表带批次标记
        self.assertTrue(all("发布批次" in row for row in store.rows("road_section")))
        self.svc.rollback(session.session_id, operator="测试员")
        self.assertEqual(self.svc.active_overview()["release"]["batch_no"], "baseline")
        # 回退迁移清理批次标记
        self.assertTrue(all(row.get("发布批次") is None or "发布批次" not in row
                            for row in store.rows("road_section")))
        # 历史台账仍保留当时批次（只追加）
        batch_entries = self.svc.ledger(session.batch_no)
        self.assertTrue(any(e["result"] == "rolled_back" for e in batch_entries))
        self.assertTrue(any(e["result"] == "passed" for e in batch_entries))

    def test_conclusions_write_back_to_ledger_todos_and_alerts(self) -> None:
        session, _ = self.svc.create_session(operator="测试员")
        run_all_gates(self.svc, session.session_id)
        ledger_results = [e["result"] for e in self.svc.ledger(session.batch_no)]
        self.assertEqual(ledger_results.count("passed"), 4)
        self.assertGreaterEqual(len(self.svc.road_todos()), 1)
        # 每条待办都带批次与辖区，可回溯
        for todo in self.svc.road_todos():
            self.assertEqual(todo["batch_no"], session.batch_no)
            self.assertTrue(todo["jurisdiction_code"])
        self.assertTrue(any(a["level"] == "info" for a in self.svc.alerts()))


if __name__ == "__main__":
    unittest.main()
