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
    validate_component = _cost_source._SEALED_COMPONENT_VALIDATOR
    validate_resolution = _cost_source._SEALED_RESOLUTION_VALIDATOR
    (
        error_cls,
        component_cls,
        resolution_cls,
        intent_cls,
        plan_cls,
        router_store_cls,
    ) = _cost_source._SEALED_CANONICAL_TYPES
    component_fields = tuple(_COMPONENT_FIELDS)
    resolution_fields = tuple(_RESOLUTION_FIELDS)

    def slot_value(value: object, name: str) -> object:
        try:
            return object.__getattribute__(value, name)
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
        if type(asserted) is not component_cls:
            raise error_cls(
                "prospective applicable-cost assertion components must use the exact canonical type"
            )
        if type(canonical) is not component_cls:
            raise error_cls(
                "canonical applicable-cost resolver returned a non-canonical component type"
            )
        # Both sides are independently shape/semantic validated with the sealed
        # validator before equality is used as an assertion check.
        validate_component(asserted)
        validate_component(canonical)
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

        if type(asserted) is not resolution_cls:
            raise error_cls(
                "prospective applicable-cost assertion must use the exact canonical type"
            )
        # Reject object.__new__ forgeries, including exact-class positive/non-schema
        # objects, before any authority-bearing source read.
        validate_resolution(asserted)

        if type(intent) is not intent_cls:
            raise error_cls("intent must be the exact canonical OpportunityIntent type")
        if type(plan) is not plan_cls:
            raise error_cls("plan must be the exact canonical PortfolioPlan type")
        if type(router_store) is not router_store_cls:
            raise error_cls(
                "router_store must be the exact canonical ModelComputeRouterStore type"
            )

        canonical = canonical_resolver(
            intent=intent,
            plan=plan,
            router_store=router_store,
            model_request_id=model_request_id,
            decision_at=decision_at,
        )
        if type(canonical) is not resolution_cls:
            raise error_cls(
                "canonical applicable-cost resolver returned a non-canonical resolution type"
            )
        # Do not trust the resolver return merely because it has the exact class.
        # Re-run the independently captured schema-v2 validator before returning it.
        validate_resolution(canonical)

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

        # The caller-supplied object never crosses the authority boundary.
        return canonical

    return require_canonical_prospective_applicable_costs


require_canonical_prospective_applicable_costs = _build_guard()
