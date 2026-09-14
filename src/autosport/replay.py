from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .domain import MarketEvent, utc_now_iso


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _validate_strict_json_value(root: object) -> None:
    stack = [root]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("JSON string contains invalid Unicode scalar") from exc
        elif value is None or isinstance(value, (bool, int)):
            continue
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("non-finite JSON number")
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise ValueError(
                        "JSON object key contains invalid Unicode scalar"
                    ) from exc
                stack.append(item)
        else:
            raise ValueError(f"unsupported decoded JSON type: {type(value).__name__}")


def _parse_jsonl_event(line: str, line_number: int) -> MarketEvent:
    try:
        raw = json.loads(
            line,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
        _validate_strict_json_value(raw)
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
        source = Path(path)
        events: list[MarketEvent] = []
        try:
            with source.open("rb") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    try:
                        line = raw_line.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValueError(
                            f"invalid replay JSONL UTF-8 at line {line_number}"
                        ) from exc
                    if line.strip(" \t\r\n"):
                        events.append(_parse_jsonl_event(line, line_number))
        except OSError as exc:
            raise ValueError(f"unable to read replay JSONL: {source}") from exc
        return cls(events, firewall)

    def run(
        self,
        on_event: Callable[[MarketEvent], None],
        speed: float = 0.0,
        run_id: str | None = None,
    ) -> ReplayRun:
        # Claim before any strategy-visible callback. The raw completion capability
        # remains local to this run; the firewall stores only its digest. A failed
        # run deliberately leaves the firewall retired IN_USE and therefore sealed.
        completion_capability = self.firewall._claim_for_replay()
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
