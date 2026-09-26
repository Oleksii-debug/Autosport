from __future__ import annotations

from dataclasses import replace
import unittest

from autosport.campaign_qualification import (
    CampaignQualificationError,
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
    name: str,
    sha: str,
    at: str,
    family: str = "canonical.test",
) -> EvidenceAnchor:
    return EvidenceAnchor(
        authority_family=family,
        evidence_id=name,
        sha256=sha,
        available_at=at,
    )


class CampaignQualificationTests(unittest.TestCase):
    def identity(self) -> CampaignQualificationIdentity:
        return CampaignQualificationIdentity(
            campaign_id="campaign-1",
            precommit_manifest_sha256=SHA_A,
            source_sha256=SHA_B,
            config_sha256=SHA_C,
            economic_goal_sha256=SHA_D,
            risk_policy_sha256=SHA_E,
            provider_scope_sha256=SHA_F,
            bankroll_currency="EUR",
            campaign_started_at="2026-09-20T00:00:00Z",
        )

    def episodes(
        self,
        identity: CampaignQualificationIdentity,
    ) -> tuple[EpisodeQualificationEvidence, ...]:
        first = EpisodeQualificationEvidence(
            episode_index=1,
            episode_id="episode-1",
            campaign_identity_sha256=identity.identity_sha256,
            predecessor_checkpoint_sha256=None,
            restart_handoff=None,
            observation=anchor("obs-1", SHA_1, "2026-09-20T01:00:00Z"),
            denominator_member=anchor("den-1", SHA_2, "2026-09-20T01:00:00Z"),
            decision=anchor("decision-1", SHA_3, "2026-09-20T01:01:00Z"),
            decision_kind=DecisionKind.BET,
            paper_action=anchor("action-1", SHA_4, "2026-09-20T01:02:00Z"),
            execution_disposition=ExecutionDisposition.ACCEPTED,
            terminal_resolution=anchor(
                "settlement-1",
                SHA_5,
                "2026-09-20T06:00:00Z",
            ),
            cost_evidence=anchor("cost-1", SHA_6, "2026-09-20T06:01:00Z"),
            economic_completeness="COMPLETE_NET_ECONOMICS",
            utility_evidence=anchor("utility-1", SHA_7, "2026-09-20T06:02:00Z"),
            learning_evidence=anchor(
                "learning-1",
                SHA_8,
                "2026-09-20T06:03:00Z",
            ),
            checkpoint=anchor(
                "checkpoint-1",
                SHA_9,
                "2026-09-20T06:04:00Z",
            ),
        )
        second = EpisodeQualificationEvidence(
            episode_index=2,
            episode_id="episode-2",
            campaign_identity_sha256=identity.identity_sha256,
            predecessor_checkpoint_sha256=first.checkpoint.sha256,
            restart_handoff=anchor(
                "handoff-2",
                SHA_A,
                "2026-09-20T06:05:00Z",
            ),
            observation=anchor("obs-2", SHA_B, "2026-09-21T02:00:00Z"),
            denominator_member=anchor("den-2", SHA_C, "2026-09-21T02:00:00Z"),
            decision=anchor("decision-2", SHA_D, "2026-09-21T02:01:00Z"),
            decision_kind=DecisionKind.WAIT,
            paper_action=None,
            execution_disposition=ExecutionDisposition.NOT_APPLICABLE,
            terminal_resolution=anchor(
                "evaluation-2",
                SHA_E,
                "2026-09-21T02:02:00Z",
            ),
            cost_evidence=anchor("cost-2", SHA_F, "2026-09-21T02:03:00Z"),
            economic_completeness="COMPLETE_NET_ECONOMICS",
            utility_evidence=anchor("utility-2", SHA_1, "2026-09-21T02:04:00Z"),
            learning_evidence=anchor(
                "learning-2",
                SHA_2,
                "2026-09-21T02:05:00Z",
            ),
            checkpoint=anchor(
                "checkpoint-2",
                SHA_3,
                "2026-09-21T02:06:00Z",
            ),
        )
        return first, second

    def complete(self) -> CampaignQualificationEvidence:
        identity = self.identity()
        episodes = self.episodes(identity)
        stop = anchor("stop", SHA_4, "2026-09-21T02:07:00Z")
        digest = structural_terminal_bundle_sha256(identity, episodes, stop)
        bundle = anchor(
            "terminal-bundle",
            digest,
            "2026-09-21T02:08:00Z",
        )
        return CampaignQualificationEvidence(identity, episodes, stop, bundle)

    def assert_blocked(
        self,
        evidence: CampaignQualificationEvidence,
        text: str,
    ) -> None:
        report = assess_campaign_qualification(evidence)
        self.assertEqual(report.state, QualificationState.BLOCKED)
        self.assertTrue(any(text in item for item in report.blockers), report.blockers)

    def rebind_bundle(
        self,
        evidence: CampaignQualificationEvidence,
    ) -> CampaignQualificationEvidence:
        digest = structural_terminal_bundle_sha256(
            evidence.identity,
            evidence.episodes,
            evidence.stop,
        )
        return replace(
            evidence,
            terminal_bundle=replace(evidence.terminal_bundle, sha256=digest),
        )

    def test_complete_multiday_chain_is_only_ready_for_canonical_resolution(self):
        report = assess_campaign_qualification(self.complete())
        self.assertEqual(
            report.state,
            QualificationState.COMPLETE_FOR_CANONICAL_RESOLUTION,
        )
        self.assertEqual(report.blockers, ())
        self.assertFalse(report.canonical_authority_verified)
        self.assertFalse(report.promotion_authority)
        self.assertFalse(report.readiness_authority)
        self.assertFalse(report.real_money_execution)
        self.assertFalse(report.human_tested)
        self.assertFalse(report.nvda_verified)
        self.assertFalse(report.whole_product_complete)

    def test_identity_drift_blocks(self):
        evidence = self.complete()
        broken = replace(
            evidence.episodes[1],
            campaign_identity_sha256=SHA_F,
        )
        changed = replace(evidence, episodes=(evidence.episodes[0], broken))
        self.assert_blocked(self.rebind_bundle(changed), "campaign identity drift")

    def test_missing_restart_handoff_blocks_second_episode(self):
        evidence = self.complete()
        broken = replace(evidence.episodes[1], restart_handoff=None)
        changed = replace(evidence, episodes=(evidence.episodes[0], broken))
        self.assert_blocked(
            self.rebind_bundle(changed),
            "restart handoff evidence is required",
        )

    def test_wrong_predecessor_checkpoint_blocks(self):
        evidence = self.complete()
        broken = replace(
            evidence.episodes[1],
            predecessor_checkpoint_sha256=SHA_F,
        )
        changed = replace(evidence, episodes=(evidence.episodes[0], broken))
        self.assert_blocked(
            self.rebind_bundle(changed),
            "predecessor checkpoint",
        )

    def test_decision_cannot_precede_observation(self):
        evidence = self.complete()
        broken = replace(
            evidence.episodes[0],
            decision=replace(
                evidence.episodes[0].decision,
                available_at="2026-09-20T00:59:59Z",
            ),
        )
        changed = replace(evidence, episodes=(broken, evidence.episodes[1]))
        self.assert_blocked(
            self.rebind_bundle(changed),
            "decision predates observation",
        )

    def test_wait_is_first_class_and_cannot_carry_action(self):
        evidence = self.complete()
        broken = replace(
            evidence.episodes[1],
            paper_action=anchor(
                "illegal-action",
                SHA_7,
                "2026-09-21T02:01:30Z",
            ),
        )
        changed = replace(evidence, episodes=(evidence.episodes[0], broken))
        self.assert_blocked(
            self.rebind_bundle(changed),
            "WAIT/NO_BET must not carry PAPER action",
        )

    def test_wait_requires_evaluation(self):
        evidence = self.complete()
        broken = replace(evidence.episodes[1], terminal_resolution=None)
        changed = replace(evidence, episodes=(evidence.episodes[0], broken))
        self.assert_blocked(
            self.rebind_bundle(changed),
            "requires authoritative evaluation",
        )

    def test_bet_unknown_execution_blocks_even_with_hashes(self):
        evidence = self.complete()
        broken = replace(
            evidence.episodes[0],
            execution_disposition=ExecutionDisposition.UNKNOWN,
        )
        changed = replace(evidence, episodes=(broken, evidence.episodes[1]))
        self.assert_blocked(
            self.rebind_bundle(changed),
            "execution is not accepted/partial",
        )

    def test_bet_requires_settlement(self):
        evidence = self.complete()
        broken = replace(evidence.episodes[0], terminal_resolution=None)
        changed = replace(evidence, episodes=(broken, evidence.episodes[1]))
        self.assert_blocked(
            self.rebind_bundle(changed),
            "requires authoritative settlement",
        )

    def test_incomplete_after_cost_economics_blocks_learning_qualification(self):
        evidence = self.complete()
        broken = replace(
            evidence.episodes[0],
            economic_completeness="INCOMPLETE_NET_ECONOMICS",
        )
        changed = replace(evidence, episodes=(broken, evidence.episodes[1]))
        self.assert_blocked(
            self.rebind_bundle(changed),
            "net economics are not COMPLETE",
        )

    def test_learning_cannot_precede_utility(self):
        evidence = self.complete()
        learning = evidence.episodes[0].learning_evidence
        assert learning is not None
        broken = replace(
            evidence.episodes[0],
            learning_evidence=replace(
                learning,
                available_at="2026-09-20T06:01:30Z",
            ),
        )
        changed = replace(evidence, episodes=(broken, evidence.episodes[1]))
        self.assert_blocked(
            self.rebind_bundle(changed),
            "learning predates authoritative utility",
        )

    def test_stop_must_follow_final_checkpoint(self):
        evidence = self.complete()
        changed = replace(
            evidence,
            stop=replace(
                evidence.stop,
                available_at="2026-09-21T02:05:30Z",
            ),
        )
        self.assert_blocked(self.rebind_bundle(changed), "STOP predates")

    def test_campaign_start_must_not_follow_first_observation(self):
        evidence = self.complete()
        identity = replace(
            evidence.identity,
            campaign_started_at="2026-09-20T03:00:00Z",
        )
        episodes = tuple(
            replace(item, campaign_identity_sha256=identity.identity_sha256)
            for item in evidence.episodes
        )
        changed = CampaignQualificationEvidence(
            identity=identity,
            episodes=episodes,
            stop=evidence.stop,
            terminal_bundle=evidence.terminal_bundle,
        )
        self.assert_blocked(
            self.rebind_bundle(changed),
            "observation predates campaign start",
        )

    def test_terminal_bundle_must_bind_exact_episode_chain(self):
        evidence = self.complete()
        changed = replace(
            evidence,
            terminal_bundle=replace(
                evidence.terminal_bundle,
                sha256=SHA_F,
            ),
        )
        self.assert_blocked(
            changed,
            "terminal evidence bundle digest",
        )

    def test_episode_indexes_are_contiguous(self):
        evidence = self.complete()
        broken = replace(evidence.episodes[1], episode_index=3)
        changed = replace(evidence, episodes=(evidence.episodes[0], broken))
        self.assert_blocked(self.rebind_bundle(changed), "episode indexes")

    def test_duplicate_episode_identity_blocks(self):
        evidence = self.complete()
        broken = replace(
            evidence.episodes[1],
            episode_id=evidence.episodes[0].episode_id,
        )
        changed = replace(evidence, episodes=(evidence.episodes[0], broken))
        self.assert_blocked(
            self.rebind_bundle(changed),
            "episode identities must be unique",
        )

    def test_reused_anchor_across_episodes_blocks_replay(self):
        evidence = self.complete()
        broken = replace(
            evidence.episodes[1],
            denominator_member=evidence.episodes[0].observation,
        )
        changed = replace(evidence, episodes=(evidence.episodes[0], broken))
        self.assert_blocked(
            self.rebind_bundle(changed),
            "reuses evidence anchor",
        )

    def test_uppercase_hash_is_rejected_as_noncanonical(self):
        with self.assertRaises(CampaignQualificationError):
            anchor(
                "uppercase",
                "A" * 64,
                "2026-09-20T00:00:00Z",
            )

    def test_malformed_hash_is_rejected_before_assessment(self):
        with self.assertRaises(CampaignQualificationError):
            anchor(
                "bad",
                "not-a-sha",
                "2026-09-20T00:00:00Z",
            )

    def test_report_digest_is_deterministic(self):
        first = assess_campaign_qualification(self.complete())
        second = assess_campaign_qualification(self.complete())
        self.assertEqual(first.qualification_sha256, second.qualification_sha256)
        self.assertEqual(
            first.expected_terminal_bundle_sha256,
            second.expected_terminal_bundle_sha256,
        )


if __name__ == "__main__":
    unittest.main()
