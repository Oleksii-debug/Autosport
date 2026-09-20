from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Sequence

from .betfair_commission_cost_evidence import (
    issue_betfair_commission_cost_evidence,
    verify_betfair_commission_cost_evidence,
)
from .betfair_market_commission_authority import BetfairMarketCommissionAuthority
from .campaign_cost_evidence import (
    CampaignEconomicEvidenceVersion,
    CostEvidence,
    CostTreatment,
    derive_campaign_economics,
)
from .campaign_economic_authority import FinalizedCampaignAuthority
from .campaign_provider_scope_authority import CampaignProviderScopeProjection


class CampaignEconomicCostCompositionError(RuntimeError):
    """Raised when source-owned cost evidence cannot be composed safely."""


def compose_betfair_commission_campaign_economics(
    *,
    source: BetfairMarketCommissionAuthority,
    campaign: FinalizedCampaignAuthority,
    provider_scope: CampaignProviderScopeProjection,
    receipt_id: str,
    record_sha256: str,
    as_of: datetime,
    additional_costs: Sequence[CostEvidence] = (),
    previous: CampaignEconomicEvidenceVersion | None = None,
) -> CampaignEconomicEvidenceVersion:
    """Compose verified Betfair commission into canonical campaign economics.

    ``derive_campaign_economics`` deliberately treats arbitrary ``CostEvidence``
    as unresolved because callers are allowed to construct that value type.  This
    product composition boundary closes that gap for the integrated Betfair
    commission authority: it re-resolves the immutable provider receipt and the
    campaign/provider applicability authority, issues the exact canonical
    ``CostEvidence``, re-verifies it, and only then removes the generic
    ``UNRESOLVED_COST_AUTHORITY`` reason for that exact cost class.

    The commission receipt remains a shared MARKET-level source and is emitted by
    the source adapter as ``INFORMATIONAL``.  This function therefore never
    subtracts it from campaign gross P&L, never infers campaign currency, and never
    upgrades completeness merely because a market commission amount is known.
    Exact allocation/accounting authority and every other required cost class are
    still required before complete net economics can exist.
    """

    try:
        commission = issue_betfair_commission_cost_evidence(
            source=source,
            campaign=campaign,
            provider_scope=provider_scope,
            receipt_id=receipt_id,
            record_sha256=record_sha256,
            as_of=as_of,
        )
    except Exception as exc:
        raise CampaignEconomicCostCompositionError(
            "Betfair commission cannot be resolved into canonical cost evidence"
        ) from exc

    if not verify_betfair_commission_cost_evidence(
        source=source,
        campaign=campaign,
        provider_scope=provider_scope,
        evidence=commission,
        as_of=as_of,
    ):
        raise CampaignEconomicCostCompositionError(
            "Betfair commission cost evidence failed exact source re-verification"
        )

    costs_by_id: dict[str, CostEvidence] = {}
    for cost in additional_costs:
        if type(cost) is not CostEvidence:
            raise CampaignEconomicCostCompositionError(
                "additional costs must be exact CostEvidence values"
            )
        prior = costs_by_id.get(cost.cost_evidence_id)
        if prior is not None and prior != cost:
            raise CampaignEconomicCostCompositionError(
                "same cost evidence identity has conflicting payloads"
            )
        costs_by_id[cost.cost_evidence_id] = cost

    prior = costs_by_id.get(commission.cost_evidence_id)
    if prior is not None and prior != commission:
        raise CampaignEconomicCostCompositionError(
            "caller cost conflicts with source-owned Betfair commission evidence"
        )
    costs_by_id[commission.cost_evidence_id] = commission
    costs = tuple(sorted(costs_by_id.values(), key=lambda value: value.cost_evidence_id))

    version = derive_campaign_economics(
        campaign=campaign,
        costs=costs,
        as_of=as_of,
        previous=previous,
    )

    effective = _effective_costs(version.costs)
    same_class = tuple(
        cost for cost in effective if cost.cost_class is commission.cost_class
    )
    if same_class == (commission,):
        reasons = set(version.incomplete_reasons)
        reasons.discard(
            f"UNRESOLVED_COST_AUTHORITY:{commission.cost_class.value}"
        )
        if commission.shared_source and commission.treatment is CostTreatment.INFORMATIONAL:
            reasons.add(f"SHARED_UNALLOCATED_COST:{commission.cost_class.value}")
        version = replace(version, incomplete_reasons=tuple(sorted(reasons)))

    return version


def _effective_costs(costs: Sequence[CostEvidence]) -> tuple[CostEvidence, ...]:
    superseded = {
        evidence_id
        for cost in costs
        for evidence_id in cost.supersedes_cost_evidence_ids
    }
    return tuple(
        cost for cost in costs if cost.cost_evidence_id not in superseded
    )
