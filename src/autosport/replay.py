from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .domain import MarketEvent, utc_now_iso
from .json_integrity import jsonl_bytes_are_blank, strict_json_loads


_REPLAY_STOP_CONTEXT = threading.local()


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


class ReplayStopRequested(RuntimeError):
    """Cooperative operator STOP before a replay may complete and unlock results."""


class ReplayStopToken:
    """Single-worker STOP authority with an atomic completion boundary."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._accepting = True

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    def request(self) -> bool:
        """Request STOP only while the replay can still honor it truthfully."""

        with self._lock:
            if not self._accepting:
                return False
            self._event.set()
            return True

    def checkpoint(self) -> None:
        if self._event.is_set():
            raise ReplayStopRequested("paper replay stopped by operator")

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)

    def finish(self, complete: Callable[[], None]) -> None:
        """Atomically choose STOP or completion; never acknowledge both."""

        with self._lock:
            if not self._accepting or self._event.is_set():
                self._accepting = False
                raise ReplayStopRequested("paper replay stopped by operator")
            try:
                complete()
            finally:
                self._accepting = False

    def disarm(self) -> None:
        with self._lock:
            self._accepting = False


@contextmanager
def replay_stop_scope(stop_token: ReplayStopToken | None) -> Iterator[None]:
    """Bind one cooperative STOP token to only the current replay worker thread.

    The token is deliberately thread-local: concurrent independent replays cannot
    stop each other, and callers outside the worker keep the historical replay API.
    Nested scopes restore the prior binding exactly.
    """

    had_previous = hasattr(_REPLAY_STOP_CONTEXT, "token")
    previous = getattr(_REPLAY_STOP_CONTEXT, "token", None)
    _REPLAY_STOP_CONTEXT.token = stop_token
    try:
        yield
    finally:
        if had_previous:
            _REPLAY_STOP_CONTEXT.token = previous
        else:
            delattr(_REPLAY_STOP_CONTEXT, "token")


def _active_replay_stop_token() -> ReplayStopToken | None:
    token = getattr(_REPLAY_STOP_CONTEXT, "token", None)
    return token if isinstance(token, ReplayStopToken) else None


def _checkpoint_replay_stop() -> None:
    token = _active_replay_stop_token()
    if token is not None:
        token.checkpoint()


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
    ) -> ReplayRun:
        stop_token = _active_replay_stop_token()
        # STOP is checked before claiming the firewall so an already-requested
        # cancellation cannot retire a fresh firewall merely by entering run().
        _checkpoint_replay_stop()
        # Claim before any strategy-visible callback. The raw completion capability
        # remains local to this run; the firewall stores only its digest. A failed
        # run deliberately leaves the firewall retired IN_USE and therefore sealed.
        completion_capability = self.firewall._claim_for_replay()
        previous: float | None = None
        started = utc_now_iso()
        count = 0
        try:
            for event in self.events:
                _checkpoint_replay_stop()
                if speed > 0:
                    current = _iso_seconds(event.observed_ts)
                    if previous is not None:
                        delay = max(0.0, current - previous) / speed
                        if delay > 0:
                            if stop_token is None:
                                time.sleep(delay)
                            elif stop_token.wait(delay):
                                raise ReplayStopRequested("paper replay stopped by operator")
                    previous = current
                _checkpoint_replay_stop()
                on_event(event)
                count += 1
            # Completion and STOP acknowledgement are serialized on the same token
            # lock. A request can therefore never return True after result unlock.
            if stop_token is None:
                self.firewall._complete_replay(completion_capability)
            else:
                stop_token.finish(
                    lambda: self.firewall._complete_replay(completion_capability)
                )
        except BaseException:
            if stop_token is not None:
                stop_token.disarm()
            raise
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
