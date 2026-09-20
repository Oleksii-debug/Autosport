from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .campaign_evidence import (
    PaperCampaign,
    _validate_authoritative_session,
    _validate_registry_bindings,
)
from .run_registry import RunRegistry
from .scientific_registry import ScientificRegistry


class CampaignEconomicAuthorityError(ValueError):
    """Raised when finalized campaign economic authority cannot be proven."""


@dataclass(frozen=True, order=True, slots=True)
class CanonicalSessionRef:
    evidence_id: str
    evidence_sha256: str


@dataclass(frozen=True, order=True, slots=True)
class CanonicalMembershipRef:
    kind: str
    evidence_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CanonicalCampaignProjection:
    campaign_id: str
    campaign_version: int
    campaign_sha256: str
    session_refs: tuple[CanonicalSessionRef, ...]
    membership_refs: tuple[CanonicalMembershipRef, ...]
    gross_run_pnl: Decimal


class FinalizedCampaignAuthority:
    """Product-owned capability over an already-authoritative PaperCampaign.

    The projection is deliberately derived from a live finalized PaperCampaign
    and its concrete ScientificRegistry/RunRegistry bindings. Callers cannot
    supply gross P&L, session membership, run membership, evaluation membership,
    or a campaign digest to this capability.
    """

    __slots__ = ("_campaign",)

    def __init__(self, campaign: PaperCampaign) -> None:
        if type(campaign) is not PaperCampaign:
            raise CampaignEconomicAuthorityError(
                "campaign authority requires an exact PaperCampaign"
            )
        self._campaign = campaign
        self.projection()

    def projection(self) -> CanonicalCampaignProjection:
        campaign = self._campaign
        if not campaign.finalized or campaign.campaign_sha256 is None:
            raise CampaignEconomicAuthorityError(
                "campaign authority requires a finalized PaperCampaign"
            )

        scientific_registry = campaign._scientific_registry
        run_registry = campaign._run_registry
        if type(scientific_registry) is not ScientificRegistry:
            raise CampaignEconomicAuthorityError(
                "finalized campaign lacks ScientificRegistry authority"
            )
        if type(run_registry) is not RunRegistry:
            raise CampaignEconomicAuthorityError(
                "finalized campaign lacks RunRegistry authority"
            )

        if campaign.campaign_sha256 != campaign._computed_campaign_sha256():
            raise CampaignEconomicAuthorityError(
                "finalized campaign state no longer matches campaign_sha256"
            )

        _validate_registry_bindings(campaign, scientific_registry)
        sessions: list[CanonicalSessionRef] = []
        memberships: list[CanonicalMembershipRef] = []

        for session in campaign.sessions:
            _validate_authoritative_session(
                campaign,
                session,
                scientific_registry=scientific_registry,
                run_registry=run_registry,
            )
            _, run_summary_sha256 = run_registry.verified_completed_summary_for_run(
                session.run_id
            )
            bundle = scientific_registry.get("EvaluationBundle", session.evidence_id)
            if bundle is None:
                raise CampaignEconomicAuthorityError(
                    "campaign session lacks EvaluationBundle authority"
                )
            bundle_sha256 = bundle.payload.get("bundle_sha256")
            if not isinstance(bundle_sha256, str) or len(bundle_sha256) != 64:
                raise CampaignEconomicAuthorityError(
                    "EvaluationBundle lacks canonical bundle_sha256"
                )

            sessions.append(
                CanonicalSessionRef(
                    evidence_id=session.evidence_id,
                    evidence_sha256=session.evidence_sha256,
                )
            )
            memberships.extend(
                (
                    CanonicalMembershipRef(
                        kind="SESSION",
                        evidence_id=session.evidence_id,
                        sha256=session.evidence_sha256,
                    ),
                    CanonicalMembershipRef(
                        kind="RUN",
                        evidence_id=session.run_id,
                        sha256=run_summary_sha256,
                    ),
                    CanonicalMembershipRef(
                        kind="EVALUATION",
                        evidence_id=session.evidence_id,
                        sha256=bundle_sha256.lower(),
                    ),
                )
            )

        summary = campaign.summary()
        if summary.status != "FINALIZED":
            raise CampaignEconomicAuthorityError(
                "campaign summary is not finalized"
            )
        if summary.campaign_sha256 != campaign.campaign_sha256:
            raise CampaignEconomicAuthorityError(
                "campaign summary digest disagrees with finalized campaign"
            )
        if not sessions:
            raise CampaignEconomicAuthorityError(
                "finalized campaign contains no authoritative session evidence"
            )

        return CanonicalCampaignProjection(
            campaign_id=campaign.campaign_id,
            campaign_version=campaign.campaign_version,
            campaign_sha256=campaign.campaign_sha256,
            session_refs=tuple(sorted(sessions)),
            membership_refs=tuple(sorted(memberships)),
            gross_run_pnl=summary.net_profit_total,
        )
