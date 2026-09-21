from __future__ import annotations

"""Fresh product-owned re-resolution verifier for Betfair standard-LIMIT bounds.

``BetfairStandardLimitPriceBoundEvidence`` and ``BoundSupervisedExecutionPlan``
are ordinary Python values, not unforgeable capabilities. Positive product
authority therefore starts from the existing durable ``RealExecutionLedger``:
the exact plan id/fingerprint and supervised-approval binding must already be
product-issued there before the canonical fail-before-I/O request resolver is
allowed to supply a record for downstream acceptance.
"""

from decimal import Decimal

from .betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
    resolve_betfair_standard_limit_price_bound,
)
from .real_execution_ledger import RealExecutionLedger
from .supervised_execution import BoundSupervisedExecutionPlan


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


def _require_product_owned_bound(
    *,
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
) -> None:
    """Bind caller DTO self-consistency to the existing durable product authority."""

    if type(ledger) is not RealExecutionLedger:
        raise BetfairStandardLimitPriceBoundError(
            "ledger must be the exact canonical RealExecutionLedger type"
        )
    if type(bound) is not BoundSupervisedExecutionPlan:
        raise BetfairStandardLimitPriceBoundError(
            "bound must be the exact canonical BoundSupervisedExecutionPlan type"
        )

    # Call class implementations directly so instance-level method shadowing
    # cannot replace either the deterministic binding proof or ledger reads.
    BoundSupervisedExecutionPlan.verify_binding(bound)
    try:
        saga = RealExecutionLedger.saga(ledger, bound.execution_plan.plan_id)
    except KeyError as exc:
        raise BetfairStandardLimitPriceBoundError(
            "bound execution plan is not durably reserved by product authority"
        ) from exc
    if saga.plan_fingerprint != bound.execution_plan.fingerprint:
        raise BetfairStandardLimitPriceBoundError(
            "durable execution-plan fingerprint mismatches caller bound"
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


def verify_betfair_standard_limit_price_bound(
    *,
    evidence: BetfairStandardLimitPriceBoundEvidence,
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    action_id: str,
) -> BetfairStandardLimitPriceBoundEvidence:
    """Accept a candidate record only by product-owned durable re-resolution.

    The candidate record and caller-supplied bound are never the trust root.
    First the exact execution plan must exist in the canonical durable ledger
    with the same immutable fingerprint and active supervised-approval binding.
    Only then does the existing resolver capture the current production
    ``place_action`` request through its fail-before-I/O transport. Every
    authority-bearing field is exact-compared, and the fresh record is returned.
    """

    _require_product_owned_bound(ledger=ledger, bound=bound)
    expected = resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action_id,
    )
    if _exact_snapshot(evidence) != _exact_snapshot(expected):
        raise BetfairStandardLimitPriceBoundError(
            "price-bound evidence does not match fresh canonical re-resolution"
        )
    return expected
