from __future__ import annotations

"""Fresh product-owned re-resolution verifier for Betfair standard-LIMIT bounds.

``BetfairStandardLimitPriceBoundEvidence`` and ``BoundSupervisedExecutionPlan``
are ordinary Python values, not unforgeable capabilities. Positive product
authority therefore starts from the independent durable supervised-plan issuance
store. The execution ledger is consulted only after that provenance is restored,
for exact reservation/approval execution-state continuity. A caller cannot supply
a bound DTO to this public verifier.

The verifier deliberately does not late-dispatch through its public resolver
module global.  One exact resolver/writer graph is captured when this module is
loaded and witnessed immediately before and after fresh re-resolution.  This
prevents a caller from installing the same hostile resolver in issuance and in
verification and thereby turning common-mode agreement into provider authority.
"""

from decimal import Decimal

from . import betfair_standard_limit_price_bound as _price_bound_module
from .betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
)
from .real_execution_ledger import ExecutionAction, RealExecutionLedger
from .supervised_execution import BoundSupervisedExecutionPlan
from .supervised_plan_issuance import (
    SupervisedPlanIssuanceError,
    SupervisedPlanIssuanceStore,
)


# Compatibility/debug alias only.  The authoritative verifier entrypoint below
# never late-dispatches through this mutable module global.
resolve_betfair_standard_limit_price_bound = (
    _price_bound_module.resolve_betfair_standard_limit_price_bound
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


def _build_product_verifier():
    """Capture the exact resolver graph before any caller can use the verifier."""

    resolver = _price_bound_module.resolve_betfair_standard_limit_price_bound
    resolver_code = resolver.__code__

    direct_helpers = (
        ("resolver text", "_text", _price_bound_module._text),
        (
            "resolver positive decimal",
            "_positive_decimal",
            _price_bound_module._positive_decimal,
        ),
        ("resolver digest", "_digest", _price_bound_module._digest),
        (
            "canonical instruction projection",
            "_canonical_instruction_projection",
            _price_bound_module._canonical_instruction_projection,
        ),
        (
            "canonical request capture",
            "_capture_canonical_instruction",
            _price_bound_module._capture_canonical_instruction,
        ),
        ("evidence issuer", "_issue_evidence", _price_bound_module._issue_evidence),
    )
    helper_witnesses = tuple(
        (label, name, function, function.__code__)
        for label, name, function in direct_helpers
    )

    captured_place_action = _price_bound_module._CANONICAL_PLACE_ACTION
    captured_place_action_code = captured_place_action.__code__
    execution_to_dict = ExecutionAction.to_dict
    execution_to_dict_code = execution_to_dict.__code__
    bound_verify = BoundSupervisedExecutionPlan.verify_binding
    bound_verify_code = bound_verify.__code__
    bound_action_for = BoundSupervisedExecutionPlan.action_for
    bound_action_for_code = bound_action_for.__code__

    def require_canonical_resolver_authority() -> None:
        if (
            _price_bound_module.resolve_betfair_standard_limit_price_bound is not resolver
            or resolver.__code__ is not resolver_code
        ):
            raise BetfairStandardLimitPriceBoundError(
                "canonical Betfair price-bound resolver authority changed"
            )
        for label, name, function, code in helper_witnesses:
            if (
                getattr(_price_bound_module, name, None) is not function
                or function.__code__ is not code
            ):
                raise BetfairStandardLimitPriceBoundError(
                    f"canonical Betfair price-bound {label} authority changed"
                )
        if (
            _price_bound_module._CANONICAL_PLACE_ACTION is not captured_place_action
            or captured_place_action.__code__ is not captured_place_action_code
            or _price_bound_module.BetfairSupervisedPlaceOrdersClient.place_action
            is not captured_place_action
        ):
            raise BetfairStandardLimitPriceBoundError(
                "canonical Betfair placeOrders writer authority changed"
            )
        if (
            ExecutionAction.to_dict is not execution_to_dict
            or execution_to_dict.__code__ is not execution_to_dict_code
            or BoundSupervisedExecutionPlan.verify_binding is not bound_verify
            or bound_verify.__code__ is not bound_verify_code
            or BoundSupervisedExecutionPlan.action_for is not bound_action_for
            or bound_action_for.__code__ is not bound_action_for_code
        ):
            raise BetfairStandardLimitPriceBoundError(
                "canonical Betfair bound-plan dependency authority changed"
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
        authority.  Provider-request evidence is then re-resolved through the exact
        resolver/writer graph captured by this verifier, not through a mutable public
        resolver binding.  This makes coordinated pre-issuance/verifier rebinding
        fail closed instead of turning common-mode agreement into provider authority.
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

        require_canonical_resolver_authority()
        expected = resolver(
            bound=bound,
            action_id=action_id,
        )
        require_canonical_resolver_authority()

        _require_issuance_time_provider_request(
            issued_requests=issued.provider_requests,
            expected=expected,
        )
        if _exact_snapshot(evidence) != _exact_snapshot(expected):
            raise BetfairStandardLimitPriceBoundError(
                "price-bound evidence does not match fresh canonical re-resolution"
            )
        return expected

    verify_betfair_standard_limit_price_bound.__name__ = (
        "verify_betfair_standard_limit_price_bound"
    )
    verify_betfair_standard_limit_price_bound.__qualname__ = (
        "verify_betfair_standard_limit_price_bound"
    )
    return verify_betfair_standard_limit_price_bound


verify_betfair_standard_limit_price_bound = _build_product_verifier()
del _build_product_verifier
