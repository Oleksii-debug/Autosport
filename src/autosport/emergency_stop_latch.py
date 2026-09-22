"""Durable product-wide emergency STOP veto authority.

This module owns only the STOP veto. It does not own provider writes, execution
permission, risk approval, reconciliation, or Windows presentation. Durable rollback
protection is delegated to the existing independent ``MonotonicWorkspaceAuthority``.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Final, Protocol

from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import MonotonicWorkspaceAuthority
from .workspace_lock import WorkspaceEconomicLock, _open_read_only_descriptor


SCHEMA: Final = "autosport.emergency_stop_latch"
SCHEMA_VERSION: Final = 1
AUTHORITY_DOMAIN: Final = "execution.safety.emergency-stop"
AUTHORITY_KEY: Final = "product-wide-latch-v1"
_STATE_FILE: Final = "emergency_stop_v1.json"
_SHA256_CHARS: Final = frozenset("0123456789abcdef")

_SNAPSHOT_KEYS: Final = frozenset(
    {"schema", "schema_version", "workspace_instance_id", "events"}
)
_EVENT_KEYS: Final = frozenset(
    {
        "sequence",
        "event_id",
        "action",
        "recorded_at",
        "reason",
        "authority_id",
        "authority_evidence_sha256",
        "previous_event_sha256",
        "event_sha256",
    }
)


class EmergencyStopError(RuntimeError):
    """Base error for emergency STOP state and transitions."""


class EmergencyStopIntegrityError(EmergencyStopError):
    """Durable STOP bytes or independent monotonic authority cannot be trusted."""


class EmergencyStopTransitionError(EmergencyStopError):
    """A requested STOP transition is invalid or insufficiently authorized."""


class EmergencyStopState(str, Enum):
    UNKNOWN = "UNKNOWN"
    ENGAGED = "ENGAGED"
    CLEARED = "CLEARED"


class EmergencyStopAction(str, Enum):
    ENGAGE = "ENGAGE"
    CLEAR = "CLEAR"


class EmergencyActionClass(str, Enum):
    NEW_EXPOSURE = "NEW_EXPOSURE"
    INCREASE_EXPOSURE = "INCREASE_EXPOSURE"
    CANCEL_EXISTING = "CANCEL_EXISTING"
    RISK_REDUCING_HEDGE = "RISK_REDUCING_HEDGE"
    READ_ONLY_RECONCILIATION = "READ_ONLY_RECONCILIATION"


@dataclass(frozen=True, slots=True)
class EmergencyStopEvent:
    sequence: int
    event_id: str
    action: EmergencyStopAction
    recorded_at: str
    reason: str
    authority_id: str
    authority_evidence_sha256: str | None
    previous_event_sha256: str | None
    event_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "action": self.action.value,
            "recorded_at": self.recorded_at,
            "reason": self.reason,
            "authority_id": self.authority_id,
            "authority_evidence_sha256": self.authority_evidence_sha256,
            "previous_event_sha256": self.previous_event_sha256,
            "event_sha256": self.event_sha256,
        }


@dataclass(frozen=True, slots=True)
class EmergencyStopSnapshot:
    workspace_instance_id: str
    events: tuple[EmergencyStopEvent, ...]

    @property
    def state(self) -> EmergencyStopState:
        return (
            EmergencyStopState.ENGAGED
            if self.events[-1].action is EmergencyStopAction.ENGAGE
            else EmergencyStopState.CLEARED
        )

    @property
    def sequence(self) -> int:
        return self.events[-1].sequence

    @property
    def latest_event_id(self) -> str:
        return self.events[-1].event_id

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "workspace_instance_id": self.workspace_instance_id,
            "events": [event.to_dict() for event in self.events],
        }


@dataclass(frozen=True, slots=True)
class EmergencyStopDecision:
    action_class: EmergencyActionClass
    stop_state: EmergencyStopState
    stop_veto: bool
    may_proceed_past_stop_gate: bool
    requires_separate_authority: bool
    grants_execution_authority: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class EmergencyStopClearAuthorization:
    """Proof returned by a separately owned operator-clear authority.

    The latch never mints this authority and never treats this object alone as
    trustworthy.  It is accepted only as the direct result of the resolver
    configured by the product composition root for this latch instance.
    """

    workspace_instance_id: str
    stop_event_sha256: str
    clear_event_id: str
    clear_recorded_at: str
    authority_id: str
    evidence_sha256: str


class EmergencyStopClearAuthority(Protocol):
    """Read-only seam to the canonical operator authority that may clear STOP."""

    def resolve_emergency_stop_clear(
        self,
        *,
        workspace_instance_id: str,
        stop_event_sha256: str,
        clear_event_id: str,
        reason: str,
        clear_recorded_at: str,
    ) -> EmergencyStopClearAuthorization:
        ...


@dataclass(frozen=True, slots=True)
class _LoadedState:
    snapshot: EmergencyStopSnapshot
    raw: bytes
    sha256: str


def _text(value: object, name: str, *, max_length: int = 1024) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EmergencyStopIntegrityError(f"{name} must be non-empty canonical text")
    if len(value) > max_length or "\x00" in value or any(ord(ch) < 32 for ch in value):
        raise EmergencyStopIntegrityError(f"{name} contains unsupported characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EmergencyStopIntegrityError(f"{name} contains invalid Unicode") from exc
    return value


def _sha256(value: object, name: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in _SHA256_CHARS for ch in value)
    ):
        raise EmergencyStopIntegrityError(f"{name} must be lowercase SHA-256 hex")
    return value


def _timestamp(value: object, name: str) -> datetime:
    value = _text(value, name, max_length=64)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EmergencyStopIntegrityError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EmergencyStopIntegrityError(f"{name} must be timezone-aware")
    if parsed.utcoffset().total_seconds() != 0:
        raise EmergencyStopIntegrityError(f"{name} must be canonical UTC")
    canonical = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if value != canonical:
        raise EmergencyStopIntegrityError(
            f"{name} must be canonical UTC with microseconds"
        )
    return parsed


def _canonical_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EmergencyStopIntegrityError(
            "STOP state is outside canonical JSON domain"
        ) from exc


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _event_payload(
    *,
    sequence: int,
    event_id: str,
    action: EmergencyStopAction,
    recorded_at: str,
    reason: str,
    authority_id: str,
    authority_evidence_sha256: str | None,
    previous_event_sha256: str | None,
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "event_id": event_id,
        "action": action.value,
        "recorded_at": recorded_at,
        "reason": reason,
        "authority_id": authority_id,
        "authority_evidence_sha256": authority_evidence_sha256,
        "previous_event_sha256": previous_event_sha256,
    }


def _event_digest(payload: dict[str, object]) -> str:
    return _digest_bytes(_canonical_bytes(payload))


def _semantic_binding(event: EmergencyStopEvent) -> str:
    return _digest_bytes(
        _canonical_bytes(
            {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "authority_domain": AUTHORITY_DOMAIN,
                "authority_key": AUTHORITY_KEY,
                "event": event.to_dict(),
            }
        )
    )


def _event_from_dict(raw: object) -> EmergencyStopEvent:
    if not isinstance(raw, dict) or set(raw) != _EVENT_KEYS:
        raise EmergencyStopIntegrityError("STOP event schema is invalid")
    sequence = raw["sequence"]
    if type(sequence) is not int or sequence <= 0:
        raise EmergencyStopIntegrityError(
            "STOP event sequence must be a positive integer"
        )
    event_id = _text(raw["event_id"], "event_id", max_length=256)
    try:
        action = EmergencyStopAction(raw["action"])
    except (TypeError, ValueError) as exc:
        raise EmergencyStopIntegrityError("STOP event action is invalid") from exc
    recorded_at = _text(raw["recorded_at"], "recorded_at", max_length=64)
    _timestamp(recorded_at, "recorded_at")
    reason = _text(raw["reason"], "reason", max_length=1024)
    authority_id = _text(raw["authority_id"], "authority_id", max_length=512)
    evidence = _sha256(
        raw["authority_evidence_sha256"],
        "authority_evidence_sha256",
        allow_none=True,
    )
    previous = _sha256(
        raw["previous_event_sha256"],
        "previous_event_sha256",
        allow_none=True,
    )
    event_sha = _sha256(raw["event_sha256"], "event_sha256")
    assert isinstance(event_sha, str)
    if action is EmergencyStopAction.CLEAR and evidence is None:
        raise EmergencyStopIntegrityError(
            "CLEAR requires resolved operator-authority evidence"
        )
    payload = _event_payload(
        sequence=sequence,
        event_id=event_id,
        action=action,
        recorded_at=recorded_at,
        reason=reason,
        authority_id=authority_id,
        authority_evidence_sha256=evidence,
        previous_event_sha256=previous,
    )
    if _event_digest(payload) != event_sha:
        raise EmergencyStopIntegrityError("STOP event hash is invalid")
    return EmergencyStopEvent(
        sequence=sequence,
        event_id=event_id,
        action=action,
        recorded_at=recorded_at,
        reason=reason,
        authority_id=authority_id,
        authority_evidence_sha256=evidence,
        previous_event_sha256=previous,
        event_sha256=event_sha,
    )


def _snapshot_from_dict(
    raw: object, *, expected_workspace_instance_id: str
) -> EmergencyStopSnapshot:
    if not isinstance(raw, dict) or set(raw) != _SNAPSHOT_KEYS:
        raise EmergencyStopIntegrityError("STOP snapshot schema is invalid")
    if raw["schema"] != SCHEMA:
        raise EmergencyStopIntegrityError("STOP snapshot schema identifier is invalid")
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != SCHEMA_VERSION
    ):
        raise EmergencyStopIntegrityError("STOP snapshot schema version is invalid")
    workspace_instance_id = _text(
        raw["workspace_instance_id"], "workspace_instance_id", max_length=256
    )
    if workspace_instance_id != expected_workspace_instance_id:
        raise EmergencyStopIntegrityError("STOP snapshot workspace identity drifted")
    raw_events = raw["events"]
    if not isinstance(raw_events, list) or not raw_events:
        raise EmergencyStopIntegrityError("STOP snapshot requires at least one event")

    events = tuple(_event_from_dict(item) for item in raw_events)
    seen_ids: set[str] = set()
    previous_hash: str | None = None
    previous_time: datetime | None = None
    previous_action: EmergencyStopAction | None = None
    for expected_sequence, event in enumerate(events, 1):
        if event.sequence != expected_sequence:
            raise EmergencyStopIntegrityError(
                "STOP event sequence has a gap or reorder"
            )
        if event.event_id in seen_ids:
            raise EmergencyStopIntegrityError("STOP event_id is duplicated")
        seen_ids.add(event.event_id)
        if event.previous_event_sha256 != previous_hash:
            raise EmergencyStopIntegrityError("STOP event hash chain is broken")
        current_time = _timestamp(event.recorded_at, "recorded_at")
        if previous_time is not None and current_time <= previous_time:
            raise EmergencyStopIntegrityError("STOP event timestamp rolled back")
        if expected_sequence == 1 and event.action is not EmergencyStopAction.ENGAGE:
            raise EmergencyStopIntegrityError(
                "first STOP event must ENGAGE fail-closed state"
            )
        if (
            event.action is EmergencyStopAction.CLEAR
            and previous_action is not EmergencyStopAction.ENGAGE
        ):
            raise EmergencyStopIntegrityError("CLEAR requires an engaged STOP state")
        previous_hash = event.event_sha256
        previous_time = current_time
        previous_action = event.action
    return EmergencyStopSnapshot(workspace_instance_id, events)


def _decode_snapshot(
    raw: bytes, *, expected_workspace_instance_id: str
) -> EmergencyStopSnapshot:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EmergencyStopIntegrityError("STOP state is not UTF-8") from exc
    try:
        decoded = strict_json_loads(text)
    except (TypeError, ValueError, RecursionError) as exc:
        raise EmergencyStopIntegrityError("STOP state JSON is invalid") from exc
    snapshot = _snapshot_from_dict(
        decoded, expected_workspace_instance_id=expected_workspace_instance_id
    )
    if raw != _canonical_bytes(snapshot.to_dict()):
        raise EmergencyStopIntegrityError("STOP state bytes are not canonical")
    return snapshot


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class EmergencyStopLatch:
    """Workspace-scoped durable veto for money-moving execution admission."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        authority_root: str | Path | None = None,
        clock: Callable[[], str] | None = None,
        clear_authority: EmergencyStopClearAuthority | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        if not self.workspace.is_absolute():
            raise EmergencyStopTransitionError("workspace must be an absolute path")
        self.authority = MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            workspace_instance_id=None,
            domain=AUTHORITY_DOMAIN,
            key=AUTHORITY_KEY,
            authority_root=authority_root,
        )
        self.workspace_instance_id = self.authority.workspace_instance_id
        self.state_path = self.workspace / ".autosport" / _STATE_FILE
        self._clock = clock or _default_clock
        self._clear_authority = clear_authority

    def decision(self, action_class: EmergencyActionClass) -> EmergencyStopDecision:
        if not isinstance(action_class, EmergencyActionClass):
            raise TypeError("action_class must be EmergencyActionClass")
        if action_class is EmergencyActionClass.READ_ONLY_RECONCILIATION:
            try:
                loaded = self._read_current()
                state = (
                    EmergencyStopState.UNKNOWN
                    if loaded is None
                    else loaded.snapshot.state
                )
                detail = "read-only reconciliation remains allowed under STOP"
            except Exception:
                state = EmergencyStopState.UNKNOWN
                detail = (
                    "STOP state unproven; read-only reconciliation remains "
                    "non-money-moving"
                )
            return EmergencyStopDecision(
                action_class=action_class,
                stop_state=state,
                stop_veto=False,
                may_proceed_past_stop_gate=True,
                requires_separate_authority=False,
                detail=detail,
            )

        try:
            loaded = self._read_current()
        except Exception:
            return EmergencyStopDecision(
                action_class=action_class,
                stop_state=EmergencyStopState.UNKNOWN,
                stop_veto=True,
                may_proceed_past_stop_gate=False,
                requires_separate_authority=True,
                detail=(
                    "STOP state missing, corrupt, rolled back, busy, or otherwise "
                    "unproven"
                ),
            )

        if loaded is None:
            return EmergencyStopDecision(
                action_class=action_class,
                stop_state=EmergencyStopState.UNKNOWN,
                stop_veto=True,
                may_proceed_past_stop_gate=False,
                requires_separate_authority=True,
                detail="STOP state has not been durably initialized",
            )

        state = loaded.snapshot.state
        if state is EmergencyStopState.ENGAGED and action_class in {
            EmergencyActionClass.NEW_EXPOSURE,
            EmergencyActionClass.INCREASE_EXPOSURE,
        }:
            return EmergencyStopDecision(
                action_class=action_class,
                stop_state=state,
                stop_veto=True,
                may_proceed_past_stop_gate=False,
                requires_separate_authority=True,
                detail="durable emergency STOP vetoes new or increased exposure",
            )

        return EmergencyStopDecision(
            action_class=action_class,
            stop_state=state,
            stop_veto=False,
            may_proceed_past_stop_gate=True,
            requires_separate_authority=True,
            detail=(
                "STOP does not authorize external mutation; separate canonical "
                "authority is required"
            ),
        )

    def inspect(self) -> EmergencyStopSnapshot | None:
        loaded = self._read_current()
        return None if loaded is None else loaded.snapshot

    def engage(
        self,
        *,
        event_id: str,
        reason: str,
        authority_id: str,
        authority_evidence_sha256: str | None = None,
        recorded_at: str | None = None,
    ) -> EmergencyStopSnapshot:
        return self._transition(
            action=EmergencyStopAction.ENGAGE,
            event_id=event_id,
            reason=reason,
            authority_id=authority_id,
            authority_evidence_sha256=authority_evidence_sha256,
            recorded_at=recorded_at,
        )

    def clear(
        self,
        *,
        event_id: str,
        reason: str,
        recorded_at: str | None = None,
    ) -> EmergencyStopSnapshot:
        """Clear only through the separately configured canonical operator authority.

        Bare caller-supplied authority ids or evidence digests are intentionally not
        accepted: a caller must not be able to remove the STOP veto by inventing an
        apparently well-formed SHA-256 string.
        """

        return self._transition(
            action=EmergencyStopAction.CLEAR,
            event_id=event_id,
            reason=reason,
            authority_id=None,
            authority_evidence_sha256=None,
            recorded_at=recorded_at,
        )

    def _read_current(self) -> _LoadedState | None:
        try:
            with WorkspaceEconomicLock(self.workspace):
                return self._read_current_locked()
        except EmergencyStopError:
            raise
        except Exception as exc:
            raise EmergencyStopIntegrityError(
                "cannot establish durable emergency STOP truth"
            ) from exc

    def _read_current_locked(self) -> _LoadedState | None:
        raw = self._read_state_bytes()
        if raw is None:
            self.authority.recover(observed_state_sha256=None)
            return None
        snapshot = _decode_snapshot(
            raw, expected_workspace_instance_id=self.workspace_instance_id
        )
        digest = _digest_bytes(raw)
        latest = snapshot.events[-1]
        recovery = self.authority.recover(
            observed_state_sha256=digest,
            tx_id=latest.event_id,
            semantic_binding_sha256=_semantic_binding(latest),
        )
        if recovery.committed_state_sha256 != digest:
            raise EmergencyStopIntegrityError(
                "independent monotonic authority did not commit current STOP state"
            )
        return _LoadedState(snapshot=snapshot, raw=raw, sha256=digest)

    def _transition(
        self,
        *,
        action: EmergencyStopAction,
        event_id: str,
        reason: str,
        authority_id: str | None,
        authority_evidence_sha256: str | None,
        recorded_at: str | None,
    ) -> EmergencyStopSnapshot:
        try:
            event_id = _text(event_id, "event_id", max_length=256)
            reason = _text(reason, "reason", max_length=1024)
            if action is EmergencyStopAction.ENGAGE:
                authority_id = _text(authority_id, "authority_id", max_length=512)
                evidence = _sha256(
                    authority_evidence_sha256,
                    "authority_evidence_sha256",
                    allow_none=True,
                )
            else:
                authority_id = None
                evidence = None
            timestamp = recorded_at if recorded_at is not None else self._clock()
            parsed_time = _timestamp(timestamp, "recorded_at")
        except EmergencyStopTransitionError:
            raise
        except EmergencyStopIntegrityError as exc:
            raise EmergencyStopTransitionError(str(exc)) from exc

        clear_authorization: EmergencyStopClearAuthorization | None = None
        if action is EmergencyStopAction.CLEAR:
            resolver = self._clear_authority
            if resolver is None:
                raise EmergencyStopTransitionError(
                    "CLEAR requires a configured separate operator authority"
                )
            try:
                # Resolve operator authority outside the workspace writer lock.  The
                # current STOP event hash is captured under lock, then revalidated
                # after resolution to close the TOCTOU window without deadlocking a
                # canonical authority reader that may own its own persistence locks.
                with WorkspaceEconomicLock(self.workspace):
                    initial = self._read_current_locked()
                    if (
                        initial is None
                        or initial.snapshot.state is not EmergencyStopState.ENGAGED
                    ):
                        raise EmergencyStopTransitionError(
                            "CLEAR requires a currently engaged durable STOP"
                        )
                    initial_stop_event_sha256 = initial.snapshot.events[-1].event_sha256
                clear_authorization = resolver.resolve_emergency_stop_clear(
                    workspace_instance_id=self.workspace_instance_id,
                    stop_event_sha256=initial_stop_event_sha256,
                    clear_event_id=event_id,
                    reason=reason,
                    clear_recorded_at=timestamp,
                )
            except EmergencyStopError:
                raise
            except Exception as exc:
                raise EmergencyStopTransitionError(
                    "operator authority did not resolve emergency STOP CLEAR"
                ) from exc
            if not isinstance(clear_authorization, EmergencyStopClearAuthorization):
                raise EmergencyStopTransitionError(
                    "operator authority returned invalid CLEAR evidence type"
                )
            if clear_authorization.workspace_instance_id != self.workspace_instance_id:
                raise EmergencyStopTransitionError(
                    "CLEAR authority workspace identity does not match latch"
                )
            if clear_authorization.stop_event_sha256 != initial_stop_event_sha256:
                raise EmergencyStopTransitionError(
                    "CLEAR authority is stale for the captured STOP event"
                )
            if clear_authorization.clear_event_id != event_id:
                raise EmergencyStopTransitionError(
                    "CLEAR authority event identity does not match request"
                )
            if clear_authorization.clear_recorded_at != timestamp:
                raise EmergencyStopTransitionError(
                    "CLEAR authority timestamp does not match request"
                )
            try:
                _timestamp(
                    clear_authorization.clear_recorded_at,
                    "clear authority recorded_at",
                )
                authority_id = _text(
                    clear_authorization.authority_id,
                    "authority_id",
                    max_length=512,
                )
                evidence = _sha256(
                    clear_authorization.evidence_sha256,
                    "authority_evidence_sha256",
                )
            except EmergencyStopIntegrityError as exc:
                raise EmergencyStopTransitionError(
                    "operator authority returned malformed CLEAR evidence"
                ) from exc
            assert isinstance(evidence, str)

        try:
            with WorkspaceEconomicLock(self.workspace):
                loaded = self._read_current_locked()
                events = () if loaded is None else loaded.snapshot.events
                if any(item.event_id == event_id for item in events):
                    raise EmergencyStopTransitionError(
                        "duplicate STOP event_id is forbidden"
                    )
                if action is EmergencyStopAction.CLEAR:
                    if (
                        loaded is None
                        or loaded.snapshot.state is not EmergencyStopState.ENGAGED
                    ):
                        raise EmergencyStopTransitionError(
                            "CLEAR requires a currently engaged durable STOP"
                        )
                    assert clear_authorization is not None
                    if (
                        loaded.snapshot.events[-1].event_sha256
                        != clear_authorization.stop_event_sha256
                    ):
                        raise EmergencyStopTransitionError(
                            "CLEAR authority became stale before durable transition"
                        )
                if events:
                    previous_time = _timestamp(events[-1].recorded_at, "recorded_at")
                    if parsed_time <= previous_time:
                        raise EmergencyStopTransitionError(
                            "STOP transition timestamp must advance monotonically"
                        )
                previous_hash = events[-1].event_sha256 if events else None
                payload = _event_payload(
                    sequence=len(events) + 1,
                    event_id=event_id,
                    action=action,
                    recorded_at=timestamp,
                    reason=reason,
                    authority_id=authority_id,
                    authority_evidence_sha256=evidence,
                    previous_event_sha256=previous_hash,
                )
                event = EmergencyStopEvent(
                    sequence=len(events) + 1,
                    event_id=event_id,
                    action=action,
                    recorded_at=timestamp,
                    reason=reason,
                    authority_id=authority_id,
                    authority_evidence_sha256=evidence,
                    previous_event_sha256=previous_hash,
                    event_sha256=_event_digest(payload),
                )
                snapshot = EmergencyStopSnapshot(
                    workspace_instance_id=self.workspace_instance_id,
                    events=events + (event,),
                )
                raw = _canonical_bytes(snapshot.to_dict())
                intended_digest = _digest_bytes(raw)
                binding = _semantic_binding(event)
                self.authority.prepare(
                    tx_id=event.event_id,
                    observed_state_sha256=None if loaded is None else loaded.sha256,
                    intended_state_sha256=intended_digest,
                    semantic_binding_sha256=binding,
                )
                self._publish_state_bytes(raw, intended_digest)
                observed = self._read_state_bytes()
                if observed is None or _digest_bytes(observed) != intended_digest:
                    raise EmergencyStopIntegrityError(
                        "durable STOP state readback does not match prepared state"
                    )
                _decode_snapshot(
                    observed, expected_workspace_instance_id=self.workspace_instance_id
                )
                self.authority.commit(
                    tx_id=event.event_id,
                    observed_state_sha256=intended_digest,
                    semantic_binding_sha256=binding,
                )
                return snapshot
        except EmergencyStopError:
            raise
        except Exception as exc:
            raise EmergencyStopIntegrityError(
                "emergency STOP transition could not be proven durable"
            ) from exc

    def _read_state_bytes(self) -> bytes | None:
        try:
            metadata = os.lstat(self.state_path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise EmergencyStopIntegrityError("cannot inspect STOP state path") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise EmergencyStopIntegrityError(
                "STOP state path must be a regular non-symlink file"
            )
        if metadata.st_nlink != 1:
            raise EmergencyStopIntegrityError(
                "STOP state file must not have hard-link aliases"
            )

        try:
            descriptor = _open_read_only_descriptor(self.state_path)
        except OSError as exc:
            raise EmergencyStopIntegrityError(
                "cannot open STOP state path without following aliases"
            ) from exc
        try:
            verification_descriptor = _open_read_only_descriptor(self.state_path)
        except OSError as exc:
            os.close(descriptor)
            raise EmergencyStopIntegrityError(
                "cannot verify STOP state path without following aliases"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            verification = os.fstat(verification_descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(verification.st_mode)
                or opened.st_nlink != 1
                or verification.st_nlink != 1
            ):
                raise EmergencyStopIntegrityError("STOP state file identity is unsafe")
            try:
                same_file = os.path.sameopenfile(descriptor, verification_descriptor)
            except OSError as exc:
                raise EmergencyStopIntegrityError(
                    "cannot verify STOP state descriptor identity"
                ) from exc
            if not same_file:
                raise EmergencyStopIntegrityError(
                    "STOP state path changed during verification"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read()

            # Re-open after the read so a pathname replacement cannot make a
            # successfully verified old descriptor stand in for the current state.
            try:
                final_descriptor = _open_read_only_descriptor(self.state_path)
            except OSError as exc:
                raise EmergencyStopIntegrityError(
                    "STOP state path changed during read"
                ) from exc
            try:
                final_stat = os.fstat(final_descriptor)
                if not stat.S_ISREG(final_stat.st_mode) or final_stat.st_nlink != 1:
                    raise EmergencyStopIntegrityError(
                        "STOP state final descriptor identity is unsafe"
                    )
                if not os.path.sameopenfile(descriptor, final_descriptor):
                    raise EmergencyStopIntegrityError(
                        "STOP state path changed during read"
                    )
            finally:
                os.close(final_descriptor)
            return raw
        finally:
            os.close(verification_descriptor)
            os.close(descriptor)

    def _publish_state_bytes(self, raw: bytes, digest: str) -> None:
        parent = self.state_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        temp = parent / (
            f".{_STATE_FILE}.{digest[:24]}.{uuid.uuid4().hex}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        if os.name != "nt":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        created = False
        try:
            fd = os.open(temp, flags, 0o600)
            created = True
            with os.fdopen(fd, "wb") as handle:
                fd = None
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.state_path)
            created = False
            if os.name != "nt":
                dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except BaseException:
            if fd is not None:
                os.close(fd)
            if created:
                try:
                    temp.unlink()
                except OSError:
                    pass
            raise
