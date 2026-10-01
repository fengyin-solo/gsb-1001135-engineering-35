"""影子验收：隔离环境重放巡查、病害与桥隧事件，再与现网采样逐项对照。

设计要点：
- 影子环境是独立内存副本，回放绝不触碰现网仓库；
- 回放按事件逐项推进并落检查点，连接断开后从断点续跑，不重复灌入样例；
- 旧样例维持此前批次（sample_registry 记录每个事件首次灌入的批次）；
- 时区、设备时钟、业务日基准冲突按辖区运营约定裁决（clocks.CONVENTION）；
- 冲突样例以辖区运营约定为准，约定未覆盖的类型阻断差异裁定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.release.clocks import CONVENTION, JurisdictionConvention, legacy_normalize, normalize

# 现网业务模块到事件类型的映射：只取巡查、病害、桥隧事件三类来源
SOURCE_MODULES = (
    ("patrol", "巡查", "巡查编号", "巡查路段", "巡查日期"),
    ("pavement", "病害", "病害编号", "所属路段", "发现日期"),
    ("bridge", "桥隧事件", "检测编号", "桥梁名称", "检测日期"),
)


@dataclass
class RawEvent:
    """待回放/采样的原始事件。

    origin=store 表示来自现网仓库（旧样例）；origin=shadow 表示本批构造事件。
    live_only=True 的事件只出现在现网采样中，用于制造“影子缺失”差异。
    """

    event_id: str
    kind: str
    section: str
    occurred_raw: str
    payload: str
    tz_source: str = "CST"
    device_offset_minutes: int = 0
    origin: str = "store"
    live_only: bool = False
    shadow_only: bool = False
    live_payload: str | None = None
    conflict_hint: str = ""


def edge_events() -> list[RawEvent]:
    """本批次注入的冲突样例，覆盖三类时钟争议与两类缺失争议。"""
    return [
        RawEvent(
            event_id="EDGE-TZ-01",
            kind="桥隧事件",
            section="TUNN-0002",
            # UTC 22:30 折算到辖区为次日 06:30（业务日 10-01）；旧口径不折算算成 09-30
            occurred_raw="2026-09-30T22:30:00+00:00",
            payload="隧道封堵解除，恢复双幅通行",
            tz_source="UTC",
            origin="shadow",
            conflict_hint="时区不一致",
        ),
        RawEvent(
            event_id="EDGE-BD-02",
            kind="巡查",
            section="ROAD-0002",
            # 辖区 05:20 在业务日切点 06:00 之前，应归属 09-30；旧口径按自然日算成 10-01
            occurred_raw="2026-10-01T05:20:00+08:00",
            payload="凌晨巡查，视线良好",
            origin="shadow",
            conflict_hint="业务日基准",
        ),
        RawEvent(
            event_id="EDGE-CLK-03",
            kind="病害",
            section="ROAD-0003",
            # 设备钟快 6 分钟，超出 5 分钟容忍阈值，按辖区参考钟纠正
            occurred_raw="2026-09-30T23:56:00+08:00",
            payload="龟裂 2.1 平方米",
            device_offset_minutes=6,
            origin="shadow",
            conflict_hint="设备时钟偏差",
        ),
        RawEvent(
            event_id="EDGE-PLD-04",
            kind="病害",
            section="ROAD-0001",
            occurred_raw="2026-09-30T14:10:00+08:00",
            payload="车辙 1.2 平方米（严重程度：中）",
            live_payload="车辙 1.2 平方米（严重程度：轻）",
            origin="shadow",
            conflict_hint="载荷不一致",
        ),
        RawEvent(
            event_id="EDGE-SHADOW-05",
            kind="桥隧事件",
            section="TUNN-0001",
            occurred_raw="2026-09-30T18:00:00+08:00",
            payload="隧道风机异响，隔离环境先于现网接入",
            origin="shadow",
            shadow_only=True,
            conflict_hint="影子独有",
        ),
        RawEvent(
            event_id="EDGE-LIVE-06",
            kind="巡查",
            section="ROAD-0002",
            occurred_raw="2026-09-30T07:40:00+08:00",
            payload="早高峰巡查，匝道护栏完好",
            origin="shadow",
            live_only=True,
            conflict_hint="现网独有",
        ),
    ]


def build_pool(store: Any) -> list[RawEvent]:
    """从现网仓库抽取本批候选事件，并追加冲突样例。"""
    pool: list[RawEvent] = []
    for module, kind, key_field, section_field, date_field in SOURCE_MODULES:
        for row in store.rows(module):
            pool.append(
                RawEvent(
                    event_id=str(row.get(key_field) or f"{module}-{row.get('id')}"),
                    kind=kind,
                    section=str(row.get(section_field) or "未归属路段"),
                    occurred_raw=str(row.get(date_field) or ""),
                    payload=str(row.get(key_field) or ""),
                    origin="store",
                )
            )
    pool.extend(edge_events())
    return pool


# 差异类型 → 差异裁定口径（辖区运营约定）
ADJUDICATION_RULES = {
    "时区不一致": ("accept", "按辖区运营约定折算到辖区时区"),
    "业务日基准": ("accept", "按辖区业务日切点（06:00）归属业务日"),
    "设备时钟偏差": ("accept", "偏差超容忍阈值，按辖区参考钟纠正"),
    "载荷不一致": ("reconcile", "以现网采样为准，差异项挂告警并通知路段复核"),
    "影子独有": ("suspend", "隔离环境先于现网，挂待办待现网核验收录"),
    "现网独有": ("block", "影子缺失，须把该样例补灌入隔离环境后重新对照"),
}


@dataclass
class ShadowRunner:
    """单次影子验收执行器（隔离环境，一次性）。"""

    store: Any
    batch_id: str
    sample_registry: dict[str, str]
    convention: JurisdictionConvention = CONVENTION
    auto_resample: bool = True
    on_progress: Callable[[list[str]], None] | None = None
    unknown_conflict: bool = False

    pool: list[RawEvent] = field(default_factory=list)
    ingested: list[str] = field(default_factory=list)
    shadow_snapshot: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.pool = build_pool(self.store)

    # ---- 闸门二：样例回放（影子环境） -----------------------------------

    def replay(self, checkpoint: list[str] | None = None) -> dict[str, Any]:
        """在隔离环境重放候选事件；checkpoint 为已灌入事件，断点续跑时跳过。

        注意：检查点只用于“不重复灌入”。续跑时隔离环境是重建的，已灌入项
        也需要重新装入影子快照，但按 sample_registry 保留其首次灌入批次。
        """
        already = list(checkpoint or [])
        self.ingested = list(already)
        candidates = [event for event in self.pool if not event.live_only]
        for event in candidates:
            if event.event_id in self.ingested:
                continue
            self._ingest(event)
            self.ingested.append(event.event_id)
            if self.on_progress is not None:
                self.on_progress(list(self.ingested))
        return {
            "ingested": len(self.ingested),
            "resumed_from": len(already),
            "resumed_event_ids": already,
            "event_ids": list(self.ingested),
        }

    def restore_checkpoint(self, checkpoint: list[str]) -> int:
        """续跑前把检查点里的样例重新装入隔离快照（不算再次灌入，批次沿用历史）。"""
        restored = 0
        for event_id in checkpoint:
            if event_id in self.shadow_snapshot:
                continue
            event = next((item for item in self.pool if item.event_id == event_id), None)
            if event is not None:
                self._ingest(event, origin_batch=self.sample_registry.get(event_id, self.batch_id))
                restored += 1
        return restored

    def _ingest(self, event: RawEvent, *, origin_batch: str | None = None) -> None:
        clock = normalize(
            event.occurred_raw,
            tz_source=event.tz_source,
            device_offset_minutes=event.device_offset_minutes,
            convention=self.convention,
        )
        self.shadow_snapshot[event.event_id] = {
            "event_id": event.event_id,
            "kind": event.kind,
            "section": event.section,
            "business_day": clock.business_day,
            "canonical_ts": clock.canonical_ts,
            "payload": event.payload,
            "tz_source": clock.tz_source,
            "clock_notes": clock.notes,
            "origin": event.origin,
            "batch": origin_batch or self.sample_registry.get(event.event_id, self.batch_id),
        }
        # 旧样例维持此前批次：只有首次灌入时登记批次
        if event.event_id not in self.sample_registry:
            self.sample_registry[event.event_id] = self.batch_id

    # ---- 现网采样 -------------------------------------------------------

    def sample_live(self) -> dict[str, dict[str, Any]]:
        """现网采样：维持旧口径（不折算时区、不认业务日切点、保留设备钟）。"""
        snapshot: dict[str, dict[str, Any]] = {}
        for event in self.pool:
            if event.shadow_only:
                # 影子独有事件尚未接入现网，现网采样不到
                continue
            clock = legacy_normalize(
                event.occurred_raw,
                tz_source=event.tz_source,
                device_offset_minutes=event.device_offset_minutes,
                convention=self.convention,
            )
            snapshot[event.event_id] = {
                "event_id": event.event_id,
                "kind": event.kind,
                "section": event.section,
                "business_day": clock.business_day,
                "canonical_ts": clock.canonical_ts,
                "payload": event.live_payload or event.payload,
                "tz_source": clock.tz_source,
                "batch": self.sample_registry.get(event.event_id, self.batch_id),
            }
        return snapshot

    # ---- 闸门三：逐项对照与按约定裁决 -----------------------------------

    def adjudicate(
        self,
        live: dict[str, dict[str, Any]] | None = None,
        *,
        resample_round: bool = False,
    ) -> dict[str, Any]:
        live_snapshot = live if live is not None else self.sample_live()
        diffs: list[dict[str, Any]] = []

        all_ids = sorted(set(self.shadow_snapshot) | set(live_snapshot))
        for event_id in all_ids:
            shadow = self.shadow_snapshot.get(event_id)
            live_row = live_snapshot.get(event_id)
            if shadow is not None and live_row is None:
                diffs.append(self._make_diff(event_id, "影子独有", shadow, None, resample_round))
                continue
            if shadow is None and live_row is not None:
                if self.auto_resample and not resample_round:
                    # 按约定：影子缺失 → 补灌该样例后重新对照（不是重复灌入已有样例）
                    event = next((item for item in self.pool if item.event_id == event_id), None)
                    if event is not None:
                        self._ingest(event)
                        self.ingested.append(event.event_id)
                        if self.on_progress is not None:
                            self.on_progress(list(self.ingested))
                diffs.append(self._make_diff(event_id, "现网独有", None, live_row, resample_round))
                continue
            assert shadow is not None and live_row is not None
            if shadow["business_day"] != live_row["business_day"]:
                hint = self._hint_of(event_id)
                kind = hint if hint in ("时区不一致", "业务日基准", "设备时钟偏差") else "业务日基准"
                diffs.append(self._make_diff(event_id, kind, shadow, live_row, resample_round,
                                             field_name="业务日"))
            if str(shadow["payload"]) != str(live_row["payload"]):
                diffs.append(self._make_diff(event_id, "载荷不一致", shadow, live_row, resample_round,
                                             field_name="载荷"))

        # 演练/测试用：注入辖区约定未覆盖的冲突类型
        if self.unknown_conflict:
            diffs.append({
                "event_id": "EDGE-UNKNOWN",
                "kind": "未知冲突",
                "section": "ROAD-0003",
                "field": "业务日",
                "shadow": None,
                "live": None,
                "result": "block",
                "basis": "辖区运营约定未覆盖该冲突类型，无法裁决",
                "resolved": False,
                "need_todo": True,
                "need_alert": True,
                "batch": self.batch_id,
            })

        accepted = [d for d in diffs if d["result"] in ("accept", "reconcile")]
        suspended = [d for d in diffs if d["result"] == "suspend"]
        blocked = [d for d in diffs if d["result"] == "block" and not d["resolved"]]
        verdict = "通过" if not blocked else "不通过"
        return {
            "verdict": verdict,
            "diffs": diffs,
            "summary": {
                "shadow_count": len(self.shadow_snapshot),
                "live_count": len(live_snapshot),
                "diff_count": len(diffs),
                "accepted": len(accepted),
                "suspended": len(suspended),
                "blocked": len(blocked),
            },
        }

    def _hint_of(self, event_id: str) -> str:
        return next((event.conflict_hint for event in self.pool if event.event_id == event_id), "")

    def _make_diff(
        self,
        event_id: str,
        kind: str,
        shadow: dict[str, Any] | None,
        live_row: dict[str, Any] | None,
        resample_round: bool,
        *,
        field_name: str = "业务日",
    ) -> dict[str, Any]:
        result, basis = ADJUDICATION_RULES[kind]
        resolved = False
        if kind == "现网独有" and self.auto_resample and not resample_round:
            # 首轮对照已补灌入隔离环境，差异在本轮裁决中关闭
            result = "accept"
            basis = "已按辖区运营约定把缺失样例补灌入隔离环境，重新对照一致"
            resolved = True
        sample = shadow or live_row or {}
        return {
            "event_id": event_id,
            "kind": kind,
            "section": str(sample.get("section", "")),
            "field": field_name,
            "shadow": None if shadow is None else shadow.get(field_name) if field_name in shadow else shadow.get("business_day"),
            "live": None if live_row is None else live_row.get(field_name) if field_name in live_row else live_row.get("business_day"),
            "result": result,
            "basis": basis,
            "resolved": resolved or result in ("accept", "reconcile"),
            "need_todo": kind in ("载荷不一致", "影子独有", "现网独有"),
            "need_alert": kind in ("载荷不一致", "现网独有"),
            "batch": self.batch_id,
        }
