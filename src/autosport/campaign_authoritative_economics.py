from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from .campaign_cost_evidence import (
    CampaignEconomicEvidenceVersion,
    CostEvidence,
    CostEvidenceError,
    CostTreatment,
    EconomicCompleteness,
    REQUIRED_COST_CLASSES,
    derive_campaign_economics,
)
from .campaign_economic_authority import FinalizedCampaignAuthority
from .monetary_cost_authority import MonetaryCostAuthority


def derive_authoritative_campaign_economics(
    *,
    campaign: FinalizedCampaignAuthority,
    costs: Sequence[CostEvidence],
    as_of: datetime,
    monetary_authority: MonetaryCostAuthority,
    previous: CampaignEconomicEvidenceVersion | None = None,
) -> CampaignEconomicEvidenceVersion:
    """Resolve #645 campaign economics only through exact monetary authority.

    The integrated #645 derivation remains the sole owner of campaign identity,
    membership, source de-duplication and correction-lineage validation. This
    narrow resolver pass can only remove its fail-closed monetary blockers when
    every effective cost item for a class re-resolves byte-for-byte through the
    product-owned ``MonetaryCostAuthority``.

    No currency conversion is performed. Mixed authoritative currencies remain
    incomplete until a separate canonical FX authority exists.
    """

    if type(monetary_authority) is not MonetaryCostAuthority:
        raise CostEvidenceError(
            "monetary_authority must be exact MonetaryCostAuthority"
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

    reasons = set(base.incomplete_reasons)
    by_class: dict[object, list[CostEvidence]] = {
        value: [] for value in REQUIRED_COST_CLASSES
    }
    qualified_by_class: dict[object, list[CostEvidence]] = {
        value: [] for value in REQUIRED_COST_CLASSES
    }
    qualified: list[CostEvidence] = []

    for cost in effective:
        by_class[cost.cost_class].append(cost)
        if monetary_authority.verify_cost_evidence(
            campaign=campaign,
            evidence=cost,
            as_of=as_of,
        ):
            qualified.append(cost)
            qualified_by_class[cost.cost_class].append(cost)

    all_required_resolved = True
    for cost_class in REQUIRED_COST_CLASSES:
        class_items = by_class[cost_class]
        resolved_items = qualified_by_class[cost_class]
        if not class_items or len(resolved_items) != len(class_items):
            all_required_resolved = False
            continue
        reasons.discard(f"MISSING_COST_CLASS:{cost_class.value}")
        reasons.discard(f"UNRESOLVED_COST_AUTHORITY:{cost_class.value}")
        reasons.discard(f"UNRESOLVED_COST_CLASS:{cost_class.value}")
        reasons.discard(f"ESTIMATE_ONLY:{cost_class.value}")
        reasons.discard(f"NON_MONEY_UNIT:{cost_class.value}")
        reasons.discard(f"INFORMATIONAL_ONLY:{cost_class.value}")

    currencies = {cost.currency for cost in qualified if cost.currency is not None}
    if all_required_resolved and len(currencies) == 1:
        reasons.discard("MISSING_CAMPAIGN_CURRENCY_AUTHORITY")
        reasons.discard("CROSS_CURRENCY_REQUIRES_FX_AUTHORITY")
    elif all_required_resolved and len(currencies) > 1:
        reasons.discard("MISSING_CAMPAIGN_CURRENCY_AUTHORITY")
        reasons.add("CROSS_CURRENCY_REQUIRES_FX_AUTHORITY")

    if len(currencies) == 1:
        known_cost_total = sum(
            (
                cost.amount or Decimal("0")
                for cost in qualified
                if cost.treatment is CostTreatment.SUBTRACT_FROM_GROSS
            ),
            Decimal("0"),
        )
    else:
        # Never add unlike currencies merely because their Decimal values exist.
        known_cost_total = Decimal("0")

    complete = not reasons
    return replace(
        base,
        known_cost_total=known_cost_total,
        net_after_known_costs=(
            base.gross_run_pnl - known_cost_total if complete else None
        ),
        completeness=(
            EconomicCompleteness.COMPLETE_NET_ECONOMICS
            if complete
            else EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
        ),
        incomplete_reasons=tuple(sorted(reasons)),
    )
