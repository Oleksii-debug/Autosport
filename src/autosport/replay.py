from __future__ import annotations

import hashlib
import json
import threading
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

    _SEALED = "sealed"
    _IN_USE = "in_use"
    _UNLOCKED = "unlocked"

    def __init__(self, final_results: dict[str, str] | None = None) -> None:
        self._results = dict(final_results or {})
        self._state = self._SEALED
        self._state_lock = threading.Lock()

    def result_for(self, event_id: str) -> str | None:
        with self._state_lock:
            if self._state != self._UNLOCKED:
                raise FutureLeakageError("Final result is sealed until replay completion")
            return self._results.get(event_id)

    def require_sealed(self) -> None:
        """Atomically claim this firewall for exactly one replay run."""
        with self._state_lock:
            if self._state == self._UNLOCKED:
                raise FutureLeakageError(
                    "Final result firewall was already unlocked by a completed replay; "
                    "use a fresh firewall for each replay"
                )
            if self._state == self._IN_USE:
                raise FutureLeakageError(
                    "Final result firewall was already claimed by another or failed replay; "
                    "use a fresh firewall for each replay"
                )
            self._state = self._IN_USE

    def unlock(self) -> None:
        with self._state_lock:
            if self._state != self._IN_USE:
                raise FutureLeakageError(
                    "Final result firewall can only unlock after its claimed replay completes"
                )
            self._state = self._UNLOCKED


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
        # Claim the firewall before any strategy-visible callback. The atomic
        # SEALED -> IN_USE transition rejects both completed reuse and concurrent
        # reuse. A failed run deliberately leaves the firewall retired IN_USE
        # rather than risking a later replay against ambiguous causal state.
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
