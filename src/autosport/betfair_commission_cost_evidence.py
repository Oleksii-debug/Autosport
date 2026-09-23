from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from . import _betfair_market_commission_origin_binding as _source_origin
from . import _campaign_provider_scope_devapp_identity as _devapp
from . import campaign_provider_scope_authority as _scope
from .betfair_account_readonly import BetfairReadOnlyClient
from .betfair_market_commission_authority import (
    SOURCE_FAMILY as BETFAIR_COMMISSION_SOURCE_FAMILY,
    BetfairMarketCommissionAuthority,
    BetfairMarketCommissionReceipt,
)
from .campaign_cost_evidence import (
    CostBasis,
    CostClass,
    CostEvidence,
    CostSourceRef,
    CostTreatment,
    CostTruth,
    CostUnit,
)
from .campaign_economic_authority import (
    CanonicalMembershipRef,
    FinalizedCampaignAuthority,
)


class BetfairCommissionCostEvidenceError(RuntimeError):
    """Raised when Betfair commission cannot be bound to campaign cost truth."""


def issue_betfair_commission_cost_evidence(
    *,
    source: BetfairMarketCommissionAuthority,
    campaign: FinalizedCampaignAuthority,
    provider_scope: _scope.CampaignProviderScopeProjection,
    receipt_id: str,
    record_sha256: str,
    as_of: datetime,
    supersedes: CostEvidence | None = None,
) -> CostEvidence:
    """Issue source-owned incurred commission evidence for an exact campaign slice.

    The monetary amount, currency, settlement time and immutable source identity are
    re-resolved from ``BetfairMarketCommissionAuthority``. Campaign applicability is
    accepted only from the canonical campaign/provider-scope resolver. The source
    resolver also returns the exact client origin that acquired/reacquired this
    receipt; stable Betfair account identity is read from that bound client rather
    than from mutable ``source._client`` state.

    Betfair's MARKET rollup can cover more than one execution/campaign. Therefore
    the result is explicitly a shared source and remains ``INFORMATIONAL`` until a
    separate allocation/accounting authority proves both the campaign share and
    whether commission is subtractive or already embedded in gross P&L. This path
    advances authoritative incurred-source truth without claiming complete net
    economics.

    A corrected provider receipt may carry one append-only campaign-cost correction
    edge. The caller must supply the exact predecessor ``CostEvidence`` and the
    source receipt must name that predecessor receipt identity. This function does
    not make an arbitrary predecessor authoritative: durable campaign economics must
    still prove that predecessor is the exact active cost in the prior version.
    """

    if type(source) is not BetfairMarketCommissionAuthority:
        raise BetfairCommissionCostEvidenceError(
            "source must be exact BetfairMarketCommissionAuthority"
        )
    if type(campaign) is not FinalizedCampaignAuthority:
        raise BetfairCommissionCostEvidenceError(
            "campaign must be exact FinalizedCampaignAuthority"
        )
    _utc(as_of, "as_of")

    try:
        _scope.assert_campaign_provider_scope_authoritative(provider_scope)
    except Exception as exc:
        raise BetfairCommissionCostEvidenceError(
            "provider scope is not canonical campaign applicability authority"
        ) from exc

    projection = campaign.projection()
    if (
        provider_scope.campaign_id != projection.campaign_id
        or provider_scope.campaign_version != projection.campaign_version
        or provider_scope.campaign_sha256 != projection.campaign_sha256
    ):
        raise BetfairCommissionCostEvidenceError(
            "provider scope belongs to a different finalized campaign"
        )

    scope_available = _instant(provider_scope.available_at, "provider scope available_at")
    scope_observed = _instant(provider_scope.observed_at, "provider scope observed_at")
    if scope_available > as_of:
        raise BetfairCommissionCostEvidenceError(
            "future provider applicability cannot be used as campaign cost evidence"
        )

    try:
        receipt, origin_client = _source_origin.resolve_bound_receipt(
            source,
            receipt_id=receipt_id,
            record_sha256=record_sha256,
            as_of=as_of,
        )
    except Exception as exc:
        raise BetfairCommissionCostEvidenceError(
            "Betfair commission receipt lacks current source-origin authority"
        ) from exc
    if type(receipt) is not BetfairMarketCommissionReceipt:
        raise BetfairCommissionCostEvidenceError(
            "commission source returned non-canonical receipt"
        )
    if type(origin_client) is not BetfairReadOnlyClient:
        raise BetfairCommissionCostEvidenceError(
            "commission source returned non-canonical client origin"
        )

    try:
        stable_account_id, account_observed = _stable_client_account_identity(origin_client)
    except Exception as exc:
        raise BetfairCommissionCostEvidenceError(
            "stable authenticated Betfair receipt account identity is unavailable"
        ) from exc
    if account_observed > as_of:
        raise BetfairCommissionCostEvidenceError(
            "future account-identity verification cannot be backdated"
        )
    if stable_account_id != provider_scope.authenticated_account_id:
        raise BetfairCommissionCostEvidenceError(
            "commission receipt account is outside campaign provider scope"
        )

    if receipt.venue_id != provider_scope.venue_id:
        raise BetfairCommissionCostEvidenceError(
            "commission venue is outside campaign provider scope"
        )
    if receipt.market_id != provider_scope.market_id:
        raise BetfairCommissionCostEvidenceError(
            "commission market is outside campaign provider scope"
        )

    memberships = _scoped_memberships(projection.membership_refs, provider_scope)
    supersedes_ids = _correction_supersedes(
        receipt=receipt,
        predecessor=supersedes,
        campaign_sha256=projection.campaign_sha256,
        memberships=memberships,
    )
    available_at = max(receipt.available_at, scope_available, account_observed)
    observed_at = max(receipt.observed_at, scope_observed, account_observed)
    if available_at > as_of:
        raise BetfairCommissionCostEvidenceError(
            "combined source/applicability evidence is not yet available"
        )

    truth = (
        CostTruth.KNOWN_ZERO
        if receipt.commission == Decimal("0")
        else CostTruth.KNOWN_AMOUNT
    )
    return CostEvidence(
        cost_class=CostClass.EXECUTION_FEES_COMMISSION_TAX,
        truth=truth,
        basis=CostBasis.OBSERVED_INCURRED,
        treatment=CostTreatment.INFORMATIONAL,
        source=CostSourceRef(
            family=BETFAIR_COMMISSION_SOURCE_FAMILY,
            evidence_id=receipt.receipt_id,
            sha256=receipt.record_sha256,
        ),
        campaign_sha256=projection.campaign_sha256,
        memberships=memberships,
        unit=CostUnit.MONEY,
        currency=receipt.currency,
        amount=receipt.commission,
        observed_at=observed_at,
        available_at=available_at,
        incurred_at=receipt.settled_at,
        shared_source=True,
        supersedes_cost_evidence_ids=supersedes_ids,
    )


def verify_betfair_commission_cost_evidence(
    *,
    source: BetfairMarketCommissionAuthority,
    campaign: FinalizedCampaignAuthority,
    provider_scope: _scope.CampaignProviderScopeProjection,
    evidence: CostEvidence,
    as_of: datetime,
    supersedes: CostEvidence | None = None,
) -> bool:
    """Re-resolve both authorities and require byte-semantic CostEvidence equality."""

    if type(evidence) is not CostEvidence:
        return False
    try:
        expected = issue_betfair_commission_cost_evidence(
            source=source,
            campaign=campaign,
            provider_scope=provider_scope,
            receipt_id=evidence.source.evidence_id,
            record_sha256=evidence.source.sha256,
            as_of=as_of,
            supersedes=supersedes,
        )
    except (BetfairCommissionCostEvidenceError, ValueError, TypeError):
        return False
    return evidence == expected


def _correction_supersedes(
    *,
    receipt: BetfairMarketCommissionReceipt,
    predecessor: CostEvidence | None,
    campaign_sha256: str,
    memberships: tuple[CanonicalMembershipRef, ...],
) -> tuple[str, ...]:
    if receipt.supersedes_receipt_id is None:
        if predecessor is not None:
            raise BetfairCommissionCostEvidenceError(
                "uncorrected commission receipt cannot supersede campaign cost evidence"
            )
        return ()
    if type(predecessor) is not CostEvidence:
        raise BetfairCommissionCostEvidenceError(
            "corrected commission receipts require append-only campaign cost correction lineage"
        )
    if (
        predecessor.cost_class is not CostClass.EXECUTION_FEES_COMMISSION_TAX
        or predecessor.source.family != BETFAIR_COMMISSION_SOURCE_FAMILY
        or predecessor.source.evidence_id != receipt.supersedes_receipt_id
        or predecessor.source.sha256 != receipt.supersedes_receipt_id
        or predecessor.campaign_sha256 != campaign_sha256
        or predecessor.memberships != memberships
        or predecessor.basis is not CostBasis.OBSERVED_INCURRED
        or predecessor.treatment is not CostTreatment.INFORMATIONAL
        or predecessor.truth not in {CostTruth.KNOWN_ZERO, CostTruth.KNOWN_AMOUNT}
        or predecessor.unit is not CostUnit.MONEY
        or predecessor.currency != receipt.currency
        or not predecessor.shared_source
        or predecessor.allocation_source is not None
    ):
        raise BetfairCommissionCostEvidenceError(
            "corrected commission receipt does not exactly supersede active Betfair campaign cost evidence"
        )
    return (predecessor.cost_evidence_id,)


def _stable_client_account_identity(
    client: BetfairReadOnlyClient,
) -> tuple[str, datetime]:
    """Re-verify stable account identity from the receipt's bound client origin."""

    if type(client) is not BetfairReadOnlyClient:
        raise BetfairCommissionCostEvidenceError(
            "receipt client origin must be canonical BetfairReadOnlyClient"
        )
    identity = _devapp._read_developer_account_identity(client)
    return (
        f"betfair-account-evidence:{identity.account_identity_sha256}",
        _instant(identity.observed_at, "stable account observed_at"),
    )


def _scoped_memberships(
    memberships: tuple[CanonicalMembershipRef, ...],
    provider_scope: _scope.CampaignProviderScopeProjection,
) -> tuple[CanonicalMembershipRef, ...]:
    session = [
        value
        for value in memberships
        if value.kind == "SESSION"
        and value.evidence_id == provider_scope.session_evidence_id
        and value.sha256 == provider_scope.session_evidence_sha256
    ]
    run = [
        value
        for value in memberships
        if value.kind == "RUN"
        and value.evidence_id == provider_scope.run_id
        and value.sha256 == provider_scope.run_summary_sha256
    ]
    evaluation = [
        value
        for value in memberships
        if value.kind == "EVALUATION"
        and value.evidence_id == provider_scope.session_evidence_id
    ]
    if len(session) != 1 or len(run) != 1 or len(evaluation) != 1:
        raise BetfairCommissionCostEvidenceError(
            "provider scope does not resolve exact SESSION/RUN/EVALUATION membership"
        )
    return tuple(sorted((*session, *run, *evaluation)))


def _instant(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairCommissionCostEvidenceError(
            f"{label} must be canonical timestamp text"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairCommissionCostEvidenceError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairCommissionCostEvidenceError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime, label: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise BetfairCommissionCostEvidenceError(f"{label} must be UTC")
