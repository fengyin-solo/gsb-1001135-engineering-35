"""发布所依赖的基础设施：消息队列、地图服务、缓存。

任何一个不可达，都禁止转换流量；依赖检查闸门与切换流量闸门都会复查。
当前为内存模拟实现，通过 set_reachable 注入故障，便于演练与自动化测试。
"""
from __future__ import annotations

from threading import RLock


INFRA_ITEMS = ("queue", "map", "cache")
INFRA_LABELS = {"queue": "消息队列", "map": "地图服务", "cache": "缓存"}


class Infrastructure:
    def __init__(self) -> None:
        self._lock = RLock()
        self._reachable = {name: True for name in INFRA_ITEMS}

    def is_reachable(self, name: str) -> bool:
        with self._lock:
            return self._reachable.get(name, False)

    def status(self) -> list[dict[str, object]]:
        with self._lock:
            return [
                {"name": name, "label": INFRA_LABELS[name], "reachable": self._reachable[name]}
                for name in INFRA_ITEMS
            ]

    def set_reachable(self, name: str, reachable: bool) -> dict[str, object]:
        if name not in INFRA_ITEMS:
            raise KeyError(f"未知基础设施：{name}")
        with self._lock:
            self._reachable[name] = reachable
            return {"name": name, "label": INFRA_LABELS[name], "reachable": reachable}

    def all_reachable(self) -> bool:
        with self._lock:
            return all(self._reachable.values())

    def unreachable_names(self) -> list[str]:
        with self._lock:
            return [INFRA_LABELS[name] for name in INFRA_ITEMS if not self._reachable[name]]
