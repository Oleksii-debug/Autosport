from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .real_execution_ledger import ExecutionAction, ExecutionPlan


_SCHEMA_VERSION = 2
_ANCHOR_SCHEMA_VERSION = 1


class PaperExecutionRealityError(RuntimeError):
    """Base error for PAPER/SHADOW execution-reality evidence."""


class PaperExecutionIntegrityError(PaperExecutionRealityError):
    """Raised when durable PAPER execution evidence is malformed or tampered."""


class PaperExecutionStateError(PaperExecutionRealityError):
    """Raised when a run cannot safely continue from its durable state."""


class EvidenceGrade(str, Enum):
    CONFIGURED = "CONFIGURED"
    EMPIRICAL = "EMPIRICAL"
    SYNTHETIC = "SYNTHETIC"


class PaperAttemptOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class RecoveryDecision(str, Enum):
    NONE = "NONE"
    NO_EXPOSURE = "NO_EXPOSURE"
    HEDGE_REVIEW_REQUIRED = "HEDGE_REVIEW_REQUIRED"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value or "\x00" in value:
        raise ValueError(f"{name} must be non-empty canonical text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be UTF-8 encodable") from exc
    return value


def _timestamp(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _decimal(value: object, name: str, *, allow_zero: bool = False) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a finite Decimal") from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        comparator = ">= 0" if allow_zero else "> 0"
        raise ValueError(f"{name} must be finite and {comparator}")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Decimal must be finite")
    return format(value, "f")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PaperExecutionIntegrityError("value is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _parse_json_object(raw: str, *, what: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise PaperExecutionIntegrityError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                PaperExecutionIntegrityError(
                    f"non-finite JSON constant {token!r}"
                )
            ),
        )
    except PaperExecutionIntegrityError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PaperExecutionIntegrityError(f"invalid {what} JSON") from exc
    if type(value) is not dict:
        raise PaperExecutionIntegrityError(f"{what} must be a JSON object")
    return value


def _milliseconds(delta: timedelta, name: str) -> int:
    microseconds = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if microseconds < 0:
        raise ValueError(f"{name} must be non-negative")
    return microseconds // 1000


def _deterministic_int(seed_material: str, label: str, modulus: int) -> int:
    if modulus <= 0:
        raise ValueError("modulus must be positive")
    payload = f"{seed_material}\x1f{label}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % modulus


@dataclass(frozen=True, slots=True)
class PaperExecutionModelConfig:
    model_id: str
    model_version: str
    evidence_grade: EvidenceGrade
    evidence_source: str
    seed: str
    max_quote_age_ms: int
    min_delay_ms: int = 0
    max_delay_ms: int = 0
    rejected_bps: int = 0
    partial_bps: int = 0
    unknown_bps: int = 0
    partial_fill_bps: int = 5000
    max_slippage_bps: int = 0

    def __post_init__(self) -> None:
        _text(self.model_id, "model_id")
        _text(self.model_version, "model_version")
        _text(self.evidence_source, "evidence_source")
        _text(self.seed, "seed")
        if not isinstance(self.evidence_grade, EvidenceGrade):
            raise ValueError("evidence_grade must be EvidenceGrade")
        for name in (
            "max_quote_age_ms",
            "min_delay_ms",
            "max_delay_ms",
            "rejected_bps",
            "partial_bps",
            "unknown_bps",
            "partial_fill_bps",
            "max_slippage_bps",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        if self.max_delay_ms < self.min_delay_ms:
            raise ValueError("max_delay_ms must be >= min_delay_ms")
        if self.max_quote_age_ms <= 0:
            raise ValueError("max_quote_age_ms must be > 0")
        if self.rejected_bps + self.partial_bps + self.unknown_bps > 10_000:
            raise ValueError("outcome basis points must total <= 10000")
        if not 0 < self.partial_fill_bps < 10_000:
            raise ValueError("partial_fill_bps must be in 1..9999")
        if self.max_slippage_bps >= 10_000:
            raise ValueError("max_slippage_bps must be < 10000")

    @property
    def fingerprint(self) -> str:
        return _digest(
            {
                "schema": "autosport.paper_execution_model",
                "schema_version": _SCHEMA_VERSION,
                "model_id": self.model_id,
                "model_version": self.model_version,
                "evidence_grade": self.evidence_grade.value,
                "evidence_source": self.evidence_source,
                "seed": self.seed,
                "max_quote_age_ms": self.max_quote_age_ms,
                "min_delay_ms": self.min_delay_ms,
                "max_delay_ms": self.max_delay_ms,
                "rejected_bps": self.rejected_bps,
                "partial_bps": self.partial_bps,
                "unknown_bps": self.unknown_bps,
                "partial_fill_bps": self.partial_fill_bps,
                "max_slippage_bps": self.max_slippage_bps,
            }
        )


@dataclass(frozen=True, slots=True)
class PaperExecutionEvidenceRecord:
    action_id: str
    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    quote_id: str
    outcome: PaperAttemptOutcome
    observed_at: str
    evidence_grade: EvidenceGrade
    evidence_source: str
    accepted_odds: Decimal | str | int | None = None
    accepted_stake: Decimal | str | int | None = None
    suspended: bool = False
    reason: str = "observed execution evidence"

    def __post_init__(self) -> None:
        for name in (
            "action_id",
            "bookmaker_id",
            "account_id",
            "event_id",
            "market_id",
            "selection_id",
            "side",
            "quote_id",
            "evidence_source",
            "reason",
        ):
            _text(getattr(self, name), name)
        _timestamp(self.observed_at, "observed_at")
        if not isinstance(self.outcome, PaperAttemptOutcome):
            raise ValueError("outcome must be PaperAttemptOutcome")
        if self.evidence_grade not in {EvidenceGrade.CONFIGURED, EvidenceGrade.EMPIRICAL}:
            raise ValueError("registered execution evidence must be CONFIGURED or EMPIRICAL")
        if type(self.suspended) is not bool:
            raise ValueError("suspended must be bool")
        if self.outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}:
            if self.accepted_odds is None or self.accepted_stake is None:
                raise ValueError("accepted/partial evidence requires odds and stake")
            odds = _decimal(self.accepted_odds, "accepted_odds")
            stake = _decimal(self.accepted_stake, "accepted_stake")
            if odds <= 1:
                raise ValueError("accepted_odds must be > 1")
            object.__setattr__(self, "accepted_odds", odds)
            object.__setattr__(self, "accepted_stake", stake)
        elif self.accepted_odds is not None or self.accepted_stake is not None:
            raise ValueError("rejected/unknown evidence cannot claim accepted odds/stake")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "quote_id": self.quote_id,
            "outcome": self.outcome.value,
            "observed_at": self.observed_at,
            "evidence_grade": self.evidence_grade.value,
            "evidence_source": self.evidence_source,
            "accepted_odds": (
                None if self.accepted_odds is None else _decimal_text(self.accepted_odds)
            ),
            "accepted_stake": (
                None if self.accepted_stake is None else _decimal_text(self.accepted_stake)
            ),
            "suspended": self.suspended,
            "reason": self.reason,
        }

    @property
    def evidence_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.paper_execution_observation_evidence",
                "schema_version": 1,
                "record": self.to_dict(),
            }
        )

    @property
    def evidence_id(self) -> str:
        return "paper-exec-evidence-v1-" + self.evidence_sha256

    def as_observation(self) -> "ObservedPaperExecution":
        return ObservedPaperExecution(
            action_id=self.action_id,
            outcome=self.outcome,
            observed_at=self.observed_at,
            evidence_grade=self.evidence_grade,
            evidence_source=self.evidence_source,
            evidence_id=self.evidence_id,
            evidence_sha256=self.evidence_sha256,
            accepted_odds=self.accepted_odds,
            accepted_stake=self.accepted_stake,
            suspended=self.suspended,
            reason=self.reason,
        )

    @classmethod
    def from_dict(cls, raw: object) -> "PaperExecutionEvidenceRecord":
        if type(raw) is not dict:
            raise PaperExecutionIntegrityError("evidence record must be an object")
        expected = {
            "action_id",
            "bookmaker_id",
            "account_id",
            "event_id",
            "market_id",
            "selection_id",
            "side",
            "quote_id",
            "outcome",
            "observed_at",
            "evidence_grade",
            "evidence_source",
            "accepted_odds",
            "accepted_stake",
            "suspended",
            "reason",
        }
        if set(raw) != expected:
            raise PaperExecutionIntegrityError("evidence record schema is invalid")
        try:
            return cls(
                action_id=raw["action_id"],
                bookmaker_id=raw["bookmaker_id"],
                account_id=raw["account_id"],
                event_id=raw["event_id"],
                market_id=raw["market_id"],
                selection_id=raw["selection_id"],
                side=raw["side"],
                quote_id=raw["quote_id"],
                outcome=PaperAttemptOutcome(raw["outcome"]),
                observed_at=raw["observed_at"],
                evidence_grade=EvidenceGrade(raw["evidence_grade"]),
                evidence_source=raw["evidence_source"],
                accepted_odds=raw["accepted_odds"],
                accepted_stake=raw["accepted_stake"],
                suspended=raw["suspended"],
                reason=raw["reason"],
            )
        except (KeyError, ValueError, InvalidOperation, TypeError) as exc:
            raise PaperExecutionIntegrityError("invalid evidence record") from exc


@dataclass(frozen=True, slots=True)
class ObservedPaperExecution:
    action_id: str
    outcome: PaperAttemptOutcome
    observed_at: str
    evidence_grade: EvidenceGrade
    evidence_source: str
    evidence_id: str
    evidence_sha256: str
    accepted_odds: Decimal | str | int | None = None
    accepted_stake: Decimal | str | int | None = None
    suspended: bool = False
    reason: str = "observed execution evidence"

    def __post_init__(self) -> None:
        _text(self.action_id, "action_id")
        _timestamp(self.observed_at, "observed_at")
        if not isinstance(self.outcome, PaperAttemptOutcome):
            raise ValueError("outcome must be PaperAttemptOutcome")
        if self.evidence_grade not in {EvidenceGrade.CONFIGURED, EvidenceGrade.EMPIRICAL}:
            raise ValueError("observed execution evidence must be CONFIGURED or EMPIRICAL")
        _text(self.evidence_source, "evidence_source")
        _text(self.evidence_id, "evidence_id")
        digest = _text(self.evidence_sha256, "evidence_sha256")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("evidence_sha256 must be lowercase SHA-256 hex")
        _text(self.reason, "reason")
        if type(self.suspended) is not bool:
            raise ValueError("suspended must be bool")
        if self.outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}:
            if self.accepted_odds is None or self.accepted_stake is None:
                raise ValueError("accepted/partial observation requires odds and stake")
            odds = _decimal(self.accepted_odds, "accepted_odds")
            stake = _decimal(self.accepted_stake, "accepted_stake")
            if odds <= 1:
                raise ValueError("accepted_odds must be > 1")
            object.__setattr__(self, "accepted_odds", odds)
            object.__setattr__(self, "accepted_stake", stake)
        elif self.accepted_odds is not None or self.accepted_stake is not None:
            raise ValueError("rejected/unknown observation cannot claim accepted odds/stake")


@dataclass(frozen=True, slots=True)
class PaperLegAttempt:
    attempt_id: str
    run_id: str
    plan_id: str
    action_id: str
    sequence: int
    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    decision_quote_id: str
    decision_odds: Decimal
    requested_stake: Decimal
    decision_observed_at: str
    execution_observed_at: str
    delay_ms: int
    quote_age_ms: int
    outcome: PaperAttemptOutcome
    execution_odds: Decimal | None
    execution_stake: Decimal | None
    suspended: bool
    evidence_grade: EvidenceGrade
    evidence_source: str
    evidence_id: str | None
    evidence_sha256: str | None
    model_fingerprint: str
    reason: str

    def __post_init__(self) -> None:
        for name in (
            "attempt_id",
            "run_id",
            "plan_id",
            "action_id",
            "bookmaker_id",
            "account_id",
            "event_id",
            "market_id",
            "selection_id",
            "side",
            "decision_quote_id",
            "evidence_source",
            "model_fingerprint",
            "reason",
        ):
            _text(getattr(self, name), name)
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("sequence must be a non-negative int")
        decision_odds = _decimal(self.decision_odds, "decision_odds")
        if decision_odds <= 1:
            raise ValueError("decision_odds must be > 1")
        object.__setattr__(self, "decision_odds", decision_odds)
        object.__setattr__(
            self,
            "requested_stake",
            _decimal(self.requested_stake, "requested_stake"),
        )
        decision_time = _timestamp(self.decision_observed_at, "decision_observed_at")
        execution_time = _timestamp(self.execution_observed_at, "execution_observed_at")
        if execution_time < decision_time:
            raise ValueError("execution evidence cannot predate decision quote")
        if type(self.delay_ms) is not int or self.delay_ms < 0:
            raise ValueError("delay_ms must be non-negative int")
        if type(self.quote_age_ms) is not int or self.quote_age_ms < 0:
            raise ValueError("quote_age_ms must be non-negative int")
        if not isinstance(self.outcome, PaperAttemptOutcome):
            raise ValueError("outcome must be PaperAttemptOutcome")
        if type(self.suspended) is not bool:
            raise ValueError("suspended must be bool")
        if not isinstance(self.evidence_grade, EvidenceGrade):
            raise ValueError("evidence_grade must be EvidenceGrade")
        if self.evidence_grade is EvidenceGrade.SYNTHETIC:
            if self.evidence_id is not None or self.evidence_sha256 is not None:
                raise ValueError("synthetic attempt cannot claim registered evidence identity")
        else:
            _text(self.evidence_id, "evidence_id")
            digest = _text(self.evidence_sha256, "evidence_sha256")
            if len(digest) != 64:
                raise ValueError("evidence_sha256 must be SHA-256 hex")
        if self.outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}:
            if self.execution_odds is None or self.execution_stake is None:
                raise ValueError("accepted/partial attempt requires execution odds and stake")
            execution_odds = _decimal(self.execution_odds, "execution_odds")
            if execution_odds <= 1:
                raise ValueError("execution_odds must be > 1")
            execution_stake = _decimal(self.execution_stake, "execution_stake")
            if execution_stake > self.requested_stake:
                raise ValueError("execution_stake cannot exceed requested_stake")
            if self.outcome is PaperAttemptOutcome.ACCEPTED and execution_stake != self.requested_stake:
                raise ValueError("ACCEPTED attempt must fill the requested stake")
            if self.outcome is PaperAttemptOutcome.PARTIAL and execution_stake >= self.requested_stake:
                raise ValueError("PARTIAL attempt must fill less than requested stake")
            object.__setattr__(self, "execution_odds", execution_odds)
            object.__setattr__(self, "execution_stake", execution_stake)
        elif self.execution_odds is not None or self.execution_stake is not None:
            raise ValueError("rejected/unknown attempt cannot claim execution odds/stake")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "action_id": self.action_id,
            "sequence": self.sequence,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "decision_quote_id": self.decision_quote_id,
            "decision_odds": _decimal_text(self.decision_odds),
            "requested_stake": _decimal_text(self.requested_stake),
            "decision_observed_at": self.decision_observed_at,
            "execution_observed_at": self.execution_observed_at,
            "delay_ms": self.delay_ms,
            "quote_age_ms": self.quote_age_ms,
            "outcome": self.outcome.value,
            "execution_odds": (
                None if self.execution_odds is None else _decimal_text(self.execution_odds)
            ),
            "execution_stake": (
                None if self.execution_stake is None else _decimal_text(self.execution_stake)
            ),
            "suspended": self.suspended,
            "evidence_grade": self.evidence_grade.value,
            "evidence_source": self.evidence_source,
            "evidence_id": self.evidence_id,
            "evidence_sha256": self.evidence_sha256,
            "model_fingerprint": self.model_fingerprint,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "PaperLegAttempt":
        if type(raw) is not dict:
            raise PaperExecutionIntegrityError("attempt payload must be an object")
        expected = {
            "attempt_id", "run_id", "plan_id", "action_id", "sequence",
            "bookmaker_id", "account_id", "event_id", "market_id", "selection_id",
            "side", "decision_quote_id", "decision_odds", "requested_stake",
            "decision_observed_at", "execution_observed_at", "delay_ms", "quote_age_ms",
            "outcome", "execution_odds", "execution_stake", "suspended",
            "evidence_grade", "evidence_source", "evidence_id", "evidence_sha256",
            "model_fingerprint", "reason",
        }
        if set(raw) != expected:
            raise PaperExecutionIntegrityError("attempt payload schema is invalid")
        try:
            return cls(
                attempt_id=raw["attempt_id"],
                run_id=raw["run_id"],
                plan_id=raw["plan_id"],
                action_id=raw["action_id"],
                sequence=raw["sequence"],
                bookmaker_id=raw["bookmaker_id"],
                account_id=raw["account_id"],
                event_id=raw["event_id"],
                market_id=raw["market_id"],
                selection_id=raw["selection_id"],
                side=raw["side"],
                decision_quote_id=raw["decision_quote_id"],
                decision_odds=Decimal(raw["decision_odds"]),
                requested_stake=Decimal(raw["requested_stake"]),
                decision_observed_at=raw["decision_observed_at"],
                execution_observed_at=raw["execution_observed_at"],
                delay_ms=raw["delay_ms"],
                quote_age_ms=raw["quote_age_ms"],
                outcome=PaperAttemptOutcome(raw["outcome"]),
                execution_odds=None if raw["execution_odds"] is None else Decimal(raw["execution_odds"]),
                execution_stake=None if raw["execution_stake"] is None else Decimal(raw["execution_stake"]),
                suspended=raw["suspended"],
                evidence_grade=EvidenceGrade(raw["evidence_grade"]),
                evidence_source=raw["evidence_source"],
                evidence_id=raw["evidence_id"],
                evidence_sha256=raw["evidence_sha256"],
                model_fingerprint=raw["model_fingerprint"],
                reason=raw["reason"],
            )
        except (KeyError, ValueError, InvalidOperation, TypeError) as exc:
            raise PaperExecutionIntegrityError("invalid attempt payload") from exc


@dataclass(frozen=True, slots=True)
class PaperExecutionRun:
    run_id: str
    trigger_id: str
    plan_id: str
    plan_fingerprint: str
    model_fingerprint: str
    started_at: str
    attempts: tuple[PaperLegAttempt, ...]
    pending_action_ids: tuple[str, ...]
    recovery_decision: RecoveryDecision
    worst_case_exposure: Decimal
    completed: bool

    def __post_init__(self) -> None:
        for name in ("run_id", "trigger_id", "plan_id", "plan_fingerprint", "model_fingerprint"):
            _text(getattr(self, name), name)
        _timestamp(self.started_at, "started_at")
        if not all(isinstance(item, PaperLegAttempt) for item in self.attempts):
            raise ValueError("attempts must contain PaperLegAttempt values")
        if len({item.action_id for item in self.attempts}) != len(self.attempts):
            raise ValueError("run attempts must have unique action ids")
        if any(type(value) is not str or not value for value in self.pending_action_ids):
            raise ValueError("pending_action_ids must contain non-empty strings")
        if not isinstance(self.recovery_decision, RecoveryDecision):
            raise ValueError("recovery_decision must be RecoveryDecision")
        object.__setattr__(
            self,
            "worst_case_exposure",
            _decimal(self.worst_case_exposure, "worst_case_exposure", allow_zero=True),
        )
        if type(self.completed) is not bool:
            raise ValueError("completed must be bool")

    @property
    def all_actions_accepted(self) -> bool:
        return (
            self.completed
            and not self.pending_action_ids
            and bool(self.attempts)
            and all(item.outcome is PaperAttemptOutcome.ACCEPTED for item in self.attempts)
        )


class PaperExecutionLedger:
    """Append-only PAPER evidence with a chained log and durable latest-root anchor."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_name(self.path.name + ".writer.lock")
        self._anchor_path = self.path.with_name(self.path.name + ".anchor.json")
        self._lock = threading.RLock()
        self._path_durable = False

    def _sync_parent_directory(self) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(self.path.parent, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _ensure_existing_path_durable(self) -> None:
        if self._path_durable or not self.path.exists():
            return
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            self._sync_parent_directory()
        except OSError as exc:
            self._path_durable = False
            raise PaperExecutionIntegrityError(
                "PAPER execution ledger durability barrier failed"
            ) from exc
        self._path_durable = True

    def _with_writer_lock(self, operation):
        with self._lock:
            try:
                fd = os.open(
                    self._lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError as exc:
                raise PaperExecutionStateError(
                    "PAPER execution writer lock exists; fail closed until "
                    "writer/crash ownership is resolved"
                ) from exc
            try:
                return operation()
            finally:
                os.close(fd)
                try:
                    self._lock_path.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _event(
        *,
        event_type: str,
        run_id: str,
        key: str,
        payload: dict[str, Any],
        sequence: int,
        previous_sha256: str | None,
    ) -> dict[str, Any]:
        body = {
            "schema_version": _SCHEMA_VERSION,
            "event_type": _text(event_type, "event_type"),
            "run_id": _text(run_id, "run_id"),
            "event_key": _text(key, "event_key"),
            "sequence": sequence,
            "previous_sha256": previous_sha256,
            "payload": payload,
        }
        return {**body, "event_sha256": _digest(body)}

    def _read_anchor_unlocked(self) -> dict[str, Any] | None:
        if not self._anchor_path.exists():
            return None
        try:
            raw = self._anchor_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise PaperExecutionIntegrityError("cannot read PAPER execution anchor") from exc
        anchor = _parse_json_object(raw, what="ledger anchor")
        expected = {
            "anchor_schema_version", "ledger_schema_version", "event_count",
            "ledger_root_sha256", "anchor_sha256",
        }
        if set(anchor) != expected:
            raise PaperExecutionIntegrityError("ledger anchor schema is invalid")
        body = {key: anchor[key] for key in expected if key != "anchor_sha256"}
        if anchor["anchor_sha256"] != _digest(body):
            raise PaperExecutionIntegrityError("ledger anchor digest mismatch")
        return anchor

    def _write_anchor_unlocked(self, events: list[dict[str, Any]]) -> None:
        root = None if not events else events[-1]["event_sha256"]
        body = {
            "anchor_schema_version": _ANCHOR_SCHEMA_VERSION,
            "ledger_schema_version": _SCHEMA_VERSION,
            "event_count": len(events),
            "ledger_root_sha256": root,
        }
        anchor = {**body, "anchor_sha256": _digest(body)}
        tmp = self._anchor_path.with_name(
            self._anchor_path.name + f".tmp-{os.getpid()}-{threading.get_ident()}"
        )
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(_canonical(anchor) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._anchor_path)
            self._sync_parent_directory()
        except OSError as exc:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise PaperExecutionIntegrityError(
                "PAPER execution anchor durability barrier failed"
            ) from exc

    def _load_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            if self._anchor_path.exists():
                anchor = self._read_anchor_unlocked()
                if anchor is None or anchor["event_count"] != 0:
                    raise PaperExecutionIntegrityError("ledger is missing but anchor claims history")
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise PaperExecutionIntegrityError("cannot read PAPER execution ledger") from exc

        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        previous_sha256: str | None = None
        expected_keys = {
            "schema_version", "event_type", "run_id", "event_key", "sequence",
            "previous_sha256", "payload", "event_sha256",
        }
        for sequence, raw in enumerate(lines):
            if not raw:
                raise PaperExecutionIntegrityError("ledger contains blank event line")
            event = _parse_json_object(raw, what="ledger event")
            if set(event) != expected_keys or event["schema_version"] != _SCHEMA_VERSION:
                raise PaperExecutionIntegrityError("ledger event schema is invalid")
            if event["sequence"] != sequence:
                raise PaperExecutionIntegrityError("ledger event sequence is not contiguous")
            if event["previous_sha256"] != previous_sha256:
                raise PaperExecutionIntegrityError("ledger event chain predecessor mismatch")
            body = {key: event[key] for key in expected_keys if key != "event_sha256"}
            if event["event_sha256"] != _digest(body):
                raise PaperExecutionIntegrityError("ledger event digest mismatch")
            key = event["event_key"]
            if type(key) is not str or not key:
                raise PaperExecutionIntegrityError("ledger event_key is invalid")
            if key in seen:
                raise PaperExecutionIntegrityError("duplicate event_key in durable ledger")
            seen.add(key)
            events.append(event)
            previous_sha256 = event["event_sha256"]

        anchor = self._read_anchor_unlocked()
        if events and anchor is None:
            raise PaperExecutionIntegrityError("non-empty ledger is missing latest-root anchor")
        if anchor is not None:
            if anchor["anchor_schema_version"] != _ANCHOR_SCHEMA_VERSION:
                raise PaperExecutionIntegrityError("unsupported ledger anchor schema")
            if anchor["ledger_schema_version"] != _SCHEMA_VERSION:
                raise PaperExecutionIntegrityError("ledger/anchor schema mismatch")
            if anchor["event_count"] != len(events):
                raise PaperExecutionIntegrityError("ledger event count does not match durable anchor")
            expected_root = None if not events else events[-1]["event_sha256"]
            if anchor["ledger_root_sha256"] != expected_root:
                raise PaperExecutionIntegrityError("ledger root does not match durable anchor")
        return events

    def events(self, run_id: str | None = None) -> tuple[dict[str, Any], ...]:
        with self._lock:
            events = self._load_unlocked()
            if run_id is None:
                return tuple(events)
            _text(run_id, "run_id")
            return tuple(event for event in events if event["run_id"] == run_id)

    def _append_event(
        self,
        *,
        event_type: str,
        run_id: str,
        key: str,
        payload: dict[str, Any],
    ) -> None:
        if event_type == "PAPER_EXPOSURE_SCOPE_BOUND":
            raise PaperExecutionIntegrityError(
                "PAPER exposure-scope event is reserved for canonical adoption authority"
            )

        def mutate() -> None:
            self._ensure_existing_path_durable()
            events = self._load_unlocked()
            by_key = {item["event_key"]: item for item in events}
            prior = by_key.get(key)
            sequence = len(events)
            previous_sha256 = None if not events else events[-1]["event_sha256"]
            event = self._event(
                event_type=event_type,
                run_id=run_id,
                key=key,
                payload=payload,
                sequence=sequence,
                previous_sha256=previous_sha256,
            )
            if prior is not None:
                comparable = dict(prior)
                comparable.pop("sequence", None)
                comparable.pop("previous_sha256", None)
                comparable.pop("event_sha256", None)
                proposed = dict(event)
                proposed.pop("sequence", None)
                proposed.pop("previous_sha256", None)
                proposed.pop("event_sha256", None)
                if comparable != proposed:
                    raise PaperExecutionIntegrityError(
                        "event_key already has different payload"
                    )
                return
            encoded = _canonical(event) + "\n"
            path_existed_before = self.path.exists()
            try:
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                if not path_existed_before or not self._path_durable:
                    self._sync_parent_directory()
                self._write_anchor_unlocked(events + [event])
            except OSError as exc:
                self._path_durable = False
                raise PaperExecutionIntegrityError(
                    "PAPER execution ledger durability barrier failed"
                ) from exc
            self._path_durable = True

        self._with_writer_lock(mutate)

    def register_observation_evidence(
        self, record: PaperExecutionEvidenceRecord
    ) -> None:
        if not isinstance(record, PaperExecutionEvidenceRecord):
            raise TypeError("record must be PaperExecutionEvidenceRecord")
        payload = {
            "evidence_id": record.evidence_id,
            "evidence_sha256": record.evidence_sha256,
            "record": record.to_dict(),
        }
        self._append_event(
            event_type="OBSERVATION_EVIDENCE_REGISTERED",
            run_id=record.evidence_id,
            key=f"evidence:{record.evidence_id}",
            payload=payload,
        )

    def resolve_observation_evidence(
        self, evidence_id: str
    ) -> PaperExecutionEvidenceRecord:
        evidence_id = _text(evidence_id, "evidence_id")
        matches = [
            event
            for event in self.events()
            if event["event_type"] == "OBSERVATION_EVIDENCE_REGISTERED"
            and event["payload"].get("evidence_id") == evidence_id
        ]
        if len(matches) != 1:
            raise PaperExecutionStateError(
                "observed execution evidence is unknown to durable registry"
            )
        payload = matches[0]["payload"]
        if set(payload) != {"evidence_id", "evidence_sha256", "record"}:
            raise PaperExecutionIntegrityError("registered evidence payload schema is invalid")
        record = PaperExecutionEvidenceRecord.from_dict(payload["record"])
        if payload["evidence_id"] != record.evidence_id:
            raise PaperExecutionIntegrityError("registered evidence id mismatch")
        if payload["evidence_sha256"] != record.evidence_sha256:
            raise PaperExecutionIntegrityError("registered evidence digest mismatch")
        return record

    def reserve_run(
        self,
        *,
        run_id: str,
        trigger_id: str,
        plan: ExecutionPlan,
        config: PaperExecutionModelConfig,
        started_at: str,
        observation_evidence_ids: Mapping[str, str],
    ) -> None:
        payload = {
            "trigger_id": trigger_id,
            "plan_id": plan.plan_id,
            "plan_fingerprint": plan.fingerprint,
            "model_fingerprint": config.fingerprint,
            "started_at": started_at,
            "action_ids": [action.action_id for action in plan.actions],
            "observation_evidence_ids": dict(sorted(observation_evidence_ids.items())),
        }
        self._append_event(
            event_type="RUN_RESERVED",
            run_id=run_id,
            key=f"{run_id}:reserve",
            payload=payload,
        )

    def record_attempt(self, attempt: PaperLegAttempt) -> None:
        self._append_event(
            event_type="ATTEMPT_RECORDED",
            run_id=attempt.run_id,
            key=f"{attempt.run_id}:attempt:{attempt.sequence}",
            payload=attempt.to_dict(),
        )

    def complete_run(
        self,
        *,
        run_id: str,
        pending_action_ids: tuple[str, ...],
        recovery_decision: RecoveryDecision,
        worst_case_exposure: Decimal,
    ) -> None:
        payload = {
            "pending_action_ids": list(pending_action_ids),
            "recovery_decision": recovery_decision.value,
            "worst_case_exposure": _decimal_text(worst_case_exposure),
        }
        self._append_event(
            event_type="RUN_COMPLETED",
            run_id=run_id,
            key=f"{run_id}:complete",
            payload=payload,
        )

    def load_run(
        self,
        *,
        run_id: str,
        trigger_id: str,
        plan: ExecutionPlan,
        config: PaperExecutionModelConfig,
        started_at: str,
        observation_evidence_ids: Mapping[str, str],
    ) -> PaperExecutionRun | None:
        events = self.events(run_id)
        if not events:
            return None
        reserve = [event for event in events if event["event_type"] == "RUN_RESERVED"]
        if len(reserve) != 1:
            raise PaperExecutionIntegrityError("run needs exactly one reservation")
        expected_reserve = {
            "trigger_id": trigger_id,
            "plan_id": plan.plan_id,
            "plan_fingerprint": plan.fingerprint,
            "model_fingerprint": config.fingerprint,
            "started_at": started_at,
            "action_ids": [action.action_id for action in plan.actions],
            "observation_evidence_ids": dict(sorted(observation_evidence_ids.items())),
        }
        if reserve[0]["payload"] != expected_reserve:
            raise PaperExecutionStateError("run identity conflicts with durable reservation")
        attempts = tuple(
            PaperLegAttempt.from_dict(event["payload"])
            for event in events
            if event["event_type"] == "ATTEMPT_RECORDED"
        )
        attempts = tuple(sorted(attempts, key=lambda item: item.sequence))
        for index, attempt in enumerate(attempts):
            if (
                attempt.sequence != index
                or index >= len(plan.actions)
                or attempt.action_id != plan.actions[index].action_id
            ):
                raise PaperExecutionIntegrityError("durable attempts are not a plan prefix")
        completions = [event for event in events if event["event_type"] == "RUN_COMPLETED"]
        if len(completions) > 1:
            raise PaperExecutionIntegrityError("run has multiple completion events")
        if completions:
            payload = completions[0]["payload"]
            try:
                recovery = RecoveryDecision(payload["recovery_decision"])
                pending = tuple(payload["pending_action_ids"])
                exposure = Decimal(payload["worst_case_exposure"])
            except (KeyError, ValueError, InvalidOperation, TypeError) as exc:
                raise PaperExecutionIntegrityError("invalid completion payload") from exc
            return PaperExecutionRun(
                run_id=run_id,
                trigger_id=trigger_id,
                plan_id=plan.plan_id,
                plan_fingerprint=plan.fingerprint,
                model_fingerprint=config.fingerprint,
                started_at=started_at,
                attempts=attempts,
                pending_action_ids=pending,
                recovery_decision=recovery,
                worst_case_exposure=exposure,
                completed=True,
            )
        known_exposure = Decimal("0")
        worst_case = Decimal("0")
        for attempt in attempts:
            if attempt.outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}:
                assert attempt.execution_stake is not None
                known_exposure += attempt.execution_stake
                worst_case = max(worst_case, known_exposure)
            elif attempt.outcome is PaperAttemptOutcome.UNKNOWN:
                worst_case = max(worst_case, known_exposure + attempt.requested_stake)
        return PaperExecutionRun(
            run_id=run_id,
            trigger_id=trigger_id,
            plan_id=plan.plan_id,
            plan_fingerprint=plan.fingerprint,
            model_fingerprint=config.fingerprint,
            started_at=started_at,
            attempts=attempts,
            pending_action_ids=tuple(action.action_id for action in plan.actions[len(attempts):]),
            recovery_decision=(
                RecoveryDecision.HEDGE_REVIEW_REQUIRED
                if worst_case > 0
                else RecoveryDecision.NONE
            ),
            worst_case_exposure=worst_case,
            completed=False,
        )


class PaperExecutionEvidenceRegistry:
    """Immutable resolver for configured/empirical PAPER execution observations."""

    def __init__(self, ledger: PaperExecutionLedger) -> None:
        if not isinstance(ledger, PaperExecutionLedger):
            raise TypeError("ledger must be PaperExecutionLedger")
        self._ledger = ledger

    def register(self, record: PaperExecutionEvidenceRecord) -> str:
        self._ledger.register_observation_evidence(record)
        return record.evidence_id

    def resolve(self, evidence_id: str) -> PaperExecutionEvidenceRecord:
        return self._ledger.resolve_observation_evidence(evidence_id)


def _run_id(
    plan: ExecutionPlan,
    trigger_id: str,
    config: PaperExecutionModelConfig,
) -> str:
    return "paper-exec-v2-" + _digest(
        {
            "plan_fingerprint": plan.fingerprint,
            "trigger_id": _text(trigger_id, "trigger_id"),
            "model_fingerprint": config.fingerprint,
        }
    )


def _attempt_id(run_id: str, action: ExecutionAction, sequence: int) -> str:
    return "paper-attempt-v2-" + _digest(
        {
            "run_id": run_id,
            "action_id": action.action_id,
            "sequence": sequence,
        }
    )


def _synthetic_attempt(
    *,
    run_id: str,
    plan: ExecutionPlan,
    action: ExecutionAction,
    sequence: int,
    config: PaperExecutionModelConfig,
    started_at: str,
    suspended: bool,
) -> PaperLegAttempt:
    if action.side != "BACK":
        raise PaperExecutionStateError(
            "synthetic PAPER exposure model supports BACK only; non-BACK must use "
            "explicit empirical/configured execution evidence"
        )
    start = _timestamp(started_at, "started_at")
    delay_span = config.max_delay_ms - config.min_delay_ms
    delay_ms = config.min_delay_ms
    if delay_span:
        delay_ms += _deterministic_int(
            f"{config.seed}:{run_id}:{action.action_id}",
            "delay",
            delay_span + 1,
        )
    execution_time = start + timedelta(milliseconds=delay_ms)
    decision_time = _timestamp(action.quote_observed_at, "quote_observed_at")
    quote_age_ms = _milliseconds(execution_time - decision_time, "quote age")
    expires = _timestamp(action.expires_at, "expires_at")
    reason: str
    outcome: PaperAttemptOutcome
    execution_odds: Decimal | None = None
    execution_stake: Decimal | None = None

    if suspended:
        outcome = PaperAttemptOutcome.REJECTED
        reason = "configured/synthetic suspension at execution time"
    elif execution_time >= expires:
        outcome = PaperAttemptOutcome.REJECTED
        reason = "decision quote expired before PAPER-equivalent execution"
    elif quote_age_ms > config.max_quote_age_ms:
        outcome = PaperAttemptOutcome.REJECTED
        reason = "decision quote exceeded configured PAPER freshness bound"
    else:
        bucket = _deterministic_int(
            f"{config.seed}:{run_id}:{action.action_id}",
            "outcome",
            10_000,
        )
        if bucket < config.unknown_bps:
            outcome = PaperAttemptOutcome.UNKNOWN
            reason = "deterministic execution model produced UNKNOWN"
        elif bucket < config.unknown_bps + config.rejected_bps:
            outcome = PaperAttemptOutcome.REJECTED
            reason = "deterministic execution model produced REJECTED"
        elif bucket < config.unknown_bps + config.rejected_bps + config.partial_bps:
            outcome = PaperAttemptOutcome.PARTIAL
            reason = "deterministic execution model produced PARTIAL"
        else:
            outcome = PaperAttemptOutcome.ACCEPTED
            reason = "deterministic execution model produced ACCEPTED"

        if outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}:
            slippage_bps = (
                0
                if config.max_slippage_bps == 0
                else _deterministic_int(
                    f"{config.seed}:{run_id}:{action.action_id}",
                    "slippage",
                    config.max_slippage_bps + 1,
                )
            )
            odds_margin = action.requested_odds - Decimal("1")
            execution_odds = Decimal("1") + (
                odds_margin * (Decimal(10_000 - slippage_bps) / Decimal(10_000))
            )
            execution_stake = action.requested_stake
            if outcome is PaperAttemptOutcome.PARTIAL:
                execution_stake = (
                    action.requested_stake
                    * Decimal(config.partial_fill_bps)
                    / Decimal(10_000)
                )

    return PaperLegAttempt(
        attempt_id=_attempt_id(run_id, action, sequence),
        run_id=run_id,
        plan_id=plan.plan_id,
        action_id=action.action_id,
        sequence=sequence,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        side=action.side,
        decision_quote_id=action.quote_id,
        decision_odds=action.requested_odds,
        requested_stake=action.requested_stake,
        decision_observed_at=action.quote_observed_at,
        execution_observed_at=_timestamp_text(execution_time),
        delay_ms=delay_ms,
        quote_age_ms=quote_age_ms,
        outcome=outcome,
        execution_odds=execution_odds,
        execution_stake=execution_stake,
        suspended=suspended,
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source=config.evidence_source,
        evidence_id=None,
        evidence_sha256=None,
        model_fingerprint=config.fingerprint,
        reason=reason,
    )


def _verify_observation_authority(
    *,
    action: ExecutionAction,
    observation: ObservedPaperExecution,
    registry: PaperExecutionEvidenceRegistry,
) -> PaperExecutionEvidenceRecord:
    if not isinstance(observation, ObservedPaperExecution):
        raise TypeError("observation values must be ObservedPaperExecution")
    record = registry.resolve(observation.evidence_id)
    if observation.evidence_sha256 != record.evidence_sha256:
        raise PaperExecutionStateError("observation evidence digest mismatch")
    expected = record.as_observation()
    if observation != expected:
        raise PaperExecutionStateError(
            "observation does not match immutable registered evidence"
        )
    exact_action = (
        record.action_id == action.action_id
        and record.bookmaker_id == action.bookmaker_id
        and record.account_id == action.account_id
        and record.event_id == action.event_id
        and record.market_id == action.market_id
        and record.selection_id == action.selection_id
        and record.side == action.side
        and record.quote_id == action.quote_id
    )
    if not exact_action:
        raise PaperExecutionStateError(
            "registered observation evidence does not bind exact action/quote/provider/account"
        )
    return record


def _observed_attempt(
    *,
    run_id: str,
    plan: ExecutionPlan,
    action: ExecutionAction,
    sequence: int,
    config: PaperExecutionModelConfig,
    observation: ObservedPaperExecution,
    started_at: str,
) -> PaperLegAttempt:
    if observation.action_id != action.action_id:
        raise PaperExecutionStateError("observation action_id mismatch")
    execution_time = _timestamp(observation.observed_at, "observed_at")
    decision_time = _timestamp(action.quote_observed_at, "quote_observed_at")
    delay_ms = _milliseconds(
        execution_time - _timestamp(started_at, "started_at"),
        "execution delay",
    )
    quote_age_ms = _milliseconds(execution_time - decision_time, "quote age")
    if execution_time >= _timestamp(action.expires_at, "expires_at"):
        raise PaperExecutionStateError("observed execution occurs at/after action expiry")
    if quote_age_ms > config.max_quote_age_ms:
        raise PaperExecutionStateError("observed execution violates configured quote freshness")
    if observation.suspended and observation.outcome in {
        PaperAttemptOutcome.ACCEPTED,
        PaperAttemptOutcome.PARTIAL,
    }:
        raise PaperExecutionStateError("suspended observation cannot claim a fill")
    if observation.accepted_stake is not None and observation.accepted_stake > action.requested_stake:
        raise PaperExecutionStateError("observed accepted stake exceeds requested stake")
    if (
        observation.outcome is PaperAttemptOutcome.ACCEPTED
        and observation.accepted_stake != action.requested_stake
    ):
        raise PaperExecutionStateError("ACCEPTED observation must fill requested stake")
    if (
        observation.outcome is PaperAttemptOutcome.PARTIAL
        and observation.accepted_stake is not None
        and observation.accepted_stake >= action.requested_stake
    ):
        raise PaperExecutionStateError("PARTIAL observation must fill less than requested stake")
    return PaperLegAttempt(
        attempt_id=_attempt_id(run_id, action, sequence),
        run_id=run_id,
        plan_id=plan.plan_id,
        action_id=action.action_id,
        sequence=sequence,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        side=action.side,
        decision_quote_id=action.quote_id,
        decision_odds=action.requested_odds,
        requested_stake=action.requested_stake,
        decision_observed_at=action.quote_observed_at,
        execution_observed_at=observation.observed_at,
        delay_ms=delay_ms,
        quote_age_ms=quote_age_ms,
        outcome=observation.outcome,
        execution_odds=observation.accepted_odds,
        execution_stake=observation.accepted_stake,
        suspended=observation.suspended,
        evidence_grade=observation.evidence_grade,
        evidence_source=observation.evidence_source,
        evidence_id=observation.evidence_id,
        evidence_sha256=observation.evidence_sha256,
        model_fingerprint=config.fingerprint,
        reason=observation.reason,
    )


def execute_paper_plan(
    *,
    plan: ExecutionPlan,
    trigger_id: str,
    config: PaperExecutionModelConfig,
    ledger: PaperExecutionLedger,
    started_at: str,
    observations: Mapping[str, ObservedPaperExecution] | None = None,
    evidence_registry: PaperExecutionEvidenceRegistry | None = None,
    suspended_action_ids: frozenset[str] = frozenset(),
) -> PaperExecutionRun:
    """Execute/resume one PAPER/SHADOW run without provider writes or real money."""
    if not isinstance(plan, ExecutionPlan):
        raise TypeError("plan must be ExecutionPlan")
    if not isinstance(config, PaperExecutionModelConfig):
        raise TypeError("config must be PaperExecutionModelConfig")
    if not isinstance(ledger, PaperExecutionLedger):
        raise TypeError("ledger must be PaperExecutionLedger")
    trigger_id = _text(trigger_id, "trigger_id")
    _timestamp(started_at, "started_at")
    if observations is None:
        observations = {}
    if not isinstance(observations, Mapping):
        raise TypeError("observations must be a mapping")
    action_by_id = {action.action_id: action for action in plan.actions}
    unknown_observation_ids = set(observations) - set(action_by_id)
    if unknown_observation_ids:
        raise PaperExecutionStateError("observations contain action outside execution plan")
    unknown_suspended = set(suspended_action_ids) - set(action_by_id)
    if unknown_suspended:
        raise PaperExecutionStateError("suspended_action_ids contain action outside execution plan")
    if observations and not isinstance(evidence_registry, PaperExecutionEvidenceRegistry):
        raise PaperExecutionStateError(
            "configured/empirical observations require a durable evidence registry"
        )

    observation_evidence_ids: dict[str, str] = {}
    for action_id, observation in observations.items():
        assert evidence_registry is not None
        _verify_observation_authority(
            action=action_by_id[action_id],
            observation=observation,
            registry=evidence_registry,
        )
        observation_evidence_ids[action_id] = observation.evidence_id

    run_id = _run_id(plan, trigger_id, config)
    ledger.reserve_run(
        run_id=run_id,
        trigger_id=trigger_id,
        plan=plan,
        config=config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
    )
    existing = ledger.load_run(
        run_id=run_id,
        trigger_id=trigger_id,
        plan=plan,
        config=config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
    )
    assert existing is not None
    if existing.completed:
        return existing

    attempts = list(existing.attempts)
    if attempts and attempts[-1].outcome is not PaperAttemptOutcome.ACCEPTED:
        next_index = len(attempts)
        pending = tuple(action.action_id for action in plan.actions[next_index:])
        recovery = (
            RecoveryDecision.HEDGE_REVIEW_REQUIRED
            if existing.worst_case_exposure > 0
            else RecoveryDecision.NO_EXPOSURE
        )
        ledger.complete_run(
            run_id=run_id,
            pending_action_ids=pending,
            recovery_decision=recovery,
            worst_case_exposure=existing.worst_case_exposure,
        )
        result = ledger.load_run(
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
        )
        assert result is not None
        return result

    known_exposure = Decimal("0")
    worst_case_exposure = Decimal("0")
    for prior in attempts:
        assert prior.outcome is PaperAttemptOutcome.ACCEPTED
        assert prior.execution_stake is not None
        known_exposure += prior.execution_stake
        worst_case_exposure = max(worst_case_exposure, known_exposure)

    for sequence in range(len(attempts), len(plan.actions)):
        action = plan.actions[sequence]
        if action.side != "BACK":
            raise PaperExecutionStateError(
                "PAPER execution-reality exposure model supports BACK only "
                "until canonical LAY liability authority exists"
            )
        observation = observations.get(action.action_id)
        if observation is not None:
            attempt = _observed_attempt(
                run_id=run_id,
                plan=plan,
                action=action,
                sequence=sequence,
                config=config,
                observation=observation,
                started_at=started_at,
            )
        else:
            attempt = _synthetic_attempt(
                run_id=run_id,
                plan=plan,
                action=action,
                sequence=sequence,
                config=config,
                started_at=started_at,
                suspended=action.action_id in suspended_action_ids,
            )
        ledger.record_attempt(attempt)
        attempts.append(attempt)

        if attempt.outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}:
            assert attempt.execution_stake is not None
            known_exposure += attempt.execution_stake
            worst_case_exposure = max(worst_case_exposure, known_exposure)
        elif attempt.outcome is PaperAttemptOutcome.UNKNOWN:
            worst_case_exposure = max(
                worst_case_exposure,
                known_exposure + attempt.requested_stake,
            )

        if attempt.outcome is not PaperAttemptOutcome.ACCEPTED:
            pending = tuple(
                pending_action.action_id
                for pending_action in plan.actions[sequence + 1 :]
            )
            recovery = (
                RecoveryDecision.HEDGE_REVIEW_REQUIRED
                if worst_case_exposure > 0
                else RecoveryDecision.NO_EXPOSURE
            )
            ledger.complete_run(
                run_id=run_id,
                pending_action_ids=pending,
                recovery_decision=recovery,
                worst_case_exposure=worst_case_exposure,
            )
            result = ledger.load_run(
                run_id=run_id,
                trigger_id=trigger_id,
                plan=plan,
                config=config,
                started_at=started_at,
                observation_evidence_ids=observation_evidence_ids,
            )
            assert result is not None
            return result

    ledger.complete_run(
        run_id=run_id,
        pending_action_ids=(),
        recovery_decision=RecoveryDecision.NONE,
        worst_case_exposure=worst_case_exposure,
    )
    result = ledger.load_run(
        run_id=run_id,
        trigger_id=trigger_id,
        plan=plan,
        config=config,
        started_at=started_at,
        observation_evidence_ids=observation_evidence_ids,
    )
    assert result is not None
    return result
