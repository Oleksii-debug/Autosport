"""Durable product-owned approval resolver for provider-output governance.

Positive resolution means only that Autosport durably recorded owner approval for
this exact governance authority and that the approval was active at ``as_of``.
It does not interpret provider terms, prove a provider grant, authenticate an
external signer, or authorize provider writes / execution / real money.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import AuthorityPhase, MonotonicWorkspaceAuthority

_SCHEMA = 1
_DOMAIN = "autosport.provider-owner-approval.v1"
_HEX = frozenset("0123456789abcdef")
_STATE_KEYS = {"schema_version", "generation", "events", "state_sha256"}
_EVENT_KEYS = {
    "sequence", "event_type", "governance_authority_id", "provider_id", "service_id",
    "owner_approval_reference", "owner_approval_sha256", "occurred_at",
    "previous_event_sha256", "event_sha256",
}

class OwnerApprovalResolutionReason(StrEnum):
    APPROVED = "APPROVED"
    UNRESOLVED = "UNRESOLVED"
    NOT_YET_APPROVED = "NOT_YET_APPROVED"
    REVOKED = "REVOKED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"

def _text(value: object, name: str, limit: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    if len(value) > limit or "\x00" in value or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} contains unsupported characters")
    value.encode("utf-8")
    return value

def _sha(value: object, name: str) -> str:
    value = _text(value, name, 64)
    if len(value) != 64 or any(c not in _HEX for c in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value

def _dt(value: object, name: str) -> datetime:
    text = _text(value, name, 64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)

def _instant(value: object, name: str) -> str:
    parsed = _dt(value, name)
    text = parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds")
    return text.replace("+00:00", "Z")

def _utc_now() -> datetime:
    """Product clock boundary; tests monkeypatch this function, production callers cannot backdate issuance."""
    return datetime.now(timezone.utc)

def _now(name: str) -> str:
    current = _utc_now()
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise RuntimeError("product clock must return timezone-aware datetime")
    return _instant(current.isoformat(), name)

def _reference(value: object) -> str:
    text = _text(value, "owner_approval_reference")
    if "?" in text or "#" in text:
        raise ValueError("owner_approval_reference must not contain query/fragment")
    parsed = urlsplit(text)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("owner_approval_reference must not contain credentials")
    if parsed.scheme.lower() == "https":
        if not parsed.hostname:
            raise ValueError("HTTPS owner_approval_reference must identify a host")
    elif parsed.scheme.lower() != "urn":
        raise ValueError("owner_approval_reference must use HTTPS or URN")
    return text

def _json_bytes(payload: dict[str, Any], pretty: bool = False) -> bytes:
    kwargs: dict[str, Any] = dict(ensure_ascii=False, sort_keys=True, allow_nan=False)
    if pretty:
        text = json.dumps(payload, indent=2, **kwargs) + "\n"
    else:
        text = json.dumps(payload, separators=(",", ":"), **kwargs)
    return text.encode("utf-8")

def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_json_bytes(payload)).hexdigest()

def _event(
    sequence: int, event_type: str, authority_id: str, provider_id: str, service_id: str,
    reference: str, approval_sha: str, occurred_at: str, previous: str | None,
) -> dict[str, Any]:
    identity = {
        "sequence": sequence,
        "event_type": event_type,
        "governance_authority_id": _sha(authority_id, "governance_authority_id"),
        "provider_id": _text(provider_id, "provider_id"),
        "service_id": _text(service_id, "service_id"),
        "owner_approval_reference": _reference(reference),
        "owner_approval_sha256": _sha(approval_sha, "owner_approval_sha256"),
        "occurred_at": _instant(occurred_at, "occurred_at"),
        "previous_event_sha256": None if previous is None else _sha(previous, "previous_event_sha256"),
    }
    return {**identity, "event_sha256": _hash(identity)}

@dataclass(frozen=True, slots=True, init=False)
class OwnerApprovalResolution:
    approved: bool
    reason: OwnerApprovalResolutionReason
    governance_authority_id: str
    provider_id: str
    service_id: str
    owner_approval_reference: str
    owner_approval_sha256: str
    as_of: str
    approval_event_sha256: str | None
    revocation_event_sha256: str | None
    store_state_sha256: str | None

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("OwnerApprovalResolution is product-issued; use OwnerApprovalStore.resolve()")

def _resolution(**values: Any) -> OwnerApprovalResolution:
    result = object.__new__(OwnerApprovalResolution)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    return result

class OwnerApprovalStore:
    def __init__(self, path: str | Path, *, authority_root: str | Path | None = None) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("owner approval store path must be absolute")
        self._authority = MonotonicWorkspaceAuthority(
            workspace=self.path.parent, domain=_DOMAIN, key=self.path.name,
            authority_root=authority_root,
        )

    def approve(
        self, *, governance_authority_id: str, provider_id: str, service_id: str,
        owner_approval_reference: str, owner_approval_sha256: str,
    ) -> OwnerApprovalResolution:
        identity = (
            _sha(governance_authority_id, "governance_authority_id"),
            _text(provider_id, "provider_id"),
            _text(service_id, "service_id"),
            _reference(owner_approval_reference),
            _sha(owner_approval_sha256, "owner_approval_sha256"),
        )
        with durable_path_lock(self.path):
            state = self._read_locked()
            events = self._for_authority(state, identity[0])
            if events:
                e = events[0]
                exact = len(events) == 1 and e["event_type"] == "APPROVED" and (
                    e["provider_id"], e["service_id"], e["owner_approval_reference"],
                    e["owner_approval_sha256"]
                ) == identity[1:]
                if not exact:
                    raise ValueError("governance authority already has durable history; it cannot be rebound or re-approved")
                as_of = self._event_now_locked(state, "as_of")
                return self._resolve_state(state, *identity, as_of)
            approved_at = self._event_now_locked(state, "approved_at")
            event = _event(len(state["events"]) + 1, "APPROVED", *identity, approved_at, self._previous(state))
            state = self._publish_locked(state, event)
            return self._resolve_state(state, *identity, approved_at)

    def revoke(self, *, governance_authority_id: str) -> OwnerApprovalResolution:
        authority_id = _sha(governance_authority_id, "governance_authority_id")
        with durable_path_lock(self.path):
            state = self._read_locked()
            events = self._for_authority(state, authority_id)
            if not events or events[0]["event_type"] != "APPROVED":
                raise ValueError("cannot revoke authority without durable approval")
            approved = events[0]
            revoked_at = self._event_now_locked(state, "revoked_at")
            if len(events) == 2:
                if events[1]["event_type"] == "REVOKED":
                    return self._resolve_state(state, authority_id, approved["provider_id"], approved["service_id"], approved["owner_approval_reference"], approved["owner_approval_sha256"], revoked_at)
                raise ValueError("governance authority has invalid lifecycle history")
            if _dt(revoked_at, "revoked_at") <= _dt(approved["occurred_at"], "approved_at"):
                raise ValueError("revoked_at must be later than approved_at")
            event = _event(len(state["events"]) + 1, "REVOKED", authority_id, approved["provider_id"], approved["service_id"], approved["owner_approval_reference"], approved["owner_approval_sha256"], revoked_at, self._previous(state))
            state = self._publish_locked(state, event)
            return self._resolve_state(state, authority_id, approved["provider_id"], approved["service_id"], approved["owner_approval_reference"], approved["owner_approval_sha256"], revoked_at)

    def resolve(
        self, *, governance_authority_id: str, provider_id: str, service_id: str,
        owner_approval_reference: str, owner_approval_sha256: str, as_of: str,
    ) -> OwnerApprovalResolution:
        values = self._inputs(
            governance_authority_id, provider_id, service_id,
            owner_approval_reference, owner_approval_sha256, as_of, "as_of",
        )
        with durable_path_lock(self.path):
            return self._resolve_state(self._read_locked(), *values)

    @staticmethod
    def _inputs(authority_id: str, provider_id: str, service_id: str, reference: str, approval_sha: str, instant: str, instant_name: str) -> tuple[str, str, str, str, str, str]:
        return (_sha(authority_id, "governance_authority_id"), _text(provider_id, "provider_id"), _text(service_id, "service_id"), _reference(reference), _sha(approval_sha, "owner_approval_sha256"), _instant(instant, instant_name))

    @staticmethod
    def _event_now_locked(state: dict[str, Any], name: str) -> str:
        current = _now(name)
        if state["events"] and _dt(current, name) < _dt(state["events"][-1]["occurred_at"], "last_occurred_at"):
            raise RuntimeError("product clock moved backwards behind durable owner approval history")
        return current

    @staticmethod
    def _previous(state: dict[str, Any]) -> str | None:
        return None if not state["events"] else state["events"][-1]["event_sha256"]

    @staticmethod
    def _for_authority(state: dict[str, Any], authority_id: str) -> list[dict[str, Any]]:
        return [e for e in state["events"] if e["governance_authority_id"] == authority_id]

    def _resolve_state(self, state: dict[str, Any], authority_id: str, provider_id: str, service_id: str, reference: str, approval_sha: str, as_of: str) -> OwnerApprovalResolution:
        events = self._for_authority(state, authority_id)
        approval = events[0] if events else None
        revocation = events[1] if len(events) == 2 else None
        if approval is None:
            reason = OwnerApprovalResolutionReason.UNRESOLVED
        elif (approval["provider_id"], approval["service_id"], approval["owner_approval_reference"], approval["owner_approval_sha256"]) != (provider_id, service_id, reference, approval_sha):
            reason = OwnerApprovalResolutionReason.IDENTITY_MISMATCH
        elif _dt(as_of, "as_of") < _dt(approval["occurred_at"], "approved_at"):
            reason = OwnerApprovalResolutionReason.NOT_YET_APPROVED
        elif revocation is not None and _dt(as_of, "as_of") >= _dt(revocation["occurred_at"], "revoked_at"):
            reason = OwnerApprovalResolutionReason.REVOKED
        else:
            reason = OwnerApprovalResolutionReason.APPROVED
        return _resolution(
            approved=reason is OwnerApprovalResolutionReason.APPROVED, reason=reason,
            governance_authority_id=authority_id, provider_id=provider_id, service_id=service_id,
            owner_approval_reference=reference, owner_approval_sha256=approval_sha, as_of=as_of,
            approval_event_sha256=None if approval is None else approval["event_sha256"],
            revocation_event_sha256=None if revocation is None else revocation["event_sha256"],
            store_state_sha256=state["state_sha256"],
        )

    def _read_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            self._recover_locked(None)
            return {"schema_version": _SCHEMA, "generation": 0, "events": [], "state_sha256": None}
        raw = self.path.read_bytes()
        observed = hashlib.sha256(raw).hexdigest()
        try:
            payload = strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("owner approval store must be strict JSON UTF-8") from exc
        state = self._validate_state(payload)
        if raw != _json_bytes(state, pretty=True):
            raise ValueError("owner approval store JSON is not canonical")
        self._recover_locked(observed)
        return state

    def _recover_locked(self, observed: str | None) -> None:
        history = self._authority.read_history()
        pending = history[-1] if history and history[-1].phase is AuthorityPhase.PREPARE else None
        if pending is None:
            self._authority.recover(observed_state_sha256=observed)
        else:
            self._authority.recover(observed_state_sha256=observed, tx_id=pending.tx_id, semantic_binding_sha256=pending.semantic_binding_sha256)

    @staticmethod
    def _validate_state(payload: object) -> dict[str, Any]:
        if type(payload) is not dict or set(payload) != _STATE_KEYS or payload["schema_version"] != _SCHEMA:
            raise ValueError("owner approval store schema mismatch")
        generation = payload["generation"]
        events = payload["events"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0 or type(events) is not list or not events or generation != len(events):
            raise ValueError("invalid owner approval store generation/events")
        previous = None
        lifecycle: dict[str, list[str]] = {}
        approval_time: dict[str, datetime] = {}
        canonical: list[dict[str, Any]] = []
        for sequence, raw in enumerate(events, 1):
            if type(raw) is not dict or set(raw) != _EVENT_KEYS or raw["sequence"] != sequence or raw["event_type"] not in {"APPROVED", "REVOKED"}:
                raise ValueError("owner approval event schema/order mismatch")
            rebuilt = _event(sequence, raw["event_type"], raw["governance_authority_id"], raw["provider_id"], raw["service_id"], raw["owner_approval_reference"], raw["owner_approval_sha256"], raw["occurred_at"], previous)
            if raw != rebuilt:
                raise ValueError("owner approval event digest/chain mismatch")
            history = lifecycle.setdefault(raw["governance_authority_id"], [])
            if raw["event_type"] == "APPROVED":
                if history:
                    raise ValueError("authority may be approved only once")
                approval_time[raw["governance_authority_id"]] = _dt(raw["occurred_at"], "approved_at")
            else:
                if history != ["APPROVED"] or _dt(raw["occurred_at"], "revoked_at") <= approval_time[raw["governance_authority_id"]]:
                    raise ValueError("revocation requires a strictly earlier approval")
            history.append(raw["event_type"])
            canonical.append(rebuilt)
            previous = rebuilt["event_sha256"]
        identity = {"schema_version": _SCHEMA, "generation": generation, "events": canonical}
        state_sha = _sha(payload["state_sha256"], "state_sha256")
        if state_sha != _hash(identity):
            raise ValueError("state_sha256 mismatch")
        return {**identity, "state_sha256": state_sha}

    def _publish_locked(self, prior: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        identity = {"schema_version": _SCHEMA, "generation": prior["generation"] + 1, "events": prior["events"] + [event]}
        state = {**identity, "state_sha256": _hash(identity)}
        intended_bytes = _json_bytes(state, pretty=True)
        intended = hashlib.sha256(intended_bytes).hexdigest()
        observed = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
        expected = hashlib.sha256(_json_bytes(prior, pretty=True)).hexdigest() if prior["state_sha256"] is not None else None
        if observed != expected:
            raise RuntimeError("owner approval store changed during locked publication")
        binding = hashlib.sha256("\0".join((_DOMAIN, self.path.name, observed or "<PRISTINE>", intended, event["event_sha256"])).encode()).hexdigest()
        tx_id = f"owner-approval-{intended}"
        self._authority.prepare(tx_id=tx_id, observed_state_sha256=observed, intended_state_sha256=intended, semantic_binding_sha256=binding)
        atomic_write_json(self.path, state)
        published = self.path.read_bytes()
        if published != intended_bytes or hashlib.sha256(published).hexdigest() != intended:
            raise RuntimeError("published owner approval bytes do not match prepared authority state")
        self._authority.commit(tx_id=tx_id, observed_state_sha256=intended, semantic_binding_sha256=binding)
        return state

__all__ = ["OwnerApprovalResolution", "OwnerApprovalResolutionReason", "OwnerApprovalStore"]
