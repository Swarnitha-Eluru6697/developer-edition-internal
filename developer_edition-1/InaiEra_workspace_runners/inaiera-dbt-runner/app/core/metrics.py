"""Minimal in-process counters (no Prometheus client in the image).

Surfaced in /readyz's body; values are per process. Durable, cluster-wide
signals (e.g. the outbox dead-letter count) are read from the database.
"""
from __future__ import annotations

import threading
from collections import Counter

_lock = threading.Lock()
_counters: Counter = Counter()


def _key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    return name + "{" + ",".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}"


def inc(name: str, value: int = 1, **labels: str) -> None:
    with _lock:
        _counters[_key(name, labels)] += value


def get(name: str, **labels: str) -> int:
    with _lock:
        return _counters[_key(name, labels)]


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counters)


def reset() -> None:
    with _lock:
        _counters.clear()
