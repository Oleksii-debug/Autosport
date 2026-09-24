from __future__ import annotations

"""Fresh product-owned re-resolution verifier for Betfair standard-LIMIT bounds.

``BetfairStandardLimitPriceBoundEvidence`` and ``BoundSupervisedExecutionPlan``
are ordinary Python values, not unforgeable capabilities. Positive product
authority therefore starts from the independent durable supervised-plan issuance
store. The execution ledger is consulted only after that provenance is restored,
for exact reservation/approval execution-state continuity. A caller cannot supply
a bound DTO to this public verifier.
"""

from decimal import Decimal

from .betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
    resolve_betfair_standard_limit_price_bound,
)
from .real_execution_ledger import RealExecutionLedger
from .supervised_execution import BoundSupervisedExecutionPlan
from .supervised_plan_issuance import (
    SupervisedPlanIssuanceError,
    SupervisedPlanIssuanceStore,
)


_EVIDENCE_FIELDS = (
    "execution_plan_id",
    "execution_plan_sha256",
    "portfolio_plan_sha256",
    "intent_id",
    "intent_sha256",
    "action_id",
    "bookmaker_id",
    "account_id",
    "event_id",
    "market_id",
    "selection_id",
    "side",
    "requested_stake",
    "price_floor_odds",
    "quote_id",
    "quote_observed_at",
    "quote_expires_at",
    "decision_at",
    "instruction_sha256",
    "provider_contract_id",
    "provider_contract_ref",
    "write_adapter_id",
    "write_adapter_version",
    "status",
    "matchme_applicability_proven",
    "zero_adverse_price_deterioration",
    "execution_feasibility_proven",
    "realized_price_exact",
)


def _exact_snapshot(
    evidence: BetfairStandardLimitPriceBoundEvidence,
) -> tuple[tuple[str, type[object], object], ...]:
    if type(evidence) is not BetfairStandardLimitPriceBoundEvidence:
        raise BetfairStandardLimitPriceBoundError(
            "price-bound evidence must be the exact canonical evidence type"
        )

    snapshot: list[tuple[str, type[object], object]] = []
    for field in _EVIDENCE_FIELDS:
        try:
            value = object.__getattribute__(evidence, field)
        except (AttributeError, TypeError) as exc:
            raise BetfairStandardLimitPriceBoundError(
                "price-bound evidence is incomplete"
            ) from exc
        comparable: object = str(value) if type(value) is Decimal else value
        snapshot.append((field, type(value), comparable))
    return tuple(snapshot)


def _require_execution_state_continuity(
    *,
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
) -> None:
    """Require ledger continuity without allowing ledger rows to mint provenance."""

    if type(ledger) is not RealExecutionLedger:
        raise BetfairStandardLimitPriceBoundError(
            "ledger must be the exact canonical RealExecutionLedger type"
        )
    if type(bound) is not BoundSupervisedExecutionPlan:
        raise BetfairStandardLimitPriceBoundError(
            "issued bound must be the exact canonical BoundSupervisedExecutionPlan type"
        )

    BoundSupervisedExecutionPlan.verify_binding(bound)
    try:
        saga = RealExecutionLedger.saga(ledger, bound.execution_plan.plan_id)
    except KeyError as exc:
        raise BetfairStandardLimitPriceBoundError(
            "product-issued execution plan is not durably reserved"
        ) from exc
    if saga.plan_fingerprint != bound.execution_plan.fingerprint:
        raise BetfairStandardLimitPriceBoundError(
            "durable execution-plan fingerprint mismatches product issuance"
        )
    if not RealExecutionLedger.supervised_approval_is_active(
        ledger,
        plan_id=bound.execution_plan.plan_id,
        approval_id=bound.execution_plan.approval_id,
        approval_fingerprint=bound.approval_fingerprint,
    ):
        raise BetfairStandardLimitPriceBoundError(
            "durable supervised approval is missing or revoked"
        )


def _require_issuance_time_provider_request(
    *,
    issued_requests: tuple[dict[str, object], ...],
    expected: BetfairStandardLimitPriceBoundEvidence,
) -> None:
    """Require that this action had positive canonical request proof at issuance."""

    matches = [
        item
        for item in issued_requests
        if item.get("action_id") == expected.action_id
    ]
    if len(matches) != 1:
        raise BetfairStandardLimitPriceBoundError(
            "Betfair request identity was not durably proven at plan issuance"
        )
    durable = matches[0]
    expected_request = {
        "action_id": expected.action_id,
        "bookmaker_id": expected.bookmaker_id,
        "account_id": expected.account_id,
        "instruction_sha256": expected.instruction_sha256,
        "write_adapter_id": expected.write_adapter_id,
        "write_adapter_version": expected.write_adapter_version,
    }
    if durable != expected_request:
        raise BetfairStandardLimitPriceBoundError(
            "durable issuance-time Betfair request identity changed"
        )


def verify_betfair_standard_limit_price_bound(
    *,
    evidence: BetfairStandardLimitPriceBoundEvidence,
    ledger: RealExecutionLedger,
    issuance_store: SupervisedPlanIssuanceStore,
    execution_plan_id: str,
    action_id: str,
) -> BetfairStandardLimitPriceBoundEvidence:
    """Accept candidate evidence only by durable product issuance re-resolution.

    The caller supplies only identities, never a bound execution DTO. The exact
    bound+approval unit is reloaded from the rollback-resistant product issuance
    authority, including current canonical provider-request projection validation.
    The execution ledger then proves only reservation/approval continuity. Finally
    the production ``place_action`` request is captured fail-before-I/O again and
    every authority-bearing evidence field is exact-compared.
    """

    if type(issuance_store) is not SupervisedPlanIssuanceStore:
        raise BetfairStandardLimitPriceBoundError(
            "issuance_store must be the exact canonical SupervisedPlanIssuanceStore type"
        )
    try:
        issued = SupervisedPlanIssuanceStore.load(
            issuance_store,
            execution_plan_id,
        )
    except SupervisedPlanIssuanceError as exc:
        raise BetfairStandardLimitPriceBoundError(
            "durable product supervised-plan issuance is missing or invalid"
        ) from exc
    bound = issued.bound
    _require_execution_state_continuity(ledger=ledger, bound=bound)
    expected = resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action_id,
    )
    _require_issuance_time_provider_request(
        issued_requests=issued.provider_requests,
        expected=expected,
    )
    if _exact_snapshot(evidence) != _exact_snapshot(expected):
        raise BetfairStandardLimitPriceBoundError(
            "price-bound evidence does not match fresh canonical re-resolution"
        )
    return expected
