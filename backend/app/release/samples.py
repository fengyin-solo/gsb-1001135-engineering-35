"""默认影子重放样例：覆盖巡查、病害、桥隧三类事件。

样例时刻使用带时区的 ISO 时间；device_offset_minutes 表示采集设备时钟相对
辖区基准时钟的偏差（用于验证时钟超差裁决）。造数时刻意保留少量冲突样例，
用来演示差异裁定阶段按辖区运营约定逐条裁决。
"""
from __future__ import annotations

from typing import Any

# 时区均为城东辖区 UTC+8，业务日切换点 08:00
DEFAULT_EVENTS: list[dict[str, Any]] = [
    # 巡查：与现网 PATR-0001 完全一致（match）
    {
        "kind": "patrol",
        "code": "PATR-0001",
        "road_ref": "日常巡查样例1",
        "occurred_at": "2026-09-01T09:30:00+08:00",
        "device_offset_minutes": 0,
        "status": "待巡查",
    },
    # 巡查：事件实际发生在 09-03 上午 09:20（业务日 09-03），设备沿用跨时区
    # 旧日期把现网记录写成了 09-02，按辖区 08:00 业务日切换点重算后业务日冲突
    # （J-EAST-R01 裁决，采信影子侧业务日）
    {
        "kind": "patrol",
        "code": "PATR-0002",
        "road_ref": "日常巡查样例2",
        "occurred_at": "2026-09-03T09:20:00+08:00",
        "device_offset_minutes": 0,
        "status": "巡查中",
    },
    # 巡查：现网没有的新样例（new）
    {
        "kind": "patrol",
        "code": "PATR-0008",
        "road_ref": "城东快速路北段",
        "occurred_at": "2026-09-03T10:05:00+08:00",
        "device_offset_minutes": 0,
        "status": "待巡查",
    },
    # 病害：设备时钟偏差 12 分钟（超 300s 容忍，J-EAST-R02 裁决），且影子重放
    # 显示已修复、现网仍停在修复中，状态冲突（J-EAST-R04 裁决）
    {
        "kind": "disease",
        "code": "PAVE-0002",
        "road_ref": "路面病害样例2",
        "occurred_at": "2026-09-02T14:00:00+08:00",
        "device_offset_minutes": 12,
        "status": "已修复",
        "severity": "中等",
    },
    # 病害：与现网 PAVE-0001 一致（match）
    {
        "kind": "disease",
        "code": "PAVE-0001",
        "road_ref": "路面病害样例1",
        "occurred_at": "2026-09-01T11:00:00+08:00",
        "device_offset_minutes": 0,
        "status": "待修复",
        "severity": "轻微",
    },
    # 桥梁：与现网 BRID-0001 一致（match）
    {
        "kind": "bridge_tunnel",
        "code": "BRID-0001",
        "road_ref": "桥梁定检样例1",
        "occurred_at": "2026-09-01T09:00:00+08:00",
        "device_offset_minutes": 0,
        "status": "待检测",
    },
    # 隧道：状态与现网 TUNN-0001 一致，但现网缺日期记录，业务日以影子侧补齐（diff）
    {
        "kind": "bridge_tunnel",
        "code": "TUNN-0001",
        "road_ref": "隧道管养样例1",
        "occurred_at": "2026-09-01T09:00:00+08:00",
        "device_offset_minutes": 0,
        "status": "正常",
    },
]
