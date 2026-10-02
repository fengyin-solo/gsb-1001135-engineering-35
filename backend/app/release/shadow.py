"""影子验收：隔离环境重放巡查、病害、桥隧事件，与现网采样逐项对照。

隔离环境只拿现网采样的副本做对照，任何写入都不触碰现网；时间口径不直接采信
设备时钟，统一按辖区运营约定折算业务日。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.release.jurisdiction import (
    CONFLICT_BUSINESS_DAY,
    CONFLICT_CLOCK_SKEW,
    CONFLICT_SEVERITY,
    CONFLICT_STATUS,
    OperatingConvention,
)
from app.seed import SEED_ROWS

# 三类事件到现网表的字段映射（现网采样来源）；桥隧事件同时覆盖桥梁、隧道两张表
KIND_PATROL = "patrol"
KIND_DISEASE = "disease"
KIND_BRIDGE_TUNNEL = "bridge_tunnel"

FIELD_MAP: dict[str, tuple[str, str, str | None]] = {
    # 事件类型: (编号列, 业务状态列, 日期列)
    KIND_PATROL: ("巡查编号", "status", "巡查日期"),
    KIND_DISEASE: ("病害编号", "status", "发现日期"),
}

BRIDGE_TUNNEL_SOURCES: list[tuple[str, str, str | None, str | None]] = [
    # (现网表, 编号列, 业务状态列, 日期列)
    ("bridge", "检测编号", "status", "检测日期"),
    ("tunnel", "隧道编号", "status", None),
]

REQUIRED_KINDS = [KIND_PATROL, KIND_DISEASE, KIND_BRIDGE_TUNNEL]


def _iso_date(raw: Any) -> str | None:
    """现网日期只接受 ISO 日期；样例占位内容视为不可解析（None），不参与业务日比对。"""
    text = str(raw or "")
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return None


def parse_event_time(raw: Any) -> datetime:
    """解析事件时刻；无时区信息时按 UTC 处理，后续一律折算到辖区时区。"""
    if isinstance(raw, datetime):
        dt = raw
    else:
        dt = datetime.fromisoformat(str(raw))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ShadowEngine:
    def __init__(self, convention: OperatingConvention) -> None:
        self._convention = convention

    # ---- 现网采样（只读副本） ---------------------------------------------
    def production_samples(self) -> dict[str, dict[str, Any]]:
        """从现网业务表抽取巡查/病害/桥隧采样，按事件类型:编号 建索引。"""
        samples: dict[str, dict[str, Any]] = {}
        for kind, (code_col, status_col, date_col) in FIELD_MAP.items():
            table = {"patrol": "patrol", "disease": "pavement"}[kind]
            for row in SEED_ROWS.get(table, []):
                code = str(row.get(code_col) or "")
                raw_date = str(row.get(date_col) or "") if date_col else ""
                # 现网记录只有日期，视为当日辖区零点（设备/服务器时区不一致时也不采信）
                samples[f"{kind}:{code}"] = {
                    "kind": kind,
                    "code": code,
                    "status": row.get(status_col),
                    "severity": self._production_severity(kind, row),
                    "business_day": _iso_date(raw_date),
                    "raw_date": raw_date,
                    "road_ref": self._production_road(kind, row),
                    "observed_at": None,
                }
        for table, code_col, status_col, date_col in BRIDGE_TUNNEL_SOURCES:
            for row in SEED_ROWS.get(table, []):
                code = str(row.get(code_col) or "")
                raw_date = str(row.get(date_col) or "") if date_col else ""
                samples[f"{KIND_BRIDGE_TUNNEL}:{code}"] = {
                    "kind": KIND_BRIDGE_TUNNEL,
                    "code": code,
                    "status": row.get(status_col) if status_col else None,
                    "severity": None,
                    "business_day": _iso_date(raw_date),
                    "raw_date": raw_date,
                    "road_ref": str(row.get("桥梁名称") or row.get("隧道名称") or ""),
                    "observed_at": None,
                }
        return samples

    @staticmethod
    def _production_severity(kind: str, row: dict[str, Any]) -> str | None:
        if kind != KIND_DISEASE:
            return None
        value = str(row.get("严重程度") or "")
        return value if value in ("轻微", "中等", "严重") else None

    @staticmethod
    def _production_road(kind: str, row: dict[str, Any]) -> str:
        if kind == KIND_PATROL:
            return str(row.get("巡查路段") or "")
        if kind == KIND_DISEASE:
            return str(row.get("所属路段") or "")
        return ""

    # ---- 隔离重放 + 逐项对照 ----------------------------------------------
    def replay_and_compare(
        self, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        samples = self.production_samples()
        comparisons: list[dict[str, Any]] = []
        for event in events:
            normalized = self._normalize_event(event)
            key = f"{normalized['kind']}:{normalized['code']}"
            production = samples.get(key)
            comparisons.append(self._compare_one(normalized, production))
        return comparisons

    def _normalize_event(self, event: dict[str, Any]) -> dict[str, Any]:
        occurred_at = parse_event_time(event["occurred_at"])
        device_offset_minutes = int(event.get("device_offset_minutes") or 0)
        skew_seconds = device_offset_minutes * 60
        return {
            "kind": str(event["kind"]),
            "code": str(event["code"]),
            "road_ref": str(event.get("road_ref") or ""),
            "occurred_at": occurred_at.isoformat(),
            "business_day": self._convention.business_day(occurred_at),
            "status": event.get("status"),
            "severity": event.get("severity"),
            "skew_seconds": skew_seconds,
            "clock_within_tolerance": self._convention.within_clock_tolerance(skew_seconds),
        }

    def _compare_one(
        self, shadow: dict[str, Any], production: dict[str, Any] | None
    ) -> dict[str, Any]:
        if production is None:
            result = {
                "kind": shadow["kind"],
                "code": shadow["code"],
                "road_ref": shadow["road_ref"],
                "verdict": "new",
                "conflicts": [],
                "shadow": shadow,
                "production": None,
            }
            return result

        conflicts: list[str] = []
        # 业务日基准不一致（时区/业务日切换点差异以辖区约定为准）
        prod_day = production.get("business_day")
        if prod_day and prod_day != shadow["business_day"]:
            conflicts.append(CONFLICT_BUSINESS_DAY)
        # 设备时钟超差
        if not shadow["clock_within_tolerance"]:
            conflicts.append(CONFLICT_CLOCK_SKEW)
        # 病害严重程度冲突
        if (
            shadow["kind"] == KIND_DISEASE
            and shadow.get("severity")
            and production.get("severity")
            and shadow["severity"] != production["severity"]
        ):
            conflicts.append(CONFLICT_SEVERITY)
        # 业务状态冲突
        if shadow.get("status") and production.get("status") and shadow["status"] != production["status"]:
            conflicts.append(CONFLICT_STATUS)

        if conflicts:
            verdict = "conflict"
        elif shadow["status"] != production.get("status") or shadow["business_day"] != prod_day:
            verdict = "diff"
        else:
            verdict = "match"
        return {
            "kind": shadow["kind"],
            "code": shadow["code"],
            "road_ref": shadow["road_ref"],
            "verdict": verdict,
            "conflicts": conflicts,
            "shadow": shadow,
            "production": production,
        }

    # ---- 影子验收报告（闸门结论用） ---------------------------------------
    def acceptance_report(
        self, events: list[dict[str, Any]], comparisons: list[dict[str, Any]]
    ) -> dict[str, Any]:
        counts = {"match": 0, "diff": 0, "conflict": 0, "new": 0}
        covered_kinds: set[str] = set()
        for item in comparisons:
            counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
            covered_kinds.add(item["kind"])

        blocked_reasons: list[str] = []
        missing_kinds = [kind for kind in REQUIRED_KINDS if kind not in covered_kinds]
        if missing_kinds:
            blocked_reasons.append(
                "隔离环境未覆盖全部要求的事件类型，缺少：" + "、".join(missing_kinds)
            )
        if not events:
            blocked_reasons.append("隔离环境未重放任何巡查、病害或桥隧事件")
        # 注意：冲突样例不拦截影子验收——影子验收只负责隔离重放与逐项对照，
        # 冲突留给下一闸门「差异裁定」按辖区运营约定裁决。

        return {
            "jurisdiction_code": self._convention.jurisdiction_code,
            "convention_version": self._convention.convention_version,
            "replayed": len(events),
            "counts": counts,
            "missing_kinds": missing_kinds,
            "comparisons": comparisons,
            "needs_adjudication": counts["conflict"] > 0,
            "shadow_passed": not blocked_reasons,
            "blocked_reasons": blocked_reasons,
        }
