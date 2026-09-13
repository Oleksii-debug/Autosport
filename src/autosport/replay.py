from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .domain import MarketEvent, utc_now_iso


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

    def require_sealed(self) -> None:
        if self._unlocked:
            raise FutureLeakageError(
                "Final result firewall was already unlocked by a completed replay; "
                "use a fresh firewall for each replay"
            )

    def unlock(self) -> None:
        self._unlocked = True


@dataclass(frozen=True, slots=True)
class ReplayRun:
    run_id: str
    dataset_hash: str
    event_count: int
    started_at: str
    completed_at: str


class ReplayEngine:
    def __init__(self, events: Iterable[MarketEvent], firewall: ReplayLeakageFirewall | None = None) -> None:
        self.events = sorted(
            events,
            key=lambda e: (_iso_datetime(e.observed_ts), e.sequence, e.dedupe_key),
        )
        self.firewall = firewall or ReplayLeakageFirewall()
        self.dataset_hash = _dataset_hash(self.events)

    @classmethod
    def from_jsonl(cls, path: str | Path, firewall: ReplayLeakageFirewall | None = None) -> "ReplayEngine":
        events: list[MarketEvent] = []
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    events.append(MarketEvent.from_dict(json.loads(line)))
        return cls(events, firewall)

    def run(
        self,
        on_event: Callable[[MarketEvent], None],
        speed: float = 0.0,
        run_id: str | None = None,
    ) -> ReplayRun:
        # A completed replay deliberately unseals final results. Reusing that
        # firewall for another replay would expose outcome facts before the first
        # event callback, so reject the run before any strategy-visible mutation.
        self.firewall.require_sealed()
        previous: float | None = None
        started = utc_now_iso()
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
        return ReplayRun(
            run_id=run_id or str(uuid.uuid4()),
            dataset_hash=self.dataset_hash,
            event_count=count,
            started_at=started,
            completed_at=utc_now_iso(),
        )


def _dataset_hash(events: list[MarketEvent]) -> str:
    digest = hashlib.sha256()
    for event in events:
        canonical = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest.update(canonical.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _iso_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid replay observed_ts: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("replay observed_ts must include timezone")
    return parsed


def _iso_seconds(value: str) -> float:
    return _iso_datetime(value).timestamp()
