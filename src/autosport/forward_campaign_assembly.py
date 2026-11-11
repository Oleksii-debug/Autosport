"""Pure exact-denominator composition of canonical forward campaign authorities.

This module adds no collector, settlement engine, evaluator, journal, or training
loop. Population allocation is bound into the existing protocol precommit; every
frozen candidate must then be materialized by the existing authority adapter.
An assembly receipt proves that composition, not the external truth of receipts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .continual_learning_cycle import VerifiedLearningExperience
from .forward_campaign_authority_adapter import (
    admit_negative_sample,
    admit_settled_action_sample,
    admit_settled_evaluation_sample,
)
from .forward_campaign_population import (
    ForwardCampaignPopulationManifest,
    PopulationRole,
    build_forward_campaign_manifest,
    build_forward_campaign_session,
    verify_forward_campaign_restart,
)
from .forward_evidence_completeness import CampaignEvidence, DecisionState, verify_campaign
from .learning_environment import Action, Observation, Outcome, RewardEvidence, Transition
from .paper_abstention_learning import PaperAbstentionFinalizationReceipt
from .paper_settlement_learning import PaperSettlementLearningWitness
from .point_in_time_evidence import FeatureAvailabilityEvidence
from .source_rights_manifest import SourceRightsAuthorization


class ForwardCampaignAssemblyError(ValueError):
    """Frozen candidate membership does not exactly cover canonical authorities."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ForwardCampaignAssemblyError(f"{name} must be canonical non-empty text")
    value.encode("utf-8", errors="strict")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ForwardCampaignAssemblyError(f"{name} must be lowercase SHA-256")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardCampaignAssemblyError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardCampaignAssemblyError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: str) -> str:
    return _instant(value, "timestamp").isoformat().replace("+00:00", "Z")


def _bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidatePopulationAssignment:
    candidate_sequence: int
    role: PopulationRole
    session_id: str
    regime_id: str

    def __post_init__(self) -> None:
        if type(self.candidate_sequence) is not int or self.candidate_sequence <= 0:
            raise ForwardCampaignAssemblyError("candidate_sequence must be a positive exact int")
        if type(self.role) is not PopulationRole:
            raise TypeError("role must be exact PopulationRole")
        _text(self.session_id, "session_id")
        _text(self.regime_id, "regime_id")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "candidate_sequence": self.candidate_sequence,
            "role": self.role.value,
            "session_id": self.session_id,
            "regime_id": self.regime_id,
        }


@dataclass(frozen=True, slots=True)
class FrozenForwardPopulationPolicy:
    """Population part of the protocol's forward-evaluation-policy commitment.

    The underlying scientific evaluation policy remains independently identified
    by ``base_evaluation_policy_sha256``. ``policy_sha256`` commits that identity
    together with the bounded population allocation, before any observation.
    """

    policy_id: str
    base_evaluation_policy_sha256: str
    frozen_at: str
    assignments: tuple[CandidatePopulationAssignment, ...]

    def __post_init__(self) -> None:
        _text(self.policy_id, "policy_id")
        _sha(self.base_evaluation_policy_sha256, "base_evaluation_policy_sha256")
        _instant(self.frozen_at, "frozen_at")
        if type(self.assignments) is not tuple or not self.assignments:
            raise ForwardCampaignAssemblyError("assignments must be a non-empty exact tuple")
        for sequence, assignment in enumerate(self.assignments, start=1):
            if type(assignment) is not CandidatePopulationAssignment:
                raise TypeError("assignments must be exact CandidatePopulationAssignment")
            assignment.__post_init__()
            if assignment.candidate_sequence != sequence:
                raise ForwardCampaignAssemblyError("assignments must be gap-free sequence ordered")
        if {assignment.role for assignment in self.assignments} != set(PopulationRole):
            raise ForwardCampaignAssemblyError("all four population roles must be precommitted")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.forward_population_policy",
            "schema_version": 1,
            "policy_id": self.policy_id,
            "base_evaluation_policy_sha256": self.base_evaluation_policy_sha256,
            "frozen_at": _timestamp(self.frozen_at),
            "assignments": [item.canonical_payload() for item in self.assignments],
        }

    @property
    def policy_sha256(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ForwardAssemblySessionWindow:
    session_id: str
    opened_at: str
    closed_at: str
    drift_evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.session_id, "session_id")
        opened = _instant(self.opened_at, "opened_at")
        if _instant(self.closed_at, "closed_at") < opened:
            raise ForwardCampaignAssemblyError("session closes before it opens")
        _sha(self.drift_evidence_sha256, "drift_evidence_sha256")


@dataclass(frozen=True, slots=True)
class FinalizedNegativeAuthorities:
    observation: Observation
    action: Action
    outcome: Outcome
    reward: RewardEvidence
    transition: Transition
    finalization: PaperAbstentionFinalizationReceipt

    def __post_init__(self) -> None:
        for name, expected in (
            ("observation", Observation), ("action", Action), ("outcome", Outcome),
            ("reward", RewardEvidence), ("transition", Transition),
            ("finalization", PaperAbstentionFinalizationReceipt),
        ):
            if type(getattr(self, name)) is not expected:
                raise TypeError(f"{name} must be exact {expected.__name__}")


@dataclass(frozen=True, slots=True)
class ForwardCandidateAuthorities:
    candidate_sequence: int
    feature_evidence: FeatureAvailabilityEvidence
    source_rights: SourceRightsAuthorization
    terminal: PaperSettlementLearningWitness | FinalizedNegativeAuthorities

    def __post_init__(self) -> None:
        if type(self.candidate_sequence) is not int or self.candidate_sequence <= 0:
            raise ForwardCampaignAssemblyError("candidate_sequence must be a positive exact int")
        if type(self.feature_evidence) is not FeatureAvailabilityEvidence:
            raise TypeError("feature_evidence must be exact FeatureAvailabilityEvidence")
        if type(self.source_rights) is not SourceRightsAuthorization:
            raise TypeError("source_rights must be exact SourceRightsAuthorization")
        if type(self.terminal) not in {
            PaperSettlementLearningWitness, FinalizedNegativeAuthorities
        }:
            raise TypeError("terminal must use exact canonical settlement or negative authorities")


@dataclass(frozen=True, slots=True)
class ForwardCampaignAssembly:
    """Replayable composition result; no persistence, promotion or execution authority."""

    manifest: ForwardCampaignPopulationManifest
    protocol_sha256: str
    population_policy_sha256: str
    terminal_root_sha256: str
    coverage: tuple[tuple[int, str, str], ...]
    learning_experiences: tuple[VerifiedLearningExperience, ...]

    def __post_init__(self) -> None:
        if type(self.manifest) is not ForwardCampaignPopulationManifest:
            raise TypeError("manifest must be exact ForwardCampaignPopulationManifest")
        verify_forward_campaign_restart(self.manifest)
        for name in ("protocol_sha256", "population_policy_sha256", "terminal_root_sha256"):
            _sha(getattr(self, name), name)
        if type(self.coverage) is not tuple or len(self.coverage) != len(self.manifest.samples):
            raise ForwardCampaignAssemblyError("coverage must match exact manifest membership")
        for sequence, member in enumerate(self.coverage, start=1):
            if (type(member) is not tuple or len(member) != 3
                    or type(member[0]) is not int or member[0] != sequence):
                raise ForwardCampaignAssemblyError("coverage must be gap-free exact triples")
            _sha(member[1], "coverage opportunity SHA")
            _sha(member[2], "coverage sample SHA")
        if sorted(member[2] for member in self.coverage) != sorted(
            sample.sample_sha256 for sample in self.manifest.samples
        ):
            raise ForwardCampaignAssemblyError("coverage sample identities differ from manifest")
        if type(self.learning_experiences) is not tuple or any(
            type(item) is not VerifiedLearningExperience for item in self.learning_experiences
        ):
            raise TypeError("learning_experiences must be an exact tuple of canonical inputs")
        expected = {
            sample.sample_id: sample.source_evidence_sha256
            for sample in self.manifest.samples
            if sample.role in {PopulationRole.NEW, PopulationRole.REPLAY}
            and sample.decision_state is DecisionState.ACTION
        }
        actual = {item.sample_id: item.source_evidence_sha256
                  for item in self.learning_experiences}
        if actual != expected or len(actual) != len(self.learning_experiences):
            raise ForwardCampaignAssemblyError("learning members differ from training population")

    def payload_without_sha(self) -> dict[str, object]:
        return {
            "schema": "autosport.forward_campaign_assembly",
            "schema_version": 1,
            "manifest": self.manifest.canonical_payload(),
            "protocol_sha256": self.protocol_sha256,
            "population_policy_sha256": self.population_policy_sha256,
            "terminal_root_sha256": self.terminal_root_sha256,
            "coverage": [list(item) for item in self.coverage],
            "learning_experience_ids": [item.experience_id for item in self.learning_experiences],
            "profitability_claim": False,
            "promotion_authorized": False,
            "real_money_execution": False,
        }

    @property
    def assembly_sha256(self) -> str:
        return _digest(self.payload_without_sha())

    def canonical_payload(self) -> dict[str, object]:
        return {**self.payload_without_sha(), "assembly_sha256": self.assembly_sha256}


def assemble_forward_campaign(
    evidence: CampaignEvidence,
    *,
    population_policy: FrozenForwardPopulationPolicy,
    session_windows: tuple[ForwardAssemblySessionWindow, ...],
    authorities: tuple[ForwardCandidateAuthorities, ...],
    created_at: str,
) -> ForwardCampaignAssembly:
    """Require exact frozen coverage and delegate all member admission to its owner."""

    if type(evidence) is not CampaignEvidence:
        raise TypeError("evidence must be exact CampaignEvidence")
    if type(population_policy) is not FrozenForwardPopulationPolicy:
        raise TypeError("population_policy must be exact FrozenForwardPopulationPolicy")
    population_policy.__post_init__()
    verdict = verify_campaign(evidence)
    if not verdict.ok:
        raise ForwardCampaignAssemblyError("canonical campaign structural verification failed")
    if evidence.protocol.forward_evaluation_policy_sha256 != population_policy.policy_sha256:
        raise ForwardCampaignAssemblyError("population policy differs from protocol precommit")
    if (
        _instant(population_policy.frozen_at, "frozen_at")
        > evidence.protocol.precommit_anchor_lower
    ):
        raise ForwardCampaignAssemblyError("population policy was frozen after precommit began")
    created = _instant(created_at, "created_at")
    if any(close.anchor_upper is None or close.anchor_upper > created
           for close in evidence.closes):
        raise ForwardCampaignAssemblyError("assembly predates anchored campaign close")

    if type(authorities) is not tuple:
        raise TypeError("authorities must be an exact tuple")
    by_sequence = {}
    for item in authorities:
        if type(item) is not ForwardCandidateAuthorities:
            raise TypeError("authorities must be exact ForwardCandidateAuthorities")
        item.__post_init__()
        if item.candidate_sequence in by_sequence:
            raise ForwardCampaignAssemblyError("duplicate candidate authorities")
        by_sequence[item.candidate_sequence] = item
    expected = tuple(item.candidate_sequence for item in population_policy.assignments)
    opportunities = tuple(sorted(evidence.opportunities, key=lambda item: item.candidate_sequence))
    if (
        tuple(item.candidate_sequence for item in opportunities) != expected
        or tuple(sorted(evidence.denominator_sequences)) != expected
        or tuple(sorted(by_sequence)) != expected
    ):
        raise ForwardCampaignAssemblyError("exact candidate denominator coverage mismatch")

    # The adapter's composite source key includes candidate/protocol context.
    # Also fence the raw immutable evidence identities: renaming a candidate or
    # event must not make an already-used source record look independent.
    receipt_hashes = [item.source_receipt_sha256 for item in opportunities]
    record_hashes = [item.feature_evidence.dataset_record_sha256 for item in authorities]
    for identities, name in ((receipt_hashes, "source receipt"),
                             (record_hashes, "dataset record")):
        if len(identities) != len(set(identities)):
            raise ForwardCampaignAssemblyError(f"raw {name} identity reused by candidates")

    samples = []
    learning = []
    coverage = []
    for opportunity, assignment in zip(opportunities, population_policy.assignments, strict=True):
        item = by_sequence[assignment.candidate_sequence]
        common = {
            "candidate_sequence": assignment.candidate_sequence,
            "session_id": assignment.session_id,
            "role": assignment.role,
            "regime_id": assignment.regime_id,
            "feature_evidence": item.feature_evidence,
            "source_rights": item.source_rights,
        }
        terminal = item.terminal
        if opportunity.decision_state is DecisionState.ACTION:
            if type(terminal) is not PaperSettlementLearningWitness:
                raise ForwardCampaignAssemblyError("ACTION requires settlement witness")
            if assignment.role in {PopulationRole.NEW, PopulationRole.REPLAY}:
                sample, experience = admit_settled_action_sample(
                    evidence, **common, witness=terminal
                )
                learning.append(experience)
            else:
                sample = admit_settled_evaluation_sample(evidence, **common, witness=terminal)
        else:
            if type(terminal) is not FinalizedNegativeAuthorities:
                raise ForwardCampaignAssemblyError("non-action requires negative authorities")
            terminal.__post_init__()
            sample = admit_negative_sample(
                evidence, **common,
                observation=terminal.observation, action=terminal.action,
                outcome=terminal.outcome, reward=terminal.reward,
                transition=terminal.transition, finalization=terminal.finalization,
            )
        samples.append(sample)
        coverage.append((opportunity.candidate_sequence, opportunity.opportunity_sha256,
                         sample.sample_sha256))

    if type(session_windows) is not tuple or len(session_windows) < 2:
        raise ForwardCampaignAssemblyError("at least two exact session windows are required")
    sessions = []
    seen = set()
    predecessor = "GENESIS"
    for index, window in enumerate(session_windows, start=1):
        if type(window) is not ForwardAssemblySessionWindow:
            raise TypeError("windows must be exact ForwardAssemblySessionWindow")
        window.__post_init__()
        if window.session_id in seen:
            raise ForwardCampaignAssemblyError("duplicate session window")
        seen.add(window.session_id)
        if _instant(window.closed_at, "closed_at") > created:
            raise ForwardCampaignAssemblyError("assembly predates session close")
        session = build_forward_campaign_session(
            session_id=window.session_id, session_index=index,
            opened_at=window.opened_at, closed_at=window.closed_at,
            samples=tuple(sample for sample in samples if sample.session_id == window.session_id),
            drift_evidence_sha256=window.drift_evidence_sha256,
            predecessor_session_sha256=predecessor,
        )
        sessions.append(session)
        predecessor = session.session_sha256
    if seen != {assignment.session_id for assignment in population_policy.assignments}:
        raise ForwardCampaignAssemblyError("session windows do not cover exact frozen allocation")
    manifest = build_forward_campaign_manifest(
        campaign_id=evidence.protocol.campaign_id,
        scientific_protocol_sha256=evidence.protocol.scientific_protocol_sha256,
        created_at=created_at, samples=tuple(samples), sessions=tuple(sessions),
    )
    verify_forward_campaign_restart(manifest)
    return ForwardCampaignAssembly(
        manifest=manifest, protocol_sha256=verdict.protocol_sha256,
        population_policy_sha256=population_policy.policy_sha256,
        terminal_root_sha256=verdict.terminal_root_sha256,
        coverage=tuple(coverage), learning_experiences=tuple(learning),
    )


def restore_forward_campaign_assembly(
    payload: object,
    evidence: CampaignEvidence,
    *,
    population_policy: FrozenForwardPopulationPolicy,
    session_windows: tuple[ForwardAssemblySessionWindow, ...],
    authorities: tuple[ForwardCandidateAuthorities, ...],
    created_at: str,
) -> ForwardCampaignAssembly:
    """Re-resolve issued rights and terminal authorities; never trust saved counts."""

    rebuilt = assemble_forward_campaign(
        evidence, population_policy=population_policy, session_windows=session_windows,
        authorities=authorities, created_at=created_at,
    )
    if _bytes(payload) != _bytes(rebuilt.canonical_payload()):
        raise ForwardCampaignAssemblyError("assembly restart authority replay mismatch")
    return rebuilt


__all__ = [
    "CandidatePopulationAssignment", "FinalizedNegativeAuthorities",
    "ForwardAssemblySessionWindow", "ForwardCampaignAssembly",
    "ForwardCampaignAssemblyError", "ForwardCandidateAuthorities",
    "FrozenForwardPopulationPolicy", "assemble_forward_campaign",
    "restore_forward_campaign_assembly",
]
