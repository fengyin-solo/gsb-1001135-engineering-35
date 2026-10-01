"""总览发布闸门与影子验收的规则测试（仅标准库 unittest）。

覆盖：单向闸门/跳级/倒序拒绝、失败保留旧版本且总览非零、幂等、唯一发布者、
依赖不可达禁止切换、断连断点续跑不重灌、影子验收逐项裁决、时钟约定、
构建包+结构迁移+整批回退同事务、结论三回写、历史批次保留。
"""
from __future__ import annotations

import unittest

from app.release.clocks import CONVENTION, legacy_normalize, normalize
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
from app.release.manager import GATES, RELEASE_ABANDONED, RELEASE_DONE, ReleaseManager
from app.release.packages import ROAD_BATCH_COLUMN
from app.release.samples import ShadowRunner, build_pool
from app.store import Store


def fresh_manager() -> ReleaseManager:
    store = Store()
    return ReleaseManager(store, Infrastructure())


def drive_happy(manager: ReleaseManager, *, batch: str | None = None,
                operator: str = "值班员") -> str:
    created = manager.create_release(operator=operator)
    batch_id = batch or created["batch_id"]
    for gate in GATES:
        manager.advance(batch_id, gate, operator=operator)
    return batch_id


class GateFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = fresh_manager()

    def test_happy_path_publishes_and_overview_tracks_version(self) -> None:
        created = self.manager.create_release(operator="张三", revision="例行发布A")
        self.assertEqual(created["status"], "待发布")
        self.assertEqual(created["next_gate"], "dependency")
        for gate in GATES:
            result = self.manager.advance(created["batch_id"], gate, operator="张三")
        self.assertEqual(result["status"], RELEASE_DONE)
        self.assertIsNone(result["next_gate"])
        # 总览指向新版本且永不为空
        overview = self.manager.overview()
        self.assertEqual(overview["release"]["batch_id"], created["batch_id"])
        self.assertGreater(len(overview["modules"]), 0)
        self.assertGreater(sum(m["created"] for m in overview["modules"]), 0)
        # 编排锁已释放
        self.assertFalse(self.manager.status()["orchestration_lock"]["locked"])

    def test_skip_gate_rejected(self) -> None:
        created = self.manager.create_release()
        with self.assertRaises(ReleaseOrderError) as ctx:
            self.manager.advance(created["batch_id"], "traffic")
        self.assertIn("跳级", str(ctx.exception))
        # 跳级后状态不变，仍可正常发布
        for gate in GATES:
            self.manager.advance(created["batch_id"], gate)
        self.assertEqual(self.manager.get_release(created["batch_id"])["status"], RELEASE_DONE)

    def test_reverse_rollback_rejected(self) -> None:
        created = self.manager.create_release()
        self.manager.advance(created["batch_id"], "dependency")
        with self.assertRaises(ReleaseOrderError) as ctx:
            self.manager.advance(created["batch_id"], "dependency")
        self.assertIn("倒序回滚", str(ctx.exception))
        with self.assertRaises(ReleaseOrderError):
            self.manager.rollback_gate(created["batch_id"], "dependency")

    def test_second_publisher_rejected_until_done(self) -> None:
        first = self.manager.create_release(operator="甲")
        with self.assertRaises(OrchestrationLocked):
            self.manager.create_release(operator="乙")
        # 其他发布者不能推进甲的批次
        with self.assertRaises(OrchestrationLocked):
            self.manager.advance(first["batch_id"], "dependency", operator="乙")
        for gate in GATES:
            self.manager.advance(first["batch_id"], gate, operator="甲")
        # 锁释放后第二个人才能创建发布
        second = self.manager.create_release(operator="乙", revision="乙的新发布")
        self.assertIn("BATCH", second["batch_id"])
        self.assertFalse(second.get("idempotent", False))

    def test_dependency_failure_keeps_old_version_and_abandons(self) -> None:
        self.manager.infra.set_reachable("queue", False)
        created = self.manager.create_release()
        with self.assertRaises(InfraUnreachable):
            self.manager.advance(created["batch_id"], "dependency")
        release = self.manager.get_release(created["batch_id"])
        self.assertEqual(release["status"], RELEASE_ABANDONED)
        # 现网版本仍是基线
        self.assertEqual(self.manager.overview()["release"]["version"], "v0")
        # 失败也保留台账记录
        self.assertTrue(any(row["batch_id"] == created["batch_id"] for row in self.manager.list_ledger()))
        # 锁释放后可重新发布
        self.manager.infra.set_reachable("queue", True)
        again = self.manager.create_release()
        drive_happy(self.manager, batch=again["batch_id"])

    def test_replay_disconnect_resumes_from_checkpoint_without_duplicate(self) -> None:
        created = self.manager.create_release()
        batch_id = created["batch_id"]
        self.manager.advance(batch_id, "dependency")
        self.manager.inject_failure(batch_id, "replay")
        with self.assertRaises(ReplayDisconnected):
            self.manager.advance(batch_id, "replay")
        # 检查连接断开后从未完成项复位
        resume = self.manager.resume_replay(batch_id)
        self.assertGreaterEqual(resume["completed"], 1)
        checkpoint = list(resume["checkpoint"])
        # 续跑成功：完成数不重复灌入
        result = self.manager.advance(batch_id, "replay")
        self.assertEqual(result["gates"][1]["state"], "通过")
        self.assertEqual(result["replay"]["resumed_from"], len(checkpoint))
        self.assertEqual(sorted(result["replay"]["event_ids"]),
                         sorted(set(result["replay"]["event_ids"])))

    def test_adjudication_blocks_on_unknown_conflict(self) -> None:
        created = self.manager.create_release()
        batch_id = created["batch_id"]
        self.manager.advance(batch_id, "dependency")
        self.manager.advance(batch_id, "replay")
        with self.assertRaises(ConflictUnresolved):
            self.manager.advance(batch_id, "adjudication", unknown_conflict=True)
        release = self.manager.get_release(batch_id)
        self.assertEqual(release["status"], "差异阻断")
        self.assertEqual(release["shadow_report"]["verdict"], "不通过")
        # 阻断时保留旧版本
        self.assertEqual(self.manager.overview()["release"]["version"], "v0")
        # 阻断结论也回写了台账/待办/告警
        self.assertTrue(any(t["batch_id"] == batch_id for t in self.manager.list_road_todos()))
        self.assertTrue(any(a["batch_id"] == batch_id for a in self.manager.list_alerts()))

    def test_shadow_acceptance_happy_adjudication(self) -> None:
        created = self.manager.create_release()
        batch_id = created["batch_id"]
        self.manager.advance(batch_id, "dependency")
        self.manager.advance(batch_id, "replay")
        result = self.manager.advance(batch_id, "adjudication")
        report = result["shadow_report"]
        self.assertEqual(report["verdict"], "通过")
        self.assertGreaterEqual(report["summary"]["accepted"], 4)
        self.assertGreaterEqual(report["summary"]["suspended"], 1)
        self.assertEqual(report["summary"]["blocked"], 0)
        # 时区/业务日/设备钟/载荷/影子独有 五类差异都被识别
        kinds = {diff["kind"] for diff in report["diffs"]}
        self.assertIn("时区不一致", kinds)
        self.assertIn("业务日基准", kinds)
        self.assertIn("设备时钟偏差", kinds)
        self.assertIn("载荷不一致", kinds)
        self.assertIn("影子独有", kinds)
        self.assertIn("现网独有", kinds)
        # 现网独有项首轮已自动补灌重新对照，结果 resolved
        live_diffs = [d for d in report["diffs"] if d["kind"] == "现网独有"]
        self.assertTrue(all(d["resolved"] for d in live_diffs))

    def test_traffic_unreachable_blocks_switch(self) -> None:
        created = self.manager.create_release()
        batch_id = created["batch_id"]
        for gate in ("dependency", "replay", "adjudication"):
            self.manager.advance(batch_id, gate)
        self.manager.infra.set_reachable("cache", False)
        with self.assertRaises(InfraUnreachable):
            self.manager.advance(batch_id, "traffic")
        self.assertEqual(self.manager.overview()["release"]["version"], "v0")

    def test_migration_failure_full_batch_rollback(self) -> None:
        created = self.manager.create_release()
        batch_id = created["batch_id"]
        for gate in ("dependency", "replay", "adjudication"):
            self.manager.advance(batch_id, gate)
        self.manager.inject_failure(batch_id, "migration")
        with self.assertRaises(MigrationFailed):
            self.manager.advance(batch_id, "traffic")
        # 结构迁移 down：路段表新增列被移除
        for row in self.manager.store.rows("road_section"):
            self.assertNotIn(ROAD_BATCH_COLUMN, row)
        # 总览仍为基线版本，未写成零
        self.assertEqual(self.manager.overview()["release"]["version"], "v0")
        self.assertGreater(len(self.manager.overview()["modules"]), 0)

    def test_idempotent_repeated_release(self) -> None:
        first = drive_happy(self.manager)
        second = self.manager.create_release(revision="总览例行发布")
        self.assertTrue(second.get("idempotent"))
        self.assertEqual(second["batch_id"], first)
        # 不会新增活跃发布
        self.assertIsNone(self.manager.status()["active_release"])

    def test_transactional_writebacks_on_success(self) -> None:
        batch_id = drive_happy(self.manager)
        # 路段清单写入本批次
        for row in self.manager.store.rows("road_section"):
            self.assertEqual(row[ROAD_BATCH_COLUMN], batch_id)
        # 待办随发布关闭
        self.assertTrue(all(t["status"] == "已随发布关闭" for t in self.manager.list_road_todos()))
        # 台账包含全部闸门结论
        gates_recorded = {row["gate"] for row in self.manager.list_ledger() if row["batch_id"] == batch_id}
        self.assertEqual(gates_recorded, {"创建发布", *[g for g in GATES_LABELS.values()]})

    def test_empty_snapshot_build_rejected(self) -> None:
        manager = ReleaseManager(Store(), Infrastructure())
        manager.store._tables.clear()
        with self.assertRaises(PackageRejected):
            manager.create_release()
        # 没有占用锁，后续可正常发布
        manager2 = fresh_manager()
        drive_happy(manager2)


GATES_LABELS = {"dependency": "依赖检查", "replay": "样例回放",
                "adjudication": "差异裁定", "traffic": "切换流量"}


class ClockConventionTests(unittest.TestCase):
    def test_timezone_normalized_by_convention(self) -> None:
        shadow = normalize("2026-09-30T22:30:00+00:00", tz_source="UTC")
        self.assertEqual(shadow.business_day, "2026-10-01")
        self.assertIn("时区", shadow.notes)
        legacy = legacy_normalize("2026-09-30T22:30:00+00:00", tz_source="UTC")
        self.assertEqual(legacy.business_day, "2026-09-30")

    def test_business_day_cutoff(self) -> None:
        result = normalize("2026-10-01T05:20:00+08:00")
        self.assertEqual(result.business_day, "2026-09-30")
        self.assertEqual(legacy_normalize("2026-10-01T05:20:00+08:00").business_day, "2026-10-01")

    def test_device_clock_skew_beyond_tolerance(self) -> None:
        result = normalize("2026-09-30T23:56:00+08:00", device_offset_minutes=6)
        self.assertIn("参考钟", result.notes)
        self.assertEqual(result.business_day, "2026-09-30")
        legacy = legacy_normalize("2026-09-30T23:56:00+08:00", device_offset_minutes=6)
        self.assertEqual(legacy.business_day, "2026-10-01")

    def test_skew_within_tolerance_kept(self) -> None:
        result = normalize("2026-09-30T10:00:00+08:00", device_offset_minutes=3)
        self.assertIn("容忍范围", result.notes)
        self.assertTrue(result.canonical_ts.startswith("2026-09-30T10:00"))


class ShadowPreviewTests(unittest.TestCase):
    def test_preview_is_readonly(self) -> None:
        manager = fresh_manager()
        before = manager.status()["counts"]
        preview = manager.preview_shadow()
        after = manager.status()["counts"]
        self.assertEqual(before, after)
        self.assertFalse(manager.status()["orchestration_lock"]["locked"])
        self.assertEqual(preview["convention"]["business_day_cutoff"], "06:00")
        self.assertIn("report", preview)


class SampleHistoryTests(unittest.TestCase):
    def test_old_samples_keep_previous_batch(self) -> None:
        store = Store()
        registry: dict[str, str] = {}
        runner1 = ShadowRunner(store, batch_id="BATCH-A", sample_registry=registry)
        runner1.replay()
        store_id = next(
            event.event_id for event in build_pool(store)
            if event.origin == "store" and not event.live_only
        )
        self.assertEqual(registry[store_id], "BATCH-A")
        # 第二批回放：旧样例仍属 BATCH-A，只有新增样例登记 BATCH-B
        runner2 = ShadowRunner(store, batch_id="BATCH-B", sample_registry=registry)
        # BATCH-B 出现一个真正的新样例；其余都是旧样例
        from app.release.samples import RawEvent
        runner2.pool.append(RawEvent(
            event_id="EDGE-NEW-B7", kind="巡查", section="ROAD-0003",
            occurred_raw="2026-10-01T09:00:00+08:00", payload="第二批新接入巡查",
            origin="shadow",
        ))
        runner2.replay()
        self.assertEqual(runner2.shadow_snapshot[store_id]["batch"], "BATCH-A")
        # EDGE 样例在 BATCH-A 已首次灌入，历史记录保留当时批次
        self.assertEqual(runner2.shadow_snapshot["EDGE-TZ-01"]["batch"], "BATCH-A")
        self.assertEqual(runner2.shadow_snapshot["EDGE-NEW-B7"]["batch"], "BATCH-B")

    def test_resume_does_not_duplicate_ingest(self) -> None:
        store = Store()
        registry: dict[str, str] = {}
        runner = ShadowRunner(store, batch_id="BATCH-A", sample_registry=registry)
        replay = runner.replay()
        self.assertEqual(len(replay["event_ids"]), replay["ingested"])
        self.assertEqual(replay["resumed_from"], 0)
        # 再跑一次带完整检查点：不重复灌入
        again = runner.replay(checkpoint=replay["event_ids"])
        self.assertEqual(again["resumed_from"], replay["ingested"])
        self.assertEqual(again["ingested"], replay["ingested"])


if __name__ == "__main__":
    unittest.main()
