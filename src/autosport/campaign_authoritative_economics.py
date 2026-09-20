from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Sequence

from .campaign_cost_evidence import (
    CampaignEconomicEvidenceVersion,
    CostBasis,
    CostClass,
    CostEvidence,
    CostSourceRef,
    CostTreatment,
    CostTruth,
    CostUnit,
    EconomicCompleteness,
    REQUIRED_COST_CLASSES,
    derive_campaign_economics,
)
from .campaign_economic_authority import FinalizedCampaignAuthority
from .campaign_monetary_cost_authority import (
    CampaignMonetaryCostAuthority,
    MonetaryCostAuthorityError,
)


def derive_authoritative_campaign_economics(
    *,
    campaign: FinalizedCampaignAuthority,
    costs: Sequence[CostEvidence],
    as_of: datetime,
    monetary_authority: CampaignMonetaryCostAuthority,
    currency_ref: CostSourceRef | None,
    previous: CampaignEconomicEvidenceVersion | None = None,
) -> CampaignEconomicEvidenceVersion:
    """Upgrade #645 economics only from independently resolvable money evidence.

    The base #645 derivation remains deliberately fail-closed for arbitrary
    CostEvidence. This bridge first invokes that canonical derivation for all
    campaign/membership/supersession validation, then removes an unresolved
    reason only when the exact receipt/allocation bytes can be reconstructed by
    CampaignMonetaryCostAuthority. No configured/public/simulated amount can be
    upgraded by this function merely because its fields look authoritative.
    """

    if type(monetary_authority) is not CampaignMonetaryCostAuthority:
        raise MonetaryCostAuthorityError(
            "monetary_authority must be the canonical CampaignMonetaryCostAuthority"
        )

    base = derive_campaign_economics(
        campaign=campaign,
        costs=costs,
        as_of=as_of,
        previous=previous,
    )

    superseded = {
        evidence_id
        for cost in base.costs
        for evidence_id in cost.supersedes_cost_evidence_ids
    }
    effective = tuple(
        cost for cost in base.costs if cost.cost_evidence_id not in superseded
    )

    reasons: set[str] = set()
    campaign_currency: str | None = None
    if currency_ref is None:
        reasons.add("MISSING_CAMPAIGN_CURRENCY_AUTHORITY")
    else:
        campaign_currency = monetary_authority.resolve_campaign_currency(
            currency_ref,
            campaign_sha256=base.campaign_sha256,
            as_of=as_of,
        )

    by_class: dict[CostClass, list[CostEvidence]] = {
        value: [] for value in REQUIRED_COST_CLASSES
    }
    qualified: dict[str, CostEvidence] = {}
    for cost in effective:
        by_class[cost.cost_class].append(cost)
        try:
            canonical = monetary_authority.qualify_cost(cost, as_of=as_of)
        except MonetaryCostAuthorityError:
            canonical = None
        if canonical is None:
            reasons.add(f"UNRESOLVED_COST_AUTHORITY:{cost.cost_class.value}")
        else:
            qualified[cost.cost_evidence_id] = canonical

        if cost.truth is CostTruth.UNKNOWN_UNPROVEN:
            reasons.add(f"UNRESOLVED_COST_CLASS:{cost.cost_class.value}")
        if cost.truth is CostTruth.NOT_APPLICABLE:
            reasons.add(f"UNRESOLVED_NOT_APPLICABLE:{cost.cost_class.value}")
        if cost.basis in {CostBasis.CONFIGURED_ESTIMATE, CostBasis.SYNTHETIC_ESTIMATE}:
            reasons.add(f"ESTIMATE_ONLY:{cost.cost_class.value}")
        if cost.unit is not CostUnit.MONEY:
            reasons.add(f"NON_MONEY_UNIT:{cost.cost_class.value}")
        if cost.treatment is CostTreatment.INFORMATIONAL:
            reasons.add(f"INFORMATIONAL_ONLY:{cost.cost_class.value}")

    for cost_class in REQUIRED_COST_CLASSES:
        class_items = by_class[cost_class]
        if not class_items:
            reasons.add(f"MISSING_COST_CLASS:{cost_class.value}")
            continue
        if not any(item.cost_evidence_id in qualified for item in class_items):
            reasons.add(f"UNRESOLVED_COST_CLASS:{cost_class.value}")

    known_cost_total = Decimal("0")
    if campaign_currency is not None:
        for cost in qualified.values():
            if cost.currency != campaign_currency:
                reasons.add(f"CURRENCY_MISMATCH:{cost.cost_class.value}")
                continue
            if cost.amount is None:
                reasons.add(f"MISSING_KNOWN_AMOUNT:{cost.cost_class.value}")
                continue
            if cost.treatment is CostTreatment.SUBTRACT_FROM_GROSS:
                known_cost_total += cost.amount

    net_after_known_costs = (
        None
        if campaign_currency is None
        else base.gross_run_pnl - known_cost_total
    )
    completeness = (
        EconomicCompleteness.COMPLETE_NET_ECONOMICS
        if not reasons
        else EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    )

    return CampaignEconomicEvidenceVersion(
        campaign_authority=base.campaign_authority,
        costs=base.costs,
        as_of=base.as_of,
        previous_version_id=base.previous_version_id,
        previous_version_sha256=base.previous_version_sha256,
        known_cost_total=known_cost_total,
        net_after_known_costs=net_after_known_costs,
        completeness=completeness,
        incomplete_reasons=tuple(sorted(reasons)),
    )
