from __future__ import annotations

from datetime import datetime

from .campaign_cost_evidence import (
    CostBasis,
    CostClass,
    CostEvidence,
    CostEvidenceError,
    CostSourceRef,
    CostTreatment,
    CostTruth,
    CostUnit,
)
from .campaign_economic_authority import CanonicalMembershipRef, FinalizedCampaignAuthority
from .monetary_cost_authority import (
    MonetaryCostAuthorityError,
    MonetaryCostAuthorityStore,
    MonetarySourceClass,
    monetary_allocation_source_family,
)


_SOURCE_TO_COST_CLASS = {
    MonetarySourceClass.PROVIDER_DATA: CostClass.PROVIDER_DATA,
    MonetarySourceClass.MODEL_COMPUTE_AI: CostClass.MODEL_COMPUTE_AI,
    MonetarySourceClass.EXECUTION_FEES_COMMISSION_TAX: CostClass.EXECUTION_FEES_COMMISSION_TAX,
    MonetarySourceClass.FIXED_CAMPAIGN: CostClass.FIXED_CAMPAIGN,
}


def resolve_campaign_monetary_cost(
    *,
    authority: MonetaryCostAuthorityStore,
    campaign: FinalizedCampaignAuthority,
    allocation_id: str,
    allocation_sha256: str,
    memberships: tuple[CanonicalMembershipRef, ...],
    treatment: CostTreatment,
    as_of: datetime,
) -> CostEvidence:
    """Resolve one immutable monetary allocation into #645 cost evidence.

    The caller supplies only the immutable allocation reference and intended
    treatment/membership slice. Amount, currency, incurred time and source class
    are always re-resolved from the monetary authority, so a caller-authored
    amount/currency cannot be upgraded to observed incurred truth here.
    """

    if type(authority) is not MonetaryCostAuthorityStore:
        raise CostEvidenceError("authority must be MonetaryCostAuthorityStore")
    if type(campaign) is not FinalizedCampaignAuthority:
        raise CostEvidenceError("campaign must be FinalizedCampaignAuthority")
    projection = campaign.projection()
    try:
        resolved = authority.resolve_cost_source(
            family=monetary_allocation_source_family(),
            evidence_id=allocation_id,
            sha256=allocation_sha256,
            campaign_sha256=projection.campaign_sha256,
            as_of=as_of,
        )
    except MonetaryCostAuthorityError as exc:
        raise CostEvidenceError("monetary allocation failed canonical resolution") from exc

    cost_class = _SOURCE_TO_COST_CLASS.get(resolved.source_class)
    if cost_class is None:
        raise CostEvidenceError("monetary source class has no campaign cost mapping")
    truth = CostTruth.KNOWN_ZERO if resolved.amount == 0 else CostTruth.KNOWN_AMOUNT
    return CostEvidence(
        cost_class=cost_class,
        truth=truth,
        basis=CostBasis.OBSERVED_INCURRED,
        treatment=treatment,
        source=CostSourceRef(
            family=monetary_allocation_source_family(),
            evidence_id=resolved.allocation_id,
            sha256=resolved.allocation_id,
        ),
        campaign_sha256=projection.campaign_sha256,
        memberships=memberships,
        unit=CostUnit.MONEY,
        currency=resolved.currency,
        amount=resolved.amount,
        observed_at=resolved.observed_at,
        available_at=resolved.available_at,
        incurred_at=resolved.incurred_at,
        shared_source=False,
        allocation_source=None,
    )
