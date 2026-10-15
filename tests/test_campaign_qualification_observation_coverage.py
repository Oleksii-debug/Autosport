from __future__ import annotations

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


def _anchor(name: str, sha_char: str, at: str) -> EvidenceAnchor:
    return EvidenceAnchor(
        authority_family="canonical.test",
        evidence_id=name,
        sha256=sha_char * 64,
        available_at=at,
    )


def test_late_checkpoint_cannot_fake_24h_observation_coverage() -> None:
    identity = CampaignQualificationIdentity(
        campaign_id="campaign-observation-coverage",
        precommit_manifest_sha256="a" * 64,
        source_sha256="b" * 64,
        config_sha256="c" * 64,
        economic_goal_sha256="d" * 64,
        risk_policy_sha256="e" * 64,
        provider_scope_sha256="f" * 64,
        bankroll_currency="EUR",
        campaign_started_at="2026-09-20T00:00:00Z",
    )
    episode = EpisodeQualificationEvidence(
        episode_index=1,
        episode_id="episode-1",
        campaign_identity_sha256=identity.identity_sha256,
        predecessor_checkpoint_sha256=None,
        restart_handoff=None,
        observation=_anchor("observation", "1", "2026-09-20T01:00:00Z"),
        denominator_member=_anchor("denominator", "2", "2026-09-20T01:00:00Z"),
        decision=_anchor("decision", "3", "2026-09-20T01:01:00Z"),
        decision_kind=DecisionKind.WAIT,
        paper_action=None,
        execution_disposition=ExecutionDisposition.NOT_APPLICABLE,
        terminal_resolution=_anchor("evaluation", "4", "2026-09-20T01:02:00Z"),
        cost_evidence=_anchor("cost", "5", "2026-09-20T01:03:00Z"),
        economic_completeness="COMPLETE_NET_ECONOMICS",
        utility_evidence=_anchor("utility", "6", "2026-09-20T01:04:00Z"),
        learning_evidence=_anchor("learning", "7", "2026-09-20T01:05:00Z"),
        # A checkpoint delayed by more than 24h is not evidence that observation
        # itself continued for 24h.
        checkpoint=_anchor("checkpoint", "8", "2026-09-21T02:00:00Z"),
    )
    episodes = (episode,)
    stop = _anchor("stop", "9", "2026-09-21T02:01:00Z")
    terminal_bundle = EvidenceAnchor(
        authority_family="canonical.test",
        evidence_id="terminal-bundle",
        sha256=structural_terminal_bundle_sha256(identity, episodes, stop),
        available_at="2026-09-21T02:02:00Z",
    )

    report = assess_campaign_qualification(
        CampaignQualificationEvidence(
            identity=identity,
            episodes=episodes,
            stop=stop,
            terminal_bundle=terminal_bundle,
        )
    )

    assert report.state is QualificationState.BLOCKED
    assert any("observed evidence does not span" in item for item in report.blockers)


def test_relabelled_identical_observation_bytes_cannot_fake_multiday_coverage() -> None:
    identity = CampaignQualificationIdentity(
        campaign_id="campaign-relabelled-observation",
        precommit_manifest_sha256="a" * 64,
        source_sha256="b" * 64,
        config_sha256="c" * 64,
        economic_goal_sha256="d" * 64,
        risk_policy_sha256="e" * 64,
        provider_scope_sha256="f" * 64,
        bankroll_currency="EUR",
        campaign_started_at="2026-09-20T00:00:00Z",
    )
    first = EpisodeQualificationEvidence(
        episode_index=1,
        episode_id="episode-1",
        campaign_identity_sha256=identity.identity_sha256,
        predecessor_checkpoint_sha256=None,
        restart_handoff=None,
        observation=_anchor("observation-1", "1", "2026-09-20T00:00:00Z"),
        denominator_member=_anchor("denominator-1", "2", "2026-09-20T00:00:00Z"),
        decision=_anchor("decision-1", "3", "2026-09-20T00:01:00Z"),
        decision_kind=DecisionKind.WAIT,
        paper_action=None,
        execution_disposition=ExecutionDisposition.NOT_APPLICABLE,
        terminal_resolution=_anchor("evaluation-1", "4", "2026-09-20T00:02:00Z"),
        cost_evidence=_anchor("cost-1", "5", "2026-09-20T00:03:00Z"),
        economic_completeness="COMPLETE_NET_ECONOMICS",
        utility_evidence=_anchor("utility-1", "6", "2026-09-20T00:04:00Z"),
        learning_evidence=_anchor("learning-1", "7", "2026-09-20T00:05:00Z"),
        checkpoint=_anchor("checkpoint-1", "8", "2026-09-20T00:06:00Z"),
    )
    second = EpisodeQualificationEvidence(
        episode_index=2,
        episode_id="episode-2",
        campaign_identity_sha256=identity.identity_sha256,
        predecessor_checkpoint_sha256=first.checkpoint.sha256,
        restart_handoff=_anchor("restart-2", "9", "2026-09-21T00:00:00Z"),
        # Different evidence identity/time, but the exact same observation bytes.
        observation=_anchor("observation-2", "1", "2026-09-21T00:00:00Z"),
        denominator_member=_anchor("denominator-2", "a", "2026-09-21T00:00:00Z"),
        decision=_anchor("decision-2", "b", "2026-09-21T00:01:00Z"),
        decision_kind=DecisionKind.WAIT,
        paper_action=None,
        execution_disposition=ExecutionDisposition.NOT_APPLICABLE,
        terminal_resolution=_anchor("evaluation-2", "c", "2026-09-21T00:02:00Z"),
        cost_evidence=_anchor("cost-2", "d", "2026-09-21T00:03:00Z"),
        economic_completeness="COMPLETE_NET_ECONOMICS",
        utility_evidence=_anchor("utility-2", "e", "2026-09-21T00:04:00Z"),
        learning_evidence=_anchor("learning-2", "f", "2026-09-21T00:05:00Z"),
        checkpoint=_anchor("checkpoint-2", "0", "2026-09-21T00:06:00Z"),
    )
    episodes = (first, second)
    stop = _anchor("stop", "2", "2026-09-21T00:07:00Z")
    terminal_bundle = EvidenceAnchor(
        authority_family="canonical.test",
        evidence_id="terminal-bundle",
        sha256=structural_terminal_bundle_sha256(identity, episodes, stop),
        available_at="2026-09-21T00:08:00Z",
    )

    report = assess_campaign_qualification(
        CampaignQualificationEvidence(
            identity=identity,
            episodes=episodes,
            stop=stop,
            terminal_bundle=terminal_bundle,
        )
    )

    assert report.state is QualificationState.BLOCKED
    assert "episode observations must bind distinct evidence bytes" in report.blockers
