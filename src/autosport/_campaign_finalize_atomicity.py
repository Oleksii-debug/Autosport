from __future__ import annotations

"""Stage fallible denomination issuance before sealing a PaperCampaign.

The legacy finalizer validates the canonical campaign correctly but historically
sealed the mutable campaign object before denomination issuance.  This composition
layer preserves that reviewed validation/issuance implementation while running it
against an exact draft clone first.  Only after the clone has fully finalized and
published denomination authority do we copy the now-non-fallible finalized fields
onto the caller-visible campaign.
"""

from . import campaign_evidence as _campaign


_ORIGINAL_FINALIZE = _campaign.PaperCampaign.finalize


def _draft_clone(source: _campaign.PaperCampaign) -> _campaign.PaperCampaign:
    if type(source) is not _campaign.PaperCampaign:
        raise TypeError("campaign must be exact PaperCampaign")
    clone = _campaign.PaperCampaign(
        campaign_id=source.campaign_id,
        campaign_version=source.campaign_version,
        research_protocol_id=source.research_protocol_id,
        protocol_sha256=source.protocol_sha256,
        hypothesis_id=source.hypothesis_id,
        primary_metric=source.primary_metric,
        protective_metrics=source.protective_metrics,
        evaluation_window_start=source.evaluation_window_start,
        evaluation_window_end=source.evaluation_window_end,
        evaluation_as_of=source.evaluation_as_of,
        readiness_rule=source.readiness_rule,
        source_sha256=source.source_sha256,
        created_at=source.created_at,
        strategy_version_id=source.strategy_version_id,
        model_version_id=source.model_version_id,
        sessions=list(source.sessions),
    )
    object.__setattr__(clone, "_scientific_registry", source._scientific_registry)
    object.__setattr__(clone, "_run_registry", source._run_registry)
    return clone


def _atomic_finalize(
    self: _campaign.PaperCampaign,
    *,
    outcome: _campaign.CampaignOutcome,
    readiness: _campaign.CampaignReadiness,
    finalized_at: str,
) -> _campaign.CampaignSummary:
    if self.finalized:
        raise _campaign.CampaignFinalizedError("campaign is already finalized")

    # All validation, digest derivation and denomination publication remain owned by
    # the original canonical implementation.  They execute on a clone so any error
    # (invalid denomination, witness failure, registry write failure, re-resolution
    # failure) leaves the caller-visible draft byte-for-byte lifecycle-retryable.
    staged = _draft_clone(self)
    _ORIGINAL_FINALIZE(
        staged,
        outcome=outcome,
        readiness=readiness,
        finalized_at=finalized_at,
    )

    # At this boundary every fallible authority operation has succeeded.  Copy only
    # deterministic finalized fields; preserve the original exact registry objects.
    self.finalized_at = staged.finalized_at
    self.outcome = staged.outcome
    self.readiness = staged.readiness
    self.campaign_sha256 = staged.campaign_sha256
    self.sessions = tuple(self.sessions)
    object.__setattr__(self, "finalized", True)
    object.__setattr__(self, "_sealed", True)
    return self.summary()


def _install() -> None:
    cls = _campaign.PaperCampaign
    if getattr(cls, "_autosport_atomic_denomination_finalize_installed", False):
        return
    cls.finalize = _atomic_finalize
    cls._autosport_atomic_denomination_finalize_installed = True


_install()
