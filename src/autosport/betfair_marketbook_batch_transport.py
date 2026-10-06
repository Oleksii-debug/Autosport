"""Canonical read-only Betfair MarketBook batch transport bridge.

This module composes deterministic batch planning/completeness with the canonical
budget-enforcing listMarketBook physical transport.  It does not grant provider
freshness, account identity, provider writes, execution, settlement, or real-money
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
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


class MarketBookBatchTransportError(RuntimeError):
    """Raised when a planned MarketBook batch cannot be dispatched canonically."""


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
) -> MarketBookBatchTransportResult:
    batch = _canonical_batch(plan, batch_id)
    params = _params_for_batch(plan, batch)

    wire_budget = _transport._market_book_request_budget(params)
    if wire_budget.evidence_id != batch.budget_evidence_id:
        raise MarketBookBatchTransportError(
            "wire-derived request budget does not match the canonical planned batch"
        )

    response = _transport._post_market_book_readonly(
        client,
        params=params,
        resolve_application_context=False,
    )
    if response.request_budget.evidence_id != batch.budget_evidence_id:
        raise MarketBookBatchTransportError(
            "transport request budget drifted from the canonical planned batch"
        )

    try:
        receipt = MarketBookBatchReceipt.from_response(batch, list(response.rows))
    except MarketBookCompletenessError as exc:
        raise MarketBookBatchTransportError(
            "MarketBook response cannot produce canonical structural receipt"
        ) from exc

    return MarketBookBatchTransportResult(
        plan_id=plan.plan_id,
        request_contract_id=plan.request_contract_id,
        batch_id=batch.batch_id,
        request_budget_evidence_id=response.request_budget.evidence_id,
        request_payload_sha256=response.request_payload_sha256,
        source_payload_sha256=response.source_payload_sha256,
        observed_at=response.observed_at,
        canonical_network_origin=response.network_origin,
        receipt=receipt,
    )


def append_market_book_transport_attempt(
    history: MarketBookAttemptHistory,
    result: MarketBookBatchTransportResult,
    *,
    attempt_id: str,
    required: bool,
) -> MarketBookAttemptHistory:
    """Append one issued transport result to canonical retry/gap history."""

    if type(history) is not MarketBookAttemptHistory:
        raise TypeError("history must be an exact MarketBookAttemptHistory")
    if type(result) is not MarketBookBatchTransportResult:
        raise TypeError("result must be an exact MarketBookBatchTransportResult")
    result.assert_issued()
    if history.plan.plan_id != result.plan_id:
        raise MarketBookBatchTransportError(
            "transport result is bound to another or mutated MarketBook plan"
        )

    _canonical_batch(history.plan, result.batch_id)
    previous = history.records[-1] if history.records else None
    if result.receipt.status is BatchReceiptStatus.EXACT_RESPONSE:
        outcome = MarketBookAttemptOutcome.EXACT_RESPONSE
        exact_receipt = result.receipt
    elif result.receipt.status is BatchReceiptStatus.INCOMPLETE_RESPONSE:
        outcome = MarketBookAttemptOutcome.INCOMPLETE_RESPONSE
        exact_receipt = None
    else:
        raise MarketBookBatchTransportError(
            "transport result receipt has unsupported dispatch outcome"
        )

    record = MarketBookAttemptRecord.issue(
        history.plan,
        attempt_id=attempt_id,
        sequence=len(history.records) + 1,
        batch_id=result.batch_id,
        required=required,
        outcome=outcome,
        exact_receipt=exact_receipt,
        previous_record=previous,
    )
    return MarketBookAttemptHistory(history.plan, history.records + (record,))


def _install_transport_result_authority() -> None:
    issued: dict[int, tuple[object, str, bool]] = {}
    validate = MarketBookBatchTransportResult.__post_init__

    def read_market_book_batch(
        client: _base.BetfairReadOnlyClient,
        plan: MarketBookReadPlan,
        *,
        batch_id: str,
    ) -> MarketBookBatchTransportResult:
        result = _read_market_book_batch(client, plan, batch_id=batch_id)
        key = id(result)

        def forget(_weakref: object, *, result_id: int = key) -> None:
            issued.pop(result_id, None)

        issued[key] = (
            ref(result, forget),
            result._authority_fingerprint(),
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
        if record[1] != self._authority_fingerprint():
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

    globals()["read_market_book_batch"] = read_market_book_batch
    MarketBookBatchTransportResult.assert_issued = assert_issued
    MarketBookBatchTransportResult.assert_canonical_network_origin = (
        assert_canonical_network_origin
    )


_install_transport_result_authority()
del _install_transport_result_authority
