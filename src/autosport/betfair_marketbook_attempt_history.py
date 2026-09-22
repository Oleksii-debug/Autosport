"""Append-only structural attempt history for deterministic Betfair MarketBook reads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Mapping, Sequence

from .betfair_marketbook_batch_completeness import (
    BatchReceiptStatus,
    MarketBookBatchReceipt,
    MarketBookCompletenessError,
    MarketBookReadCompleteness,
    ReadCompletenessStatus,
)
from .betfair_marketbook_batch_plan import MarketBookReadPlan


_HISTORY_SCHEMA = "betfair-marketbook-attempt-history-v1"


class MarketBookAttemptHistoryError(ValueError):
    """Raised when retry/gap history is noncanonical or contradictory."""


class MarketBookAttemptOutcome(str, Enum):
    """Closed structural outcomes for one planned MarketBook batch attempt."""

    EXACT_RESPONSE = "EXACT_RESPONSE"
    INCOMPLETE_RESPONSE = "INCOMPLETE_RESPONSE"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    PARSE_FAILURE = "PARSE_FAILURE"
    NOT_DISPATCHED_RATE = "NOT_DISPATCHED_RATE"
    NOT_DISPATCHED_CONCURRENCY = "NOT_DISPATCHED_CONCURRENCY"
    CRASH_PENDING = "CRASH_PENDING"


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MarketBookAttemptHistoryError("attempt evidence must be canonical JSON data") from exc


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MarketBookAttemptHistoryError(f"{name} must be a non-empty canonical string")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MarketBookAttemptHistoryError(f"{name} must be a positive integer")
    return value


def _exact_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise MarketBookAttemptHistoryError(f"{name} must be bool")
    return value


def _sha256_token(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    token = _token(value, name)
    if len(token) != 64 or any(ch not in "0123456789abcdef" for ch in token):
        raise MarketBookAttemptHistoryError(f"{name} must be lowercase SHA-256")
    return token


def _receipt_payload(receipt: MarketBookBatchReceipt | None) -> dict[str, object] | None:
    return None if receipt is None else receipt.evidence_payload


def _record_core(
    *,
    attempt_id: str,
    sequence: int,
    batch_id: str,
    required: bool,
    outcome: MarketBookAttemptOutcome,
    receipt: MarketBookBatchReceipt | None,
    previous_record_id: str | None,
) -> dict[str, object]:
    return {
        "attempt_id": attempt_id,
        "sequence": sequence,
        "batch_id": batch_id,
        "required": required,
        "outcome": outcome.value,
        "exact_receipt": _receipt_payload(receipt),
        "previous_record_id": previous_record_id,
    }


@dataclass(frozen=True, slots=True)
class MarketBookAttemptRecord:
    """One immutable structural attempt event in a plan-bound hash chain."""

    attempt_id: str
    sequence: int
    batch_id: str
    required: bool
    outcome: MarketBookAttemptOutcome
    exact_receipt: MarketBookBatchReceipt | None
    previous_record_id: str | None
    record_id: str

    @property
    def core_payload(self) -> dict[str, object]:
        return _record_core(
            attempt_id=self.attempt_id,
            sequence=self.sequence,
            batch_id=self.batch_id,
            required=self.required,
            outcome=self.outcome,
            receipt=self.exact_receipt,
            previous_record_id=self.previous_record_id,
        )

    @property
    def evidence_payload(self) -> dict[str, object]:
        return {**self.core_payload, "record_id": self.record_id}

    @classmethod
    def issue(
        cls,
        plan: MarketBookReadPlan,
        *,
        attempt_id: str,
        sequence: int,
        batch_id: str,
        required: bool,
        outcome: MarketBookAttemptOutcome,
        exact_receipt: MarketBookBatchReceipt | None = None,
        previous_record: "MarketBookAttemptRecord | None" = None,
    ) -> "MarketBookAttemptRecord":
        attempt = _token(attempt_id, "attempt_id")
        seq = _positive_int(sequence, "sequence")
        batch = _token(batch_id, "batch_id")
        req = _exact_bool(required, "required")
        if not isinstance(outcome, MarketBookAttemptOutcome):
            raise MarketBookAttemptHistoryError("outcome must be MarketBookAttemptOutcome")
        previous_id = previous_record.record_id if previous_record is not None else None
        core = _record_core(
            attempt_id=attempt,
            sequence=seq,
            batch_id=batch,
            required=req,
            outcome=outcome,
            receipt=exact_receipt,
            previous_record_id=previous_id,
        )
        record = cls(
            attempt,
            seq,
            batch,
            req,
            outcome,
            exact_receipt,
            previous_id,
            _sha(core),
        )
        _validate_record(record, plan, previous_record)
        return record


def _validate_exact_receipt(
    plan: MarketBookReadPlan,
    batch_id: str,
    receipt: MarketBookBatchReceipt,
) -> None:
    if type(receipt) is not MarketBookBatchReceipt:
        raise MarketBookAttemptHistoryError("exact_receipt must be a canonical batch receipt")
    if receipt.batch_id != batch_id:
        raise MarketBookAttemptHistoryError("exact_receipt is bound to another planned batch")
    if receipt.status is not BatchReceiptStatus.EXACT_RESPONSE:
        raise MarketBookAttemptHistoryError("exact_receipt must have EXACT_RESPONSE status")
    try:
        MarketBookReadCompleteness(plan, (receipt,))
    except MarketBookCompletenessError as exc:
        raise MarketBookAttemptHistoryError("exact_receipt failed canonical validation") from exc


def _validate_record(
    record: MarketBookAttemptRecord,
    plan: MarketBookReadPlan,
    previous_record: MarketBookAttemptRecord | None,
) -> None:
    _token(record.attempt_id, "attempt_id")
    _positive_int(record.sequence, "sequence")
    _exact_bool(record.required, "required")
    if not isinstance(record.outcome, MarketBookAttemptOutcome):
        raise MarketBookAttemptHistoryError("outcome must be MarketBookAttemptOutcome")
    canonical_batches = {batch.batch_id for batch in plan.batches}
    if record.batch_id not in canonical_batches:
        raise MarketBookAttemptHistoryError("attempt references an unknown planned batch")

    if previous_record is None:
        if record.sequence != 1 or record.previous_record_id is not None:
            raise MarketBookAttemptHistoryError(
                "first attempt must be sequence 1 with no predecessor"
            )
    else:
        if record.sequence != previous_record.sequence + 1:
            raise MarketBookAttemptHistoryError("attempt sequence must be contiguous and monotonic")
        if record.previous_record_id != previous_record.record_id:
            raise MarketBookAttemptHistoryError("attempt predecessor does not match the hash chain")

    if record.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE:
        if record.exact_receipt is None:
            raise MarketBookAttemptHistoryError("EXACT_RESPONSE requires exact_receipt")
        _validate_exact_receipt(plan, record.batch_id, record.exact_receipt)
    elif record.exact_receipt is not None:
        raise MarketBookAttemptHistoryError("only EXACT_RESPONSE may bind exact_receipt")

    _sha256_token(record.previous_record_id, "previous_record_id", optional=True)
    _sha256_token(record.record_id, "record_id")
    if record.record_id != _sha(record.core_payload):
        raise MarketBookAttemptHistoryError("record_id does not match canonical recomputation")


def _restore_exact_receipt(value: object) -> MarketBookBatchReceipt | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise MarketBookAttemptHistoryError("exact_receipt must be an object or null")
    expected_keys = {
        "batch_id",
        "expected_market_ids",
        "observed_market_ids",
        "missing_market_ids",
        "unexpected_market_ids",
        "status",
        "failure_kind",
        "failure_code",
        "payload_sha256",
        "receipt_id",
    }
    if set(value) != expected_keys:
        raise MarketBookAttemptHistoryError("exact_receipt has a noncanonical shape")
    try:
        status = BatchReceiptStatus(value["status"])
        return MarketBookBatchReceipt(
            batch_id=value["batch_id"],
            expected_market_ids=tuple(value["expected_market_ids"]),
            observed_market_ids=tuple(value["observed_market_ids"]),
            missing_market_ids=tuple(value["missing_market_ids"]),
            unexpected_market_ids=tuple(value["unexpected_market_ids"]),
            status=status,
            failure_kind=value["failure_kind"],
            failure_code=value["failure_code"],
            payload_sha256=value["payload_sha256"],
            receipt_id=value["receipt_id"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MarketBookAttemptHistoryError("exact_receipt cannot be restored") from exc


@dataclass(frozen=True, slots=True)
class MarketBookAttemptHistory:
    """Plan-bound retry history that keeps recovered state distinct from past gaps."""

    plan: MarketBookReadPlan
    records: tuple[MarketBookAttemptRecord, ...]

    def __post_init__(self) -> None:
        seen_attempt_ids: set[str] = set()
        previous: MarketBookAttemptRecord | None = None
        for record in self.records:
            if type(record) is not MarketBookAttemptRecord:
                raise MarketBookAttemptHistoryError("records must be canonical attempt records")
            _validate_record(record, self.plan, previous)
            if record.attempt_id in seen_attempt_ids:
                raise MarketBookAttemptHistoryError("attempt_id cannot be reused")
            seen_attempt_ids.add(record.attempt_id)
            previous = record

    @property
    def latest_records(self) -> tuple[MarketBookAttemptRecord, ...]:
        latest: dict[str, MarketBookAttemptRecord] = {}
        for record in self.records:
            latest[record.batch_id] = record
        return tuple(
            latest[batch.batch_id]
            for batch in self.plan.batches
            if batch.batch_id in latest
        )

    @property
    def current_completeness(self) -> MarketBookReadCompleteness:
        receipts = tuple(
            record.exact_receipt
            for record in self.latest_records
            if record.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
            and record.exact_receipt is not None
        )
        return MarketBookReadCompleteness(self.plan, receipts)

    @property
    def current_structural_complete(self) -> bool:
        return self.current_completeness.status is ReadCompletenessStatus.COMPLETE

    @property
    def current_pending_batch_ids(self) -> tuple[str, ...]:
        return self.current_completeness.pending_batch_ids

    @property
    def required_gap_attempt_ids(self) -> tuple[str, ...]:
        return tuple(
            record.attempt_id
            for record in self.records
            if record.required and record.outcome is not MarketBookAttemptOutcome.EXACT_RESPONSE
        )

    @property
    def historical_required_gap_free(self) -> bool:
        return not self.required_gap_attempt_ids

    @property
    def evidence_payload(self) -> dict[str, object]:
        return {
            "schema": _HISTORY_SCHEMA,
            "plan_id": self.plan.plan_id,
            "request_contract_id": self.plan.request_contract_id,
            "records": [record.evidence_payload for record in self.records],
            "latest_record_ids": [record.record_id for record in self.latest_records],
            "current_completeness_id": self.current_completeness.evidence_id,
            "current_pending_batch_ids": list(self.current_pending_batch_ids),
            "current_structural_complete": self.current_structural_complete,
            "required_attempt_count": sum(1 for record in self.records if record.required),
            "required_gap_attempt_ids": list(self.required_gap_attempt_ids),
            "historical_required_gap_free": self.historical_required_gap_free,
            "campaign_denominator_complete": False,
            "provider_observation_authenticated": False,
            "provider_freshness_proven": False,
            "provider_dispatch_authorized": False,
            "execution_authorized": False,
        }

    @property
    def history_id(self) -> str:
        return _sha(self.evidence_payload)

    def to_json(self) -> str:
        return _canonical_json({"evidence": self.evidence_payload, "history_id": self.history_id})

    @classmethod
    def from_json(cls, plan: MarketBookReadPlan, encoded: str) -> "MarketBookAttemptHistory":
        try:
            envelope = json.loads(encoded) if isinstance(encoded, str) else None
        except json.JSONDecodeError as exc:
            raise MarketBookAttemptHistoryError("encoded history is not valid JSON") from exc
        if not isinstance(envelope, Mapping) or set(envelope) != {"evidence", "history_id"}:
            raise MarketBookAttemptHistoryError("encoded history has a noncanonical envelope")
        evidence = envelope["evidence"]
        if not isinstance(evidence, Mapping):
            raise MarketBookAttemptHistoryError("encoded history evidence must be an object")
        raw_records = evidence.get("records")
        if isinstance(raw_records, (str, bytes, Mapping)) or not isinstance(raw_records, Sequence):
            raise MarketBookAttemptHistoryError("encoded records must be a sequence")

        restored: list[MarketBookAttemptRecord] = []
        previous: MarketBookAttemptRecord | None = None
        for raw in raw_records:
            if not isinstance(raw, Mapping):
                raise MarketBookAttemptHistoryError("encoded record must be an object")
            expected_keys = {
                "attempt_id",
                "sequence",
                "batch_id",
                "required",
                "outcome",
                "exact_receipt",
                "previous_record_id",
                "record_id",
            }
            if set(raw) != expected_keys:
                raise MarketBookAttemptHistoryError("encoded record has a noncanonical shape")
            try:
                outcome = MarketBookAttemptOutcome(raw["outcome"])
                receipt = _restore_exact_receipt(raw["exact_receipt"])
                record = MarketBookAttemptRecord(
                    attempt_id=raw["attempt_id"],
                    sequence=raw["sequence"],
                    batch_id=raw["batch_id"],
                    required=raw["required"],
                    outcome=outcome,
                    exact_receipt=receipt,
                    previous_record_id=raw["previous_record_id"],
                    record_id=raw["record_id"],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MarketBookAttemptHistoryError("encoded record cannot be restored") from exc
            _validate_record(record, plan, previous)
            restored.append(record)
            previous = record

        history = cls(plan, tuple(restored))
        if history.evidence_payload != evidence or history.history_id != envelope["history_id"]:
            raise MarketBookAttemptHistoryError(
                "encoded history does not match canonical recomputation"
            )
        return history
