from __future__ import annotations

import hashlib

from autosport.campaign_qualification import (
    CampaignQualificationEvidence,
    CampaignQualificationIdentity,
    DecisionKind,
    EpisodeQualificationEvidence,
    EvidenceAnchor,
    ExecutionDisposition,
    QualificationState,
    assess_campaign_qualification,
    structural_terminal_bundle_sha256,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _anchor(label: str, available_at: str) -> EvidenceAnchor:
    return EvidenceAnchor(
        authority_family=f"test.{label}",
        evidence_id=f"evidence:{label}",
        sha256=_sha(label),
        available_at=available_at,
    )


def test_unknown_bet_execution_is_blocked_until_reconciliation() -> None:
    identity = CampaignQualificationIdentity(
        campaign_id="campaign-unknown-bet",
        precommit_manifest_sha256=_sha("precommit"),
        source_sha256=_sha("source"),
        config_sha256=_sha("config"),
        economic_goal_sha256=_sha("goal"),
        risk_policy_sha256=_sha("risk"),
        provider_scope_sha256=_sha("provider"),
        bankroll_currency="EUR",
        campaign_started_at="2026-09-01T00:00:00Z",
    )

    first_checkpoint = _anchor("first-checkpoint", "2026-09-01T00:06:00Z")
    first = EpisodeQualificationEvidence(
        episode_index=1,
        episode_id="episode-1-wait",
        campaign_identity_sha256=identity.identity_sha256,
        predecessor_checkpoint_sha256=None,
        restart_handoff=None,
        observation=_anchor("first-observation", "2026-09-01T00:00:00Z"),
        denominator_member=_anchor("first-denominator", "2026-09-01T00:00:30Z"),
        decision=_anchor("first-decision", "2026-09-01T00:01:00Z"),
        decision_kind=DecisionKind.WAIT,
        paper_action=None,
        execution_disposition=ExecutionDisposition.NOT_APPLICABLE,
        terminal_resolution=_anchor("first-evaluation", "2026-09-01T00:02:00Z"),
        cost_evidence=_anchor("first-cost", "2026-09-01T00:03:00Z"),
        economic_completeness="COMPLETE_NET_ECONOMICS",
        utility_evidence=_anchor("first-utility", "2026-09-01T00:04:00Z"),
        learning_evidence=_anchor("first-learning", "2026-09-01T00:05:00Z"),
        checkpoint=first_checkpoint,
    )

    second = EpisodeQualificationEvidence(
        episode_index=2,
        episode_id="episode-2-unknown-bet",
        campaign_identity_sha256=identity.identity_sha256,
        predecessor_checkpoint_sha256=first_checkpoint.sha256,
        restart_handoff=_anchor("restart", "2026-09-01T23:59:00Z"),
        observation=_anchor("second-observation", "2026-09-02T00:00:00Z"),
        denominator_member=_anchor("second-denominator", "2026-09-02T00:00:30Z"),
        decision=_anchor("second-decision", "2026-09-02T00:01:00Z"),
        decision_kind=DecisionKind.BET,
        paper_action=_anchor("unknown-action", "2026-09-02T00:02:00Z"),
        execution_disposition=ExecutionDisposition.UNKNOWN,
        terminal_resolution=_anchor("unknown-readback", "2026-09-02T00:03:00Z"),
        cost_evidence=_anchor("unknown-cost", "2026-09-02T00:04:00Z"),
        economic_completeness="COMPLETE_NET_ECONOMICS",
        utility_evidence=_anchor("unknown-utility", "2026-09-02T00:05:00Z"),
        learning_evidence=_anchor("unknown-learning", "2026-09-02T00:06:00Z"),
        checkpoint=_anchor("unknown-checkpoint", "2026-09-02T00:07:00Z"),
    )

    stop = _anchor("campaign-stop", "2026-09-02T00:08:00Z")
    terminal_bundle = EvidenceAnchor(
        authority_family="test.terminal-bundle",
        evidence_id="evidence:terminal-bundle",
        sha256=structural_terminal_bundle_sha256(identity, (first, second), stop),
        available_at="2026-09-02T00:09:00Z",
    )

    report = assess_campaign_qualification(
        CampaignQualificationEvidence(
            identity=identity,
            episodes=(first, second),
            stop=stop,
            terminal_bundle=terminal_bundle,
        )
    )

    assert report.state is QualificationState.BLOCKED
    assert "episode 2: BET execution is not accepted/partial/rejected" in report.blockers
    assert report.canonical_authority_verified is False
    assert report.real_money_execution is False
