"""Fail-closed structural qualification for multi-episode PAPER/SHADOW campaigns.

This module is composition-only.  It does not create a campaign runtime, store,
risk authority, execution authority, settlement authority, learning authority,
promotion authority, or release/readiness authority.  It verifies that a set of
already-issued evidence anchors is causally and identity-consistent enough to be
submitted to the canonical authorities for final resolution.

A structurally complete report deliberately keeps ``canonical_authority_verified``
false.  Hash-shaped caller data proves integrity/continuity only; it is not a
product-owned trust root.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final


SCHEMA: Final = "autosport.multiday_campaign_qualification"
SCHEMA_VERSION: Final = 1
MIN_MULTIDAY_SECONDS: Final = 24 * 60 * 60
_HEX: Final = frozenset("0123456789abcdef")
_COMPLETE_ECONOMICS: Final = "COMPLETE_NET_ECONOMICS"


class CampaignQualificationError(ValueError):
    """Malformed qualification evidence."""


class DecisionKind(StrEnum):
    BET = "BET"
    WAIT = "WAIT"
    NO_BET = "NO_BET"


class ExecutionDisposition(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class QualificationState(StrEnum):
    BLOCKED = "BLOCKED"
    COMPLETE_FOR_CANONICAL_RESOLUTION = "COMPLETE_FOR_CANONICAL_RESOLUTION"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise CampaignQualificationError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CampaignQualificationError(f"{name} must be valid UTF-8") from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if text != text.lower():
        raise CampaignQualificationError(
            f"{name} must use lowercase canonical SHA-256 hex"
        )
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise CampaignQualificationError(f"{name} must be canonical SHA-256 hex")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignQualificationError(
            f"{name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignQualificationError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CampaignQualificationError(
            "qualification evidence is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceAnchor:
    """Opaque integrity pointer to evidence issued by another authority."""

    authority_family: str
    evidence_id: str
    sha256: str
    available_at: str

    def __post_init__(self) -> None:
        _text(self.authority_family, "authority_family")
        _text(self.evidence_id, "evidence_id")
        _sha(self.sha256, "sha256")
        _instant(self.available_at, "available_at")

    def payload(self) -> dict[str, str]:
        return {
            "authority_family": self.authority_family,
            "evidence_id": self.evidence_id,
            "sha256": self.sha256.lower(),
            "available_at": _timestamp(self.available_at, "available_at"),
        }


@dataclass(frozen=True, slots=True)
class CampaignQualificationIdentity:
    campaign_id: str
    precommit_manifest_sha256: str
    source_sha256: str
    config_sha256: str
    economic_goal_sha256: str
    risk_policy_sha256: str
    provider_scope_sha256: str
    bankroll_currency: str
    campaign_started_at: str
    qualification_profile_version: str = "multiday-paper-shadow-v1"

    def __post_init__(self) -> None:
        _text(self.campaign_id, "campaign_id")
        for name in (
            "precommit_manifest_sha256",
            "source_sha256",
            "config_sha256",
            "economic_goal_sha256",
            "risk_policy_sha256",
            "provider_scope_sha256",
        ):
            _sha(getattr(self, name), name)
        currency = _text(self.bankroll_currency, "bankroll_currency")
        if currency != currency.upper() or not currency.isascii():
            raise CampaignQualificationError(
                "bankroll_currency must be uppercase ASCII"
            )
        if not (3 <= len(currency) <= 12) or not all(
            ch.isalnum() or ch in "_-" for ch in currency
        ):
            raise CampaignQualificationError(
                "bankroll_currency must be a canonical currency/unit token"
            )
        _instant(self.campaign_started_at, "campaign_started_at")
        _text(self.qualification_profile_version, "qualification_profile_version")

    @property
    def identity_sha256(self) -> str:
        return _digest(self.payload())

    def payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "precommit_manifest_sha256": self.precommit_manifest_sha256.lower(),
            "source_sha256": self.source_sha256.lower(),
            "config_sha256": self.config_sha256.lower(),
            "economic_goal_sha256": self.economic_goal_sha256.lower(),
            "risk_policy_sha256": self.risk_policy_sha256.lower(),
            "provider_scope_sha256": self.provider_scope_sha256.lower(),
            "bankroll_currency": self.bankroll_currency,
            "campaign_started_at": _timestamp(
                self.campaign_started_at, "campaign_started_at"
            ),
            "qualification_profile_version": self.qualification_profile_version,
        }


@dataclass(frozen=True, slots=True)
class EpisodeQualificationEvidence:
    """Cross-authority evidence anchors for one serial campaign episode."""

    episode_index: int
    episode_id: str
    campaign_identity_sha256: str
    predecessor_checkpoint_sha256: str | None
    restart_handoff: EvidenceAnchor | None
    observation: EvidenceAnchor
    denominator_member: EvidenceAnchor
    decision: EvidenceAnchor
    decision_kind: DecisionKind
    paper_action: EvidenceAnchor | None
    execution_disposition: ExecutionDisposition
    terminal_resolution: EvidenceAnchor | None
    cost_evidence: EvidenceAnchor | None
    economic_completeness: str
    utility_evidence: EvidenceAnchor | None
    learning_evidence: EvidenceAnchor | None
    checkpoint: EvidenceAnchor

    def __post_init__(self) -> None:
        if type(self.episode_index) is not int or self.episode_index <= 0:
            raise CampaignQualificationError("episode_index must be a positive integer")
        _text(self.episode_id, "episode_id")
        _sha(self.campaign_identity_sha256, "campaign_identity_sha256")
        if self.predecessor_checkpoint_sha256 is not None:
            _sha(
                self.predecessor_checkpoint_sha256,
                "predecessor_checkpoint_sha256",
            )
        for name in ("observation", "denominator_member", "decision", "checkpoint"):
            if type(getattr(self, name)) is not EvidenceAnchor:
                raise TypeError(f"{name} must be EvidenceAnchor")
        for name in (
            "restart_handoff",
            "paper_action",
            "terminal_resolution",
            "cost_evidence",
            "utility_evidence",
            "learning_evidence",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not EvidenceAnchor:
                raise TypeError(f"{name} must be EvidenceAnchor or None")
        if not isinstance(self.decision_kind, DecisionKind):
            raise TypeError("decision_kind must be DecisionKind")
        if not isinstance(self.execution_disposition, ExecutionDisposition):
            raise TypeError("execution_disposition must be ExecutionDisposition")
        _text(self.economic_completeness, "economic_completeness")

    @property
    def episode_sha256(self) -> str:
        return _digest(self.payload())

    def payload(self) -> dict[str, object]:
        def anchor(value: EvidenceAnchor | None) -> dict[str, str] | None:
            return None if value is None else value.payload()

        return {
            "episode_index": self.episode_index,
            "episode_id": self.episode_id,
            "campaign_identity_sha256": self.campaign_identity_sha256.lower(),
            "predecessor_checkpoint_sha256": (
                None
                if self.predecessor_checkpoint_sha256 is None
                else self.predecessor_checkpoint_sha256.lower()
            ),
            "restart_handoff": anchor(self.restart_handoff),
            "observation": self.observation.payload(),
            "denominator_member": self.denominator_member.payload(),
            "decision": self.decision.payload(),
            "decision_kind": self.decision_kind.value,
            "paper_action": anchor(self.paper_action),
            "execution_disposition": self.execution_disposition.value,
            "terminal_resolution": anchor(self.terminal_resolution),
            "cost_evidence": anchor(self.cost_evidence),
            "economic_completeness": self.economic_completeness,
            "utility_evidence": anchor(self.utility_evidence),
            "learning_evidence": anchor(self.learning_evidence),
            "checkpoint": self.checkpoint.payload(),
        }


@dataclass(frozen=True, slots=True)
class CampaignQualificationEvidence:
    identity: CampaignQualificationIdentity
    episodes: tuple[EpisodeQualificationEvidence, ...]
    stop: EvidenceAnchor
    terminal_bundle: EvidenceAnchor

    def __post_init__(self) -> None:
        if type(self.identity) is not CampaignQualificationIdentity:
            raise TypeError("identity must be CampaignQualificationIdentity")
        if type(self.episodes) is not tuple or not self.episodes:
            raise CampaignQualificationError("episodes must be a non-empty tuple")
        if any(type(item) is not EpisodeQualificationEvidence for item in self.episodes):
            raise TypeError("episodes must contain EpisodeQualificationEvidence")
        if type(self.stop) is not EvidenceAnchor:
            raise TypeError("stop must be EvidenceAnchor")
        if type(self.terminal_bundle) is not EvidenceAnchor:
            raise TypeError("terminal_bundle must be EvidenceAnchor")


@dataclass(frozen=True, slots=True)
class CampaignQualificationReport:
    state: QualificationState
    blockers: tuple[str, ...]
    identity_sha256: str
    expected_terminal_bundle_sha256: str
    episode_sha256s: tuple[str, ...]
    qualification_sha256: str
    canonical_authority_verified: bool = False
    promotion_authority: bool = False
    readiness_authority: bool = False
    real_money_execution: bool = False
    human_tested: bool = False
    nvda_verified: bool = False
    whole_product_complete: bool = False


def structural_terminal_bundle_sha256(
    identity: CampaignQualificationIdentity,
    episodes: tuple[EpisodeQualificationEvidence, ...],
    stop: EvidenceAnchor,
) -> str:
    """Return the deterministic structural bundle commitment.

    The digest is not authority.  It only commits to the caller-supplied anchors.
    """
    if type(identity) is not CampaignQualificationIdentity:
        raise TypeError("identity must be CampaignQualificationIdentity")
    if type(episodes) is not tuple or not episodes:
        raise CampaignQualificationError("episodes must be a non-empty tuple")
    if any(type(item) is not EpisodeQualificationEvidence for item in episodes):
        raise TypeError("episodes must contain EpisodeQualificationEvidence")
    if type(stop) is not EvidenceAnchor:
        raise TypeError("stop must be EvidenceAnchor")
    return _digest(
        {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "identity_sha256": identity.identity_sha256,
            "episode_sha256s": [item.episode_sha256 for item in episodes],
            "stop": stop.payload(),
        }
    )


def assess_campaign_qualification(
    evidence: CampaignQualificationEvidence,
) -> CampaignQualificationReport:
    """Assess structural completeness without minting canonical authority."""
    if type(evidence) is not CampaignQualificationEvidence:
        raise TypeError("evidence must be CampaignQualificationEvidence")

    blockers: list[str] = []
    identity = evidence.identity
    episodes = evidence.episodes
    identity_sha = identity.identity_sha256

    expected_indexes = tuple(range(1, len(episodes) + 1))
    actual_indexes = tuple(item.episode_index for item in episodes)
    if actual_indexes != expected_indexes:
        blockers.append("episode indexes must be contiguous and start at 1")
    if len({item.episode_id for item in episodes}) != len(episodes):
        blockers.append("episode identities must be unique")
    if len({item.checkpoint.sha256 for item in episodes}) != len(episodes):
        blockers.append("episode checkpoints must be unique")
    if len({item.observation.sha256 for item in episodes}) != len(episodes):
        blockers.append("episode observations must bind distinct evidence bytes")

    used_anchor_identities: dict[tuple[str, str], tuple[str, str]] = {}

    def record_anchor(label: str, anchor: EvidenceAnchor) -> None:
        key = (anchor.authority_family, anchor.evidence_id)
        previous = used_anchor_identities.get(key)
        if previous is None:
            used_anchor_identities[key] = (anchor.sha256, label)
            return
        previous_sha, previous_label = previous
        if anchor.sha256 == previous_sha:
            blockers.append(
                f"{label}: reuses evidence anchor already used by {previous_label}"
            )
        else:
            blockers.append(
                f"{label}: evidence identity already used by {previous_label} with different SHA-256"
            )

    start = _instant(identity.campaign_started_at, "campaign_started_at")
    previous_checkpoint: EvidenceAnchor | None = None
    for item in episodes:
        prefix = f"episode {item.episode_index}: "
        for role in (
            "restart_handoff",
            "observation",
            "denominator_member",
            "decision",
            "paper_action",
            "terminal_resolution",
            "cost_evidence",
            "utility_evidence",
            "learning_evidence",
            "checkpoint",
        ):
            anchor = getattr(item, role)
            if anchor is not None:
                record_anchor(prefix + role, anchor)
        if item.campaign_identity_sha256.lower() != identity_sha:
            blockers.append(prefix + "campaign identity drift")

        observation_at = _instant(item.observation.available_at, "observation.available_at")
        denominator_at = _instant(
            item.denominator_member.available_at,
            "denominator_member.available_at",
        )
        decision_at = _instant(item.decision.available_at, "decision.available_at")
        checkpoint_at = _instant(item.checkpoint.available_at, "checkpoint.available_at")

        if observation_at < start:
            blockers.append(prefix + "observation predates campaign start")
        if denominator_at > decision_at:
            blockers.append(prefix + "denominator evidence was not available by decision")
        if observation_at > decision_at:
            blockers.append(prefix + "decision predates observation availability")

        if previous_checkpoint is None:
            if item.predecessor_checkpoint_sha256 is not None:
                blockers.append(prefix + "first episode must not claim a predecessor checkpoint")
            if item.restart_handoff is not None:
                blockers.append(prefix + "first episode must not claim a restart handoff")
        else:
            if item.predecessor_checkpoint_sha256 != previous_checkpoint.sha256:
                blockers.append(prefix + "predecessor checkpoint does not match prior episode")
            if item.restart_handoff is None:
                blockers.append(prefix + "restart handoff evidence is required")
            else:
                restart_at = _instant(
                    item.restart_handoff.available_at,
                    "restart_handoff.available_at",
                )
                previous_at = _instant(
                    previous_checkpoint.available_at,
                    "previous checkpoint available_at",
                )
                if restart_at < previous_at:
                    blockers.append(prefix + "restart handoff predates prior checkpoint")
                if restart_at > observation_at:
                    blockers.append(prefix + "observation predates restart handoff")

        terminal_at: datetime | None = None
        if item.decision_kind is DecisionKind.BET:
            if item.paper_action is None:
                blockers.append(prefix + "BET requires PAPER action evidence")
            if item.execution_disposition not in {
                ExecutionDisposition.ACCEPTED,
                ExecutionDisposition.PARTIAL,
            }:
                blockers.append(prefix + "BET execution is not accepted/partial")
            if item.terminal_resolution is None:
                blockers.append(prefix + "BET requires authoritative settlement evidence")
            if item.paper_action is not None:
                action_at = _instant(
                    item.paper_action.available_at,
                    "paper_action.available_at",
                )
                if action_at < decision_at:
                    blockers.append(prefix + "PAPER action predates decision")
            if item.terminal_resolution is not None:
                terminal_at = _instant(
                    item.terminal_resolution.available_at,
                    "terminal_resolution.available_at",
                )
                floor = decision_at
                if item.paper_action is not None:
                    floor = max(
                        floor,
                        _instant(
                            item.paper_action.available_at,
                            "paper_action.available_at",
                        ),
                    )
                if terminal_at < floor:
                    blockers.append(prefix + "settlement predates decision/action")
        else:
            if item.paper_action is not None:
                blockers.append(prefix + "WAIT/NO_BET must not carry PAPER action evidence")
            if item.execution_disposition is not ExecutionDisposition.NOT_APPLICABLE:
                blockers.append(prefix + "WAIT/NO_BET execution must be NOT_APPLICABLE")
            if item.terminal_resolution is None:
                blockers.append(prefix + "WAIT/NO_BET requires authoritative evaluation evidence")
            else:
                terminal_at = _instant(
                    item.terminal_resolution.available_at,
                    "terminal_resolution.available_at",
                )
                if terminal_at < decision_at:
                    blockers.append(prefix + "evaluation predates decision")

        if item.cost_evidence is None:
            blockers.append(prefix + "after-cost evidence is missing")
        if item.economic_completeness != _COMPLETE_ECONOMICS:
            blockers.append(prefix + "net economics are not COMPLETE_NET_ECONOMICS")
        if item.utility_evidence is None:
            blockers.append(prefix + "authoritative utility evidence is missing")
        if item.learning_evidence is None:
            blockers.append(prefix + "learning evidence is missing")

        causal_floor = terminal_at or decision_at
        if item.cost_evidence is not None:
            cost_at = _instant(
                item.cost_evidence.available_at,
                "cost_evidence.available_at",
            )
            if cost_at < causal_floor:
                blockers.append(prefix + "cost evidence predates terminal resolution")
            causal_floor = max(causal_floor, cost_at)
        if item.utility_evidence is not None:
            utility_at = _instant(
                item.utility_evidence.available_at,
                "utility_evidence.available_at",
            )
            if utility_at < causal_floor:
                blockers.append(prefix + "utility predates terminal/cost evidence")
            causal_floor = max(causal_floor, utility_at)
        if item.learning_evidence is not None:
            learning_at = _instant(
                item.learning_evidence.available_at,
                "learning_evidence.available_at",
            )
            if learning_at < causal_floor:
                blockers.append(prefix + "learning predates authoritative utility")
            causal_floor = max(causal_floor, learning_at)
        if checkpoint_at < causal_floor:
            blockers.append(prefix + "checkpoint predates completed learning chain")

        previous_checkpoint = item.checkpoint

    record_anchor("campaign stop", evidence.stop)
    record_anchor("terminal bundle", evidence.terminal_bundle)

    last_checkpoint = episodes[-1].checkpoint
    last_checkpoint_at = _instant(last_checkpoint.available_at, "last checkpoint")
    first_observation_at = _instant(
        episodes[0].observation.available_at,
        "first observation",
    )
    last_observation_at = _instant(
        episodes[-1].observation.available_at,
        "last observation",
    )
    stop_at = _instant(evidence.stop.available_at, "stop.available_at")
    bundle_at = _instant(evidence.terminal_bundle.available_at, "terminal_bundle.available_at")

    if stop_at < last_checkpoint_at:
        blockers.append("durable STOP predates the final campaign checkpoint")
    if bundle_at < stop_at:
        blockers.append("terminal evidence bundle predates durable STOP")
    if (last_observation_at - first_observation_at).total_seconds() < MIN_MULTIDAY_SECONDS:
        blockers.append("observed evidence does not span the minimum 24-hour multi-day interval")

    expected_bundle = structural_terminal_bundle_sha256(
        identity,
        episodes,
        evidence.stop,
    )
    if evidence.terminal_bundle.sha256.lower() != expected_bundle:
        blockers.append("terminal evidence bundle digest does not bind the structural campaign")

    blockers_tuple = tuple(sorted(set(blockers)))
    state = (
        QualificationState.COMPLETE_FOR_CANONICAL_RESOLUTION
        if not blockers_tuple
        else QualificationState.BLOCKED
    )
    episode_sha256s = tuple(item.episode_sha256 for item in episodes)
    report_payload = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "state": state.value,
        "blockers": list(blockers_tuple),
        "identity_sha256": identity_sha,
        "expected_terminal_bundle_sha256": expected_bundle,
        "episode_sha256s": list(episode_sha256s),
        "canonical_authority_verified": False,
        "promotion_authority": False,
        "readiness_authority": False,
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "whole_product_complete": False,
    }
    return CampaignQualificationReport(
        state=state,
        blockers=blockers_tuple,
        identity_sha256=identity_sha,
        expected_terminal_bundle_sha256=expected_bundle,
        episode_sha256s=episode_sha256s,
        qualification_sha256=_digest(report_payload),
    )


__all__ = [
    "CampaignQualificationError",
    "CampaignQualificationEvidence",
    "CampaignQualificationIdentity",
    "CampaignQualificationReport",
    "DecisionKind",
    "EpisodeQualificationEvidence",
    "EvidenceAnchor",
    "ExecutionDisposition",
    "MIN_MULTIDAY_SECONDS",
    "QualificationState",
    "assess_campaign_qualification",
    "structural_terminal_bundle_sha256",
]
