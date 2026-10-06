"""Canonical read-only Betfair MarketBook batch transport bridge.

This module composes deterministic batch planning/completeness with the canonical
budget-enforcing listMarketBook physical transport.  It does not grant provider
freshness, account identity, provider writes, execution, settlement, or real-money
authority.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from weakref import ref

from . import betfair_account_readonly as _base
from . import betfair_marketbook_freshness as _transport
from .betfair_marketbook_batch_completeness import (
    BatchReceiptStatus,
    MarketBookBatchReceipt,
    MarketBookCompletenessError,
)
from .betfair_marketbook_batch_plan import (
    MarketBookReadBatch,
    MarketBookReadPlan,
)
from .betfair_marketbook_attempt_history import (
    MarketBookAttemptHistory,
    MarketBookAttemptOutcome,
    MarketBookAttemptRecord,
)
from .betfair_marketbook_rate_gate import BetfairMarketBookPerMarketRateGate
from .betfair_marketbook_projection_concurrency import (
    BetfairMarketBookProjectionConcurrencyGate,
)


class MarketBookBatchTransportError(RuntimeError):
    """Raised when a planned MarketBook batch cannot be dispatched canonically."""


class MarketBookBatchAdmissionError(MarketBookBatchTransportError):
    """Expected local admission denial; no provider request was dispatched."""

    def __init__(self, outcome: MarketBookAttemptOutcome, message: str) -> None:
        if outcome not in {
            MarketBookAttemptOutcome.NOT_DISPATCHED_RATE,
            MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY,
        }:
            raise ValueError("admission outcome must be a NOT_DISPATCHED outcome")
        super().__init__(message)
        self.outcome = outcome


class MarketBookPostDispatchFailure(MarketBookBatchTransportError):
    """Provider I/O occurred but canonical attempt evidence could not finalize."""


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
        raise MarketBookBatchTransportError(
            "MarketBook transport evidence must be canonical JSON data"
        ) from exc


def _sha(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _token(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MarketBookBatchTransportError(
            f"{field} must be a non-empty canonical string"
        )
    return value


def _sha256_token(value: object, field: str) -> str:
    token = _token(value, field)
    if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
        raise MarketBookBatchTransportError(f"{field} must be lowercase sha256")
    return token


def _transport_now(
    client: _base.BetfairReadOnlyClient,
    *,
    canonical_network_transport: Callable[[_base.BetfairReadOnlyClient], bool] = _transport._canonical_network_transport,
) -> datetime:
    if type(client) is not _base.BetfairReadOnlyClient:
        raise TypeError("client must be an exact BetfairReadOnlyClient")
    # Mirror the canonical physical MarketBook transport's time-origin rule.
    # The unmodified production network transport is always measured by real
    # UTC; injected transports use the client's validated clock so replay and
    # deterministic tests do not acquire a second wall-clock authority.
    if canonical_network_transport(client):
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(client._observed_at()).astimezone(timezone.utc)


def _dispatch_instant(
    client: _base.BetfairReadOnlyClient,
    value: object,
    *,
    transport_now: Callable[[_base.BetfairReadOnlyClient], datetime] = _transport_now,
) -> datetime:
    if type(value) is not datetime:
        raise TypeError("scheduled_at must be an exact datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduled_at must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    dispatch_now = transport_now(client)

    if normalized > dispatch_now:
        raise ValueError("scheduled_at must not be in the future")
    # Provider pressure is enforced against the physical immediate-dispatch
    # instant, never against caller-authored historical schedule time.  Using
    # stale scheduled_at values here would let a caller manufacture elapsed
    # rate-window time while sending requests back-to-back.
    return dispatch_now


def _release_projection_lease(
    client: _base.BetfairReadOnlyClient,
    gate: BetfairMarketBookProjectionConcurrencyGate,
    request_id: str,
    lease_generation: int | None,
    *,
    complete: Callable[..., None],
    transport_now: Callable[[_base.BetfairReadOnlyClient], datetime] = _transport_now,
) -> None:
    if lease_generation is None:
        return
    complete(
        gate,
        request_id,
        lease_generation=lease_generation,
        observed_at=transport_now(client),
    )


def _release_projection_lease_after_failure(
    client: _base.BetfairReadOnlyClient,
    gate: BetfairMarketBookProjectionConcurrencyGate,
    request_id: str,
    lease_generation: int | None,
    primary: BaseException,
    *,
    complete: Callable[..., None],
    release: Callable[..., None] = _release_projection_lease,
) -> None:
    """Best-effort cleanup without masking the primary dispatch failure."""

    try:
        release(
            client,
            gate,
            request_id,
            lease_generation,
            complete=complete,
        )
    except BaseException as cleanup_exc:
        # A fresh process-control interruption during cleanup supersedes an
        # ordinary primary error. If process control is already the primary,
        # cleanup failure must never replace it.
        if not isinstance(cleanup_exc, Exception) and isinstance(primary, Exception):
            raise
        add_note = getattr(primary, "add_note", None)
        if callable(add_note):
            add_note(
                "projection-lease cleanup also failed: "
                f"{type(cleanup_exc).__name__}: {cleanup_exc}"
            )


def _canonical_batch(
    plan: MarketBookReadPlan,
    batch_id: str,
) -> MarketBookReadBatch:
    if type(plan) is not MarketBookReadPlan:
        raise TypeError("plan must be an exact MarketBookReadPlan")
    batch_token = _token(batch_id, "batch_id")
    matches = tuple(batch for batch in plan.batches if batch.batch_id == batch_token)
    if len(matches) != 1:
        raise MarketBookBatchTransportError(
            "batch_id must identify exactly one current canonical plan batch"
        )
    return matches[0]


def _params_for_batch(
    plan: MarketBookReadPlan,
    batch: MarketBookReadBatch,
) -> dict[str, object]:
    contract = plan.request_contract_payload
    if contract.get("provider") != "BETFAIR":
        raise MarketBookBatchTransportError("request contract provider must be BETFAIR")
    if contract.get("operation") != "listMarketBook":
        raise MarketBookBatchTransportError(
            "request contract operation must be listMarketBook"
        )

    params: dict[str, object] = {"marketIds": list(batch.market_ids)}
    price_data = contract["price_data"]
    best_prices_depth = contract["best_prices_depth"]
    virtualise = contract["virtualise"]
    rollover_stakes = contract["rollover_stakes"]
    overrides_contract = contract["ex_best_offers_overrides"]

    if (
        price_data
        or best_prices_depth is not None
        or virtualise is not None
        or rollover_stakes is not None
        or overrides_contract["rollup_model"] is not None
        or overrides_contract["rollup_limit"] is not None
    ):
        projection: dict[str, object] = {}
        if price_data:
            projection["priceData"] = list(price_data)
        if virtualise is not None:
            projection["virtualise"] = virtualise
        if rollover_stakes is not None:
            projection["rolloverStakes"] = rollover_stakes

        overrides: dict[str, object] = {}
        if best_prices_depth is not None:
            overrides["bestPricesDepth"] = best_prices_depth
        if overrides_contract["rollup_model"] is not None:
            overrides["rollupModel"] = overrides_contract["rollup_model"]
        if overrides_contract["rollup_limit"] is not None:
            overrides["rollupLimit"] = overrides_contract["rollup_limit"]
        if overrides:
            projection["exBestOffersOverrides"] = overrides
        params["priceProjection"] = projection

    optional_fields = (
        ("order_projection", "orderProjection"),
        ("match_projection", "matchProjection"),
        ("include_overall_position", "includeOverallPosition"),
        ("partition_matched_by_strategy_ref", "partitionMatchedByStrategyRef"),
        ("matched_since", "matchedSince"),
        ("currency_code", "currencyCode"),
        ("locale", "locale"),
    )
    for contract_field, wire_field in optional_fields:
        value = contract[contract_field]
        if value is not None:
            params[wire_field] = value

    if contract["customer_strategy_refs"]:
        params["customerStrategyRefs"] = list(contract["customer_strategy_refs"])
    if contract["bet_ids"]:
        params["betIds"] = list(contract["bet_ids"])
    return params


@dataclass(frozen=True, slots=True, weakref_slot=True)
class MarketBookBatchTransportResult:
    """Origin-bound transport result; never provider-freshness or execution authority."""

    plan_id: str
    request_contract_id: str
    batch_id: str
    request_budget_evidence_id: str
    request_payload_sha256: str
    source_payload_sha256: str
    observed_at: str
    canonical_network_origin: bool
    receipt: MarketBookBatchReceipt

    def __post_init__(self) -> None:
        _sha256_token(self.plan_id, "plan_id")
        _sha256_token(self.request_contract_id, "request_contract_id")
        _sha256_token(self.batch_id, "batch_id")
        _sha256_token(
            self.request_budget_evidence_id,
            "request_budget_evidence_id",
        )
        _sha256_token(self.request_payload_sha256, "request_payload_sha256")
        _sha256_token(self.source_payload_sha256, "source_payload_sha256")
        _base._iso_timestamp(self.observed_at, "observed_at")
        if type(self.canonical_network_origin) is not bool:
            raise MarketBookBatchTransportError(
                "canonical_network_origin must be exact bool"
            )
        if type(self.receipt) is not MarketBookBatchReceipt:
            raise MarketBookBatchTransportError(
                "receipt must be a canonical MarketBookBatchReceipt"
            )
        if self.receipt.batch_id != self.batch_id:
            raise MarketBookBatchTransportError(
                "receipt is bound to another MarketBook batch"
            )

    @property
    def structural_exact_response(self) -> bool:
        return self.receipt.status is BatchReceiptStatus.EXACT_RESPONSE

    @property
    def evidence_payload(self) -> dict[str, object]:
        return {
            "schema": "betfair-marketbook-batch-transport-v1",
            "plan_id": self.plan_id,
            "request_contract_id": self.request_contract_id,
            "batch_id": self.batch_id,
            "request_budget_evidence_id": self.request_budget_evidence_id,
            "request_payload_sha256": self.request_payload_sha256,
            "source_payload_sha256": self.source_payload_sha256,
            "observed_at": self.observed_at,
            "canonical_network_origin_diagnostic": self.canonical_network_origin,
            "receipt": self.receipt.evidence_payload,
            "structural_exact_response": self.structural_exact_response,
            "origin_authority_requires_assert_issued": True,
            "provider_observation_authenticated": False,
            "provider_freshness_proven": False,
            "provider_dispatch_authorized": False,
            "provider_write_authorized": False,
            "execution_authorized": False,
        }

    @property
    def evidence_id(self) -> str:
        return _sha(self.evidence_payload)

    def _authority_fingerprint(self) -> str:
        return self.evidence_id

    def assert_issued(self) -> None:
        raise MarketBookBatchTransportError(
            "MarketBook batch transport result was not issued by canonical transport"
        )

    def assert_canonical_network_origin(self) -> None:
        raise MarketBookBatchTransportError(
            "MarketBook batch transport result lacks canonical network origin"
        )


def _read_market_book_batch(
    client: _base.BetfairReadOnlyClient,
    plan: MarketBookReadPlan,
    *,
    batch_id: str,
    canonical_batch: Callable[[MarketBookReadPlan, str], MarketBookReadBatch],
    params_for_batch: Callable[[MarketBookReadPlan, MarketBookReadBatch], dict[str, object]],
    request_budget: Callable[[object], object],
    post_readonly: Callable[..., _transport._MarketBookRpcResponse],
    receipt_from_response: Callable[..., MarketBookBatchReceipt],
    result_factory: Callable[..., MarketBookBatchTransportResult],
    post_dispatch_failure_type: type[MarketBookPostDispatchFailure],
    protocol_error_type: type[BaseException],
    completeness_error_type: type[MarketBookCompletenessError],
) -> MarketBookBatchTransportResult:
    batch = canonical_batch(plan, batch_id)
    params = params_for_batch(plan, batch)
    plan_id = plan.plan_id
    request_contract_id = plan.request_contract_id
    if canonical_batch(plan, batch_id) != batch:
        raise MarketBookBatchTransportError(
            "MarketBook plan changed before provider dispatch"
        )

    wire_budget = request_budget(params)
    if wire_budget.evidence_id != batch.budget_evidence_id:
        raise MarketBookBatchTransportError(
            "wire-derived request budget does not match the canonical planned batch"
        )

    response = post_readonly(
        client,
        params=params,
        resolve_application_context=False,
    )

    # From this point forward provider I/O has completed. Any ordinary internal
    # finalization failure must remain an explicit failed observation interval
    # rather than escaping the attempt-history coordinator unclassified.
    try:
        if response.request_budget.evidence_id != batch.budget_evidence_id:
            raise post_dispatch_failure_type(
                "transport request budget drifted from the canonical planned batch"
            )

        try:
            current_batch = canonical_batch(plan, batch_id)
            current_plan_id = plan.plan_id
            current_request_contract_id = plan.request_contract_id
        except Exception as exc:
            raise post_dispatch_failure_type(
                "MarketBook plan changed during provider dispatch"
            ) from exc
        if (
            current_batch != batch
            or current_plan_id != plan_id
            or current_request_contract_id != request_contract_id
        ):
            raise post_dispatch_failure_type(
                "MarketBook plan changed during provider dispatch"
            )

        try:
            receipt = receipt_from_response(batch, list(response.rows))
        except completeness_error_type as exc:
            raise protocol_error_type(
                "MarketBook response cannot produce canonical structural receipt"
            ) from exc

        return result_factory(
            plan_id=plan_id,
            request_contract_id=request_contract_id,
            batch_id=batch.batch_id,
            request_budget_evidence_id=response.request_budget.evidence_id,
            request_payload_sha256=response.request_payload_sha256,
            source_payload_sha256=response.source_payload_sha256,
            observed_at=response.observed_at,
            canonical_network_origin=response.network_origin,
            receipt=receipt,
        )
    except (
        post_dispatch_failure_type,
        protocol_error_type,
    ):
        raise
    except Exception as exc:
        raise post_dispatch_failure_type(
            "provider read completed but canonical MarketBook result finalization failed"
        ) from exc


def _make_attempt_history(
    plan: MarketBookReadPlan,
    records: tuple[MarketBookAttemptRecord, ...],
    *,
    history_type: type[MarketBookAttemptHistory] = MarketBookAttemptHistory,
    validate_history: Callable[[MarketBookAttemptHistory], None] = MarketBookAttemptHistory.__post_init__,
) -> MarketBookAttemptHistory:
    """Construct exact canonical history without mutable dataclass init dispatch."""

    history = object.__new__(history_type)
    object.__setattr__(history, "plan", plan)
    object.__setattr__(history, "records", records)
    validate_history(history)
    return history


def append_market_book_transport_attempt(
    history: MarketBookAttemptHistory,
    result: MarketBookBatchTransportResult,
    *,
    attempt_id: str,
    required: bool,
    history_type: type[MarketBookAttemptHistory] = MarketBookAttemptHistory,
    result_type: type[MarketBookBatchTransportResult] = MarketBookBatchTransportResult,
    canonical_batch: Callable[[MarketBookReadPlan, str], MarketBookReadBatch] = _canonical_batch,
    issue_record: Callable[..., MarketBookAttemptRecord] = MarketBookAttemptRecord.issue,
    make_history: Callable[
        [MarketBookReadPlan, tuple[MarketBookAttemptRecord, ...]],
        MarketBookAttemptHistory,
    ] = _make_attempt_history,
    exact_status: BatchReceiptStatus = BatchReceiptStatus.EXACT_RESPONSE,
    incomplete_status: BatchReceiptStatus = BatchReceiptStatus.INCOMPLETE_RESPONSE,
    exact_outcome: MarketBookAttemptOutcome = MarketBookAttemptOutcome.EXACT_RESPONSE,
    incomplete_outcome: MarketBookAttemptOutcome = MarketBookAttemptOutcome.INCOMPLETE_RESPONSE,
) -> MarketBookAttemptHistory:
    """Append one issued transport result to canonical retry/gap history."""

    if type(history) is not history_type:
        raise TypeError("history must be an exact MarketBookAttemptHistory")
    if type(result) is not result_type:
        raise TypeError("result must be an exact MarketBookBatchTransportResult")
    # Canonical issuance is enforced by the closure-bound public wrapper installed
    # below. Do not redispatch through the mutable class method here: a rebound
    # assert_issued implementation could mutate an already-proved result between
    # the registry check and structural history construction.
    if history.plan.plan_id != result.plan_id:
        raise MarketBookBatchTransportError(
            "transport result is bound to another or mutated MarketBook plan"
        )
    if history.plan.request_contract_id != result.request_contract_id:
        raise MarketBookBatchTransportError(
            "transport result is bound to another MarketBook request contract"
        )

    canonical_batch(history.plan, result.batch_id)
    previous = history.records[-1] if history.records else None
    if result.receipt.status is exact_status:
        outcome = exact_outcome
        exact_receipt = result.receipt
    elif result.receipt.status is incomplete_status:
        outcome = incomplete_outcome
        exact_receipt = None
    else:
        raise MarketBookBatchTransportError(
            "transport result receipt has unsupported dispatch outcome"
        )

    record = issue_record(
        history.plan,
        attempt_id=attempt_id,
        sequence=len(history.records) + 1,
        batch_id=result.batch_id,
        required=required,
        outcome=outcome,
        exact_receipt=exact_receipt,
        previous_record=previous,
    )
    return make_history(history.plan, history.records + (record,))


def _append_nonresponse_attempt(
    history: MarketBookAttemptHistory,
    *,
    batch_id: str,
    attempt_id: str,
    required: bool,
    outcome: MarketBookAttemptOutcome,
    canonical_batch: Callable[[MarketBookReadPlan, str], MarketBookReadBatch] = _canonical_batch,
    issue_record: Callable[..., MarketBookAttemptRecord] = MarketBookAttemptRecord.issue,
    make_history: Callable[
        [MarketBookReadPlan, tuple[MarketBookAttemptRecord, ...]],
        MarketBookAttemptHistory,
    ] = _make_attempt_history,
    allowed_outcomes: frozenset[MarketBookAttemptOutcome] = frozenset(
        {
            MarketBookAttemptOutcome.NOT_DISPATCHED_RATE,
            MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY,
            MarketBookAttemptOutcome.PROVIDER_FAILURE,
            MarketBookAttemptOutcome.TRANSPORT_FAILURE,
            MarketBookAttemptOutcome.PARSE_FAILURE,
        }
    ),
) -> MarketBookAttemptHistory:
    if outcome not in allowed_outcomes:
        raise MarketBookBatchTransportError(
            "nonresponse attempt outcome is not supported by this coordinator"
        )
    canonical_batch(history.plan, batch_id)
    previous = history.records[-1] if history.records else None
    record = issue_record(
        history.plan,
        attempt_id=attempt_id,
        sequence=len(history.records) + 1,
        batch_id=batch_id,
        required=required,
        outcome=outcome,
        exact_receipt=None,
        previous_record=previous,
    )
    return make_history(history.plan, history.records + (record,))


@dataclass(frozen=True, slots=True)
class MarketBookBatchAttemptExecution:
    history: MarketBookAttemptHistory
    outcome: MarketBookAttemptOutcome
    result: MarketBookBatchTransportResult | None

    def __post_init__(self) -> None:
        if type(self.history) is not MarketBookAttemptHistory:
            raise TypeError("history must be an exact MarketBookAttemptHistory")
        try:
            MarketBookAttemptHistory(self.history.plan, self.history.records)
        except Exception as exc:
            raise MarketBookBatchTransportError(
                "attempt execution history is not canonical"
            ) from exc
        if not isinstance(self.outcome, MarketBookAttemptOutcome):
            raise TypeError("outcome must be MarketBookAttemptOutcome")
        if not self.history.records:
            raise MarketBookBatchTransportError(
                "attempt execution requires an appended canonical history record"
            )
        latest = self.history.records[-1]
        if latest.outcome is not self.outcome:
            raise MarketBookBatchTransportError(
                "attempt execution outcome does not match latest history record"
            )
        response_outcomes = {
            MarketBookAttemptOutcome.EXACT_RESPONSE,
            MarketBookAttemptOutcome.INCOMPLETE_RESPONSE,
        }
        if self.outcome in response_outcomes:
            if type(self.result) is not MarketBookBatchTransportResult:
                raise MarketBookBatchTransportError(
                    "response outcome requires canonical transport result"
                )
            # Canonical issuance is enforced by validate_attempt_execution_bound.
            # Avoid mutable class redispatch after that closure-local registry proof.
            if (
                self.result.plan_id != self.history.plan.plan_id
                or self.result.request_contract_id
                != self.history.plan.request_contract_id
            ):
                raise MarketBookBatchTransportError(
                    "attempt execution result is bound to another history plan"
                )
            if latest.batch_id != self.result.batch_id:
                raise MarketBookBatchTransportError(
                    "attempt execution result is bound to another history batch"
                )
            expected_receipt_status = (
                BatchReceiptStatus.EXACT_RESPONSE
                if self.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
                else BatchReceiptStatus.INCOMPLETE_RESPONSE
            )
            if self.result.receipt.status is not expected_receipt_status:
                raise MarketBookBatchTransportError(
                    "attempt execution result receipt status contradicts outcome"
                )
            if (
                self.outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
                and latest.exact_receipt != self.result.receipt
            ):
                raise MarketBookBatchTransportError(
                    "attempt execution exact receipt does not match history"
                )
        elif self.result is not None:
            raise MarketBookBatchTransportError(
                "nonresponse outcome cannot carry transport result"
            )


def _install_transport_result_authority() -> None:
    issued: dict[int, tuple[object, str, bool]] = {}
    validate = MarketBookBatchTransportResult.__post_init__
    json_dumps = json.dumps
    hash_factory = sha256

    def authority_fingerprint(self: MarketBookBatchTransportResult) -> str:
        receipt = self.receipt
        payload = {
            "schema": "betfair-marketbook-batch-transport-v1",
            "plan_id": self.plan_id,
            "request_contract_id": self.request_contract_id,
            "batch_id": self.batch_id,
            "request_budget_evidence_id": self.request_budget_evidence_id,
            "request_payload_sha256": self.request_payload_sha256,
            "source_payload_sha256": self.source_payload_sha256,
            "observed_at": self.observed_at,
            "canonical_network_origin_diagnostic": self.canonical_network_origin,
            "receipt": {
                "batch_id": receipt.batch_id,
                "expected_market_ids": list(receipt.expected_market_ids),
                "observed_market_ids": list(receipt.observed_market_ids),
                "missing_market_ids": list(receipt.missing_market_ids),
                "unexpected_market_ids": list(receipt.unexpected_market_ids),
                "status": receipt.status.value,
                "failure_kind": receipt.failure_kind,
                "failure_code": receipt.failure_code,
                "payload_sha256": receipt.payload_sha256,
                "receipt_id": receipt.receipt_id,
            },
            "structural_exact_response": (
                receipt.status is exact_receipt_status
            ),
            "origin_authority_requires_assert_issued": True,
            "provider_observation_authenticated": False,
            "provider_freshness_proven": False,
            "provider_dispatch_authorized": False,
            "provider_write_authorized": False,
            "execution_authorized": False,
        }
        try:
            encoded = json_dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise MarketBookBatchTransportError(
                "MarketBook transport authority fingerprint is noncanonical"
            ) from exc
        return hash_factory(encoded).hexdigest()
    rate_reserve = BetfairMarketBookPerMarketRateGate.reserve
    concurrency_begin = BetfairMarketBookProjectionConcurrencyGate.begin
    concurrency_complete = BetfairMarketBookProjectionConcurrencyGate.complete
    core_read_market_book_batch = _read_market_book_batch
    canonical_batch = _canonical_batch
    params_for_batch = _params_for_batch
    market_book_request_budget = _transport._market_book_request_budget
    post_market_book_readonly = _transport._post_market_book_readonly
    receipt_from_response = MarketBookBatchReceipt.from_response
    result_factory = MarketBookBatchTransportResult
    history_type = MarketBookAttemptHistory
    execution_type = MarketBookBatchAttemptExecution
    exact_receipt_status = BatchReceiptStatus.EXACT_RESPONSE
    incomplete_receipt_status = BatchReceiptStatus.INCOMPLETE_RESPONSE
    exact_response_outcome = MarketBookAttemptOutcome.EXACT_RESPONSE
    incomplete_response_outcome = MarketBookAttemptOutcome.INCOMPLETE_RESPONSE
    response_outcomes = frozenset(
        {exact_response_outcome, incomplete_response_outcome}
    )
    all_attempt_outcomes = frozenset(MarketBookAttemptOutcome)
    batch_transport_error_type = MarketBookBatchTransportError
    validate_history = MarketBookAttemptHistory.__post_init__
    admission_error_type = MarketBookBatchAdmissionError
    post_dispatch_failure_type = MarketBookPostDispatchFailure
    protocol_error_type = _transport.BetfairMarketBookProtocolError
    completeness_error_type = MarketBookCompletenessError
    not_dispatched_rate = MarketBookAttemptOutcome.NOT_DISPATCHED_RATE
    not_dispatched_concurrency = MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY
    dispatch_instant = _dispatch_instant
    token = _token
    release_projection_lease = _release_projection_lease
    release_projection_lease_after_failure = _release_projection_lease_after_failure

    def read_market_book_batch(
        client: _base.BetfairReadOnlyClient,
        plan: MarketBookReadPlan,
        *,
        batch_id: str,
        request_id: str,
        scheduled_at: datetime,
        rate_gate: BetfairMarketBookPerMarketRateGate,
        concurrency_gate: BetfairMarketBookProjectionConcurrencyGate,
    ) -> MarketBookBatchTransportResult:
        if type(client) is not _base.BetfairReadOnlyClient:
            raise TypeError("client must be an exact BetfairReadOnlyClient")
        if type(rate_gate) is not BetfairMarketBookPerMarketRateGate:
            raise TypeError("rate_gate must be exact BetfairMarketBookPerMarketRateGate")
        if type(concurrency_gate) is not BetfairMarketBookProjectionConcurrencyGate:
            raise TypeError(
                "concurrency_gate must be exact BetfairMarketBookProjectionConcurrencyGate"
            )
        request = token(request_id, "request_id")
        instant = dispatch_instant(client, scheduled_at)

        batch = canonical_batch(plan, batch_id)
        params = params_for_batch(plan, batch)
        wire_budget = market_book_request_budget(params)
        if wire_budget.evidence_id != batch.budget_evidence_id:
            raise MarketBookBatchTransportError(
                "wire-derived request budget does not match the canonical planned batch"
            )

        contract = plan.request_contract_payload
        try:
            concurrency_decision = concurrency_begin(
                concurrency_gate,
                request,
                observed_at=instant,
                has_order_projection=contract["order_projection"] is not None,
                has_match_projection=contract["match_projection"] is not None,
            )
        except Exception as exc:
            raise admission_error_type(
                not_dispatched_concurrency,
                "MarketBook projection concurrency gate could not establish local admission",
            ) from exc
        if concurrency_decision.allowed is not True:
            raise admission_error_type(
                not_dispatched_concurrency,
                "MarketBook projection concurrency gate denied local admission",
            )

        lease_generation = concurrency_decision.lease_generation
        try:
            rate_decision = rate_reserve(
                rate_gate,
                batch.market_ids,
                scheduled_at=instant,
            )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                release_projection_lease_after_failure(
                    client,
                    concurrency_gate,
                    request,
                    lease_generation,
                    exc,
                    complete=concurrency_complete,
                )
                raise
            denial = admission_error_type(
                not_dispatched_rate,
                "MarketBook per-market rate gate could not establish local admission",
            )
            release_projection_lease_after_failure(
                client,
                concurrency_gate,
                request,
                lease_generation,
                denial,
                complete=concurrency_complete,
            )
            raise denial from exc
        if rate_decision.allowed is not True:
            denial = admission_error_type(
                not_dispatched_rate,
                "MarketBook per-market rate gate denied local admission",
            )
            release_projection_lease_after_failure(
                client,
                concurrency_gate,
                request,
                lease_generation,
                denial,
                complete=concurrency_complete,
            )
            raise denial

        try:
            result = core_read_market_book_batch(
                client,
                plan,
                batch_id=batch_id,
                canonical_batch=canonical_batch,
                params_for_batch=params_for_batch,
                request_budget=market_book_request_budget,
                post_readonly=post_market_book_readonly,
                receipt_from_response=receipt_from_response,
                result_factory=result_factory,
                post_dispatch_failure_type=post_dispatch_failure_type,
                protocol_error_type=protocol_error_type,
                completeness_error_type=completeness_error_type,
            )
        except BaseException as exc:
            # Parent process-control interruption must not strand a locally
            # admitted projection lease. Cleanup is bounded/local and the
            # original BaseException is re-raised unchanged.
            release_projection_lease_after_failure(
                client,
                concurrency_gate,
                request,
                lease_generation,
                exc,
                complete=concurrency_complete,
            )
            raise
        else:
            try:
                release_projection_lease(
                    client,
                    concurrency_gate,
                    request,
                    lease_generation,
                    complete=concurrency_complete,
                )
            except BaseException as exc:
                if isinstance(exc, Exception):
                    raise post_dispatch_failure_type(
                        "provider read completed but projection lease cleanup failed"
                    ) from exc
                # Process control may land after the provider response but
                # before local lease release completes. Retry that local cleanup
                # once, then preserve the original interruption unchanged.
                release_projection_lease_after_failure(
                    client,
                    concurrency_gate,
                    request,
                    lease_generation,
                    exc,
                    complete=concurrency_complete,
                )
                raise
        # Re-run the original structural validator before minting closure-local
        # authority. Dataclass __post_init__ and fingerprint methods are mutable
        # class attributes after import and therefore cannot be trusted here.
        if type(result) is not result_factory:
            raise post_dispatch_failure_type(
                "canonical MarketBook transport returned a noncanonical result type"
            )
        validate(result)
        key = id(result)

        def forget(_weakref: object, *, result_id: int = key) -> None:
            issued.pop(result_id, None)

        issued[key] = (
            ref(result, forget),
            authority_fingerprint(result),
            result.canonical_network_origin,
        )
        return result

    def _record(
        self: MarketBookBatchTransportResult,
    ) -> tuple[object, str, bool]:
        validate(self)
        record = issued.get(id(self))
        if record is None or record[0]() is not self:
            raise MarketBookBatchTransportError(
                "MarketBook batch transport result was not issued by canonical transport"
            )
        if record[1] != authority_fingerprint(self):
            raise MarketBookBatchTransportError(
                "MarketBook batch transport result changed after canonical issuance"
            )
        return record

    def assert_issued(self: MarketBookBatchTransportResult) -> None:
        _record(self)

    def assert_canonical_network_origin(
        self: MarketBookBatchTransportResult,
    ) -> None:
        record = _record(self)
        if not record[2]:
            raise MarketBookBatchTransportError(
                "MarketBook batch transport result lacks canonical network origin"
            )

    def validate_attempt_execution_bound(
        self: MarketBookBatchAttemptExecution,
    ) -> None:
        if type(self.history) is not history_type:
            raise TypeError("history must be an exact MarketBookAttemptHistory")
        try:
            validate_history(self.history)
        except Exception as exc:
            raise batch_transport_error_type(
                "attempt execution history is not canonical"
            ) from exc
        if self.outcome not in all_attempt_outcomes:
            raise TypeError("outcome must be MarketBookAttemptOutcome")
        if not self.history.records:
            raise batch_transport_error_type(
                "attempt execution requires an appended canonical history record"
            )
        latest = self.history.records[-1]
        if latest.outcome is not self.outcome:
            raise batch_transport_error_type(
                "attempt execution outcome does not match latest history record"
            )
        if self.outcome in response_outcomes:
            if type(self.result) is not result_factory:
                raise batch_transport_error_type(
                    "response outcome requires canonical transport result"
                )
            _record(self.result)
            if (
                self.result.plan_id != self.history.plan.plan_id
                or self.result.request_contract_id
                != self.history.plan.request_contract_id
            ):
                raise batch_transport_error_type(
                    "attempt execution result is bound to another history plan"
                )
            if latest.batch_id != self.result.batch_id:
                raise batch_transport_error_type(
                    "attempt execution result is bound to another history batch"
                )
            expected_receipt_status = (
                exact_receipt_status
                if self.outcome is exact_response_outcome
                else incomplete_receipt_status
            )
            if self.result.receipt.status is not expected_receipt_status:
                raise batch_transport_error_type(
                    "attempt execution result receipt status contradicts outcome"
                )
            if (
                self.outcome is exact_response_outcome
                and latest.exact_receipt != self.result.receipt
            ):
                raise batch_transport_error_type(
                    "attempt execution exact receipt does not match history"
                )
        elif self.result is not None:
            raise batch_transport_error_type(
                "nonresponse outcome cannot carry transport result"
            )

    append_attempt_unbound = append_market_book_transport_attempt

    def append_market_book_transport_attempt_bound(
        history: MarketBookAttemptHistory,
        result: MarketBookBatchTransportResult,
        *,
        attempt_id: str,
        required: bool,
    ) -> MarketBookAttemptHistory:
        # Consumer authority must not depend on mutable class dispatch.  Prove
        # canonical issuance through the closure-local registry before entering
        # the ordinary structural append path.
        if type(result) is not result_factory:
            raise TypeError("result must be an exact MarketBookBatchTransportResult")
        _record(result)
        return append_attempt_unbound(
            history,
            result,
            attempt_id=attempt_id,
            required=required,
        )

    globals()["read_market_book_batch"] = read_market_book_batch
    globals()["append_market_book_transport_attempt"] = (
        append_market_book_transport_attempt_bound
    )
    MarketBookBatchTransportResult.assert_issued = assert_issued
    MarketBookBatchTransportResult.assert_canonical_network_origin = (
        assert_canonical_network_origin
    )
    execution_type.__post_init__ = validate_attempt_execution_bound


_install_transport_result_authority()
del _install_transport_result_authority


def _execute_market_book_batch_attempt(
    client: _base.BetfairReadOnlyClient,
    history: MarketBookAttemptHistory,
    *,
    batch_id: str,
    attempt_id: str,
    required: bool,
    request_id: str,
    scheduled_at: datetime,
    rate_gate: BetfairMarketBookPerMarketRateGate,
    concurrency_gate: BetfairMarketBookProjectionConcurrencyGate,
    read_batch: Callable[..., MarketBookBatchTransportResult],
    append_response: Callable[..., MarketBookAttemptHistory],
    append_nonresponse: Callable[..., MarketBookAttemptHistory],
    freeze_plan: Callable[[MarketBookReadPlan], MarketBookReadPlan],
    freeze_history: Callable[
        [MarketBookReadPlan, MarketBookAttemptHistory],
        MarketBookAttemptHistory,
    ],
    token: Callable[[object, str], str],
    canonical_batch: Callable[[MarketBookReadPlan, str], MarketBookReadBatch],
    admission_error_type: type[MarketBookBatchAdmissionError],
    post_dispatch_failure_type: type[MarketBookPostDispatchFailure],
    transport_error_type: type[BaseException],
    provider_error_type: type[BaseException],
    protocol_error_type: type[BaseException],
    transport_failure_outcome: MarketBookAttemptOutcome,
    provider_failure_outcome: MarketBookAttemptOutcome,
    parse_failure_outcome: MarketBookAttemptOutcome,
    exact_response_outcome: MarketBookAttemptOutcome,
    incomplete_response_outcome: MarketBookAttemptOutcome,
    exact_response_status: BatchReceiptStatus,
    make_execution: Callable[
        [MarketBookAttemptHistory, MarketBookAttemptOutcome, MarketBookBatchTransportResult | None],
        MarketBookBatchAttemptExecution,
    ],
) -> MarketBookBatchAttemptExecution:
    """Execute one admitted read and durably classify its structural attempt truth."""

    if type(history) is not MarketBookAttemptHistory:
        raise TypeError("history must be an exact MarketBookAttemptHistory")
    # Freeze a canonical plan+history snapshot before any provider I/O. Frozen
    # dataclasses can still be adversarially mutated through object.__setattr__;
    # a detached round-trip snapshot preserves the exact pre-dispatch denominator
    # so post-I/O failures can always be recorded against what was actually sent.
    frozen_plan = freeze_plan(history.plan)
    frozen_history = freeze_history(frozen_plan, history)
    attempt = token(attempt_id, "attempt_id")
    if type(required) is not bool:
        raise TypeError("required must be exact bool")
    if any(record.attempt_id == attempt for record in frozen_history.records):
        raise MarketBookBatchTransportError(
            "attempt_id is already present in canonical MarketBook history"
        )
    canonical_batch(frozen_history.plan, batch_id)

    try:
        result = read_batch(
            client,
            frozen_history.plan,
            batch_id=batch_id,
            request_id=request_id,
            scheduled_at=scheduled_at,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
        )
    except admission_error_type as exc:
        outcome = exc.outcome
        updated = append_nonresponse(
            frozen_history,
            batch_id=batch_id,
            attempt_id=attempt,
            required=required,
            outcome=outcome,
        )
        return make_execution(updated, outcome, None)
    except (post_dispatch_failure_type, transport_error_type):
        outcome = transport_failure_outcome
        updated = append_nonresponse(
            frozen_history,
            batch_id=batch_id,
            attempt_id=attempt,
            required=required,
            outcome=outcome,
        )
        return make_execution(updated, outcome, None)
    except provider_error_type:
        outcome = provider_failure_outcome
        updated = append_nonresponse(
            frozen_history,
            batch_id=batch_id,
            attempt_id=attempt,
            required=required,
            outcome=outcome,
        )
        return make_execution(updated, outcome, None)
    except protocol_error_type:
        outcome = parse_failure_outcome
        updated = append_nonresponse(
            frozen_history,
            batch_id=batch_id,
            attempt_id=attempt,
            required=required,
            outcome=outcome,
        )
        return make_execution(updated, outcome, None)

    updated = append_response(
        frozen_history,
        result,
        attempt_id=attempt,
        required=required,
    )
    outcome = (
        exact_response_outcome
        if result.receipt.status is exact_response_status
        else incomplete_response_outcome
    )
    return make_execution(updated, outcome, result)


def _install_attempt_executor() -> None:
    core = _execute_market_book_batch_attempt
    canonical_read_batch = read_market_book_batch
    canonical_append_response = append_market_book_transport_attempt
    canonical_append_nonresponse = _append_nonresponse_attempt
    canonical_execution_type = MarketBookBatchAttemptExecution
    canonical_execution_validate = MarketBookBatchAttemptExecution.__post_init__
    canonical_plan_type = MarketBookReadPlan
    canonical_plan_validate = MarketBookReadPlan.__post_init__
    canonical_history_validate = MarketBookAttemptHistory.__post_init__
    canonical_make_history = _make_attempt_history
    deep_copy = deepcopy
    canonical_token = _token
    canonical_batch = _canonical_batch
    canonical_admission_error_type = MarketBookBatchAdmissionError
    canonical_post_dispatch_failure_type = MarketBookPostDispatchFailure
    canonical_transport_error_type = _transport.BetfairMarketBookTransportError
    canonical_provider_error_type = _transport.BetfairMarketBookProviderError
    canonical_protocol_error_type = _transport.BetfairMarketBookProtocolError
    canonical_transport_failure_outcome = MarketBookAttemptOutcome.TRANSPORT_FAILURE
    canonical_provider_failure_outcome = MarketBookAttemptOutcome.PROVIDER_FAILURE
    canonical_parse_failure_outcome = MarketBookAttemptOutcome.PARSE_FAILURE
    canonical_exact_response_outcome = MarketBookAttemptOutcome.EXACT_RESPONSE
    canonical_incomplete_response_outcome = MarketBookAttemptOutcome.INCOMPLETE_RESPONSE
    canonical_exact_response_status = BatchReceiptStatus.EXACT_RESPONSE

    def freeze_plan(plan: MarketBookReadPlan) -> MarketBookReadPlan:
        frozen = deep_copy(plan)
        if type(frozen) is not canonical_plan_type:
            raise TypeError("plan snapshot must preserve exact MarketBookReadPlan type")
        canonical_plan_validate(frozen)
        return frozen

    def freeze_history(
        plan: MarketBookReadPlan,
        history: MarketBookAttemptHistory,
    ) -> MarketBookAttemptHistory:
        records = deep_copy(history.records)
        return canonical_make_history(
            plan,
            records,
            validate_history=canonical_history_validate,
        )

    def make_execution(
        history: MarketBookAttemptHistory,
        outcome: MarketBookAttemptOutcome,
        result: MarketBookBatchTransportResult | None,
    ) -> MarketBookBatchAttemptExecution:
        # Avoid generated dataclass __init__ -> mutable self.__post_init__
        # dispatch on the canonical executor path. Construct the exact frozen
        # DTO and invoke the already closure-bound validator directly.
        execution = object.__new__(canonical_execution_type)
        object.__setattr__(execution, "history", history)
        object.__setattr__(execution, "outcome", outcome)
        object.__setattr__(execution, "result", result)
        canonical_execution_validate(execution)
        return execution

    def execute_market_book_batch_attempt(
        client: _base.BetfairReadOnlyClient,
        history: MarketBookAttemptHistory,
        *,
        batch_id: str,
        attempt_id: str,
        required: bool,
        request_id: str,
        scheduled_at: datetime,
        rate_gate: BetfairMarketBookPerMarketRateGate,
        concurrency_gate: BetfairMarketBookProjectionConcurrencyGate,
    ) -> MarketBookBatchAttemptExecution:
        return core(
            client,
            history,
            batch_id=batch_id,
            attempt_id=attempt_id,
            required=required,
            request_id=request_id,
            scheduled_at=scheduled_at,
            rate_gate=rate_gate,
            concurrency_gate=concurrency_gate,
            read_batch=canonical_read_batch,
            append_response=canonical_append_response,
            append_nonresponse=canonical_append_nonresponse,
            freeze_plan=freeze_plan,
            freeze_history=freeze_history,
            token=canonical_token,
            canonical_batch=canonical_batch,
            admission_error_type=canonical_admission_error_type,
            post_dispatch_failure_type=canonical_post_dispatch_failure_type,
            transport_error_type=canonical_transport_error_type,
            provider_error_type=canonical_provider_error_type,
            protocol_error_type=canonical_protocol_error_type,
            transport_failure_outcome=canonical_transport_failure_outcome,
            provider_failure_outcome=canonical_provider_failure_outcome,
            parse_failure_outcome=canonical_parse_failure_outcome,
            exact_response_outcome=canonical_exact_response_outcome,
            incomplete_response_outcome=canonical_incomplete_response_outcome,
            exact_response_status=canonical_exact_response_status,
            make_execution=make_execution,
        )

    globals()["execute_market_book_batch_attempt"] = execute_market_book_batch_attempt


_install_attempt_executor()
del _install_attempt_executor
