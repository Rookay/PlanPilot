"""地图结果的小型进程内 TTL 缓存。"""

import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, Optional


@dataclass
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    def __init__(self, max_size: int = 256):
        self.max_size = max_size
        self._values: Dict[str, _Entry] = {}
        self._lock = Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return None
            if entry.expires_at <= time.monotonic():
                self._values.pop(key, None)
                return None
            return entry.value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        with self._lock:
            if len(self._values) >= self.max_size:
                oldest = min(self._values, key=lambda item: self._values[item].expires_at)
                self._values.pop(oldest, None)
            self._values[key] = _Entry(
                value=value,
                expires_at=time.monotonic() + ttl_seconds,
            )
