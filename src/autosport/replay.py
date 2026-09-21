from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .domain import MarketEvent, utc_now_iso
from .json_integrity import jsonl_bytes_are_blank, strict_json_loads
from .market_mirror import MarketMirror, MirrorUpdate


def _parse_jsonl_event(line: str, line_number: int) -> MarketEvent:
    try:
        raw = strict_json_loads(line)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"invalid replay JSONL at line {line_number}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"invalid replay JSONL at line {line_number}: event must be a JSON object")
    try:
        return MarketEvent.from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid replay event schema at line {line_number}") from exc


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
        self._active_completion_digest: bytes | None = None

    def result_for(self, event_id: str) -> str | None:
        with self._state_lock:
            if self._state != self._UNLOCKED:
                raise FutureLeakageError("Final result is sealed until replay completion")
            return self._results.get(event_id)

    def _claim_for_replay(self) -> bytes:
        """Atomically claim this firewall and return the engine-only completion capability."""
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
            completion_capability = secrets.token_bytes(32)
            self._active_completion_digest = hashlib.sha256(completion_capability).digest()
            self._state = self._IN_USE
            return completion_capability

    def _complete_replay(self, completion_capability: bytes) -> None:
        """Unlock only for the exact capability returned to the owning replay run."""
        with self._state_lock:
            if self._state != self._IN_USE:
                raise FutureLeakageError(
                    "Final result firewall can only complete after its claimed replay runs"
                )
            if not isinstance(completion_capability, bytes):
                raise FutureLeakageError("invalid replay completion capability")
            candidate_digest = hashlib.sha256(completion_capability).digest()
            expected_digest = self._active_completion_digest
            if expected_digest is None or not hmac.compare_digest(
                candidate_digest, expected_digest
            ):
                raise FutureLeakageError("invalid replay completion capability")
            self._active_completion_digest = None
            self._state = self._UNLOCKED


@dataclass(frozen=True, slots=True)
class ReplayRun:
    run_id: str
    dataset_hash: str
    event_count: int
    started_at: str
    completed_at: str


def _snapshot_replay_event(event: MarketEvent) -> MarketEvent:
    """Own one canonical value snapshot without retaining caller metadata aliases."""

    if not isinstance(event, MarketEvent):
        raise TypeError("replay events must be MarketEvent values")
    try:
        # Dispatch through the canonical base-class serializer so subclasses cannot
        # replace replay identity through an overridden to_dict implementation.
        return MarketEvent.from_dict(MarketEvent.to_dict(event))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("replay event must be canonical") from exc


class ReplayEngine:
    def __init__(self, events: Iterable[MarketEvent], firewall: ReplayLeakageFirewall | None = None) -> None:
        # Snapshot each yielded value immediately. MarketEvent is frozen but nested
        # metadata is mutable, so retaining caller objects would allow strategy-visible
        # replay bytes to drift after dataset_hash was frozen.
        raw_events = [_snapshot_replay_event(event) for event in events]
        self._events = tuple(sorted(raw_events, key=_replay_order_key))
        self.firewall = firewall or ReplayLeakageFirewall()
        # Dataset identity preserves the pre-causal-delivery ordering contract.
        # Delivery order may evolve to match live availability semantics without
        # silently changing durable experiment/dataset identity for the same input.
        self.dataset_hash = _dataset_hash(raw_events)

    @property
    def events(self) -> tuple[MarketEvent, ...]:
        """Return detached audit snapshots without exposing hash-bound engine state."""

        return tuple(_snapshot_replay_event(event) for event in self._events)

    @classmethod
    def from_jsonl(cls, path: str | Path, firewall: ReplayLeakageFirewall | None = None) -> "ReplayEngine":
        source = Path(path)
        events: list[MarketEvent] = []
        try:
            with source.open("rb") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    if jsonl_bytes_are_blank(raw_line):
                        continue
                    try:
                        line = raw_line.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValueError(
                            f"invalid replay JSONL UTF-8 at line {line_number}"
                        ) from exc
                    events.append(_parse_jsonl_event(line, line_number))
        except OSError as exc:
            raise ValueError(f"unable to read replay JSONL: {source}") from exc
        return cls(events, firewall)

    def run(
        self,
        on_event: Callable[[MarketEvent], None],
        speed: float = 0.0,
        run_id: str | None = None,
        on_raw_event: Callable[[MarketEvent], object] | None = None,
    ) -> ReplayRun:
        # Claim before any strategy-visible callback. The raw completion capability
        # remains local to this run; the firewall stores only its digest. A failed
        # run deliberately leaves the firewall retired IN_USE and therefore sealed.
        completion_capability = self.firewall._claim_for_replay()
        previous: float | None = None
        started = utc_now_iso()
        count = 0
        replay_mirror = MarketMirror()
        for event in self._events:
            if speed > 0:
                current = _event_available_datetime(event).timestamp()
                if previous is not None:
                    time.sleep(max(0.0, current - previous) / speed)
                previous = current

            # Preserve the live durable-first boundary: every causally ordered
            # raw arrival may be persisted before source-local current-state
            # semantics decide whether it is strategy-visible. The callback gets
            # a detached value so it cannot mutate the engine's hash-bound state.
            if on_raw_event is not None:
                on_raw_event(_snapshot_replay_event(event))

            # Expose only the same source-local current-state transitions that the
            # live MarketMirror would make strategy-visible. Lower sequences and
            # exact duplicates are retained yet suppressed; conflicting sequence
            # reuse fails closed after the raw durable boundary has observed it.
            update = replay_mirror.apply(event)
            count += 1
            if update.status == MirrorUpdate.APPLIED:
                # A strategy callback receives a value snapshot, never the engine's
                # hash-bound internal event. Callback mutation therefore cannot
                # rewrite later audit inspection or the durable replay identity.
                on_event(_snapshot_replay_event(event))
        self.firewall._complete_replay(completion_capability)
        return ReplayRun(
            run_id=run_id or str(uuid.uuid4()),
            dataset_hash=self.dataset_hash,
            event_count=count,
            started_at=started,
            completed_at=utc_now_iso(),
        )


def _dataset_hash(events: list[MarketEvent]) -> str:
    digest = hashlib.sha256()
    for event in sorted(events, key=_dataset_identity_order_key):
        canonical = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest.update(canonical.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _dataset_identity_order_key(event: MarketEvent) -> tuple[datetime, int, str]:
    """Preserve the historical ReplayEngine dataset-hash ordering contract."""
    return (
        _iso_datetime(event.observed_ts, field_name="observed_ts"),
        event.sequence,
        event.dedupe_key,
    )


def _iso_datetime(value: str, *, field_name: str = "observed_ts") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid replay {field_name}: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"replay {field_name} must include timezone")
    return parsed


def _event_available_datetime(event: MarketEvent) -> datetime:
    """Return the first instant when a replay callback may know this event.

    Live decisions cannot consume an event before either its local observation
    instant or its durable ingestion/receipt instant.  Using the later clock
    prevents a late-arriving older observation from being replayed into the
    strategy before the live system could have received it.
    """

    observed = _iso_datetime(event.observed_ts, field_name="observed_ts")
    ingested = _iso_datetime(event.ingest_ts, field_name="ingest_ts")
    return max(observed, ingested)


def _replay_order_key(event: MarketEvent) -> tuple[datetime, datetime, int, str]:
    available = _event_available_datetime(event)
    observed = _iso_datetime(event.observed_ts, field_name="observed_ts")
    return (available, observed, event.sequence, event.dedupe_key)

