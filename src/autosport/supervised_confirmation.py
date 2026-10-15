from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import math
import os
import stat
from pathlib import Path
from typing import Callable, Mapping, Sequence, TypeAlias

from .integrity import atomic_write_json, durable_path_lock, ensure_durable_file
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)


_SCHEMA_VERSION = 1
_GENESIS_SHA256 = "0" * 64
_HEX = frozenset("0123456789abcdef")
_MAX_REVIEW_TTL_SECONDS = 3600
_CHECKPOINT_SCHEMA_VERSION = 2
_MONOTONIC_DOMAIN = "supervised-confirmation-authority"
_MONOTONIC_BINDING_SCHEMA = "autosport.supervised_confirmation.monotonic_binding"
_MONOTONIC_BINDING_VERSION = 1
_MONOTONIC_STATE_SCHEMA = "autosport.supervised_confirmation.monotonic_state"
_MONOTONIC_STATE_VERSION = 1
_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "sequence",
        "recorded_at",
        "event_type",
        "payload",
        "previous_sha256",
        "record_sha256",
    }
)
_LEGACY_CHECKPOINT_KEYS = frozenset(
    {"schema_version", "record_count", "last_record_sha256"}
)
_CHECKPOINT_KEYS = frozenset(
    {"schema_version", "record_count", "last_record_sha256", "clock_high_water"}
)
_REVIEW_KEYS = frozenset(
    {
        "review_id",
        "decision_id",
        "bookmaker_id",
        "account_id",
        "decision_sha256",
        "approval_evidence_sha256",
        "risk_evidence_sha256",
        "review_payload_sha256",
        "reviewed_at",
        "expires_at",
        "review_sha256",
    }
)
_CONFIRM_KEYS = frozenset(
    {
        "receipt_id",
        "review_id",
        "review_sha256",
        "confirmed_at",
        "receipt_sha256",
    }
)
_CONSUME_KEYS = frozenset(
    {"receipt_id", "review_sha256", "consumer_key", "consumed_at"}
)


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class SupervisedConfirmationError(RuntimeError):
    """Base failure for durable operator confirmation authority."""


class SupervisedConfirmationIntegrityError(SupervisedConfirmationError):
    """Persisted confirmation evidence is malformed or inconsistent."""


class SupervisedConfirmationConflictError(SupervisedConfirmationError):
    """Requested transition conflicts with durable confirmation state."""


class ConfirmationEventType(str, Enum):
    REVIEW_PREPARED = "REVIEW_PREPARED"
    REVIEW_CONFIRMED = "REVIEW_CONFIRMED"
    RECEIPT_CONSUMED = "RECEIPT_CONSUMED"


@dataclass(frozen=True, slots=True)
class SupervisedExecutionReview:
    review_id: str
    decision_id: str
    bookmaker_id: str
    account_id: str
    decision_sha256: str
    approval_evidence_sha256: str
    risk_evidence_sha256: str
    review_payload_sha256: str
    reviewed_at: str
    expires_at: str
    review_sha256: str


@dataclass(frozen=True, slots=True)
class OperatorConfirmationReceipt:
    receipt_id: str
    review_id: str
    review_sha256: str
    decision_id: str
    bookmaker_id: str
    account_id: str
    confirmed_at: str
    receipt_sha256: str
    consumed_by: str | None = None
    consumed_at: str | None = None


@dataclass(frozen=True, slots=True)
class SupervisedConfirmationBinding:
    """Immutable product-owned projection of one durable receipt and its review.

    This object is audit/provenance data only. Possessing it does not transfer
    confirmation authority; authority-bearing consumers must resolve and consume
    the receipt through this store.
    """

    receipt: OperatorConfirmationReceipt
    review: SupervisedExecutionReview


@dataclass(frozen=True, slots=True)
class _Record:
    sequence: int
    recorded_at: str
    event_type: ConfirmationEventType
    payload: dict[str, JsonValue]
    previous_sha256: str
    record_sha256: str


@dataclass(slots=True)
class _State:
    reviews: dict[str, SupervisedExecutionReview]
    receipts: dict[str, OperatorConfirmationReceipt]
    receipt_by_review: dict[str, str]
    decision_sha256_by_id: dict[str, str]
    receipt_by_decision: dict[str, str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_bytes(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        strict_json_loads(text)
        return text.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("confirmation evidence must be canonical strict JSON") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _domain_sha256(domain: str, value: object) -> str:
    return hashlib.sha256(domain.encode("utf-8") + b"\0" + _canonical_bytes(value)).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _require_sha256(name: str, value: object) -> str:
    if not _is_sha256(value):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    assert isinstance(value, str)
    return value


def _text(name: str, value: object, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty canonical text")
    if len(value) > max_length or "\x00" in value or any(ord(ch) < 32 for ch in value):
        raise ValueError(f"{name} contains unsupported characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be UTF-8 encodable") from exc
    return value


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(name: str, value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be canonical UTC text")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{name} must be valid ISO-8601 UTC") from exc
    if _timestamp(parsed) != value:
        raise ValueError(f"{name} must be canonical UTC text")
    return parsed


def _sanitize_json(value: object) -> JsonValue:
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("review payload must not contain non-finite numbers")
        return value
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("review payload strings must not contain NUL")
        return value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or key != key.strip():
                raise ValueError("review payload keys must be non-empty canonical strings")
            result[key] = _sanitize_json(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize_json(item) for item in value]
    raise TypeError("review payload must contain only JSON-compatible values")


def _assert_safe_regular_or_absent(path: Path, *, label: str) -> None:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SupervisedConfirmationIntegrityError(
            f"{label} must be a regular non-aliased file"
        )


def _decode_object(raw: str, *, label: str) -> dict[str, object]:
    try:
        value = strict_json_loads(raw)
    except (TypeError, ValueError) as exc:
        raise SupervisedConfirmationIntegrityError(
            f"{label} is not valid strict JSON"
        ) from exc
    if type(value) is not dict:
        raise SupervisedConfirmationIntegrityError(f"{label} must be a JSON object")
    return value


def _review_material(payload: Mapping[str, object]) -> dict[str, object]:
    return {key: payload[key] for key in _REVIEW_KEYS if key != "review_sha256"}


def _receipt_material(payload: Mapping[str, object]) -> dict[str, object]:
    return {key: payload[key] for key in _CONFIRM_KEYS if key != "receipt_sha256"}


class SupervisedConfirmationAuthority:
    """Durable REVIEW -> CONFIRM -> single-use receipt authority.

    This store proves product-owned confirmation provenance. It does not prove that
    a human physically operated an accessible UI, grant provider-write authority,
    or enable real-money execution by itself. Consumers must resolve receipt IDs
    through this store rather than trust caller-constructed receipt objects.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path)
        self.checkpoint_path = self.path.with_name(f"{self.path.name}.head.json")
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with durable_path_lock(self.path):
            _assert_safe_regular_or_absent(self.path, label="confirmation journal")
            _assert_safe_regular_or_absent(
                self.checkpoint_path, label="confirmation checkpoint"
            )
            ensure_durable_file(self.path)
            if not self.checkpoint_path.exists():
                if self.path.stat().st_size:
                    raise SupervisedConfirmationIntegrityError(
                        "non-empty confirmation journal is missing its checkpoint"
                    )
                atomic_write_json(
                    self.checkpoint_path,
                    self._checkpoint_payload(0, _GENESIS_SHA256, None),
                )
            records, _, digest, clock_high_water = self._load(
                recover_checkpoint=True
            )
            self._ensure_monotonic_current_locked(
                records,
                digest,
                clock_high_water,
            )

    @staticmethod
    def _checkpoint_payload(
        count: int,
        digest: str,
        clock_high_water: str | None,
    ) -> dict[str, object]:
        return {
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "record_count": count,
            "last_record_sha256": digest,
            "clock_high_water": clock_high_water,
        }

    @staticmethod
    def _monotonic_key(path: Path) -> str:
        name = os.path.normcase(path.name)
        return "supervised-confirmation-" + hashlib.sha256(
            name.encode("utf-8")
        ).hexdigest()

    def _absolute_path(self) -> Path:
        return Path(os.path.abspath(os.fspath(self.path)))

    def _monotonic_authority(self) -> MonotonicWorkspaceAuthority:
        absolute = self._absolute_path()
        return MonotonicWorkspaceAuthority(
            workspace=absolute.parent,
            domain=_MONOTONIC_DOMAIN,
            key=self._monotonic_key(absolute),
        )

    def _monotonic_binding(self) -> str:
        return _domain_sha256(
            _MONOTONIC_BINDING_SCHEMA,
            {
                "schema_version": _MONOTONIC_BINDING_VERSION,
                "state_key": self._monotonic_key(self._absolute_path()),
                "journal_schema_version": _SCHEMA_VERSION,
                "checkpoint_schema_version": _CHECKPOINT_SCHEMA_VERSION,
            },
        )

    def _monotonic_state_digest(
        self,
        records: Sequence[_Record],
        digest: str,
        clock_high_water: str | None,
    ) -> str | None:
        if not records and clock_high_water is None:
            return None
        return _domain_sha256(
            _MONOTONIC_STATE_SCHEMA,
            {
                "schema_version": _MONOTONIC_STATE_VERSION,
                "state_key": self._monotonic_key(self._absolute_path()),
                "record_count": len(records),
                "last_record_sha256": digest,
                "clock_high_water": clock_high_water,
            },
        )

    @staticmethod
    def _monotonic_tx_id(
        *,
        operation: str,
        observed_state_sha256: str | None,
        intended_state_sha256: str,
        semantic_binding_sha256: str,
        authority_tip_sha256: str | None,
    ) -> str:
        return _domain_sha256(
            "autosport.supervised-confirmation-monotonic-tx.v1",
            {
                "operation": operation,
                "observed_state_sha256": observed_state_sha256,
                "intended_state_sha256": intended_state_sha256,
                "semantic_binding_sha256": semantic_binding_sha256,
                "authority_tip_sha256": authority_tip_sha256,
            },
        )

    @staticmethod
    def _raise_monotonic_error(exc: MonotonicWorkspaceAuthorityError) -> None:
        raise SupervisedConfirmationIntegrityError(
            f"confirmation monotonic authority rejected local state: {exc}"
        ) from exc

    def _ensure_monotonic_current_locked(
        self,
        records: Sequence[_Record],
        digest: str,
        clock_high_water: str | None,
    ) -> None:
        observed = self._monotonic_state_digest(
            records, digest, clock_high_water
        )
        binding = self._monotonic_binding()
        try:
            authority = self._monotonic_authority()
            history = authority.read_history()
            if not history:
                if observed is None:
                    return
                raise SupervisedConfirmationIntegrityError(
                    "confirmation state exists without independent monotonic authority"
                )

            latest = history[-1]
            if (
                latest.phase is AuthorityPhase.PREPARE
                and observed == latest.intended_state_sha256
            ):
                authority.recover(
                    observed_state_sha256=observed,
                    tx_id=latest.tx_id,
                    semantic_binding_sha256=binding,
                )
            else:
                authority.recover(observed_state_sha256=observed)
        except MonotonicWorkspaceAuthorityError as exc:
            self._raise_monotonic_error(exc)

    def _prepare_monotonic_transition_locked(
        self,
        *,
        records: Sequence[_Record],
        digest: str,
        clock_high_water: str | None,
        intended_record_count: int,
        intended_digest: str,
        intended_clock_high_water: str | None,
        operation: str,
    ) -> tuple[MonotonicWorkspaceAuthority, str, str, str]:
        self._ensure_monotonic_current_locked(
            records,
            digest,
            clock_high_water,
        )
        observed = self._monotonic_state_digest(
            records, digest, clock_high_water
        )
        intended = _domain_sha256(
            _MONOTONIC_STATE_SCHEMA,
            {
                "schema_version": _MONOTONIC_STATE_VERSION,
                "state_key": self._monotonic_key(self._absolute_path()),
                "record_count": intended_record_count,
                "last_record_sha256": intended_digest,
                "clock_high_water": intended_clock_high_water,
            },
        )
        binding = self._monotonic_binding()
        try:
            authority = self._monotonic_authority()
            history = authority.read_history()
            tip = None if not history else history[-1].record_sha256
            tx_id = self._monotonic_tx_id(
                operation=operation,
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
                authority_tip_sha256=tip,
            )
            authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            return authority, tx_id, binding, intended
        except MonotonicWorkspaceAuthorityError as exc:
            self._raise_monotonic_error(exc)
            raise AssertionError("unreachable")

    def _commit_monotonic_transition_locked(
        self,
        *,
        authority: MonotonicWorkspaceAuthority,
        tx_id: str,
        binding: str,
        intended_state_sha256: str,
    ) -> None:
        records, _, digest, clock_high_water = self._load(
            recover_checkpoint=False
        )
        observed = self._monotonic_state_digest(
            records, digest, clock_high_water
        )
        if observed != intended_state_sha256:
            raise SupervisedConfirmationIntegrityError(
                "confirmation local state differs from monotonic PREPARE"
            )
        assert observed is not None
        try:
            authority.commit(
                tx_id=tx_id,
                observed_state_sha256=observed,
                semantic_binding_sha256=binding,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            self._raise_monotonic_error(exc)

    def _observe_clock_locked(
        self,
        records: Sequence[_Record],
        digest: str,
        clock_high_water: str | None,
        now: datetime,
    ) -> str:
        now_text = _timestamp(now)
        if clock_high_water is not None:
            prior = _parse_timestamp("clock_high_water", clock_high_water)
            if now < prior:
                raise SupervisedConfirmationConflictError(
                    "product clock moved backwards behind durable confirmation history"
                )
            if now_text == clock_high_water:
                return clock_high_water

        authority, tx_id, binding, intended = (
            self._prepare_monotonic_transition_locked(
                records=records,
                digest=digest,
                clock_high_water=clock_high_water,
                intended_record_count=len(records),
                intended_digest=digest,
                intended_clock_high_water=now_text,
                operation="ADVANCE_CLOCK_HIGH_WATER",
            )
        )
        try:
            atomic_write_json(
                self.checkpoint_path,
                self._checkpoint_payload(len(records), digest, now_text),
            )
        except OSError as exc:
            raise SupervisedConfirmationError(
                "failed to durably advance confirmation clock high-water"
            ) from exc
        self._commit_monotonic_transition_locked(
            authority=authority,
            tx_id=tx_id,
            binding=binding,
            intended_state_sha256=intended,
        )
        return now_text

    def prepare_review(
        self,
        *,
        review_id: str,
        decision_id: str,
        bookmaker_id: str,
        account_id: str,
        decision_sha256: str,
        approval_evidence_sha256: str,
        risk_evidence_sha256: str,
        review_payload: Mapping[str, object],
        ttl_seconds: int = 120,
    ) -> SupervisedExecutionReview:
        review_id = _text("review_id", review_id)
        decision_id = _text("decision_id", decision_id)
        bookmaker_id = _text("bookmaker_id", bookmaker_id)
        account_id = _text("account_id", account_id)
        decision_sha256 = _require_sha256("decision_sha256", decision_sha256)
        approval_evidence_sha256 = _require_sha256(
            "approval_evidence_sha256", approval_evidence_sha256
        )
        risk_evidence_sha256 = _require_sha256(
            "risk_evidence_sha256", risk_evidence_sha256
        )
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= _MAX_REVIEW_TTL_SECONDS:
            raise ValueError(
                f"ttl_seconds must be int in [1, {_MAX_REVIEW_TTL_SECONDS}]"
            )
        sanitized_payload = _sanitize_json(review_payload)
        if not isinstance(sanitized_payload, dict):
            raise TypeError("review_payload must be a mapping")
        review_payload_sha256 = _domain_sha256(
            "autosport.supervised-review-payload.v1", sanitized_payload
        )

        with durable_path_lock(self.path):
            records, state, digest, clock_high_water = self._load(
                recover_checkpoint=True
            )
            self._ensure_monotonic_current_locked(
                records, digest, clock_high_water
            )
            if review_id in state.reviews:
                raise SupervisedConfirmationConflictError(
                    "review_id already exists in durable confirmation authority"
                )
            bound_decision_sha = state.decision_sha256_by_id.get(decision_id)
            if bound_decision_sha is not None and bound_decision_sha != decision_sha256:
                raise SupervisedConfirmationConflictError(
                    "decision_id is already bound to different durable decision evidence"
                )
            if decision_id in state.receipt_by_decision:
                raise SupervisedConfirmationConflictError(
                    "decision already has a durable confirmation receipt"
                )
            now = self._now()
            if clock_high_water is not None and now < _parse_timestamp(
                "clock_high_water", clock_high_water
            ):
                raise SupervisedConfirmationConflictError(
                    "product clock moved backwards behind durable confirmation history"
                )
            for prior in state.reviews.values():
                if (
                    prior.decision_id == decision_id
                    and prior.review_id not in state.receipt_by_review
                    and now < _parse_timestamp("expires_at", prior.expires_at)
                ):
                    raise SupervisedConfirmationConflictError(
                        "decision already has an active unconfirmed review"
                    )
            reviewed_at = _timestamp(now)
            expires_at = _timestamp(now + timedelta(seconds=ttl_seconds))
            payload: dict[str, object] = {
                "review_id": review_id,
                "decision_id": decision_id,
                "bookmaker_id": bookmaker_id,
                "account_id": account_id,
                "decision_sha256": decision_sha256,
                "approval_evidence_sha256": approval_evidence_sha256,
                "risk_evidence_sha256": risk_evidence_sha256,
                "review_payload_sha256": review_payload_sha256,
                "reviewed_at": reviewed_at,
                "expires_at": expires_at,
            }
            payload["review_sha256"] = _domain_sha256(
                "autosport.supervised-review.v1", payload
            )
            record = self._append_locked(
                records,
                event_type=ConfirmationEventType.REVIEW_PREPARED,
                recorded_at=reviewed_at,
                payload=payload,
                digest=digest,
                clock_high_water=clock_high_water,
            )
            return self._review_from_payload(record.payload)

    def confirm_review(
        self,
        *,
        review_id: str,
        expected_review_sha256: str,
    ) -> OperatorConfirmationReceipt:
        review_id = _text("review_id", review_id)
        expected_review_sha256 = _require_sha256(
            "expected_review_sha256", expected_review_sha256
        )
        with durable_path_lock(self.path):
            records, state, digest, clock_high_water = self._load(
                recover_checkpoint=True
            )
            self._ensure_monotonic_current_locked(
                records, digest, clock_high_water
            )
            review = state.reviews.get(review_id)
            if review is None:
                raise SupervisedConfirmationConflictError("review_id is not durable")
            if review.review_sha256 != expected_review_sha256:
                raise SupervisedConfirmationConflictError(
                    "review digest changed or does not match displayed review"
                )
            if review_id in state.receipt_by_review:
                raise SupervisedConfirmationConflictError(
                    "review already has a durable confirmation receipt"
                )
            if review.decision_id in state.receipt_by_decision:
                raise SupervisedConfirmationConflictError(
                    "decision already has a durable confirmation receipt"
                )
            now = self._now()
            clock_high_water = self._observe_clock_locked(
                records, digest, clock_high_water, now
            )
            if now >= _parse_timestamp("expires_at", review.expires_at):
                raise SupervisedConfirmationConflictError(
                    "review expired before confirmation"
                )
            if now < _parse_timestamp("reviewed_at", review.reviewed_at):
                raise SupervisedConfirmationConflictError(
                    "confirmation cannot predate durable review"
                )
            confirmed_at = _timestamp(now)
            receipt_id = _domain_sha256(
                "autosport.supervised-confirmation-id.v1",
                {
                    "review_id": review.review_id,
                    "review_sha256": review.review_sha256,
                    "confirmed_at": confirmed_at,
                },
            )
            payload: dict[str, object] = {
                "receipt_id": receipt_id,
                "review_id": review.review_id,
                "review_sha256": review.review_sha256,
                "confirmed_at": confirmed_at,
            }
            payload["receipt_sha256"] = _domain_sha256(
                "autosport.supervised-confirmation-receipt.v1", payload
            )
            record = self._append_locked(
                records,
                event_type=ConfirmationEventType.REVIEW_CONFIRMED,
                recorded_at=confirmed_at,
                payload=payload,
                digest=digest,
                clock_high_water=clock_high_water,
            )
            return self._receipt_from_confirmation(record.payload, review)

    def resolve_receipt_binding(
        self,
        *,
        receipt_id: str,
        expected_review_sha256: str,
        require_unconsumed: bool = True,
    ) -> SupervisedConfirmationBinding:
        """Resolve exact durable receipt + review provenance under product authority.

        require_unconsumed=True is the authority-bearing read: it rejects an
        already-consumed or expired receipt. False is audit-only and may return
        consumed/expired history. The returned dataclass is descriptive evidence,
        not a transferable capability.
        """

        receipt_id = _require_sha256("receipt_id", receipt_id)
        expected_review_sha256 = _require_sha256(
            "expected_review_sha256", expected_review_sha256
        )
        if type(require_unconsumed) is not bool:
            raise TypeError("require_unconsumed must be bool")
        with durable_path_lock(self.path):
            records, state, digest, clock_high_water = self._load(
                recover_checkpoint=True
            )
            self._ensure_monotonic_current_locked(
                records, digest, clock_high_water
            )
            receipt = state.receipts.get(receipt_id)
            if receipt is None:
                raise SupervisedConfirmationConflictError(
                    "confirmation receipt is not durable"
                )
            if receipt.review_sha256 != expected_review_sha256:
                raise SupervisedConfirmationConflictError(
                    "confirmation receipt does not match expected review"
                )
            review = state.reviews.get(receipt.review_id)
            if review is None:
                raise SupervisedConfirmationIntegrityError(
                    "confirmation receipt references a missing durable review"
                )
            if (
                receipt.review_sha256 != review.review_sha256
                or receipt.decision_id != review.decision_id
                or receipt.bookmaker_id != review.bookmaker_id
                or receipt.account_id != review.account_id
            ):
                raise SupervisedConfirmationIntegrityError(
                    "confirmation receipt durable review binding is inconsistent"
                )
            if require_unconsumed and receipt.consumed_at is not None:
                raise SupervisedConfirmationConflictError(
                    "confirmation receipt has already been consumed"
                )
            if require_unconsumed:
                now = self._now()
                self._observe_clock_locked(
                    records,
                    digest,
                    clock_high_water,
                    now,
                )
                if now >= _parse_timestamp("expires_at", review.expires_at):
                    raise SupervisedConfirmationConflictError(
                        "confirmation receipt expired with its bound review"
                    )
            return SupervisedConfirmationBinding(
                receipt=receipt,
                review=review,
            )

    def verify_receipt(
        self,
        *,
        receipt_id: str,
        expected_review_sha256: str,
        require_unconsumed: bool = True,
    ) -> OperatorConfirmationReceipt:
        return self.resolve_receipt_binding(
            receipt_id=receipt_id,
            expected_review_sha256=expected_review_sha256,
            require_unconsumed=require_unconsumed,
        ).receipt

    def consume_receipt(
        self,
        *,
        receipt_id: str,
        expected_review_sha256: str,
        consumer_key: str,
    ) -> OperatorConfirmationReceipt:
        receipt_id = _require_sha256("receipt_id", receipt_id)
        expected_review_sha256 = _require_sha256(
            "expected_review_sha256", expected_review_sha256
        )
        consumer_key = _text("consumer_key", consumer_key, max_length=512)
        with durable_path_lock(self.path):
            records, state, digest, clock_high_water = self._load(
                recover_checkpoint=True
            )
            self._ensure_monotonic_current_locked(
                records, digest, clock_high_water
            )
            receipt = state.receipts.get(receipt_id)
            if receipt is None:
                raise SupervisedConfirmationConflictError(
                    "confirmation receipt is not durable"
                )
            if receipt.review_sha256 != expected_review_sha256:
                raise SupervisedConfirmationConflictError(
                    "confirmation receipt does not match expected review"
                )
            if receipt.consumed_at is not None:
                raise SupervisedConfirmationConflictError(
                    "confirmation receipt has already been consumed"
                )
            now = self._now()
            clock_high_water = self._observe_clock_locked(
                records, digest, clock_high_water, now
            )
            review = state.reviews[receipt.review_id]
            if now >= _parse_timestamp("expires_at", review.expires_at):
                raise SupervisedConfirmationConflictError(
                    "confirmation receipt expired with its bound review"
                )
            consumed_at = _timestamp(now)
            self._append_locked(
                records,
                event_type=ConfirmationEventType.RECEIPT_CONSUMED,
                recorded_at=consumed_at,
                payload={
                    "receipt_id": receipt.receipt_id,
                    "review_sha256": receipt.review_sha256,
                    "consumer_key": consumer_key,
                    "consumed_at": consumed_at,
                },
                digest=digest,
                clock_high_water=clock_high_water,
            )
            return OperatorConfirmationReceipt(
                receipt_id=receipt.receipt_id,
                review_id=receipt.review_id,
                review_sha256=receipt.review_sha256,
                decision_id=receipt.decision_id,
                bookmaker_id=receipt.bookmaker_id,
                account_id=receipt.account_id,
                confirmed_at=receipt.confirmed_at,
                receipt_sha256=receipt.receipt_sha256,
                consumed_by=consumer_key,
                consumed_at=consumed_at,
            )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _read_checkpoint(self) -> tuple[int, str, str | None, bool]:
        _assert_safe_regular_or_absent(
            self.checkpoint_path, label="confirmation checkpoint"
        )
        try:
            raw = self.checkpoint_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SupervisedConfirmationIntegrityError(
                "confirmation checkpoint is unreadable"
            ) from exc
        payload = _decode_object(raw, label="confirmation checkpoint")
        legacy = (
            set(payload) == _LEGACY_CHECKPOINT_KEYS
            and payload.get("schema_version") == _SCHEMA_VERSION
        )
        current = (
            set(payload) == _CHECKPOINT_KEYS
            and payload.get("schema_version") == _CHECKPOINT_SCHEMA_VERSION
        )
        if not legacy and not current:
            raise SupervisedConfirmationIntegrityError(
                "confirmation checkpoint schema is invalid"
            )
        count = payload.get("record_count")
        digest = payload.get("last_record_sha256")
        if type(count) is not int or count < 0 or not _is_sha256(digest):
            raise SupervisedConfirmationIntegrityError(
                "confirmation checkpoint values are invalid"
            )
        if (count == 0) != (digest == _GENESIS_SHA256):
            raise SupervisedConfirmationIntegrityError(
                "confirmation checkpoint genesis state is inconsistent"
            )
        high_water: str | None = None
        if current:
            raw_high_water = payload.get("clock_high_water")
            if raw_high_water is not None:
                try:
                    high_water = _timestamp(
                        _parse_timestamp("clock_high_water", raw_high_water)
                    )
                except ValueError as exc:
                    raise SupervisedConfirmationIntegrityError(str(exc)) from exc
        assert isinstance(digest, str)
        return count, digest, high_water, legacy

    def _read_records(self) -> list[_Record]:
        _assert_safe_regular_or_absent(self.path, label="confirmation journal")
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise SupervisedConfirmationIntegrityError(
                "confirmation journal is unreadable"
            ) from exc
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise SupervisedConfirmationIntegrityError(
                "confirmation journal has a truncated trailing record"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SupervisedConfirmationIntegrityError(
                "confirmation journal must be UTF-8"
            ) from exc
        records: list[_Record] = []
        previous = _GENESIS_SHA256
        for sequence, line in enumerate(text.splitlines(), start=1):
            if not line:
                raise SupervisedConfirmationIntegrityError(
                    "confirmation journal contains an empty record"
                )
            payload = _decode_object(line, label=f"confirmation record {sequence}")
            if (
                set(payload) != _RECORD_KEYS
                or payload.get("schema_version") != _SCHEMA_VERSION
                or payload.get("sequence") != sequence
            ):
                raise SupervisedConfirmationIntegrityError(
                    "confirmation record schema/sequence is invalid"
                )
            try:
                recorded_at = _timestamp(
                    _parse_timestamp("recorded_at", payload.get("recorded_at"))
                )
                event_type = ConfirmationEventType(payload.get("event_type"))
            except (TypeError, ValueError) as exc:
                raise SupervisedConfirmationIntegrityError(str(exc)) from exc
            event_payload = payload.get("payload")
            if type(event_payload) is not dict:
                raise SupervisedConfirmationIntegrityError(
                    "confirmation record payload must be an object"
                )
            previous_value = payload.get("previous_sha256")
            digest = payload.get("record_sha256")
            if previous_value != previous or not _is_sha256(digest):
                raise SupervisedConfirmationIntegrityError(
                    "confirmation hash chain is discontinuous"
                )
            material = dict(payload)
            del material["record_sha256"]
            if _sha256(material) != digest:
                raise SupervisedConfirmationIntegrityError(
                    "confirmation record digest mismatch"
                )
            assert isinstance(digest, str)
            record = _Record(
                sequence=sequence,
                recorded_at=recorded_at,
                event_type=event_type,
                payload=event_payload,
                previous_sha256=previous,
                record_sha256=digest,
            )
            records.append(record)
            previous = digest
        return records

    def _state_from_records(self, records: Sequence[_Record]) -> _State:
        state = _State(
            reviews={},
            receipts={},
            receipt_by_review={},
            decision_sha256_by_id={},
            receipt_by_decision={},
        )
        previous_recorded_at: datetime | None = None
        for record in records:
            recorded_at = _parse_timestamp("recorded_at", record.recorded_at)
            if previous_recorded_at is not None and recorded_at < previous_recorded_at:
                raise SupervisedConfirmationIntegrityError(
                    "confirmation record clock regressed"
                )
            previous_recorded_at = recorded_at

            if record.event_type is ConfirmationEventType.REVIEW_PREPARED:
                review = self._review_from_payload(record.payload)
                if record.recorded_at != review.reviewed_at:
                    raise SupervisedConfirmationIntegrityError(
                        "review record timestamp mismatches review payload"
                    )
                if review.review_id in state.reviews:
                    raise SupervisedConfirmationIntegrityError(
                        "confirmation journal reuses review_id"
                    )
                bound_decision_sha = state.decision_sha256_by_id.get(review.decision_id)
                if bound_decision_sha is not None and bound_decision_sha != review.decision_sha256:
                    raise SupervisedConfirmationIntegrityError(
                        "decision_id was rebound to different durable decision evidence"
                    )
                if review.decision_id in state.receipt_by_decision:
                    raise SupervisedConfirmationIntegrityError(
                        "confirmation journal prepares review after durable receipt"
                    )
                reviewed_instant = _parse_timestamp("reviewed_at", review.reviewed_at)
                for prior in state.reviews.values():
                    if (
                        prior.decision_id == review.decision_id
                        and prior.review_id not in state.receipt_by_review
                        and _parse_timestamp("expires_at", prior.expires_at)
                        > reviewed_instant
                    ):
                        raise SupervisedConfirmationIntegrityError(
                            "confirmation journal overlaps active reviews for one decision"
                        )
                state.decision_sha256_by_id[review.decision_id] = review.decision_sha256
                state.reviews[review.review_id] = review
                continue

            if record.event_type is ConfirmationEventType.REVIEW_CONFIRMED:
                review_id = self._payload_text(record.payload, "review_id")
                review = state.reviews.get(review_id)
                if review is None:
                    raise SupervisedConfirmationIntegrityError(
                        "confirmation references unknown review"
                    )
                receipt = self._receipt_from_confirmation(record.payload, review)
                if record.recorded_at != receipt.confirmed_at:
                    raise SupervisedConfirmationIntegrityError(
                        "confirmation record timestamp mismatches receipt"
                    )
                if review_id in state.receipt_by_review or receipt.receipt_id in state.receipts:
                    raise SupervisedConfirmationIntegrityError(
                        "review/receipt was confirmed more than once"
                    )
                if review.decision_id in state.receipt_by_decision:
                    raise SupervisedConfirmationIntegrityError(
                        "decision was confirmed more than once"
                    )
                confirmed_at = _parse_timestamp("confirmed_at", receipt.confirmed_at)
                if confirmed_at < _parse_timestamp("reviewed_at", review.reviewed_at):
                    raise SupervisedConfirmationIntegrityError(
                        "persisted confirmation predates review"
                    )
                if confirmed_at >= _parse_timestamp("expires_at", review.expires_at):
                    raise SupervisedConfirmationIntegrityError(
                        "persisted confirmation occurs at or after review expiry"
                    )
                state.receipts[receipt.receipt_id] = receipt
                state.receipt_by_review[review_id] = receipt.receipt_id
                state.receipt_by_decision[review.decision_id] = receipt.receipt_id
                continue

            if record.event_type is ConfirmationEventType.RECEIPT_CONSUMED:
                if set(record.payload) != _CONSUME_KEYS:
                    raise SupervisedConfirmationIntegrityError(
                        "receipt-consumption payload schema is invalid"
                    )
                receipt_id = self._payload_sha256(record.payload, "receipt_id")
                receipt = state.receipts.get(receipt_id)
                if receipt is None:
                    raise SupervisedConfirmationIntegrityError(
                        "consumption references unknown receipt"
                    )
                if receipt.consumed_at is not None:
                    raise SupervisedConfirmationIntegrityError(
                        "confirmation receipt was consumed more than once"
                    )
                review_sha256 = self._payload_sha256(record.payload, "review_sha256")
                if review_sha256 != receipt.review_sha256:
                    raise SupervisedConfirmationIntegrityError(
                        "consumption review digest mismatches receipt"
                    )
                consumer_key = self._payload_text(record.payload, "consumer_key", 512)
                consumed_at = self._payload_timestamp(record.payload, "consumed_at")
                if record.recorded_at != consumed_at:
                    raise SupervisedConfirmationIntegrityError(
                        "consumption record timestamp mismatches payload"
                    )
                consumed_instant = _parse_timestamp("consumed_at", consumed_at)
                if consumed_instant < _parse_timestamp("confirmed_at", receipt.confirmed_at):
                    raise SupervisedConfirmationIntegrityError(
                        "receipt consumption predates confirmation"
                    )
                review = state.reviews[receipt.review_id]
                if consumed_instant >= _parse_timestamp("expires_at", review.expires_at):
                    raise SupervisedConfirmationIntegrityError(
                        "persisted receipt consumption occurs at or after review expiry"
                    )
                state.receipts[receipt_id] = OperatorConfirmationReceipt(
                    receipt_id=receipt.receipt_id,
                    review_id=receipt.review_id,
                    review_sha256=receipt.review_sha256,
                    decision_id=receipt.decision_id,
                    bookmaker_id=receipt.bookmaker_id,
                    account_id=receipt.account_id,
                    confirmed_at=receipt.confirmed_at,
                    receipt_sha256=receipt.receipt_sha256,
                    consumed_by=consumer_key,
                    consumed_at=consumed_at,
                )
                continue

            raise AssertionError("unreachable confirmation event type")
        return state

    def _load(
        self, *, recover_checkpoint: bool
    ) -> tuple[list[_Record], _State, str, str | None]:
        records = self._read_records()
        state = self._state_from_records(records)
        checkpoint_count, checkpoint_digest, checkpoint_high_water, legacy = self._read_checkpoint()
        count = len(records)
        digest = records[-1].record_sha256 if records else _GENESIS_SHA256
        record_high_water = records[-1].recorded_at if records else None
        if checkpoint_count > count:
            raise SupervisedConfirmationIntegrityError(
                "confirmation journal was truncated behind durable checkpoint"
            )
        if checkpoint_count == count:
            if checkpoint_digest != digest:
                raise SupervisedConfirmationIntegrityError(
                    "confirmation checkpoint does not match journal head"
                )
            if not legacy and record_high_water is not None and (
                checkpoint_high_water is None
                or _parse_timestamp("clock_high_water", checkpoint_high_water)
                < _parse_timestamp("recorded_at", record_high_water)
            ):
                raise SupervisedConfirmationIntegrityError(
                    "confirmation checkpoint clock high-water is behind journal"
                )
        else:
            prefix_digest = _GENESIS_SHA256 if checkpoint_count == 0 else records[checkpoint_count - 1].record_sha256
            if checkpoint_digest != prefix_digest:
                raise SupervisedConfirmationIntegrityError(
                    "confirmation checkpoint is not a valid journal prefix"
                )
            if not recover_checkpoint:
                raise SupervisedConfirmationIntegrityError(
                    "confirmation journal has durable records beyond checkpoint"
                )

        high_water = checkpoint_high_water
        if record_high_water is not None and (
            high_water is None
            or _parse_timestamp("clock_high_water", high_water)
            < _parse_timestamp("recorded_at", record_high_water)
        ):
            high_water = record_high_water

        if legacy or checkpoint_count < count:
            if not recover_checkpoint:
                raise SupervisedConfirmationIntegrityError(
                    "confirmation checkpoint requires deterministic recovery"
                )
            atomic_write_json(
                self.checkpoint_path,
                self._checkpoint_payload(count, digest, high_water),
            )
        return records, state, digest, high_water

    def _append_locked(
        self,
        records: Sequence[_Record],
        *,
        event_type: ConfirmationEventType,
        recorded_at: str,
        payload: Mapping[str, object],
        digest: str,
        clock_high_water: str | None,
    ) -> _Record:
        recorded_instant = _parse_timestamp("recorded_at", recorded_at)
        if clock_high_water is not None and recorded_instant < _parse_timestamp(
            "clock_high_water", clock_high_water
        ):
            raise SupervisedConfirmationConflictError(
                "product clock moved backwards behind durable confirmation history"
            )
        sequence = len(records) + 1
        previous = records[-1].record_sha256 if records else _GENESIS_SHA256
        if previous != digest:
            raise SupervisedConfirmationIntegrityError(
                "confirmation append base digest changed"
            )
        event: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "sequence": sequence,
            "recorded_at": recorded_at,
            "event_type": event_type.value,
            "payload": dict(payload),
            "previous_sha256": previous,
        }
        event_digest = _sha256(event)
        event["record_sha256"] = event_digest
        candidate = _Record(
            sequence=sequence,
            recorded_at=recorded_at,
            event_type=event_type,
            payload=dict(payload),
            previous_sha256=previous,
            record_sha256=event_digest,
        )
        self._state_from_records([*records, candidate])
        next_high_water = recorded_at
        authority, tx_id, binding, intended = self._prepare_monotonic_transition_locked(
            records=records,
            digest=digest,
            clock_high_water=clock_high_water,
            intended_record_count=sequence,
            intended_digest=event_digest,
            intended_clock_high_water=next_high_water,
            operation=f"APPEND_{event_type.value}",
        )
        _assert_safe_regular_or_absent(self.path, label="confirmation journal")
        try:
            with self.path.open("ab") as handle:
                handle.write(_canonical_bytes(event) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise SupervisedConfirmationError(
                "failed to durably append confirmation record"
            ) from exc
        atomic_write_json(
            self.checkpoint_path,
            self._checkpoint_payload(sequence, event_digest, next_high_water),
        )
        self._commit_monotonic_transition_locked(
            authority=authority,
            tx_id=tx_id,
            binding=binding,
            intended_state_sha256=intended,
        )
        return candidate

    def _review_from_payload(self, payload: Mapping[str, object]) -> SupervisedExecutionReview:
        if set(payload) != _REVIEW_KEYS:
            raise SupervisedConfirmationIntegrityError("review payload schema is invalid")
        try:
            review = SupervisedExecutionReview(
                review_id=_text("review_id", payload.get("review_id")),
                decision_id=_text("decision_id", payload.get("decision_id")),
                bookmaker_id=_text("bookmaker_id", payload.get("bookmaker_id")),
                account_id=_text("account_id", payload.get("account_id")),
                decision_sha256=_require_sha256(
                    "decision_sha256", payload.get("decision_sha256")
                ),
                approval_evidence_sha256=_require_sha256(
                    "approval_evidence_sha256", payload.get("approval_evidence_sha256")
                ),
                risk_evidence_sha256=_require_sha256(
                    "risk_evidence_sha256", payload.get("risk_evidence_sha256")
                ),
                review_payload_sha256=_require_sha256(
                    "review_payload_sha256", payload.get("review_payload_sha256")
                ),
                reviewed_at=_timestamp(
                    _parse_timestamp("reviewed_at", payload.get("reviewed_at"))
                ),
                expires_at=_timestamp(
                    _parse_timestamp("expires_at", payload.get("expires_at"))
                ),
                review_sha256=_require_sha256(
                    "review_sha256", payload.get("review_sha256")
                ),
            )
        except ValueError as exc:
            raise SupervisedConfirmationIntegrityError(str(exc)) from exc
        if _parse_timestamp("expires_at", review.expires_at) <= _parse_timestamp(
            "reviewed_at", review.reviewed_at
        ):
            raise SupervisedConfirmationIntegrityError(
                "review expiry must be after review time"
            )
        expected = _domain_sha256(
            "autosport.supervised-review.v1", _review_material(payload)
        )
        if review.review_sha256 != expected:
            raise SupervisedConfirmationIntegrityError("review digest mismatch")
        return review

    def _receipt_from_confirmation(
        self,
        payload: Mapping[str, object],
        review: SupervisedExecutionReview,
    ) -> OperatorConfirmationReceipt:
        if set(payload) != _CONFIRM_KEYS:
            raise SupervisedConfirmationIntegrityError(
                "confirmation payload schema is invalid"
            )
        try:
            receipt_id = _require_sha256("receipt_id", payload.get("receipt_id"))
            review_id = _text("review_id", payload.get("review_id"))
            review_sha256 = _require_sha256(
                "review_sha256", payload.get("review_sha256")
            )
            confirmed_at = _timestamp(
                _parse_timestamp("confirmed_at", payload.get("confirmed_at"))
            )
            receipt_sha256 = _require_sha256(
                "receipt_sha256", payload.get("receipt_sha256")
            )
        except ValueError as exc:
            raise SupervisedConfirmationIntegrityError(str(exc)) from exc
        if review_id != review.review_id or review_sha256 != review.review_sha256:
            raise SupervisedConfirmationIntegrityError(
                "confirmation does not bind exact durable review"
            )
        expected_id = _domain_sha256(
            "autosport.supervised-confirmation-id.v1",
            {
                "review_id": review_id,
                "review_sha256": review_sha256,
                "confirmed_at": confirmed_at,
            },
        )
        if receipt_id != expected_id:
            raise SupervisedConfirmationIntegrityError("confirmation receipt_id mismatch")
        expected_receipt = _domain_sha256(
            "autosport.supervised-confirmation-receipt.v1",
            _receipt_material(payload),
        )
        if receipt_sha256 != expected_receipt:
            raise SupervisedConfirmationIntegrityError(
                "confirmation receipt digest mismatch"
            )
        return OperatorConfirmationReceipt(
            receipt_id=receipt_id,
            review_id=review.review_id,
            review_sha256=review.review_sha256,
            decision_id=review.decision_id,
            bookmaker_id=review.bookmaker_id,
            account_id=review.account_id,
            confirmed_at=confirmed_at,
            receipt_sha256=receipt_sha256,
        )

    @staticmethod
    def _payload_text(
        payload: Mapping[str, object], name: str, max_length: int = 256
    ) -> str:
        try:
            return _text(name, payload.get(name), max_length=max_length)
        except ValueError as exc:
            raise SupervisedConfirmationIntegrityError(str(exc)) from exc

    @staticmethod
    def _payload_sha256(payload: Mapping[str, object], name: str) -> str:
        try:
            return _require_sha256(name, payload.get(name))
        except ValueError as exc:
            raise SupervisedConfirmationIntegrityError(str(exc)) from exc

    @staticmethod
    def _payload_timestamp(payload: Mapping[str, object], name: str) -> str:
        try:
            return _timestamp(_parse_timestamp(name, payload.get(name)))
        except ValueError as exc:
            raise SupervisedConfirmationIntegrityError(str(exc)) from exc