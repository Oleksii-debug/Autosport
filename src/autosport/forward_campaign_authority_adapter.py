"""Materialize forward population samples from existing canonical authorities.

The adapter performs no collection, settlement, evaluation, journal persistence,
training, promotion, or execution.  It composes already-issued source-rights,
point-in-time, forward-cohort, PAPER settlement, and abstention evidence into the
immutable ``ForwardPopulationSample`` schema.

Pre-decision source identity and post-decision terminal identity are deliberately
hashed separately.  Future outcomes therefore cannot leak into the source-evidence
key used by the population firewall.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Final

from .continual_learning_cycle import VerifiedLearningExperience
from .forward_campaign_population import (
    ForwardPopulationSample,
    PopulationRole,
    TerminalEvidenceState,
)
from .forward_evidence_completeness import (
    AuthoritativeSourceReceipt,
    CampaignEvidence,
    DecisionState,
    ForwardOpportunityEnvelope,
    RevealBoundaryReceipt,
    VerificationResult,
    verify_campaign,
)
from .learning_environment import (
    Action,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
    Transition,
)
from .paper_abstention_learning import PaperAbstentionFinalizationReceipt
from .paper_settlement_learning import PaperSettlementLearningWitness
from .point_in_time_evidence import FeatureAvailabilityEvidence
from .source_rights_manifest import SourceRightsAuthorization


_DECISION_PARAMETER: Final = "economic_decision_id"
_NEGATIVE_ACTIONS: Final = frozenset({"NO_BET", "WAIT"})
_HEX: Final = frozenset("0123456789abcdef")


class ForwardCampaignAuthorityAdapterError(ValueError):
    """Canonical campaign authorities do not bind one causal sample exactly."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ForwardCampaignAuthorityAdapterError(
            f"{name} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ForwardCampaignAuthorityAdapterError(
            f"{name} must be valid UTF-8"
        ) from exc
    return value


def _instant(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _text(value, name)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ForwardCampaignAuthorityAdapterError(
                f"{name} must be ISO-8601"
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardCampaignAuthorityAdapterError(
            f"{name} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ForwardCampaignAuthorityAdapterError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ForwardCampaignAuthorityAdapterError(
            "authority binding is not canonical JSON"
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def _campaign_member(
    evidence: CampaignEvidence,
    *,
    candidate_sequence: int,
) -> tuple[
    ForwardOpportunityEnvelope,
    AuthoritativeSourceReceipt,
    RevealBoundaryReceipt,
    VerificationResult,
]:
    if type(evidence) is not CampaignEvidence:
        raise TypeError("evidence must be exact CampaignEvidence")
    if type(candidate_sequence) is not int or candidate_sequence <= 0:
        raise ForwardCampaignAuthorityAdapterError(
            "candidate_sequence must be a positive exact integer"
        )
    verification = verify_campaign(evidence)
    if not verification.ok:
        raise ForwardCampaignAuthorityAdapterError(
            "forward campaign structural verification failed: "
            + ",".join(code.value for code in verification.codes)
        )
    opportunities = tuple(
        item
        for item in evidence.opportunities
        if item.candidate_sequence == candidate_sequence
    )
    if len(opportunities) != 1:
        raise ForwardCampaignAuthorityAdapterError(
            "candidate sequence does not resolve to exactly one opportunity"
        )
    opportunity = opportunities[0]
    receipts = tuple(
        item
        for item in evidence.authoritative_receipts
        if item.receipt_id == opportunity.source_receipt_id
    )
    if len(receipts) != 1:
        raise ForwardCampaignAuthorityAdapterError(
            "opportunity does not resolve to exactly one authoritative source receipt"
        )
    receipt = receipts[0]
    boundaries = tuple(
        item
        for item in evidence.reveal_boundaries
        if item.receipt_id == opportunity.reveal_boundary_receipt_id
    )
    if len(boundaries) != 1:
        raise ForwardCampaignAuthorityAdapterError(
            "opportunity does not resolve to exactly one reveal boundary"
        )
    return opportunity, receipt, boundaries[0], verification


def _predecision_binding(
    *,
    opportunity: ForwardOpportunityEnvelope,
    receipt: AuthoritativeSourceReceipt,
    observation: Observation,
    feature_evidence: FeatureAvailabilityEvidence,
    source_rights: SourceRightsAuthorization,
    decided_at: str,
) -> tuple[str, str, str, str]:
    if type(observation) is not Observation:
        raise TypeError("observation must be exact Observation")
    if type(feature_evidence) is not FeatureAvailabilityEvidence:
        raise TypeError(
            "feature_evidence must be exact FeatureAvailabilityEvidence"
        )
    if type(source_rights) is not SourceRightsAuthorization:
        raise TypeError("source_rights must be exact SourceRightsAuthorization")

    source_identity = source_rights.source_identity
    required_scope = source_rights.required_scope
    rights_checked_at = source_rights.checked_at
    rights_manifest_sha256 = source_rights.manifest_sha256
    if feature_evidence.source_identity != source_identity:
        raise ForwardCampaignAuthorityAdapterError(
            "point-in-time evidence source differs from source-rights authorization"
        )
    cutoff = _instant(
        feature_evidence.decision_cutoff_utc,
        "feature decision_cutoff_utc",
    )
    decision = _instant(decided_at, "decided_at")
    available = max(
        _instant(observation.available_at, "observation.available_at"),
        _instant(feature_evidence.available_at_utc, "feature available_at_utc"),
    )
    observed = max(
        _instant(observation.observed_at, "observation.observed_at"),
        _instant(feature_evidence.as_of_utc, "feature as_of_utc"),
    )
    if not observed <= available <= cutoff <= decision:
        raise ForwardCampaignAuthorityAdapterError(
            "pre-decision evidence violates observed/available/cutoff/decision order"
        )
    if _instant(rights_checked_at, "source rights checked_at") > cutoff:
        raise ForwardCampaignAuthorityAdapterError(
            "source-rights authorization was checked after decision cutoff"
        )
    if opportunity.causal_cutoff > cutoff:
        raise ForwardCampaignAuthorityAdapterError(
            "forward opportunity causal cutoff exceeds decision cutoff"
        )
    observation_time = _instant(observation.observed_at, "observation.observed_at")
    if not opportunity.observed_lower <= observation_time <= opportunity.observed_upper:
        raise ForwardCampaignAuthorityAdapterError(
            "observation falls outside frozen opportunity interval"
        )
    source_evidence_sha256 = _digest(
        {
            "schema": "autosport.forward_campaign_predecision_binding",
            "schema_version": 1,
            "campaign_id": opportunity.campaign_id,
            "protocol_sha256": opportunity.protocol_sha256,
            "candidate_sequence": opportunity.candidate_sequence,
            "opportunity_sha256": opportunity.opportunity_sha256,
            "source_receipt_id": receipt.receipt_id,
            "source_receipt_sha256": receipt.receipt_sha256,
            "observation_id": observation.observation_id,
            "feature_evidence_id": feature_evidence.evidence_id,
            "source_identity": source_identity,
            "required_scope": required_scope,
            "source_rights_manifest_sha256": rights_manifest_sha256,
        }
    )
    return (
        source_evidence_sha256,
        rights_manifest_sha256,
        _timestamp(observed, "observed"),
        _timestamp(available, "available"),
    )


def _terminal_common(
    *,
    action: Action,
    outcome: Outcome,
    reward: RewardEvidence,
    transition: Transition,
) -> tuple[str, str]:
    for name, value, expected_type in (
        ("action", action, Action),
        ("outcome", outcome, Outcome),
        ("reward", reward, RewardEvidence),
        ("transition", transition, Transition),
    ):
        if type(value) is not expected_type:
            raise TypeError(f"{name} must be exact {expected_type.__name__}")
    if (
        action.environment_id != outcome.environment_id
        or action.environment_id != reward.environment_id
        or action.environment_id != transition.environment_id
        or action.action_id != outcome.action_id
        or action.action_id != reward.action_id
        or action.action_id != transition.action_id
        or outcome.outcome_id != reward.outcome_id
        or outcome.outcome_id != transition.outcome_id
        or reward.reward_id != transition.reward_id
        or action.observation_id != transition.observation_id
    ):
        raise ForwardCampaignAuthorityAdapterError(
            "terminal evidence causal identities do not match"
        )
    if outcome.truth is not EvidenceTruth.OBSERVED or reward.truth is not EvidenceTruth.OBSERVED:
        raise ForwardCampaignAuthorityAdapterError(
            "forward terminal outcome and reward must be OBSERVED"
        )
    if outcome.simulation_model_id is not None or reward.simulation_model_id is not None:
        raise ForwardCampaignAuthorityAdapterError(
            "observed forward evidence cannot carry simulation identity"
        )
    outcome_available = max(
        _instant(outcome.revealed_at, "outcome.revealed_at"),
        _instant(reward.available_at, "reward.available_at"),
    )
    settled = _instant(transition.resolved_at, "transition.resolved_at")
    decision = _instant(action.decided_at, "action.decided_at")
    if outcome_available <= decision:
        raise ForwardCampaignAuthorityAdapterError(
            "terminal outcome must become available strictly after decision"
        )
    if settled < outcome_available:
        raise ForwardCampaignAuthorityAdapterError(
            "terminal transition resolved before outcome availability"
        )
    return (
        _timestamp(outcome_available, "outcome_available"),
        _timestamp(settled, "settled"),
    )


def admit_settled_action_sample(
    evidence: CampaignEvidence,
    *,
    candidate_sequence: int,
    session_id: str,
    role: PopulationRole,
    regime_id: str,
    feature_evidence: FeatureAvailabilityEvidence,
    source_rights: SourceRightsAuthorization,
    witness: PaperSettlementLearningWitness,
) -> tuple[ForwardPopulationSample, VerifiedLearningExperience]:
    """Bind one settled PAPER action to a population sample and learning input."""

    if type(role) is not PopulationRole:
        raise TypeError("role must be exact PopulationRole")
    _text(session_id, "session_id")
    _text(regime_id, "regime_id")
    if type(witness) is not PaperSettlementLearningWitness:
        raise TypeError("witness must be exact PaperSettlementLearningWitness")
    opportunity, receipt, boundary, verification = _campaign_member(
        evidence,
        candidate_sequence=candidate_sequence,
    )
    if opportunity.decision_state is not DecisionState.ACTION:
        raise ForwardCampaignAuthorityAdapterError(
            "settled action witness requires ACTION opportunity"
        )
    if witness.action.action_type != "PAPER_PROPOSAL":
        raise ForwardCampaignAuthorityAdapterError(
            "settled ACTION requires canonical PAPER_PROPOSAL action"
        )
    decision_id = opportunity.decision_id
    if decision_id is None or dict(witness.action.parameters).get(
        _DECISION_PARAMETER
    ) != decision_id:
        raise ForwardCampaignAuthorityAdapterError(
            "PAPER action does not bind forward opportunity decision"
        )
    source_sha, rights_sha, observed_at, available_at = _predecision_binding(
        opportunity=opportunity,
        receipt=receipt,
        observation=witness.observation,
        feature_evidence=feature_evidence,
        source_rights=source_rights,
        decided_at=witness.action.decided_at,
    )
    outcome_available_at, settled_at = _terminal_common(
        action=witness.action,
        outcome=witness.outcome,
        reward=witness.reward,
        transition=witness.transition,
    )
    if _instant(witness.outcome.revealed_at, "outcome.revealed_at") < boundary.boundary_lower:
        raise ForwardCampaignAuthorityAdapterError(
            "settled outcome predates frozen reveal boundary"
        )
    terminal_sha = _digest(
        {
            "schema": "autosport.forward_campaign_action_terminal",
            "schema_version": 1,
            "terminal_root_sha256": verification.terminal_root_sha256,
            "boundary_receipt_sha256": boundary.boundary_receipt_sha256,
            "ticket_id": witness.ticket_id,
            "binding_id": witness.binding_id,
            "settlement_bundle_sha256": witness.settlement_bundle_sha256,
            "outcome_id": witness.outcome.outcome_id,
            "reward_id": witness.reward.reward_id,
            "transition_id": witness.transition.transition_id,
            "next_checkpoint_id": witness.next_checkpoint.checkpoint_id,
        }
    )
    sample = ForwardPopulationSample(
        sample_id=opportunity.opportunity_id,
        session_id=session_id,
        role=role,
        source_id=source_rights.source_identity,
        event_id=boundary.event_or_market_id,
        regime_id=regime_id,
        source_evidence_sha256=source_sha,
        source_rights_manifest_sha256=rights_sha,
        observed_at=observed_at,
        available_at=available_at,
        decision_cutoff=feature_evidence.decision_cutoff_utc,
        decided_at=witness.action.decided_at,
        outcome_available_at=outcome_available_at,
        settled_at=settled_at,
        decision_state=DecisionState.ACTION,
        terminal_state=TerminalEvidenceState.SETTLED,
        terminal_evidence_sha256=terminal_sha,
        observed_reward=witness.reward.reward,
    )
    experience = VerifiedLearningExperience(
        sample_id=sample.sample_id,
        source_evidence_sha256=sample.source_evidence_sha256,
        action=witness.action,
        reward=witness.reward,
        transition=witness.transition,
    )
    return sample, experience


def admit_negative_sample(
    evidence: CampaignEvidence,
    *,
    candidate_sequence: int,
    session_id: str,
    role: PopulationRole,
    regime_id: str,
    feature_evidence: FeatureAvailabilityEvidence,
    source_rights: SourceRightsAuthorization,
    observation: Observation,
    action: Action,
    outcome: Outcome,
    reward: RewardEvidence,
    transition: Transition,
    finalization: PaperAbstentionFinalizationReceipt,
) -> ForwardPopulationSample:
    """Bind a finalized WAIT/NO_BET as negative evidence without synthetic reward."""

    if type(role) is not PopulationRole:
        raise TypeError("role must be exact PopulationRole")
    _text(session_id, "session_id")
    _text(regime_id, "regime_id")
    if type(observation) is not Observation:
        raise TypeError("observation must be exact Observation")
    if type(finalization) is not PaperAbstentionFinalizationReceipt:
        raise TypeError(
            "finalization must be exact PaperAbstentionFinalizationReceipt"
        )
    opportunity, receipt, boundary, verification = _campaign_member(
        evidence,
        candidate_sequence=candidate_sequence,
    )
    if opportunity.decision_state is DecisionState.ACTION:
        raise ForwardCampaignAuthorityAdapterError(
            "negative admission cannot consume ACTION opportunity"
        )
    if action.action_type not in _NEGATIVE_ACTIONS:
        raise ForwardCampaignAuthorityAdapterError(
            "negative admission requires canonical WAIT or NO_BET action"
        )
    if opportunity.decision_state is DecisionState.WAIT and action.action_type != "WAIT":
        raise ForwardCampaignAuthorityAdapterError(
            "WAIT opportunity requires exact WAIT action"
        )
    if opportunity.decision_state is not DecisionState.WAIT and action.action_type != "NO_BET":
        raise ForwardCampaignAuthorityAdapterError(
            "non-WAIT negative opportunity requires exact NO_BET action"
        )
    if action.observation_id != observation.observation_id:
        raise ForwardCampaignAuthorityAdapterError(
            "negative action does not bind exact observation"
        )
    source_sha, rights_sha, observed_at, available_at = _predecision_binding(
        opportunity=opportunity,
        receipt=receipt,
        observation=observation,
        feature_evidence=feature_evidence,
        source_rights=source_rights,
        decided_at=action.decided_at,
    )
    outcome_available_at, settled_at = _terminal_common(
        action=action,
        outcome=outcome,
        reward=reward,
        transition=transition,
    )
    if (
        finalization.action_id != action.action_id
        or finalization.transition_id != transition.transition_id
        or finalization.reward_id != reward.reward_id
        or finalization.reward_truth is not EvidenceTruth.OBSERVED
    ):
        raise ForwardCampaignAuthorityAdapterError(
            "abstention finalization does not bind exact terminal evidence"
        )
    for name in ("attribution_id", "postmortem_id", "checkpoint_id"):
        _sha256(getattr(finalization, name), f"finalization.{name}")
    if _instant(outcome.revealed_at, "outcome.revealed_at") < boundary.boundary_lower:
        raise ForwardCampaignAuthorityAdapterError(
            "negative outcome predates frozen reveal boundary"
        )
    terminal_sha = _digest(
        {
            "schema": "autosport.forward_campaign_negative_terminal",
            "schema_version": 1,
            "terminal_root_sha256": verification.terminal_root_sha256,
            "boundary_receipt_sha256": boundary.boundary_receipt_sha256,
            "action_id": finalization.action_id,
            "outcome_id": outcome.outcome_id,
            "reward_id": finalization.reward_id,
            "transition_id": finalization.transition_id,
            "attribution_id": finalization.attribution_id,
            "postmortem_id": finalization.postmortem_id,
            "checkpoint_id": finalization.checkpoint_id,
        }
    )
    return ForwardPopulationSample(
        sample_id=opportunity.opportunity_id,
        session_id=session_id,
        role=role,
        source_id=source_rights.source_identity,
        event_id=boundary.event_or_market_id,
        regime_id=regime_id,
        source_evidence_sha256=source_sha,
        source_rights_manifest_sha256=rights_sha,
        observed_at=observed_at,
        available_at=available_at,
        decision_cutoff=feature_evidence.decision_cutoff_utc,
        decided_at=action.decided_at,
        outcome_available_at=outcome_available_at,
        settled_at=settled_at,
        decision_state=opportunity.decision_state,
        terminal_state=TerminalEvidenceState.NEGATIVE,
        terminal_evidence_sha256=terminal_sha,
        observed_reward=None,
    )


__all__ = [
    "ForwardCampaignAuthorityAdapterError",
    "admit_negative_sample",
    "admit_settled_action_sample",
]
