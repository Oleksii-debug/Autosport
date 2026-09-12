from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Iterable

from .domain import MarketEvent


class FutureLeakageError(RuntimeError):
    pass


class ReplayLeakageFirewall:
    """Results remain physically inaccessible to strategy code until replay completion."""

    def __init__(self, final_results: dict[str, str] | None = None) -> None:
        self._results = dict(final_results or {})
        self._unlocked = False

    def result_for(self, event_id: str) -> str | None:
        if not self._unlocked:
            raise FutureLeakageError("Final result is sealed until replay completion")
        return self._results.get(event_id)

    def unlock(self) -> None:
        self._unlocked = True


class ReplayEngine:
    def __init__(self, events: Iterable[MarketEvent], firewall: ReplayLeakageFirewall | None = None) -> None:
        self.events = sorted(events, key=lambda e: (e.observed_ts, e.sequence))
        self.firewall = firewall or ReplayLeakageFirewall()

    @classmethod
    def from_jsonl(cls, path: str | Path, firewall: ReplayLeakageFirewall | None = None) -> "ReplayEngine":
        events: list[MarketEvent] = []
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    events.append(MarketEvent.from_dict(json.loads(line)))
        return cls(events, firewall)

    def run(self, on_event: Callable[[MarketEvent], None], speed: float = 0.0) -> int:
        previous: float | None = None
        count = 0
        for event in self.events:
            if speed > 0:
                current = _iso_seconds(event.observed_ts)
                if previous is not None:
                    time.sleep(max(0.0, current - previous) / speed)
                previous = current
            on_event(event)
            count += 1
        self.firewall.unlock()
        return count


def _iso_seconds(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
