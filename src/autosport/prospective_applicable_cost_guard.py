from __future__ import annotations

"""Canonical consumer boundary for prospective applicable-cost assertions.

The aggregate object is assertion/transport data, never authority by possession.
This module installs its consumer from a closure seal capturing the source module's
already-sealed resolver, validators and exact canonical types.  Later rebinding of
this module's resolver/type globals therefore cannot redirect authority.
"""

from datetime import datetime

from .model_compute_router import ModelComputeRouterStore
from .portfolio_plan import OpportunityIntent, PortfolioPlan
from . import prospective_applicable_cost as _cost_source


# Compatibility/debug mirrors only.  The installed guard captures the original
# objects below and never reads these names after module initialization.
ProspectiveApplicableCostComponent = _cost_source.ProspectiveApplicableCostComponent
ProspectiveApplicableCostError = _cost_source.ProspectiveApplicableCostError
ProspectiveApplicableCostResolution = _cost_source.ProspectiveApplicableCostResolution
resolve_prospective_applicable_costs = _cost_source.resolve_prospective_applicable_costs
resolve_prospective_applicable_costs_with_betfair_standard_limit = (
    _cost_source.resolve_prospective_applicable_costs_with_betfair_standard_limit
)

_COMPONENT_FIELDS = (
    "cost_class",
    "status",
    "reason",
    "dependency_axes",
    "source_family",
    "source_evidence_id",
    "source_sha256",
)
_RESOLUTION_FIELDS = (
    "intent_sha256",
    "opportunity_id",
    "portfolio_plan_sha256",
    "decision_at",
    "completeness",
    "total_subtractable_amount",
    "currency",
)


def _build_guard():
    canonical_resolver = _cost_source.resolve_prospective_applicable_costs
    canonical_slippage_resolver = (
        _cost_source.resolve_prospective_applicable_costs_with_betfair_standard_limit
    )
    canonical_resolver_code = canonical_resolver.__code__
    canonical_slippage_resolver_code = canonical_slippage_resolver.__code__
    validate_component = _cost_source._SEALED_COMPONENT_VALIDATOR
    validate_resolution = _cost_source._SEALED_RESOLUTION_VALIDATOR
    validate_component_code = validate_component.__code__
    validate_resolution_code = validate_resolution.__code__
    (
        error_cls,
        component_cls,
        resolution_cls,
        intent_cls,
        plan_cls,
        router_store_cls,
    ) = _cost_source._SEALED_CANONICAL_TYPES
    component_fields = tuple(_COMPONENT_FIELDS)
    canonical_getattribute = object.__getattribute__
    canonical_type = type
    canonical_len = len
    canonical_enumerate = enumerate
    canonical_zip = zip
    resolution_fields = tuple(_RESOLUTION_FIELDS)

    def sealed_validate_component(value: object) -> None:
        if validate_component.__code__ is not validate_component_code:
            raise error_cls("canonical applicable-cost component validator authority changed")
        validate_component(value)
        if validate_component.__code__ is not validate_component_code:
            raise error_cls("canonical applicable-cost component validator authority changed")

    def sealed_validate_resolution(value: object) -> None:
        if validate_resolution.__code__ is not validate_resolution_code:
            raise error_cls("canonical applicable-cost resolution validator authority changed")
        validate_resolution(value)
        if validate_resolution.__code__ is not validate_resolution_code:
            raise error_cls("canonical applicable-cost resolution validator authority changed")

    def slot_value(value: object, name: str) -> object:
        try:
            return canonical_getattribute(value, name)
        except (AttributeError, TypeError) as exc:
            raise error_cls(
                f"prospective applicable-cost assertion is missing canonical field {name}"
            ) from exc

    def assert_component_matches(
        asserted: object,
        canonical: object,
        *,
        index: int,
    ) -> None:
        if canonical_type(asserted) is not component_cls:
            raise error_cls(
                "prospective applicable-cost assertion components must use the exact canonical type"
            )
        if canonical_type(canonical) is not component_cls:
            raise error_cls(
                "canonical applicable-cost resolver returned a non-canonical component type"
            )
        # Both sides are independently shape/semantic validated with the sealed
        # validator before equality is used as an assertion check.
        sealed_validate_component(asserted)
        sealed_validate_component(canonical)
        for field in component_fields:
            if slot_value(asserted, field) != slot_value(canonical, field):
                raise error_cls(
                    "prospective applicable-cost assertion does not match canonical "
                    f"re-resolution at component {index} field {field}"
                )

    def require_canonical_prospective_applicable_costs(
        asserted: ProspectiveApplicableCostResolution,
        *,
        intent: OpportunityIntent,
        plan: PortfolioPlan,
        router_store: ModelComputeRouterStore,
        model_request_id: str,
        decision_at: datetime,
    ) -> ProspectiveApplicableCostResolution:
        """Return fresh sealed product truth only when an assertion matches it."""

        if canonical_type(asserted) is not resolution_cls:
            raise error_cls(
                "prospective applicable-cost assertion must use the exact canonical type"
            )
        # Reject object.__new__ forgeries, including exact-class positive/non-schema
        # objects, before any authority-bearing source read.
        sealed_validate_resolution(asserted)

        if canonical_type(intent) is not intent_cls:
            raise error_cls("intent must be the exact canonical OpportunityIntent type")
        if canonical_type(plan) is not plan_cls:
            raise error_cls("plan must be the exact canonical PortfolioPlan type")
        if canonical_type(router_store) is not router_store_cls:
            raise error_cls(
                "router_store must be the exact canonical ModelComputeRouterStore type"
            )

        if canonical_resolver.__code__ is not canonical_resolver_code:
            raise error_cls("canonical applicable-cost resolver authority changed")
        canonical = canonical_resolver(
            intent=intent,
            plan=plan,
            router_store=router_store,
            model_request_id=model_request_id,
            decision_at=decision_at,
        )
        if canonical_resolver.__code__ is not canonical_resolver_code:
            raise error_cls("canonical applicable-cost resolver authority changed")
        if canonical_type(canonical) is not resolution_cls:
            raise error_cls(
                "canonical applicable-cost resolver returned a non-canonical resolution type"
            )
        # Do not trust the resolver return merely because it has the exact class.
        # Re-run the independently captured schema-v2 validator before returning it.
        sealed_validate_resolution(canonical)

        for field in resolution_fields:
            if slot_value(asserted, field) != slot_value(canonical, field):
                raise error_cls(
                    "prospective applicable-cost assertion does not match canonical "
                    f"re-resolution at field {field}"
                )

        asserted_components = slot_value(asserted, "components")
        canonical_components = slot_value(canonical, "components")
        if canonical_type(asserted_components) is not tuple or canonical_type(canonical_components) is not tuple:
            raise error_cls(
                "prospective applicable-cost component collections must be canonical tuples"
            )
        if canonical_len(asserted_components) != canonical_len(canonical_components):
            raise error_cls(
                "prospective applicable-cost assertion component count does not match canonical re-resolution"
            )
        for index, (asserted_component, canonical_component) in canonical_enumerate(
            canonical_zip(asserted_components, canonical_components, strict=True)
        ):
            assert_component_matches(
                asserted_component,
                canonical_component,
                index=index,
            )

        # The caller-supplied object never crosses the authority boundary.
        return canonical

    def require_canonical_prospective_applicable_costs_with_betfair_standard_limit(
        asserted: ProspectiveApplicableCostResolution,
        *,
        intent: OpportunityIntent,
        plan: PortfolioPlan,
        router_store: ModelComputeRouterStore,
        model_request_id: str,
        decision_at: datetime,
        slippage_evidence,
        ledger,
        issuance_store,
        runtime_profile,
        execution_plan_id: str,
        action_id: str,
    ) -> ProspectiveApplicableCostResolution:
        """Return fresh source-backed truth only after canonical product verification."""

        if type(asserted) is not resolution_cls:
            raise error_cls(
                "prospective applicable-cost assertion must use the exact canonical type"
            )
        sealed_validate_resolution(asserted)
        if type(intent) is not intent_cls:
            raise error_cls("intent must be the exact canonical OpportunityIntent type")
        if type(plan) is not plan_cls:
            raise error_cls("plan must be the exact canonical PortfolioPlan type")
        if type(router_store) is not router_store_cls:
            raise error_cls(
                "router_store must be the exact canonical ModelComputeRouterStore type"
            )

        if canonical_slippage_resolver.__code__ is not canonical_slippage_resolver_code:
            raise error_cls("canonical Betfair applicable-cost resolver authority changed")
        canonical = canonical_slippage_resolver(
            intent=intent,
            plan=plan,
            router_store=router_store,
            model_request_id=model_request_id,
            decision_at=decision_at,
            slippage_evidence=slippage_evidence,
            ledger=ledger,
            issuance_store=issuance_store,
            runtime_profile=runtime_profile,
            execution_plan_id=execution_plan_id,
            action_id=action_id,
        )
        if canonical_slippage_resolver.__code__ is not canonical_slippage_resolver_code:
            raise error_cls("canonical Betfair applicable-cost resolver authority changed")
        if type(canonical) is not resolution_cls:
            raise error_cls(
                "canonical applicable-cost resolver returned a non-canonical resolution type"
            )
        sealed_validate_resolution(canonical)
        for field in resolution_fields:
            if slot_value(asserted, field) != slot_value(canonical, field):
                raise error_cls(
                    "prospective applicable-cost assertion does not match canonical "
                    f"re-resolution at field {field}"
                )
        asserted_components = slot_value(asserted, "components")
        canonical_components = slot_value(canonical, "components")
        if type(asserted_components) is not tuple or type(canonical_components) is not tuple:
            raise error_cls(
                "prospective applicable-cost component collections must be canonical tuples"
            )
        if len(asserted_components) != len(canonical_components):
            raise error_cls(
                "prospective applicable-cost assertion component count does not match canonical re-resolution"
            )
        for index, (asserted_component, canonical_component) in enumerate(
            zip(asserted_components, canonical_components, strict=True)
        ):
            assert_component_matches(
                asserted_component,
                canonical_component,
                index=index,
            )
        return canonical

    return (
        require_canonical_prospective_applicable_costs,
        require_canonical_prospective_applicable_costs_with_betfair_standard_limit,
    )


(
    require_canonical_prospective_applicable_costs,
    require_canonical_prospective_applicable_costs_with_betfair_standard_limit,
) = _build_guard()
