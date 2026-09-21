from __future__ import annotations

"""Canonical consumer boundary for prospective applicable-cost assertions.

`ProspectiveApplicableCostResolution` is a transport/assertion shape, not an
issuance capability.  Python callers can allocate exact dataclass instances via
`object.__new__`, so authority-bearing consumers must never trust an object (or
its digest) merely because its local invariants are valid.

This module closes that boundary by re-resolving the current canonical product
truth from the exact intent/plan/router authorities, comparing the supplied
assertion field-for-field, and returning the newly re-resolved canonical value.
A byte-equivalent caller reconstruction is therefore harmless: it can only
assert truth that the product independently re-derived, and it is never the
object returned as authority.
"""

from datetime import datetime

from .model_compute_router import ModelComputeRouterStore
from .portfolio_plan import OpportunityIntent, PortfolioPlan
from .prospective_applicable_cost import (
    ProspectiveApplicableCostComponent,
    ProspectiveApplicableCostError,
    ProspectiveApplicableCostResolution,
    resolve_prospective_applicable_costs,
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


def _slot_value(value: object, name: str) -> object:
    """Read an exact slot without invoking caller-supplied instance dispatch."""

    try:
        return object.__getattribute__(value, name)
    except (AttributeError, TypeError) as exc:
        raise ProspectiveApplicableCostError(
            f"prospective applicable-cost assertion is missing canonical field {name}"
        ) from exc


def _assert_component_matches(
    asserted: object,
    canonical: ProspectiveApplicableCostComponent,
    *,
    index: int,
) -> None:
    if type(asserted) is not ProspectiveApplicableCostComponent:
        raise ProspectiveApplicableCostError(
            "prospective applicable-cost assertion components must use the exact canonical type"
        )
    if type(canonical) is not ProspectiveApplicableCostComponent:
        raise ProspectiveApplicableCostError(
            "canonical applicable-cost resolver returned a non-canonical component type"
        )

    for field in _COMPONENT_FIELDS:
        if _slot_value(asserted, field) != _slot_value(canonical, field):
            raise ProspectiveApplicableCostError(
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
    """Return freshly re-resolved cost truth only when an assertion matches it.

    `asserted` is never treated as authority.  The function first obtains a new
    canonical resolution from the product-owned resolver and then compares the
    assertion using exact types and direct slot reads.  The canonical resolution
    is returned even when a caller reconstructed a byte-equivalent assertion.
    """

    if type(asserted) is not ProspectiveApplicableCostResolution:
        raise ProspectiveApplicableCostError(
            "prospective applicable-cost assertion must use the exact canonical type"
        )

    canonical = resolve_prospective_applicable_costs(
        intent=intent,
        plan=plan,
        router_store=router_store,
        model_request_id=model_request_id,
        decision_at=decision_at,
    )
    if type(canonical) is not ProspectiveApplicableCostResolution:
        raise ProspectiveApplicableCostError(
            "canonical applicable-cost resolver returned a non-canonical resolution type"
        )

    for field in _RESOLUTION_FIELDS:
        if _slot_value(asserted, field) != _slot_value(canonical, field):
            raise ProspectiveApplicableCostError(
                "prospective applicable-cost assertion does not match canonical "
                f"re-resolution at field {field}"
            )

    asserted_components = _slot_value(asserted, "components")
    canonical_components = _slot_value(canonical, "components")
    if type(asserted_components) is not tuple or type(canonical_components) is not tuple:
        raise ProspectiveApplicableCostError(
            "prospective applicable-cost component collections must be canonical tuples"
        )
    if len(asserted_components) != len(canonical_components):
        raise ProspectiveApplicableCostError(
            "prospective applicable-cost assertion component count does not match canonical re-resolution"
        )

    for index, (asserted_component, canonical_component) in enumerate(
        zip(asserted_components, canonical_components, strict=True)
    ):
        _assert_component_matches(
            asserted_component,
            canonical_component,
            index=index,
        )

    # Never return the caller-supplied object.  A reconstructed assertion has no
    # authority of its own; only the freshly re-resolved value crosses this gate.
    return canonical
