"""辖区运营约定与时钟口径。

时区、设备时钟、业务日基准三者不一致时，以辖区运营约定为准（本模块的
JurisdictionConvention）。影子环境按约定归一化；现网旧样例维持旧批次当时的口径，
只在对照阶段暴露差异，由差异裁定环节按约定裁决，历史记录不改写。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

try:  # 运行环境自带时区库时优先使用 IANA 名称，缺数据时退化为固定 +8 偏移
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - 老版本 Python 无 zoneinfo
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):
        """无 zoneinfo 模块时的占位异常。"""


FALLBACK_TZ = timezone(timedelta(hours=8))
# 样例里出现的时区缩写到 UTC 偏移的映射，避免引入额外时区数据库依赖
TZ_OFFSETS = {
    "UTC": 0,
    "CST": 8,
    "JST": 9,
    "+08:00": 8,
    "+09:00": 9,
    "+00:00": 0,
}


@dataclass(frozen=True)
class JurisdictionConvention:
    """辖区运营约定：所有时钟争议的唯一裁决依据。"""

    name: str = "辖区运营约定"
    timezone_name: str = "Asia/Shanghai"
    tz_offset_hours: int = 8
    business_day_cutoff_hour: int = 6
    """业务日切点：00:00 至切点前的事件计入前一业务日。"""
    device_clock_tolerance: timedelta = timedelta(minutes=5)
    """设备时钟偏差容忍阈值，超出按约定参考钟纠正。"""

    def tzinfo(self) -> Any:
        if ZoneInfo is not None:
            try:
                return ZoneInfo(self.timezone_name)
            except Exception:  # pragma: no cover - 时区数据缺失时退化
                pass
        return timezone(timedelta(hours=self.tz_offset_hours))


    @property
    def tz_label(self) -> str:
        return f"UTC+{self.tz_offset_hours}"


def _parse_offset_suffix(text: str) -> tuple[datetime, str] | None:
    """识别形如 2026-09-30T02:30:00+00:00 的显式偏移时间。"""
    text = text.strip()
    for suffix in ("+00:00", "+08:00", "+09:00", "Z"):
        if text.endswith(suffix):
            raw = text[: -len(suffix)] if suffix != "Z" else text[:-1]
            try:
                moment = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return None
            offset = 0 if suffix in ("+00:00", "Z") else int(suffix[1:3])
            return moment, ("UTC" if offset == 0 else f"+{offset:02d}:00")
    return None


@dataclass(frozen=True)
class ClockResult:
    """时钟归一化结论。"""

    business_day: str
    canonical_ts: str
    tz_source: str
    clock_skew_seconds: int
    notes: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "business_day": self.business_day,
            "canonical_ts": self.canonical_ts,
            "tz_source": self.tz_source,
            "clock_skew_seconds": self.clock_skew_seconds,
            "notes": self.notes,
        }


def _resolve_business_day(moment: datetime, convention: JurisdictionConvention) -> date:
    if moment.timetz() < time(hour=convention.business_day_cutoff_hour):
        return moment.date() - timedelta(days=1)
    return moment.date()


def normalize(
    value: str,
    *,
    tz_source: str = "CST",
    device_offset_minutes: int = 0,
    convention: JurisdictionConvention | None = None,
    reference_now: datetime | None = None,
) -> ClockResult:
    """按辖区约定把原始时钟归一化到辖区时区与业务日。

    value 支持日期（YYYY-MM-DD）与带时间戳两种形态；device_offset_minutes 表示
    设备钟相对真实时间的偏移（正为设备钟快）。
    """
    convention = convention or JurisdictionConvention()
    notes: list[str] = []
    text = str(value).strip()

    offset_pair = _parse_offset_suffix(text)
    if offset_pair is not None:
        moment, parsed_tz = offset_pair
        tz_source = parsed_tz
        moment = moment.replace(tzinfo=timezone(timedelta(hours=TZ_OFFSETS[parsed_tz])))
    elif "T" in text:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
        moment = moment.replace(tzinfo=timezone(timedelta(hours=TZ_OFFSETS.get(tz_source, convention.tz_offset_hours))))
    else:
        parsed_date = datetime.strptime(text, "%Y-%m-%d").date()
        return ClockResult(
            business_day=parsed_date.isoformat(),
            canonical_ts=f"{text}T00:00:00+08:00",
            tz_source="日期样例",
            clock_skew_seconds=0,
        )

    # 设备时钟偏差：超出容忍阈值则按约定参考钟纠正
    skew_seconds = device_offset_minutes * 60
    if device_offset_minutes:
        if abs(timedelta(minutes=device_offset_minutes)) > convention.device_clock_tolerance:
            moment = moment - timedelta(minutes=device_offset_minutes)
            notes.append(f"设备时钟偏差{device_offset_minutes}分钟超阈值，已按{convention.name}参考钟纠正")
        else:
            notes.append(f"设备时钟偏差{device_offset_minutes}分钟在容忍范围内，保持设备读数")

    # 时区不一致：统一折算到辖区时区，以辖区约定为准
    local = moment.astimezone(convention.tzinfo())
    tz_target = convention.timezone_name
    if tz_source not in (tz_target, f"+{convention.tz_offset_hours:02d}:00"):
        notes.append(f"时区来源{tz_source}与辖区{tz_target}不一致，已按{convention.name}折算")

    business_day = _resolve_business_day(local.replace(tzinfo=None), convention)
    return ClockResult(
        business_day=business_day.isoformat(),
        canonical_ts=local.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S") + "+08:00",
        tz_source=tz_source,
        clock_skew_seconds=skew_seconds,
        notes="；".join(notes),
    )


def legacy_normalize(
    value: str,
    *,
    tz_source: str = "CST",
    device_offset_minutes: int = 0,
    convention: JurisdictionConvention | None = None,
) -> ClockResult:
    """现网旧口径：不做时区折算、不认业务日切点、保留设备钟读数。

    旧样例维持此前批次的口径，这里只用于影子验收时还原现网采样的业务日，
    供逐项对照暴露差异。
    """
    convention = convention or JurisdictionConvention()
    text = str(value).strip()
    offset_pair = _parse_offset_suffix(text)
    if offset_pair is not None:
        moment, parsed_tz = offset_pair
        tz_source = parsed_tz
        # 旧口径：直接把时间戳当地钟读，不折算时区
        moment = moment.replace(tzinfo=None)
    elif "T" in text:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    else:
        parsed_date = datetime.strptime(text, "%Y-%m-%d").date()
        return ClockResult(
            business_day=parsed_date.isoformat(),
            canonical_ts=f"{text}T00:00:00",
            tz_source="日期样例",
            clock_skew_seconds=0,
        )

    # 旧口径保留设备钟快/慢读数，不做纠正
    moment = moment + timedelta(minutes=device_offset_minutes)
    return ClockResult(
        business_day=moment.date().isoformat(),
        canonical_ts=moment.strftime("%Y-%m-%dT%H:%M:%S"),
        tz_source=tz_source,
        clock_skew_seconds=device_offset_minutes * 60,
    )


CONVENTION = JurisdictionConvention()
