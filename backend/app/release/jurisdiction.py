"""辖区运营约定：时区、设备时钟偏差容忍、业务日基准与冲突样例裁决规则。

当时区、设备时钟和业务日基准互不相同时，一律以辖区运营约定为准（而不是以
设备本地时钟或服务器时区为准）。每个辖区的约定带版本号，规则一旦变更会升版本，
旧批次样例仍按当时批次的约定复核，维持此前批次结论不变。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# 冲突样例类型（与影子验收逐项对照输出保持一致）
CONFLICT_BUSINESS_DAY = "business_day"   # 业务日不一致
CONFLICT_CLOCK_SKEW = "clock_skew"       # 设备时钟与辖区基准偏差过大
CONFLICT_SEVERITY = "severity"           # 病害严重程度不一致
CONFLICT_STATUS = "status"               # 业务状态不一致


@dataclass(frozen=True)
class OperatingConvention:
    """单个辖区的运营约定。

    tz_offset_hours：辖区标准时区相对 UTC 的偏移，用固定偏移避免依赖系统时区库。
    business_day_cutoff_hour：业务日切换点（小时，0-23），例如 8 表示凌晨 08:00
        之前发生的事件归入前一业务日（夜班巡查口径）。
    clock_skew_tolerance_seconds：设备时钟相对辖区基准时钟的最大允许偏差。
    rules：四类冲突样例的裁决规则；缺规则的冲突保留 unresolved，必须人工介入，
        系统不自动放行。
    """

    jurisdiction_code: str
    jurisdiction_name: str
    tz_offset_hours: int
    business_day_cutoff_hour: int
    clock_skew_tolerance_seconds: int
    rules: dict[str, str]
    convention_version: str = "v1"

    def tzinfo(self) -> timezone:
        return timezone(timedelta(hours=self.tz_offset_hours))

    def business_day(self, occurred_at: datetime) -> str:
        """按辖区业务日基准把事件时刻折算为业务日（YYYY-MM-DD）。"""
        local = occurred_at.astimezone(self.tzinfo())
        if local.hour < self.business_day_cutoff_hour:
            local = local - timedelta(days=1)
        return local.date().isoformat()

    def within_clock_tolerance(self, skew_seconds: float) -> bool:
        return abs(skew_seconds) <= self.clock_skew_tolerance_seconds

    def adjudicate(self, conflict: str, production: dict[str, Any], shadow: dict[str, Any]) -> dict[str, Any]:
        """冲突样例以辖区运营约定裁决。

        返回 decision=accept 表示以影子侧结论为准；reject 表示维持现网样例；
        辖区没有对应约定时返回 unresolved，闸门不得放行。
        """
        rule = self.rules.get(conflict)
        if rule is None:
            return {
                "conflict": conflict,
                "decision": "unresolved",
                "rule_id": None,
                "reason": "辖区运营约定未覆盖该类冲突，需人工裁决",
            }
        if conflict == CONFLICT_BUSINESS_DAY:
            # 以辖区业务日基准重算，不采信设备本地日期
            return {
                "conflict": conflict,
                "decision": "accept",
                "rule_id": rule,
                "winner": "shadow",
                "reason": f"按辖区{self.business_day_cutoff_hour:02d}:00业务日切换点重算，以影子侧业务日 {shadow.get('business_day')} 为准",
            }
        if conflict == CONFLICT_CLOCK_SKEW:
            # 设备时钟超差时以辖区基准时钟校正
            return {
                "conflict": conflict,
                "decision": "accept",
                "rule_id": rule,
                "winner": "shadow",
                "reason": "设备时钟超出辖区偏差容忍，按辖区基准时钟校正后采信影子事件",
            }
        if conflict == CONFLICT_SEVERITY:
            rank = {"轻微": 1, "中等": 2, "严重": 3}
            prod_rank = rank.get(str(production.get("severity") or ""), 0)
            shadow_rank = rank.get(str(shadow.get("severity") or ""), 0)
            if shadow_rank >= prod_rank:
                return {
                    "conflict": conflict,
                    "decision": "accept",
                    "rule_id": rule,
                    "winner": "shadow",
                    "reason": "影子样例病害严重程度不低于现网，按就高口径以影子侧为准",
                }
            return {
                "conflict": conflict,
                "decision": "reject",
                "rule_id": rule,
                "winner": "production",
                "reason": "现网样例病害严重程度更高，按就高口径维持现网结论",
            }
        if conflict == CONFLICT_STATUS:
            # 以现场最新处置状态（影子侧）为准
            return {
                "conflict": conflict,
                "decision": "accept",
                "rule_id": rule,
                "winner": "shadow",
                "reason": "以影子环境重放得到的最新业务状态为准",
            }
        return {
            "conflict": conflict,
            "decision": "unresolved",
            "rule_id": None,
            "reason": "未知冲突类型，辖区运营约定无法裁决",
        }


# 城东辖区：四类冲突均有运营约定，发布可自动裁决
JURISDICTION_CITY_EAST = OperatingConvention(
    jurisdiction_code="J-EAST",
    jurisdiction_name="城东管养辖区",
    tz_offset_hours=8,
    business_day_cutoff_hour=8,
    clock_skew_tolerance_seconds=300,
    rules={
        CONFLICT_BUSINESS_DAY: "J-EAST-R01",
        CONFLICT_CLOCK_SKEW: "J-EAST-R02",
        CONFLICT_SEVERITY: "J-EAST-R03",
        CONFLICT_STATUS: "J-EAST-R04",
    },
)

# 远郊辖区：约定不全，未覆盖的冲突必须人工介入（用于演示拦截）
JURISDICTION_REMOTE = OperatingConvention(
    jurisdiction_code="J-REMOTE",
    jurisdiction_name="远郊管养辖区",
    tz_offset_hours=8,
    business_day_cutoff_hour=0,
    clock_skew_tolerance_seconds=120,
    rules={
        CONFLICT_BUSINESS_DAY: "J-REMOTE-R01",
    },
)

CONVENTIONS: dict[str, OperatingConvention] = {
    JURISDICTION_CITY_EAST.jurisdiction_code: JURISDICTION_CITY_EAST,
    JURISDICTION_REMOTE.jurisdiction_code: JURISDICTION_REMOTE,
}

DEFAULT_JURISDICTION = JURISDICTION_CITY_EAST.jurisdiction_code


def get_convention(jurisdiction_code: str | None) -> OperatingConvention:
    """按辖区编码取约定；未登记的辖区回落到城东默认约定。"""
    return CONVENTIONS.get(jurisdiction_code or "", JURISDICTION_CITY_EAST)
