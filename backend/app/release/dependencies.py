"""外部依赖连通性登记：消息队列、地图服务、缓存。

任一项不可达时，依赖检查不通过，切换流量阶段也会再次复核——
队列、地图服务或缓存不可达时禁止转换流量。
测试时通过 fail(name) 注入故障，reset() 恢复。
"""
from __future__ import annotations

from dataclasses import dataclass

QUEUE = "queue"
MAP_SERVICE = "map_service"
CACHE = "cache"

DEPENDENCY_NAMES = [QUEUE, MAP_SERVICE, CACHE]
DEPENDENCY_LABELS = {
    QUEUE: "消息队列",
    MAP_SERVICE: "地图服务",
    CACHE: "缓存",
}


@dataclass
class DependencyStatus:
    name: str
    label: str
    reachable: bool

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "label": self.label, "reachable": self.reachable}


class DependencyRegistry:
    def __init__(self) -> None:
        self._forced_down: set[str] = set()

    def fail(self, name: str) -> None:
        if name not in DEPENDENCY_NAMES:
            raise ValueError(f"未知依赖：{name}")
        self._forced_down.add(name)

    def recover(self, name: str) -> None:
        self._forced_down.discard(name)

    def reset(self) -> None:
        self._forced_down.clear()

    def statuses(self) -> list[DependencyStatus]:
        return [
            DependencyStatus(name=name, label=DEPENDENCY_LABELS[name], reachable=name not in self._forced_down)
            for name in DEPENDENCY_NAMES
        ]

    def all_reachable(self) -> bool:
        return not self._forced_down
