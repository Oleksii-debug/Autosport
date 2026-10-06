from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any

from .integrity import durable_path_lock, sha256_file
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
)


_SCHEMA_VERSION = 1
_MONOTONIC_DOMAIN = "live-stream-continuity-journal"
_MONOTONIC_BINDING_SCHEMA = "autosport.live_stream_continuity.monotonic_binding.v1"
_HEX = frozenset("0123456789abcdef")


def _canonical_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be UTF-8 encodable") from exc
    return value


def _sha256_text(value: object, field_name: str) -> str:
    digest = _canonical_text(value, field_name)
    if len(digest) != 64 or any(ch not in _HEX for ch in digest):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")
    return digest


def _aware_timestamp(value: object, field_name: str) -> str:
    timestamp = _canonical_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_json_loads(payload: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is forbidden: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    try:
        loaded = json.loads(
            payload,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid continuity journal JSON") from exc
    if type(loaded) is not dict:
        raise ValueError("continuity journal row must be a JSON object")
    return loaded


@dataclass(frozen=True, slots=True)
class StreamIdentity:
    provider_id: str
    account_id: str
    adapter_id: str
    source_id: str
    sport: str
    event_id: str
    market_id: str
    stream_family: str

    def __post_init__(self) -> None:
        for field_name in (
            "provider_id",
            "account_id",
            "adapter_id",
            "source_id",
            "sport",
            "event_id",
            "market_id",
            "stream_family",
        ):
            object.__setattr__(
                self,
                field_name,
                _canonical_text(getattr(self, field_name), field_name),
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "source_id": self.source_id,
            "sport": self.sport,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "stream_family": self.stream_family,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "StreamIdentity":
        if type(raw) is not dict:
            raise ValueError("stream_identity must be a JSON object")
        expected = {
            "provider_id",
            "account_id",
            "adapter_id",
            "source_id",
            "sport",
            "event_id",
            "market_id",
            "stream_family",
        }
        if set(raw) != expected:
            raise ValueError("stream_identity schema mismatch")
        return cls(**raw)

    @property
    def identity_sha256(self) -> str:
        return _sha256_json(self.to_dict())


class StreamObservationKind(str, Enum):
    BASELINE = "baseline"
    DELTA = "delta"
    HEARTBEAT = "heartbeat"
    DISCONNECTED = "disconnected"
    RECONNECTED = "reconnected"
    RESUME_PROOF = "resume_proof"
    SUSPENDED = "suspended"
    UNSUSPENDED = "unsuspended"


@dataclass(frozen=True, slots=True)
class StreamObservation:
    """Structurally validated stream assertion; construction grants no provider authority."""

    identity: StreamIdentity
    kind: StreamObservationKind
    observed_at: str
    raw_evidence_id: str
    raw_evidence_sha256: str
    semantic_payload_sha256: str
    provider_cursor: str | None = None
    resume_from_cursor: str | None = None
    provider_sequence: int | None = None

    def __post_init__(self) -> None:
        if type(self.identity) is not StreamIdentity:
            raise TypeError("identity must be an exact StreamIdentity")
        if type(self.kind) is not StreamObservationKind:
            raise TypeError("kind must be an exact StreamObservationKind")
        object.__setattr__(
            self,
            "observed_at",
            _aware_timestamp(self.observed_at, "observed_at"),
        )
        object.__setattr__(
            self,
            "raw_evidence_id",
            _canonical_text(self.raw_evidence_id, "raw_evidence_id"),
        )
        object.__setattr__(
            self,
            "raw_evidence_sha256",
            _sha256_text(self.raw_evidence_sha256, "raw_evidence_sha256"),
        )
        object.__setattr__(
            self,
            "semantic_payload_sha256",
            _sha256_text(self.semantic_payload_sha256, "semantic_payload_sha256"),
        )
        if self.provider_cursor is not None:
            object.__setattr__(
                self,
                "provider_cursor",
                _canonical_text(self.provider_cursor, "provider_cursor"),
            )
        if self.resume_from_cursor is not None:
            object.__setattr__(
                self,
                "resume_from_cursor",
                _canonical_text(self.resume_from_cursor, "resume_from_cursor"),
            )
        if self.provider_sequence is not None:
            if type(self.provider_sequence) is not int or self.provider_sequence < 0:
                raise ValueError("provider_sequence must be a non-negative int or None")

        if self.kind is StreamObservationKind.RESUME_PROOF:
            if self.resume_from_cursor is None or self.provider_cursor is None:
                raise ValueError(
                    "resume_proof requires resume_from_cursor and provider_cursor"
                )
        elif self.resume_from_cursor is not None:
            raise ValueError("resume_from_cursor is only valid for resume_proof")

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "kind": self.kind.value,
            "observed_at": self.observed_at,
            "raw_evidence_id": self.raw_evidence_id,
            "raw_evidence_sha256": self.raw_evidence_sha256,
            "semantic_payload_sha256": self.semantic_payload_sha256,
            "provider_cursor": self.provider_cursor,
            "resume_from_cursor": self.resume_from_cursor,
            "provider_sequence": self.provider_sequence,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "StreamObservation":
        if type(raw) is not dict:
            raise ValueError("observation must be a JSON object")
        expected = {
            "identity",
            "kind",
            "observed_at",
            "raw_evidence_id",
            "raw_evidence_sha256",
            "semantic_payload_sha256",
            "provider_cursor",
            "resume_from_cursor",
            "provider_sequence",
        }
        if set(raw) != expected:
            raise ValueError("observation schema mismatch")
        try:
            kind = StreamObservationKind(raw["kind"])
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown stream observation kind") from exc
        return cls(
            identity=StreamIdentity.from_dict(raw["identity"]),
            kind=kind,
            observed_at=raw["observed_at"],
            raw_evidence_id=raw["raw_evidence_id"],
            raw_evidence_sha256=raw["raw_evidence_sha256"],
            semantic_payload_sha256=raw["semantic_payload_sha256"],
            provider_cursor=raw["provider_cursor"],
            resume_from_cursor=raw["resume_from_cursor"],
            provider_sequence=raw["provider_sequence"],
        )

    @property
    def observation_sha256(self) -> str:
        return _sha256_json(self.to_dict())


class ContinuityStatus(str, Enum):
    CONTINUOUS = "continuous"
    WAIT_DISCONNECTED = "wait_disconnected"
    WAIT_NO_BASELINE = "wait_no_baseline"
    WAIT_GAPPED = "wait_gapped"
    WAIT_SUSPENDED = "wait_suspended"


@dataclass(frozen=True, slots=True)
class ContinuitySnapshot:
    identity_sha256: str
    connected: bool
    baseline_qualified: bool
    gapped: bool
    suspended: bool
    generation: int
    last_provider_cursor: str | None
    resume_anchor_cursor: str | None
    last_provider_sequence: int | None
    record_count: int
    last_record_sha256: str | None

    @property
    def continuity_status(self) -> ContinuityStatus:
        if not self.connected:
            return ContinuityStatus.WAIT_DISCONNECTED
        if self.suspended:
            return ContinuityStatus.WAIT_SUSPENDED
        if not self.baseline_qualified:
            return ContinuityStatus.WAIT_NO_BASELINE
        if self.gapped:
            return ContinuityStatus.WAIT_GAPPED
        return ContinuityStatus.CONTINUOUS

    @property
    def continuity_qualified(self) -> bool:
        return self.continuity_status is ContinuityStatus.CONTINUOUS

    @property
    def provider_origin_verified(self) -> bool:
        # Exact local hashes, cursor relations and monotonic persistence do not prove
        # that a remote provider emitted caller-constructed StreamObservation values.
        # Positive provider-origin authority must come from a fixed acquisition issuer.
        return False

    @property
    def decision_eligible(self) -> bool:
        # Fail closed until a separate product composition binds this structural
        # continuity witness to non-caller-mintable provider acquisition authority.
        return False

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False


@dataclass(slots=True)
class _MutableState:
    connected: bool = False
    baseline_qualified: bool = False
    gapped: bool = True
    suspended: bool = False
    generation: int = 0
    last_provider_cursor: str | None = None
    resume_anchor_cursor: str | None = None
    last_provider_sequence: int | None = None
    record_count: int = 0
    last_record_sha256: str | None = None

    def clone(self) -> "_MutableState":
        return _MutableState(
            connected=self.connected,
            baseline_qualified=self.baseline_qualified,
            gapped=self.gapped,
            suspended=self.suspended,
            generation=self.generation,
            last_provider_cursor=self.last_provider_cursor,
            resume_anchor_cursor=self.resume_anchor_cursor,
            last_provider_sequence=self.last_provider_sequence,
            record_count=self.record_count,
            last_record_sha256=self.last_record_sha256,
        )


class LiveStreamContinuityJournal:
    """Durable provider-agnostic reconnect/gap structural continuity journal.

    This journal never decodes provider transport bytes and never mutates MarketMirror.
    StreamObservation values are caller-constructible assertion data, so neither their
    hashes nor this journal's durability may be promoted into remote-provider origin.
    It serializes each mutation with the repository's OS-backed durable path lock,
    validates the latest journal bytes, PREPAREs their exact successor in the independent
    MonotonicWorkspaceAuthority, fsyncs the journal row, COMMITs that exact byte image,
    and only then publishes the resulting in-memory continuity snapshot. Valid-old
    rollback/deletion therefore fails closed while the machine authority survives.

    A heartbeat is liveness evidence only. A reconnect remains structurally gapped until
    a fresh baseline or an exact resume assertion closes the prior cursor gap. Suspension
    is orthogonal to connectivity and therefore survives disconnect/reconnect/baseline.
    continuity_qualified can therefore become true, while decision_eligible and
    provider_origin_verified deliberately remain false until a separate fixed
    production acquisition issuer supplies non-caller-mintable origin authority.
    """

    def __init__(self, path: str | Path, identity: StreamIdentity) -> None:
        if type(identity) is not StreamIdentity:
            raise TypeError("identity must be an exact StreamIdentity")
        self.path = Path(path)
        self.identity = identity
        self._lock = RLock()
        self._state = _MutableState()
        self._observation_hashes: set[str] = set()
        self._sequence_facts: dict[int, tuple[str | None, str]] = {}
        self._cursor_facts: dict[str, str] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._authority_key = "continuity-" + hashlib.sha256(
            self.path.name.encode("utf-8")
        ).hexdigest()
        self._load_existing()

    def _monotonic_authority(self) -> MonotonicWorkspaceAuthority:
        # Re-resolve the workspace binding under the durable path lock instead of
        # caching an ephemeral pre-bind workspace_instance_id. This also makes the
        # protected path, not caller-supplied stream identity, own rollback history.
        return MonotonicWorkspaceAuthority(
            workspace=self.path.parent.resolve(strict=False),
            domain=_MONOTONIC_DOMAIN,
            key=self._authority_key,
        )

    def snapshot(self) -> ContinuitySnapshot:
        with self._lock:
            return self._snapshot_from(self._state)

    def _snapshot_from(self, state: _MutableState) -> ContinuitySnapshot:
        return ContinuitySnapshot(
            identity_sha256=self.identity.identity_sha256,
            connected=state.connected,
            baseline_qualified=state.baseline_qualified,
            gapped=state.gapped,
            suspended=state.suspended,
            generation=state.generation,
            last_provider_cursor=state.last_provider_cursor,
            resume_anchor_cursor=state.resume_anchor_cursor,
            last_provider_sequence=state.last_provider_sequence,
            record_count=state.record_count,
            last_record_sha256=state.last_record_sha256,
        )

    def apply(self, observation: StreamObservation) -> ContinuitySnapshot:
        if type(observation) is not StreamObservation:
            raise TypeError("observation must be an exact StreamObservation")
        if observation.identity != self.identity:
            raise ValueError("observation stream identity does not match journal identity")

        with self._lock:
            # The OS-backed path lock is intentionally outside the refresh/prepare/
            # append/commit sequence. A second process may have advanced the journal
            # since this object was opened; every mutation therefore revalidates the
            # exact latest bytes and independent monotonic authority before deriving
            # its successor.
            with durable_path_lock(self.path):
                authority = self._monotonic_authority()
                self._reload_existing_under_lock(authority)

                observation_hash = observation.observation_sha256
                if observation_hash in self._observation_hashes:
                    return self._snapshot_from(self._state)

                next_state = self._state.clone()
                next_sequences = dict(self._sequence_facts)
                next_cursors = dict(self._cursor_facts)
                self._transition(
                    next_state,
                    next_sequences,
                    next_cursors,
                    observation,
                )
                next_state.record_count += 1

                row_without_hash = {
                    "schema_version": _SCHEMA_VERSION,
                    "ordinal": next_state.record_count,
                    "stream_identity": self.identity.to_dict(),
                    "observation": observation.to_dict(),
                    "previous_record_sha256": self._state.last_record_sha256,
                }
                record_sha256 = _sha256_json(row_without_hash)
                row = {**row_without_hash, "record_sha256": record_sha256}
                encoded = (_canonical_json(row) + "\n").encode("utf-8")

                current_bytes = self.path.read_bytes() if self.path.exists() else b""
                observed_state_sha256 = (
                    hashlib.sha256(current_bytes).hexdigest()
                    if current_bytes
                    else None
                )
                intended_state_sha256 = hashlib.sha256(
                    current_bytes + encoded
                ).hexdigest()
                semantic_binding_sha256 = _sha256_json(
                    {
                        "schema": _MONOTONIC_BINDING_SCHEMA,
                        "identity_sha256": self.identity.identity_sha256,
                        "observation_sha256": observation_hash,
                        "previous_record_sha256": self._state.last_record_sha256,
                        "record_sha256": record_sha256,
                    }
                )
                authority_history = authority.read_history()
                authority_tip_sha256 = (
                    authority_history[-1].record_sha256
                    if authority_history
                    else None
                )
                tx_id = _sha256_json(
                    {
                        "operation": "APPEND",
                        "observed_state_sha256": observed_state_sha256,
                        "intended_state_sha256": intended_state_sha256,
                        "semantic_binding_sha256": semantic_binding_sha256,
                        "authority_tip_sha256": authority_tip_sha256,
                    }
                )

                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=observed_state_sha256,
                    intended_state_sha256=intended_state_sha256,
                    semantic_binding_sha256=semantic_binding_sha256,
                )
                self._append_durable(encoded)
                published_state_sha256 = sha256_file(self.path)
                if published_state_sha256 != intended_state_sha256:
                    raise RuntimeError(
                        "continuity journal bytes differ from prepared monotonic state"
                    )
                authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=intended_state_sha256,
                    semantic_binding_sha256=semantic_binding_sha256,
                )

                # Publish volatile state only after both the exact journal bytes and
                # the independent machine-state authority have durably committed.
                next_state.last_record_sha256 = record_sha256
                self._state = next_state
                self._sequence_facts = next_sequences
                self._cursor_facts = next_cursors
                self._observation_hashes.add(observation_hash)
                return self._snapshot_from(self._state)

    def _append_durable(self, encoded: bytes) -> None:
        with self.path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            descriptor = os.open(self.path.parent, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _provider_fact(observation: StreamObservation) -> tuple[str | None, str]:
        return (observation.provider_cursor, observation.semantic_payload_sha256)

    def _register_progress_fact(
        self,
        state: _MutableState,
        sequence_facts: dict[int, tuple[str | None, str]],
        cursor_facts: dict[str, str],
        observation: StreamObservation,
    ) -> bool:
        """Return True iff this provider fact may advance continuity state."""
        fact = self._provider_fact(observation)
        sequence = observation.provider_sequence
        cursor = observation.provider_cursor

        if sequence is not None:
            previous_fact = sequence_facts.get(sequence)
            if previous_fact is not None:
                if previous_fact != fact:
                    raise ValueError(
                        "conflicting provider payload reused an existing provider_sequence"
                    )
                return False
            if (
                state.last_provider_sequence is not None
                and sequence < state.last_provider_sequence
            ):
                raise ValueError("unseen provider_sequence regression is ambiguous")
            if (
                state.last_provider_sequence is not None
                and sequence == state.last_provider_sequence
            ):
                raise ValueError(
                    "provider_sequence reused without an exact prior provider fact"
                )

        if cursor is not None:
            previous_payload = cursor_facts.get(cursor)
            if previous_payload is not None:
                if previous_payload != observation.semantic_payload_sha256:
                    raise ValueError(
                        "conflicting provider payload reused an existing provider cursor"
                    )
                return False

        if sequence is not None:
            sequence_facts[sequence] = fact
            state.last_provider_sequence = sequence
        if cursor is not None:
            cursor_facts[cursor] = observation.semantic_payload_sha256
            state.last_provider_cursor = cursor
        return True

    def _reset_generation_progress(
        self,
        state: _MutableState,
        sequence_facts: dict[int, tuple[str | None, str]],
        cursor_facts: dict[str, str],
    ) -> None:
        state.generation += 1
        state.last_provider_sequence = None
        state.last_provider_cursor = None
        state.resume_anchor_cursor = None
        sequence_facts.clear()
        cursor_facts.clear()

    def _transition(
        self,
        state: _MutableState,
        sequence_facts: dict[int, tuple[str | None, str]],
        cursor_facts: dict[str, str],
        observation: StreamObservation,
    ) -> None:
        kind = observation.kind

        if kind is StreamObservationKind.BASELINE:
            self._reset_generation_progress(state, sequence_facts, cursor_facts)
            state.connected = True
            state.baseline_qualified = True
            state.gapped = False
            self._register_progress_fact(
                state, sequence_facts, cursor_facts, observation
            )
            return

        if kind is StreamObservationKind.DISCONNECTED:
            state.resume_anchor_cursor = state.last_provider_cursor
            state.connected = False
            state.gapped = True
            return

        if kind is StreamObservationKind.RECONNECTED:
            if state.resume_anchor_cursor is None:
                state.resume_anchor_cursor = state.last_provider_cursor
            state.connected = True
            state.gapped = True
            return

        if kind is StreamObservationKind.HEARTBEAT:
            state.connected = True
            return

        if kind is StreamObservationKind.SUSPENDED:
            state.suspended = True
            return

        if kind is StreamObservationKind.UNSUSPENDED:
            state.suspended = False
            return

        if kind is StreamObservationKind.RESUME_PROOF:
            if not state.connected:
                raise ValueError("resume_proof requires a connected stream")
            if not state.baseline_qualified:
                raise ValueError("resume_proof cannot replace the first baseline")
            if state.resume_anchor_cursor is None:
                raise ValueError("resume_proof requires a prior persisted cursor anchor")
            if observation.resume_from_cursor != state.resume_anchor_cursor:
                raise ValueError("resume_proof does not match the persisted cursor anchor")

            # A provider may prove that the reconnect resumed exactly at the
            # previously persisted cursor, with no intervening semantic delta.
            # That proof closes the gap but must not relabel the old cursor with
            # a new payload digest or invent sequence advancement.
            if observation.provider_cursor == state.resume_anchor_cursor:
                if (
                    observation.provider_sequence is not None
                    and observation.provider_sequence != state.last_provider_sequence
                ):
                    raise ValueError(
                        "same-cursor resume_proof cannot change provider_sequence"
                    )
                state.gapped = False
                state.resume_anchor_cursor = None
                return

            advanced = self._register_progress_fact(
                state, sequence_facts, cursor_facts, observation
            )
            if not advanced:
                raise ValueError(
                    "resume_proof must either confirm the anchor or advance continuity"
                )
            state.gapped = False
            state.resume_anchor_cursor = None
            return

        if kind is StreamObservationKind.DELTA:
            if not state.connected:
                state.connected = True
            if not state.baseline_qualified or state.gapped:
                state.gapped = True
                return
            self._register_progress_fact(
                state, sequence_facts, cursor_facts, observation
            )
            return

        raise AssertionError(f"unhandled stream observation kind: {kind!r}")

    def _load_existing(self) -> None:
        with self._lock:
            with durable_path_lock(self.path):
                authority = self._monotonic_authority()
                self._reload_existing_under_lock(authority)

    def _reload_existing_under_lock(
        self,
        authority: MonotonicWorkspaceAuthority,
    ) -> None:
        payload = self.path.read_bytes() if self.path.exists() else b""
        if payload and not payload.endswith(b"\n"):
            raise ValueError("continuity journal is truncated: missing final newline")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("continuity journal must be UTF-8") from exc

        state = _MutableState()
        observation_hashes: set[str] = set()
        sequence_facts: dict[int, tuple[str | None, str]] = {}
        cursor_facts: dict[str, str] = {}

        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line:
                raise ValueError(
                    f"continuity journal contains blank row at line {line_number}"
                )
            row = _strict_json_loads(line)
            expected = {
                "schema_version",
                "ordinal",
                "stream_identity",
                "observation",
                "previous_record_sha256",
                "record_sha256",
            }
            if set(row) != expected:
                raise ValueError("continuity journal row schema mismatch")
            if row["schema_version"] != _SCHEMA_VERSION:
                raise ValueError("unsupported continuity journal schema version")
            if type(row["ordinal"]) is not int or row["ordinal"] != line_number:
                raise ValueError("continuity journal ordinal mismatch")
            row_identity = StreamIdentity.from_dict(row["stream_identity"])
            if row_identity != self.identity:
                raise ValueError("continuity journal stream identity mismatch")
            previous = row["previous_record_sha256"]
            if previous is not None:
                previous = _sha256_text(previous, "previous_record_sha256")
            if previous != state.last_record_sha256:
                raise ValueError("continuity journal hash chain predecessor mismatch")

            record_sha256 = _sha256_text(row["record_sha256"], "record_sha256")
            unsigned = {key: row[key] for key in row if key != "record_sha256"}
            if _sha256_json(unsigned) != record_sha256:
                raise ValueError("continuity journal row hash mismatch")

            observation = StreamObservation.from_dict(row["observation"])
            if observation.identity != self.identity:
                raise ValueError("continuity observation identity mismatch")
            observation_hash = observation.observation_sha256
            if observation_hash in observation_hashes:
                raise ValueError("duplicate continuity observation persisted twice")

            self._transition(state, sequence_facts, cursor_facts, observation)
            state.record_count += 1
            state.last_record_sha256 = record_sha256
            observation_hashes.add(observation_hash)

        observed_state_sha256 = (
            hashlib.sha256(payload).hexdigest() if payload else None
        )
        history = authority.read_history()
        if history and history[-1].phase is AuthorityPhase.PREPARE:
            pending = history[-1]
            authority.recover(
                observed_state_sha256=observed_state_sha256,
                tx_id=pending.tx_id,
                semantic_binding_sha256=pending.semantic_binding_sha256,
            )
        else:
            authority.recover(
                observed_state_sha256=observed_state_sha256,
            )

        self._state = state
        self._observation_hashes = observation_hashes
        self._sequence_facts = sequence_facts
        self._cursor_facts = cursor_facts
