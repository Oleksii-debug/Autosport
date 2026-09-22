"""Structural completeness evidence for deterministic Betfair MarketBook batches."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Mapping, Sequence

from .betfair_marketbook_batch_plan import MarketBookReadBatch, MarketBookReadPlan


class MarketBookCompletenessError(ValueError):
    """Raised when batch-result completeness evidence is noncanonical or contradictory."""


class BatchReceiptStatus(str, Enum):
    EXACT_RESPONSE = "EXACT_RESPONSE"
    INCOMPLETE_RESPONSE = "INCOMPLETE_RESPONSE"
    EXPLICIT_FAILURE = "EXPLICIT_FAILURE"


class ReadCompletenessStatus(str, Enum):
    COMPLETE = "COMPLETE"
    DEGRADED = "DEGRADED"


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise MarketBookCompletenessError("provider evidence must be canonical JSON data") from exc


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MarketBookCompletenessError(f"{name} must be a non-empty canonical string")
    return value


def _market_ids_from_response(response: object) -> tuple[str, ...]:
    if isinstance(response, (str, bytes, Mapping)) or not isinstance(response, Sequence):
        raise MarketBookCompletenessError("MarketBook response must be a sequence of objects")
    ids: list[str] = []
    for entry in response:
        if not isinstance(entry, Mapping):
            raise MarketBookCompletenessError("each MarketBook response entry must be an object")
        market_id = _token(entry.get("marketId"), "marketId")
        ids.append(market_id)
    if len(ids) != len(set(ids)):
        raise MarketBookCompletenessError("MarketBook response contains duplicate marketId values")
    return tuple(sorted(ids))


def _receipt_id(payload: Mapping[str, object]) -> str:
    return _sha(payload)


@dataclass(frozen=True, slots=True)
class MarketBookBatchReceipt:
    batch_id: str
    expected_market_ids: tuple[str, ...]
    observed_market_ids: tuple[str, ...]
    missing_market_ids: tuple[str, ...]
    unexpected_market_ids: tuple[str, ...]
    status: BatchReceiptStatus
    failure_kind: str | None
    failure_code: str | None
    payload_sha256: str
    receipt_id: str

    @property
    def core_payload(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "expected_market_ids": list(self.expected_market_ids),
            "observed_market_ids": list(self.observed_market_ids),
            "missing_market_ids": list(self.missing_market_ids),
            "unexpected_market_ids": list(self.unexpected_market_ids),
            "status": self.status.value,
            "failure_kind": self.failure_kind,
            "failure_code": self.failure_code,
            "payload_sha256": self.payload_sha256,
        }

    @property
    def evidence_payload(self) -> dict[str, object]:
        return {**self.core_payload, "receipt_id": self.receipt_id}

    @classmethod
    def from_response(cls, batch: MarketBookReadBatch, response: object) -> "MarketBookBatchReceipt":
        observed = _market_ids_from_response(response)
        expected = tuple(sorted(batch.market_ids))
        expected_set, observed_set = set(expected), set(observed)
        missing = tuple(sorted(expected_set - observed_set))
        unexpected = tuple(sorted(observed_set - expected_set))
        status = (
            BatchReceiptStatus.EXACT_RESPONSE
            if not missing and not unexpected
            else BatchReceiptStatus.INCOMPLETE_RESPONSE
        )
        payload_sha256 = _sha(response)
        core = {
            "batch_id": batch.batch_id,
            "expected_market_ids": list(expected),
            "observed_market_ids": list(observed),
            "missing_market_ids": list(missing),
            "unexpected_market_ids": list(unexpected),
            "status": status.value,
            "failure_kind": None,
            "failure_code": None,
            "payload_sha256": payload_sha256,
        }
        return cls(
            batch.batch_id,
            expected,
            observed,
            missing,
            unexpected,
            status,
            None,
            None,
            payload_sha256,
            _receipt_id(core),
        )

    @classmethod
    def from_failure(
        cls,
        batch: MarketBookReadBatch,
        *,
        failure_kind: str,
        failure_code: str,
        detail_payload: object = None,
    ) -> "MarketBookBatchReceipt":
        kind = _token(failure_kind, "failure_kind")
        code = _token(failure_code, "failure_code")
        expected = tuple(sorted(batch.market_ids))
        payload_sha256 = _sha(detail_payload)
        core = {
            "batch_id": batch.batch_id,
            "expected_market_ids": list(expected),
            "observed_market_ids": [],
            "missing_market_ids": list(expected),
            "unexpected_market_ids": [],
            "status": BatchReceiptStatus.EXPLICIT_FAILURE.value,
            "failure_kind": kind,
            "failure_code": code,
            "payload_sha256": payload_sha256,
        }
        return cls(
            batch.batch_id,
            expected,
            (),
            expected,
            (),
            BatchReceiptStatus.EXPLICIT_FAILURE,
            kind,
            code,
            payload_sha256,
            _receipt_id(core),
        )


def _validate_receipt(receipt: MarketBookBatchReceipt, batch: MarketBookReadBatch) -> None:
    expected = tuple(sorted(batch.market_ids))
    if receipt.batch_id != batch.batch_id or receipt.expected_market_ids != expected:
        raise MarketBookCompletenessError("receipt is not bound to the canonical planned batch")
    if receipt.observed_market_ids != tuple(sorted(receipt.observed_market_ids)):
        raise MarketBookCompletenessError("receipt observed market ids are noncanonical")
    if len(receipt.observed_market_ids) != len(set(receipt.observed_market_ids)):
        raise MarketBookCompletenessError("receipt contains duplicate observed market ids")
    expected_set, observed_set = set(expected), set(receipt.observed_market_ids)
    missing = tuple(sorted(expected_set - observed_set))
    unexpected = tuple(sorted(observed_set - expected_set))
    if receipt.missing_market_ids != missing or receipt.unexpected_market_ids != unexpected:
        raise MarketBookCompletenessError("receipt coverage fields contradict observed market ids")
    if receipt.status is BatchReceiptStatus.EXACT_RESPONSE:
        if missing or unexpected or receipt.failure_kind is not None or receipt.failure_code is not None:
            raise MarketBookCompletenessError("EXACT_RESPONSE receipt is contradictory")
    elif receipt.status is BatchReceiptStatus.INCOMPLETE_RESPONSE:
        if not (missing or unexpected) or receipt.failure_kind is not None or receipt.failure_code is not None:
            raise MarketBookCompletenessError("INCOMPLETE_RESPONSE receipt is contradictory")
    elif receipt.status is BatchReceiptStatus.EXPLICIT_FAILURE:
        if receipt.observed_market_ids or missing != expected:
            raise MarketBookCompletenessError("failure receipt cannot contain provider market truth")
        _token(receipt.failure_kind, "failure_kind")
        _token(receipt.failure_code, "failure_code")
    else:
        raise MarketBookCompletenessError("unknown batch receipt status")
    if len(receipt.payload_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in receipt.payload_sha256):
        raise MarketBookCompletenessError("payload_sha256 must be lowercase SHA-256")
    if receipt.receipt_id != _receipt_id(receipt.core_payload):
        raise MarketBookCompletenessError("receipt identity does not match canonical recomputation")


@dataclass(frozen=True, slots=True)
class MarketBookReadCompleteness:
    plan: MarketBookReadPlan
    receipts: tuple[MarketBookBatchReceipt, ...]

    def __post_init__(self) -> None:
        canonical = {batch.batch_id: batch for batch in self.plan.batches}
        seen: dict[str, MarketBookBatchReceipt] = {}
        for receipt in self.receipts:
            if receipt.batch_id not in canonical:
                raise MarketBookCompletenessError("receipt references an unknown batch_id")
            if receipt.batch_id in seen:
                raise MarketBookCompletenessError("duplicate receipt for one planned batch")
            _validate_receipt(receipt, canonical[receipt.batch_id])
            seen[receipt.batch_id] = receipt
        ordered = tuple(seen[batch.batch_id] for batch in self.plan.batches if batch.batch_id in seen)
        object.__setattr__(self, "receipts", ordered)

    @property
    def missing_receipt_batch_ids(self) -> tuple[str, ...]:
        seen = {receipt.batch_id for receipt in self.receipts}
        return tuple(batch.batch_id for batch in self.plan.batches if batch.batch_id not in seen)

    @property
    def pending_batch_ids(self) -> tuple[str, ...]:
        by_id = {receipt.batch_id: receipt for receipt in self.receipts}
        return tuple(
            batch.batch_id
            for batch in self.plan.batches
            if batch.batch_id not in by_id
            or by_id[batch.batch_id].status is not BatchReceiptStatus.EXACT_RESPONSE
        )

    @property
    def status(self) -> ReadCompletenessStatus:
        return (
            ReadCompletenessStatus.COMPLETE
            if not self.pending_batch_ids
            else ReadCompletenessStatus.DEGRADED
        )

    @property
    def evidence_payload(self) -> dict[str, object]:
        return {
            "schema": "betfair-marketbook-batch-completeness-v1",
            "plan_id": self.plan.plan_id,
            "request_contract_id": self.plan.request_contract_id,
            "planned_batch_ids": [batch.batch_id for batch in self.plan.batches],
            "receipts": [receipt.evidence_payload for receipt in self.receipts],
            "missing_receipt_batch_ids": list(self.missing_receipt_batch_ids),
            "pending_batch_ids": list(self.pending_batch_ids),
            "status": self.status.value,
            "provider_observation_complete": self.status is ReadCompletenessStatus.COMPLETE,
            "provider_dispatch_authorized": False,
            "execution_authorized": False,
        }

    @property
    def evidence_id(self) -> str:
        return _sha(self.evidence_payload)
