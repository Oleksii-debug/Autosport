from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from . import _campaign_provider_scope_devapp_identity as _devapp
from . import campaign_provider_scope_authority as _scope
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
) -> CostEvidence:
    """Issue one source-owned incurred commission item for an exact campaign slice.

    The monetary amount, currency, settlement time and immutable source identity are
    re-resolved from ``BetfairMarketCommissionAuthority``. Campaign applicability is
    accepted only from the canonical campaign/provider-scope resolver. The two
    authorities are joined through a fresh source-owned stable Betfair account
    discriminator plus exact market identity.

    The result deliberately remains ``INFORMATIONAL``. The current campaign gross
    P&L contract does not prove whether Betfair commission is already embedded in
    gross P&L, so this adapter must not guess ``SUBTRACT_FROM_GROSS`` versus
    ``EMBEDDED_IN_GROSS``. It therefore advances authoritative incurred-cost truth
    without falsely promoting COMPLETE net economics.
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
        stable_account_id = _stable_source_account_id(source)
    except Exception as exc:
        raise BetfairCommissionCostEvidenceError(
            "stable authenticated Betfair account identity is unavailable"
        ) from exc
    if stable_account_id != provider_scope.authenticated_account_id:
        raise BetfairCommissionCostEvidenceError(
            "commission source account is outside campaign provider scope"
        )

    try:
        receipt = source.resolve(
            receipt_id=receipt_id,
            record_sha256=record_sha256,
            as_of=as_of,
        )
    except Exception as exc:
        raise BetfairCommissionCostEvidenceError(
            "Betfair commission receipt is not current source authority"
        ) from exc
    if type(receipt) is not BetfairMarketCommissionReceipt:
        raise BetfairCommissionCostEvidenceError(
            "commission source returned non-canonical receipt"
        )
    if receipt.venue_id != provider_scope.venue_id:
        raise BetfairCommissionCostEvidenceError(
            "commission venue is outside campaign provider scope"
        )
    if receipt.market_id != provider_scope.market_id:
        raise BetfairCommissionCostEvidenceError(
            "commission market is outside campaign provider scope"
        )
    if receipt.supersedes_receipt_id is not None:
        raise BetfairCommissionCostEvidenceError(
            "corrected commission receipts require append-only campaign cost correction lineage"
        )

    memberships = _scoped_memberships(projection.membership_refs, provider_scope)
    available_at = max(receipt.available_at, scope_available)
    observed_at = max(receipt.observed_at, scope_observed)
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
    )


def verify_betfair_commission_cost_evidence(
    *,
    source: BetfairMarketCommissionAuthority,
    campaign: FinalizedCampaignAuthority,
    provider_scope: _scope.CampaignProviderScopeProjection,
    evidence: CostEvidence,
    as_of: datetime,
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
        )
    except (BetfairCommissionCostEvidenceError, ValueError, TypeError):
        return False
    return evidence == expected


def _stable_source_account_id(source: BetfairMarketCommissionAuthority) -> str:
    """Derive stable account identity from the source's canonical live client.

    ``BetfairMarketCommissionAuthority`` constructs this client internally and does
    not accept injected transport/clock. The developer-app identity helper itself
    rejects non-canonical origins, so ordinary callers cannot relabel one receipt as
    another Betfair account by passing an account string.
    """

    identity = _devapp._read_developer_account_identity(source._client)
    return f"betfair-account-evidence:{identity.account_identity_sha256}"


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
