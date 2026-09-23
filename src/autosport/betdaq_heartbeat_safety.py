"""BETDAQ heartbeat safety authority.

This module composes the canonical BETDAQ authenticated HTTPS/session context with the
durable execution STOP authority.  It owns only heartbeat control/evidence.  It does
not place, update, cancel, unsuspend, or settle orders and never grants write or
real-money execution permission.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from hashlib import sha256
import json
import os
from pathlib import Path
from secrets import token_hex
import tempfile
from typing import Any
import xml.etree.ElementTree as ET

from .betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqAccountReadOnlyError,
    _EXTERNAL_NS,
    _SECURE_ENDPOINT,
    _SOAP11_NS,
    _SOAP12_NS,
    _authenticated_account_context,
    _require_canonical_account_transport,
)
from .execution_stop_authority import (
    ExecutionAuthorityMode,
    ExecutionStopAuthority,
)


_SCHEMA = "autosport.betdaq-heartbeat-safety"
_SCHEMA_VERSION = 1
_ANCHOR_SCHEMA = "autosport.betdaq-heartbeat-safety-anchor"
_ANCHOR_VERSION = 1
_PROCESS_INSTANCE_ID = token_hex(32)
_ALLOWED_METHODS = frozenset(
    {
        "RegisterHeartbeat",
        "ChangeHeartbeatRegistration",
        "DeregisterHeartbeat",
        "Pulse",
    }
)
_REQUEST_ELEMENT = {
    "RegisterHeartbeat": "registerHeartbeatRequest",
    "ChangeHeartbeatRegistration": "changeHeartbeatRegistrationRequest",
    "DeregisterHeartbeat": "deregisterHeartbeatRequest",
    "Pulse": "pulseRequest",
}
_REMOTE_ACTIVE_STATES = frozenset({"ACTIVE", "RECONCILIATION_REQUIRED"})
_HEX = frozenset("0123456789abcdef")


class BetdaqHeartbeatSafetyError(RuntimeError):
    """Fail-closed heartbeat authority or provider-control error."""


class HeartbeatAction(IntEnum):
    CANCEL_ORDERS = 1
    SUSPEND_ORDERS = 2
    SUSPEND_PUNTER = 3


class HeartbeatState(StrEnum):
    UNREGISTERED = "UNREGISTERED"
    ACTIVE = "ACTIVE"
    LOST_PROVIDER_REGISTRATION = "LOST_PROVIDER_REGISTRATION"
    REVOKED = "REVOKED"
    DEGRADED_UNKNOWN = "DEGRADED_UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    RESTART_FENCED = "RESTART_FENCED"


@dataclass(frozen=True, slots=True)
class HeartbeatProviderEvidence:
    method: str
    observed_at: str
    response_sha256: str
    provider_return_code: int | None
    provider_performed_at: str | None
    performed_action: HeartbeatAction | None

    def __post_init__(self) -> None:
        if self.method not in _ALLOWED_METHODS:
            raise BetdaqHeartbeatSafetyError("heartbeat evidence method is not canonical")
        _instant(self.observed_at, "observed_at")
        _digest_text(self.response_sha256, "response_sha256")
        if self.provider_performed_at is not None:
            _instant(self.provider_performed_at, "provider_performed_at")
        if self.performed_action is not None and not isinstance(
            self.performed_action, HeartbeatAction
        ):
            raise BetdaqHeartbeatSafetyError("performed_action is not canonical")


@dataclass(frozen=True, slots=True)
class HeartbeatEvent:
    sequence: int
    generation_id: str
    predecessor_generation_id: str | None
    account_context_id: str
    process_instance_id: str
    state: HeartbeatState
    threshold_ms: int | None
    registered_action: HeartbeatAction | None
    operation: str
    observed_at: str
    provider_performed_at: str | None
    performed_action: HeartbeatAction | None
    provider_return_code: int | None
    response_sha256: str | None
    reconciliation_required: bool
    external_pulse_masking_possible: bool
    execution_write_authorized: bool
    real_money_execution_authorized: bool
    cross_session_equivalence_proven: bool
    previous_event_sha256: str | None
    event_sha256: str

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise BetdaqHeartbeatSafetyError("heartbeat sequence must be positive")
        _digest_text(self.generation_id, "generation_id")
        if self.predecessor_generation_id is not None:
            _digest_text(
                self.predecessor_generation_id,
                "predecessor_generation_id",
            )
        _text(self.account_context_id, "account_context_id")
        _digest_text(self.process_instance_id, "process_instance_id")
        if not isinstance(self.state, HeartbeatState):
            raise BetdaqHeartbeatSafetyError("heartbeat state is not canonical")
        if self.threshold_ms is None:
            if self.registered_action is not None:
                raise BetdaqHeartbeatSafetyError(
                    "heartbeat action cannot exist without threshold"
                )
        else:
            _threshold(self.threshold_ms)
            if not isinstance(self.registered_action, HeartbeatAction):
                raise BetdaqHeartbeatSafetyError(
                    "registered_action must be canonical when threshold is present"
                )
        _text(self.operation, "operation")
        _instant(self.observed_at, "observed_at")
        if self.provider_performed_at is not None:
            _instant(self.provider_performed_at, "provider_performed_at")
        if self.performed_action is not None and not isinstance(
            self.performed_action, HeartbeatAction
        ):
            raise BetdaqHeartbeatSafetyError("performed_action is not canonical")
        if self.response_sha256 is not None:
            _digest_text(self.response_sha256, "response_sha256")
        if self.previous_event_sha256 is not None:
            _digest_text(self.previous_event_sha256, "previous_event_sha256")
        _digest_text(self.event_sha256, "event_sha256")
        if self.external_pulse_masking_possible is not True:
            raise BetdaqHeartbeatSafetyError(
                "BETDAQ account-level heartbeat masking risk must remain explicit"
            )
        if self.execution_write_authorized is not False:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat evidence cannot authorize provider writes"
            )
        if self.real_money_execution_authorized is not False:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat evidence cannot authorize real-money execution"
            )
        if self.cross_session_equivalence_proven is not False:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat evidence cannot prove cross-session account equivalence"
            )


@dataclass(frozen=True, slots=True)
class HeartbeatSafetyStatus:
    event: HeartbeatEvent | None
    provider_registration_active: bool
    local_process_owner: bool
    reconciliation_required: bool
    execution_write_authorized: bool = False
    real_money_execution_authorized: bool = False
    exclusive_account_heartbeat_owner_proven: bool = False


def _text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise BetdaqHeartbeatSafetyError(f"{name} must be non-empty canonical text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BetdaqHeartbeatSafetyError(f"{name} must be UTF-8 text") from exc
    return value


def _digest_text(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(char not in _HEX for char in raw):
        raise BetdaqHeartbeatSafetyError(f"{name} must be lowercase SHA-256 hex")
    return raw


def _threshold(value: object) -> int:
    if (
        type(value) is not int
        or value <= 0
        or value > 2_147_483_647
    ):
        raise BetdaqHeartbeatSafetyError(
            "threshold_ms must be a positive signed 32-bit integer"
        )
    return value


def _instant(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetdaqHeartbeatSafetyError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetdaqHeartbeatSafetyError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise BetdaqHeartbeatSafetyError(
            "heartbeat evidence must be canonical finite JSON"
        ) from exc


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _event_body(event: HeartbeatEvent) -> dict[str, object]:
    payload = asdict(event)
    payload["state"] = event.state.value
    payload["registered_action"] = (
        None if event.registered_action is None else int(event.registered_action)
    )
    payload["performed_action"] = (
        None if event.performed_action is None else int(event.performed_action)
    )
    payload.pop("event_sha256")
    return payload


def _event_to_payload(event: HeartbeatEvent) -> dict[str, object]:
    return {**_event_body(event), "event_sha256": event.event_sha256}


def _event_from_payload(raw: object) -> HeartbeatEvent:
    if type(raw) is not dict:
        raise BetdaqHeartbeatSafetyError("heartbeat event must be an object")
    expected = {
        "sequence",
        "generation_id",
        "predecessor_generation_id",
        "account_context_id",
        "process_instance_id",
        "state",
        "threshold_ms",
        "registered_action",
        "operation",
        "observed_at",
        "provider_performed_at",
        "performed_action",
        "provider_return_code",
        "response_sha256",
        "reconciliation_required",
        "external_pulse_masking_possible",
        "execution_write_authorized",
        "real_money_execution_authorized",
        "cross_session_equivalence_proven",
        "previous_event_sha256",
        "event_sha256",
    }
    if set(raw) != expected:
        raise BetdaqHeartbeatSafetyError("heartbeat event schema is invalid")
    try:
        event = HeartbeatEvent(
            sequence=raw["sequence"],
            generation_id=raw["generation_id"],
            predecessor_generation_id=raw["predecessor_generation_id"],
            account_context_id=raw["account_context_id"],
            process_instance_id=raw["process_instance_id"],
            state=HeartbeatState(raw["state"]),
            threshold_ms=raw["threshold_ms"],
            registered_action=(
                None
                if raw["registered_action"] is None
                else HeartbeatAction(raw["registered_action"])
            ),
            operation=raw["operation"],
            observed_at=raw["observed_at"],
            provider_performed_at=raw["provider_performed_at"],
            performed_action=(
                None
                if raw["performed_action"] is None
                else HeartbeatAction(raw["performed_action"])
            ),
            provider_return_code=raw["provider_return_code"],
            response_sha256=raw["response_sha256"],
            reconciliation_required=raw["reconciliation_required"],
            external_pulse_masking_possible=raw[
                "external_pulse_masking_possible"
            ],
            execution_write_authorized=raw["execution_write_authorized"],
            real_money_execution_authorized=raw[
                "real_money_execution_authorized"
            ],
            cross_session_equivalence_proven=raw[
                "cross_session_equivalence_proven"
            ],
            previous_event_sha256=raw["previous_event_sha256"],
            event_sha256=raw["event_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BetdaqHeartbeatSafetyError(
            "heartbeat event contains invalid values"
        ) from exc
    if event.event_sha256 != _digest(_event_body(event)):
        raise BetdaqHeartbeatSafetyError("heartbeat event digest mismatch")
    return event


class _ProcessLease:
    """Hold one non-blocking process lease for the canonical heartbeat state path."""

    def __init__(self, state_path: Path) -> None:
        self.path = state_path.with_name(f".{state_path.name}.owner.lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        try:
            self._acquire()
        except BaseException:
            self._handle.close()
            raise

    def _acquire(self) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0, os.SEEK_END)
                if self._handle.tell() == 0:
                    self._handle.write(b"\0")
                    self._handle.flush()
                    os.fsync(self._handle.fileno())
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    self._handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except (OSError, BlockingIOError) as exc:
            raise BetdaqHeartbeatSafetyError(
                "another process owns the heartbeat safety state"
            ) from exc

    def close(self) -> None:
        if self._handle.closed:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat process lease release failed"
            ) from exc
        finally:
            self._handle.close()


class BetdaqHeartbeatSafetyStore:
    """Atomic hash-chained durable heartbeat history with a separate tip anchor."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.anchor_path = self.path.with_name(self.path.name + ".anchor.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def history(self) -> tuple[HeartbeatEvent, ...]:
        state_exists = self.path.exists()
        anchor_exists = self.anchor_path.exists()
        if not state_exists and not anchor_exists:
            return ()
        if state_exists != anchor_exists:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat state/anchor pair is incomplete"
            )
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            anchor = json.loads(self.anchor_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat durable state is unreadable"
            ) from exc
        if (
            type(document) is not dict
            or set(document) != {"schema", "schema_version", "events"}
            or document["schema"] != _SCHEMA
            or document["schema_version"] != _SCHEMA_VERSION
            or type(document["events"]) is not list
        ):
            raise BetdaqHeartbeatSafetyError("heartbeat durable state schema is invalid")
        events = tuple(_event_from_payload(item) for item in document["events"])
        previous: str | None = None
        previous_time: datetime | None = None
        for index, event in enumerate(events, start=1):
            if event.sequence != index or event.previous_event_sha256 != previous:
                raise BetdaqHeartbeatSafetyError(
                    "heartbeat durable history is not contiguous"
                )
            current_time = _instant(event.observed_at, "observed_at")
            if previous_time is not None and current_time < previous_time:
                raise BetdaqHeartbeatSafetyError(
                    "heartbeat durable history time regressed"
                )
            previous = event.event_sha256
            previous_time = current_time
        expected_anchor = {
            "schema": _ANCHOR_SCHEMA,
            "schema_version": _ANCHOR_VERSION,
            "sequence": len(events),
            "event_sha256": None if not events else events[-1].event_sha256,
        }
        if type(anchor) is not dict or set(anchor) != {
            *expected_anchor.keys(),
            "anchor_sha256",
        }:
            raise BetdaqHeartbeatSafetyError("heartbeat anchor schema is invalid")
        if any(anchor[key] != value for key, value in expected_anchor.items()):
            raise BetdaqHeartbeatSafetyError("heartbeat anchor does not match state tip")
        if anchor["anchor_sha256"] != _digest(expected_anchor):
            raise BetdaqHeartbeatSafetyError("heartbeat anchor digest mismatch")
        return events

    def append(
        self,
        *,
        generation_id: str,
        predecessor_generation_id: str | None,
        account_context_id: str,
        state: HeartbeatState,
        threshold_ms: int | None,
        registered_action: HeartbeatAction | None,
        operation: str,
        observed_at: str,
        provider_performed_at: str | None = None,
        performed_action: HeartbeatAction | None = None,
        provider_return_code: int | None = None,
        response_sha256: str | None = None,
        reconciliation_required: bool,
    ) -> HeartbeatEvent:
        history = self.history()
        event = HeartbeatEvent(
            sequence=len(history) + 1,
            generation_id=generation_id,
            predecessor_generation_id=predecessor_generation_id,
            account_context_id=account_context_id,
            process_instance_id=_PROCESS_INSTANCE_ID,
            state=state,
            threshold_ms=threshold_ms,
            registered_action=registered_action,
            operation=operation,
            observed_at=observed_at,
            provider_performed_at=provider_performed_at,
            performed_action=performed_action,
            provider_return_code=provider_return_code,
            response_sha256=response_sha256,
            reconciliation_required=bool(reconciliation_required),
            external_pulse_masking_possible=True,
            execution_write_authorized=False,
            real_money_execution_authorized=False,
            cross_session_equivalence_proven=False,
            previous_event_sha256=(
                None if not history else history[-1].event_sha256
            ),
            event_sha256="0" * 64,
        )
        event = HeartbeatEvent(
            **{
                **asdict(event),
                "state": event.state,
                "registered_action": event.registered_action,
                "performed_action": event.performed_action,
                "event_sha256": _digest(_event_body(event)),
            }
        )
        updated = (*history, event)
        document = {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "events": [_event_to_payload(item) for item in updated],
        }
        anchor_body = {
            "schema": _ANCHOR_SCHEMA,
            "schema_version": _ANCHOR_VERSION,
            "sequence": len(updated),
            "event_sha256": event.event_sha256,
        }
        anchor = {**anchor_body, "anchor_sha256": _digest(anchor_body)}
        self._atomic_write(self.path, _canonical_json(document) + "\n")
        self._atomic_write(self.anchor_path, _canonical_json(anchor) + "\n")
        verified = self.history()
        if not verified or verified[-1].event_sha256 != event.event_sha256:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat durable publication could not be verified"
            )
        return verified[-1]

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temp = Path(handle.name)
            os.replace(temp, path)
            temp = None
        except OSError as exc:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat durable publication failed"
            ) from exc
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass


def _parse_provider_result(
    payload: bytes,
    method: str,
    observed_at: str,
) -> HeartbeatProviderEvidence:
    if method not in _ALLOWED_METHODS:
        raise BetdaqHeartbeatSafetyError("unsupported heartbeat method")
    if type(payload) is not bytes or not payload or len(payload) > 4 * 1024 * 1024:
        raise BetdaqHeartbeatSafetyError("heartbeat SOAP payload size is invalid")
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise BetdaqHeartbeatSafetyError(
            "heartbeat SOAP payload contains forbidden DTD/entity"
        )
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, UnicodeError) as exc:
        raise BetdaqHeartbeatSafetyError("heartbeat response is not valid SOAP") from exc
    if root.tag == f"{{{_SOAP11_NS}}}Envelope":
        soap_ns = _SOAP11_NS
    elif root.tag == f"{{{_SOAP12_NS}}}Envelope":
        soap_ns = _SOAP12_NS
    else:
        raise BetdaqHeartbeatSafetyError("heartbeat response has invalid SOAP envelope")
    bodies = [child for child in root if child.tag == f"{{{soap_ns}}}Body"]
    if len(bodies) != 1:
        raise BetdaqHeartbeatSafetyError("heartbeat response requires one SOAP Body")
    body = bodies[0]
    if any(child.tag == f"{{{soap_ns}}}Fault" for child in body):
        raise BetdaqHeartbeatSafetyError("BETDAQ heartbeat SOAP fault")
    responses = [
        child
        for child in body
        if child.tag == f"{{{_EXTERNAL_NS}}}{method}Response"
    ]
    if len(responses) != 1 or len(list(body)) != 1:
        raise BetdaqHeartbeatSafetyError(
            f"heartbeat response requires exact {method}Response"
        )
    results = [
        child
        for child in responses[0]
        if child.tag == f"{{{_EXTERNAL_NS}}}{method}Result"
    ]
    if len(results) != 1 or len(list(responses[0])) != 1:
        raise BetdaqHeartbeatSafetyError(
            f"heartbeat response requires exact {method}Result"
        )
    result = results[0]
    statuses = [
        child
        for child in result
        if child.tag == f"{{{_EXTERNAL_NS}}}ReturnStatus"
    ]
    if len(statuses) > 1:
        raise BetdaqHeartbeatSafetyError(
            "heartbeat result contains duplicate ReturnStatus"
        )
    return_code: int | None = None
    if statuses:
        raw = statuses[0].attrib.get("Code")
        if raw is None:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat ReturnStatus is missing Code"
            )
        try:
            return_code = int(raw, 10)
        except ValueError as exc:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat ReturnStatus Code must be integer text"
            ) from exc
    for child in result:
        if child not in statuses:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat result contains unexpected child content"
            )
    performed_at: str | None = None
    performed_action: HeartbeatAction | None = None
    if method == "Pulse" and return_code in {None, 0}:
        raw_time = result.attrib.get("PerformedAt")
        raw_action = result.attrib.get("HeartbeatAction")
        if raw_time is None or raw_action is None:
            raise BetdaqHeartbeatSafetyError(
                "successful Pulse requires PerformedAt and HeartbeatAction"
            )
        performed_at = (
            _instant(raw_time, "Pulse PerformedAt")
            .isoformat()
            .replace("+00:00", "Z")
        )
        try:
            action_code = int(raw_action, 10)
        except ValueError as exc:
            raise BetdaqHeartbeatSafetyError(
                "Pulse HeartbeatAction must be integer text"
            ) from exc
        if action_code != 0:
            try:
                performed_action = HeartbeatAction(action_code)
            except ValueError as exc:
                raise BetdaqHeartbeatSafetyError(
                    "Pulse returned unknown HeartbeatAction"
                ) from exc
    elif method != "Pulse" and result.attrib:
        raise BetdaqHeartbeatSafetyError(
            f"{method} result contains unexpected attributes"
        )
    return HeartbeatProviderEvidence(
        method=method,
        observed_at=observed_at,
        response_sha256=sha256(payload).hexdigest(),
        provider_return_code=return_code,
        provider_performed_at=performed_at,
        performed_action=performed_action,
    )


class BetdaqHeartbeatSafetyController:
    """Synchronous heartbeat controller; scheduling belongs to the product runtime."""

    def __init__(
        self,
        *,
        account_client: BetdaqAccountReadOnlyClient,
        stop_authority: ExecutionStopAuthority,
        state_path: str | Path,
    ) -> None:
        if type(account_client) is not BetdaqAccountReadOnlyClient:
            raise TypeError(
                "account_client must be the canonical BetdaqAccountReadOnlyClient"
            )
        if type(stop_authority) is not ExecutionStopAuthority:
            raise TypeError("stop_authority must be ExecutionStopAuthority")
        _require_canonical_account_transport(account_client._transport)
        self._client = account_client
        self._stop = stop_authority
        self._store = BetdaqHeartbeatSafetyStore(state_path)
        self._lease = _ProcessLease(self._store.path)
        self._closed = False
        self._apply_restart_fence()

    def __enter__(self) -> "BetdaqHeartbeatSafetyController":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self.close()

    def _context_id(self) -> str:
        context = _authenticated_account_context(
            self._client._credentials,
            self._client._venue_id,
        )
        return context.session_context_id

    def _now(self) -> str:
        value = self._client._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise BetdaqHeartbeatSafetyError(
                "canonical BETDAQ clock must return timezone-aware datetime"
            )
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _latest(self) -> HeartbeatEvent | None:
        history = self._store.history()
        return None if not history else history[-1]

    def _apply_restart_fence(self) -> None:
        latest = self._latest()
        if (
            latest is None
            or latest.process_instance_id == _PROCESS_INSTANCE_ID
            or latest.state.value not in _REMOTE_ACTIVE_STATES
        ):
            return
        self._store.append(
            generation_id=latest.generation_id,
            predecessor_generation_id=latest.predecessor_generation_id,
            account_context_id=latest.account_context_id,
            state=HeartbeatState.RESTART_FENCED,
            threshold_ms=latest.threshold_ms,
            registered_action=latest.registered_action,
            operation="LOCAL_RESTART_FENCE",
            observed_at=self._now(),
            reconciliation_required=True,
        )

    def _fence_stop_if_needed(self) -> None:
        latest = self._latest()
        if latest is None or latest.state.value not in _REMOTE_ACTIVE_STATES:
            return
        decision = self._stop.decision()
        if decision.allowed and decision.mode is ExecutionAuthorityMode.ARMED:
            return
        self._store.append(
            generation_id=latest.generation_id,
            predecessor_generation_id=latest.predecessor_generation_id,
            account_context_id=latest.account_context_id,
            state=HeartbeatState.REVOKED,
            threshold_ms=latest.threshold_ms,
            registered_action=latest.registered_action,
            operation="LOCAL_STOP_FENCE",
            observed_at=self._now(),
            reconciliation_required=True,
        )

    def status(self) -> HeartbeatSafetyStatus:
        self._require_open()
        self._fence_stop_if_needed()
        latest = self._latest()
        active = bool(
            latest is not None
            and latest.state.value in _REMOTE_ACTIVE_STATES
            and latest.process_instance_id == _PROCESS_INSTANCE_ID
        )
        return HeartbeatSafetyStatus(
            event=latest,
            provider_registration_active=active,
            local_process_owner=True,
            reconciliation_required=bool(
                latest is not None and latest.reconciliation_required
            ),
        )

    def register(
        self,
        *,
        threshold_ms: int,
        action: HeartbeatAction,
    ) -> HeartbeatEvent:
        self._require_open()
        threshold = _threshold(threshold_ms)
        if not isinstance(action, HeartbeatAction):
            raise TypeError("action must be HeartbeatAction")
        self._require_armed()
        latest = self._latest()
        if latest is not None and latest.state.value in _REMOTE_ACTIVE_STATES:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat is already active; use change_registration"
            )
        predecessor = None if latest is None else latest.generation_id
        try:
            evidence = self._call(
                "RegisterHeartbeat",
                {
                    "ThresholdMs": str(threshold),
                    "HeartbeatAction": str(int(action)),
                },
            )
        except BetdaqHeartbeatSafetyError:
            return self._degraded(
                operation="RegisterHeartbeat",
                latest=latest,
                threshold_ms=threshold,
                action=action,
            )
        if evidence.provider_return_code not in {None, 0}:
            return self._degraded(
                operation="RegisterHeartbeat",
                latest=latest,
                threshold_ms=threshold,
                action=action,
                evidence=evidence,
            )
        generation = _digest(
            {
                "kind": "betdaq-heartbeat-generation",
                "process_instance_id": _PROCESS_INSTANCE_ID,
                "account_context_id": self._context_id(),
                "predecessor_generation_id": predecessor,
                "threshold_ms": threshold,
                "action": int(action),
                "response_sha256": evidence.response_sha256,
                "observed_at": evidence.observed_at,
            }
        )
        carry_reconciliation = bool(
            latest is not None and latest.reconciliation_required
        )
        return self._store.append(
            generation_id=generation,
            predecessor_generation_id=predecessor,
            account_context_id=self._context_id(),
            state=(
                HeartbeatState.RECONCILIATION_REQUIRED
                if carry_reconciliation
                else HeartbeatState.ACTIVE
            ),
            threshold_ms=threshold,
            registered_action=action,
            operation="RegisterHeartbeat",
            observed_at=evidence.observed_at,
            provider_return_code=evidence.provider_return_code,
            response_sha256=evidence.response_sha256,
            reconciliation_required=carry_reconciliation,
        )

    def change_registration(
        self,
        *,
        threshold_ms: int,
        action: HeartbeatAction,
    ) -> HeartbeatEvent:
        self._require_open()
        threshold = _threshold(threshold_ms)
        if not isinstance(action, HeartbeatAction):
            raise TypeError("action must be HeartbeatAction")
        self._require_armed()
        latest = self._require_remote_active()
        try:
            evidence = self._call(
                "ChangeHeartbeatRegistration",
                {
                    "ThresholdMs": str(threshold),
                    "HeartbeatAction": str(int(action)),
                },
            )
        except BetdaqHeartbeatSafetyError:
            return self._degraded(
                operation="ChangeHeartbeatRegistration",
                latest=latest,
                threshold_ms=threshold,
                action=action,
            )
        if evidence.provider_return_code not in {None, 0}:
            return self._degraded(
                operation="ChangeHeartbeatRegistration",
                latest=latest,
                threshold_ms=threshold,
                action=action,
                evidence=evidence,
            )
        generation = _digest(
            {
                "kind": "betdaq-heartbeat-generation",
                "process_instance_id": _PROCESS_INSTANCE_ID,
                "account_context_id": self._context_id(),
                "predecessor_generation_id": latest.generation_id,
                "threshold_ms": threshold,
                "action": int(action),
                "response_sha256": evidence.response_sha256,
                "observed_at": evidence.observed_at,
            }
        )
        return self._store.append(
            generation_id=generation,
            predecessor_generation_id=latest.generation_id,
            account_context_id=self._context_id(),
            state=(
                HeartbeatState.RECONCILIATION_REQUIRED
                if latest.reconciliation_required
                else HeartbeatState.ACTIVE
            ),
            threshold_ms=threshold,
            registered_action=action,
            operation="ChangeHeartbeatRegistration",
            observed_at=evidence.observed_at,
            provider_return_code=evidence.provider_return_code,
            response_sha256=evidence.response_sha256,
            reconciliation_required=latest.reconciliation_required,
        )

    def pulse(self) -> HeartbeatEvent:
        self._require_open()
        self._require_armed()
        latest = self._require_remote_active()
        try:
            evidence = self._call("Pulse", {})
        except BetdaqHeartbeatSafetyError:
            return self._degraded(
                operation="Pulse",
                latest=latest,
                threshold_ms=latest.threshold_ms,
                action=latest.registered_action,
            )
        if evidence.provider_return_code == 462:
            return self._store.append(
                generation_id=latest.generation_id,
                predecessor_generation_id=latest.predecessor_generation_id,
                account_context_id=latest.account_context_id,
                state=HeartbeatState.LOST_PROVIDER_REGISTRATION,
                threshold_ms=latest.threshold_ms,
                registered_action=latest.registered_action,
                operation="Pulse",
                observed_at=evidence.observed_at,
                provider_return_code=462,
                response_sha256=evidence.response_sha256,
                reconciliation_required=True,
            )
        if evidence.provider_return_code not in {None, 0}:
            return self._degraded(
                operation="Pulse",
                latest=latest,
                threshold_ms=latest.threshold_ms,
                action=latest.registered_action,
                evidence=evidence,
            )
        previous_pulse = self._latest_pulse_for_generation(latest.generation_id)
        if (
            previous_pulse is not None
            and previous_pulse.provider_performed_at
            == evidence.provider_performed_at
        ):
            if (
                previous_pulse.response_sha256 == evidence.response_sha256
                and previous_pulse.performed_action == evidence.performed_action
            ):
                return previous_pulse
            return self._degraded(
                operation="Pulse",
                latest=latest,
                threshold_ms=latest.threshold_ms,
                action=latest.registered_action,
                evidence=evidence,
            )
        if (
            previous_pulse is not None
            and previous_pulse.provider_performed_at is not None
            and evidence.provider_performed_at is not None
            and _instant(
                evidence.provider_performed_at,
                "provider_performed_at",
            )
            < _instant(
                previous_pulse.provider_performed_at,
                "previous provider_performed_at",
            )
        ):
            return self._degraded(
                operation="Pulse",
                latest=latest,
                threshold_ms=latest.threshold_ms,
                action=latest.registered_action,
                evidence=evidence,
            )
        action_performed = evidence.performed_action
        if (
            action_performed is not None
            and action_performed is not latest.registered_action
        ):
            return self._degraded(
                operation="Pulse",
                latest=latest,
                threshold_ms=latest.threshold_ms,
                action=latest.registered_action,
                evidence=evidence,
            )
        reconciliation_required = (
            latest.reconciliation_required or action_performed is not None
        )
        return self._store.append(
            generation_id=latest.generation_id,
            predecessor_generation_id=latest.predecessor_generation_id,
            account_context_id=latest.account_context_id,
            state=(
                HeartbeatState.RECONCILIATION_REQUIRED
                if reconciliation_required
                else HeartbeatState.ACTIVE
            ),
            threshold_ms=latest.threshold_ms,
            registered_action=latest.registered_action,
            operation="Pulse",
            observed_at=evidence.observed_at,
            provider_performed_at=evidence.provider_performed_at,
            performed_action=evidence.performed_action,
            provider_return_code=evidence.provider_return_code,
            response_sha256=evidence.response_sha256,
            reconciliation_required=reconciliation_required,
        )

    def deregister(self) -> HeartbeatEvent:
        self._require_open()
        latest = self._latest()
        if latest is None:
            raise BetdaqHeartbeatSafetyError("no heartbeat generation exists")
        if latest.state not in {
            HeartbeatState.ACTIVE,
            HeartbeatState.RECONCILIATION_REQUIRED,
            HeartbeatState.DEGRADED_UNKNOWN,
            HeartbeatState.LOST_PROVIDER_REGISTRATION,
        }:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat generation is not eligible for deregistration"
            )
        try:
            evidence = self._call("DeregisterHeartbeat", {})
        except BetdaqHeartbeatSafetyError:
            return self._degraded(
                operation="DeregisterHeartbeat",
                latest=latest,
                threshold_ms=latest.threshold_ms,
                action=latest.registered_action,
            )
        if evidence.provider_return_code not in {None, 0}:
            return self._degraded(
                operation="DeregisterHeartbeat",
                latest=latest,
                threshold_ms=latest.threshold_ms,
                action=latest.registered_action,
                evidence=evidence,
            )
        return self._store.append(
            generation_id=latest.generation_id,
            predecessor_generation_id=latest.predecessor_generation_id,
            account_context_id=latest.account_context_id,
            state=HeartbeatState.UNREGISTERED,
            threshold_ms=latest.threshold_ms,
            registered_action=latest.registered_action,
            operation="DeregisterHeartbeat",
            observed_at=evidence.observed_at,
            provider_return_code=evidence.provider_return_code,
            response_sha256=evidence.response_sha256,
            reconciliation_required=latest.reconciliation_required,
        )

    def close(self) -> None:
        if self._closed:
            return
        try:
            latest = self._latest()
            if (
                latest is not None
                and latest.state.value in _REMOTE_ACTIVE_STATES
                and latest.process_instance_id == _PROCESS_INSTANCE_ID
            ):
                self._store.append(
                    generation_id=latest.generation_id,
                    predecessor_generation_id=latest.predecessor_generation_id,
                    account_context_id=latest.account_context_id,
                    state=HeartbeatState.REVOKED,
                    threshold_ms=latest.threshold_ms,
                    registered_action=latest.registered_action,
                    operation="LOCAL_CLOSE_FENCE",
                    observed_at=self._now(),
                    reconciliation_required=True,
                )
        finally:
            self._lease.close()
            self._closed = True

    def _latest_pulse_for_generation(
        self,
        generation_id: str,
    ) -> HeartbeatEvent | None:
        for event in reversed(self._store.history()):
            if event.generation_id == generation_id and event.operation == "Pulse":
                return event
        return None

    def _require_open(self) -> None:
        if self._closed:
            raise BetdaqHeartbeatSafetyError("heartbeat controller is closed")

    def _require_armed(self) -> None:
        self._fence_stop_if_needed()
        decision = self._stop.decision()
        if not decision.allowed or decision.mode is not ExecutionAuthorityMode.ARMED:
            raise BetdaqHeartbeatSafetyError(
                "execution STOP authority forbids heartbeat activation/pulse"
            )

    def _require_remote_active(self) -> HeartbeatEvent:
        latest = self._latest()
        if latest is None or latest.state.value not in _REMOTE_ACTIVE_STATES:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat provider registration is not currently proven"
            )
        if latest.process_instance_id != _PROCESS_INSTANCE_ID:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat generation belongs to another process instance"
            )
        if latest.account_context_id != self._context_id():
            raise BetdaqHeartbeatSafetyError(
                "heartbeat generation belongs to another authenticated account context"
            )
        return latest

    def _degraded(
        self,
        *,
        operation: str,
        latest: HeartbeatEvent | None,
        threshold_ms: int | None,
        action: HeartbeatAction | None,
        evidence: HeartbeatProviderEvidence | None = None,
    ) -> HeartbeatEvent:
        context_id = self._context_id()
        generation_id = (
            latest.generation_id
            if latest is not None
            else _digest(
                {
                    "kind": "betdaq-heartbeat-unresolved-generation",
                    "process_instance_id": _PROCESS_INSTANCE_ID,
                    "account_context_id": context_id,
                    "operation": operation,
                    "observed_at": (
                        evidence.observed_at if evidence is not None else self._now()
                    ),
                }
            )
        )
        return self._store.append(
            generation_id=generation_id,
            predecessor_generation_id=(
                None if latest is None else latest.predecessor_generation_id
            ),
            account_context_id=context_id,
            state=HeartbeatState.DEGRADED_UNKNOWN,
            threshold_ms=threshold_ms,
            registered_action=action,
            operation=operation,
            observed_at=(
                evidence.observed_at if evidence is not None else self._now()
            ),
            provider_performed_at=(
                None if evidence is None else evidence.provider_performed_at
            ),
            performed_action=(
                None if evidence is None else evidence.performed_action
            ),
            provider_return_code=(
                None if evidence is None else evidence.provider_return_code
            ),
            response_sha256=(
                None if evidence is None else evidence.response_sha256
            ),
            reconciliation_required=True,
        )

    def _call(
        self,
        method: str,
        request_attributes: dict[str, str],
    ) -> HeartbeatProviderEvidence:
        if method not in _ALLOWED_METHODS:
            raise BetdaqHeartbeatSafetyError("heartbeat method is not allowlisted")
        _require_canonical_account_transport(self._client._transport)
        context_before = self._context_id()
        body = self._request_xml(method, request_attributes)
        headers = {
            "Accept": "text/xml",
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{_EXTERNAL_NS}{method}"',
        }
        try:
            with self._client._call_lock:
                payload = self._client._transport.post(
                    _SECURE_ENDPOINT,
                    headers=headers,
                    body=body,
                    timeout_seconds=self._client._timeout_seconds,
                )
        except Exception:
            raise BetdaqHeartbeatSafetyError(
                "BETDAQ heartbeat transport failed"
            ) from None
        if type(payload) is not bytes:
            raise BetdaqHeartbeatSafetyError(
                "BETDAQ heartbeat transport returned non-bytes payload"
            )
        context_after = self._context_id()
        if context_after != context_before:
            raise BetdaqHeartbeatSafetyError(
                "authenticated BETDAQ account context changed during heartbeat call"
            )
        observed_at = self._now()
        try:
            return _parse_provider_result(payload, method, observed_at)
        except BetdaqHeartbeatSafetyError:
            raise
        except Exception:
            raise BetdaqHeartbeatSafetyError(
                "BETDAQ heartbeat response validation failed"
            ) from None

    def _request_xml(
        self,
        method: str,
        request_attributes: dict[str, str],
    ) -> bytes:
        expected_attrs = (
            {"ThresholdMs", "HeartbeatAction"}
            if method in {"RegisterHeartbeat", "ChangeHeartbeatRegistration"}
            else set()
        )
        if set(request_attributes) != expected_attrs:
            raise BetdaqHeartbeatSafetyError(
                "heartbeat request attributes do not match method contract"
            )
        credentials = self._client._credentials
        ET.register_namespace("soap", _SOAP11_NS)
        envelope = ET.Element(f"{{{_SOAP11_NS}}}Envelope")
        header = ET.SubElement(envelope, f"{{{_SOAP11_NS}}}Header")
        ET.SubElement(
            header,
            f"{{{_EXTERNAL_NS}}}ExternalApiHeader",
            {
                "version": credentials.version,
                "languageCode": credentials.language_code,
                "username": credentials.username,
                "password": credentials.password,
                "applicationIdentifier": credentials.application_identifier,
            },
        )
        body = ET.SubElement(envelope, f"{{{_SOAP11_NS}}}Body")
        method_element = ET.SubElement(
            body,
            f"{{{_EXTERNAL_NS}}}{method}",
        )
        ET.SubElement(
            method_element,
            f"{{{_EXTERNAL_NS}}}{_REQUEST_ELEMENT[method]}",
            request_attributes,
        )
        return ET.tostring(
            envelope,
            encoding="utf-8",
            xml_declaration=True,
        )
