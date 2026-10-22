from __future__ import annotations

import unittest

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


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
SHA_1 = "1" * 64
SHA_2 = "2" * 64
SHA_3 = "3" * 64
SHA_4 = "4" * 64
SHA_5 = "5" * 64
SHA_6 = "6" * 64
SHA_7 = "7" * 64
SHA_8 = "8" * 64
SHA_9 = "9" * 64


def anchor(
    evidence_id: str,
    sha256: str,
    available_at: str,
    *,
    authority_family: str = "canonical.test",
) -> EvidenceAnchor:
    return EvidenceAnchor(
        authority_family=authority_family,
        evidence_id=evidence_id,
        sha256=sha256,
        available_at=available_at,
    )


def qualification(
    *,
    denominator_family: str,
    denominator_id: str,
    denominator_sha: str,
) -> CampaignQualificationEvidence:
    identity = CampaignQualificationIdentity(
        campaign_id="campaign-evidence-identity",
        precommit_manifest_sha256=SHA_A,
        source_sha256=SHA_B,
        config_sha256=SHA_C,
        economic_goal_sha256=SHA_D,
        risk_policy_sha256=SHA_E,
        provider_scope_sha256=SHA_F,
        bankroll_currency="EUR",
        campaign_started_at="2026-01-01T00:00:00Z",
    )
    episode = EpisodeQualificationEvidence(
        episode_index=1,
        episode_id="episode-1",
        campaign_identity_sha256=identity.identity_sha256,
        predecessor_checkpoint_sha256=None,
        restart_handoff=None,
        observation=anchor(
            "shared-evidence-id",
            SHA_1,
            "2026-01-01T00:01:00Z",
            authority_family="canonical.source",
        ),
        denominator_member=anchor(
            denominator_id,
            denominator_sha,
            "2026-01-01T00:02:00Z",
            authority_family=denominator_family,
        ),
        decision=anchor("decision", SHA_3, "2026-01-01T00:03:00Z"),
        decision_kind=DecisionKind.WAIT,
        paper_action=None,
        execution_disposition=ExecutionDisposition.NOT_APPLICABLE,
        terminal_resolution=anchor(
            "evaluation",
            SHA_4,
            "2026-01-01T00:04:00Z",
        ),
        cost_evidence=anchor("cost", SHA_5, "2026-01-01T00:05:00Z"),
        economic_completeness="COMPLETE_NET_ECONOMICS",
        utility_evidence=anchor("utility", SHA_6, "2026-01-01T00:06:00Z"),
        learning_evidence=anchor("learning", SHA_7, "2026-01-01T00:07:00Z"),
        checkpoint=anchor("checkpoint", SHA_8, "2026-01-02T00:08:00Z"),
    )
    episodes = (episode,)
    stop = anchor("stop", SHA_9, "2026-01-02T00:09:00Z")
    terminal_bundle = anchor(
        "terminal-bundle",
        structural_terminal_bundle_sha256(identity, episodes, stop),
        "2026-01-02T00:10:00Z",
    )
    return CampaignQualificationEvidence(
        identity=identity,
        episodes=episodes,
        stop=stop,
        terminal_bundle=terminal_bundle,
    )


class CampaignEvidenceIdentityTests(unittest.TestCase):
    def test_same_authority_identity_cannot_rebind_to_different_digest(self) -> None:
        evidence = qualification(
            denominator_family="canonical.source",
            denominator_id="shared-evidence-id",
            denominator_sha=SHA_2,
        )

        report = assess_campaign_qualification(evidence)

        self.assertEqual(report.state, QualificationState.BLOCKED)
        self.assertTrue(
            any(
                "evidence identity already used" in blocker
                and "different SHA-256" in blocker
                for blocker in report.blockers
            ),
            report.blockers,
        )

    def test_exact_same_authority_identity_still_counts_as_replay(self) -> None:
        evidence = qualification(
            denominator_family="canonical.source",
            denominator_id="shared-evidence-id",
            denominator_sha=SHA_1,
        )

        report = assess_campaign_qualification(evidence)

        self.assertEqual(report.state, QualificationState.BLOCKED)
        self.assertTrue(
            any("reuses evidence anchor" in blocker for blocker in report.blockers),
            report.blockers,
        )

    def test_same_local_id_in_different_authority_family_is_not_a_collision(self) -> None:
        evidence = qualification(
            denominator_family="canonical.denominator",
            denominator_id="shared-evidence-id",
            denominator_sha=SHA_2,
        )

        report = assess_campaign_qualification(evidence)

        self.assertEqual(report.state, QualificationState.BLOCKED)
        self.assertEqual(
            report.blockers,
            (
                "observed evidence does not span the minimum 24-hour multi-day interval",
            ),
        )
        self.assertFalse(
            any(
                "reuses evidence anchor" in blocker
                or "evidence identity already used" in blocker
                for blocker in report.blockers
            ),
            report.blockers,
        )


if __name__ == "__main__":
    unittest.main()
