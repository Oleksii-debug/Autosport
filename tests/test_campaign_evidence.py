from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal, getcontext
from pathlib import Path

from autosport.campaign_evidence import (
    CampaignFinalizedError,
    CampaignIntegrityError,
    CampaignOutcome,
    CampaignReadiness,
    PaperCampaign,
    SessionEvidence,
)


class PaperCampaignTests(unittest.TestCase):
    BASE = {
        "campaign_id": "campaign-1",
        "campaign_version": 1,
        "research_protocol_id": "protocol-1",
        "protocol_sha256": "11" * 32,
        "hypothesis_id": "hypothesis-1",
        "primary_metric": "net_profit",
        "protective_metrics": ("max_drawdown", "risk_of_ruin"),
        "evaluation_window_start": "2026-09-01T00:00:00Z",
        "evaluation_window_end": "2026-09-10T23:59:59Z",
        "evaluation_as_of": "2026-09-11T00:00:00Z",
        "readiness_rule": "predeclared campaign gate v1",
        "source_sha256": "22" * 32,
        "created_at": "2026-09-11T01:00:00Z",
        "strategy_version_id": "strategy-1",
        "model_version_id": "model-1",
    }

    @staticmethod
    def session(**overrides) -> SessionEvidence:
        values = {
            "session_id": "session-1",
            "run_id": "run-1",
            "evidence_id": "evidence-1",
            "source_sha256": "22" * 32,
            "research_protocol_id": "protocol-1",
            "protocol_sha256": "11" * 32,
            "dataset_snapshot_id": "dataset-1",
            "dataset_manifest_sha256": "33" * 32,
            "strategy_version_id": "strategy-1",
            "model_version_id": "model-1",
            "config_sha256": "44" * 32,
            "evaluation_window_start": "2026-09-01T00:00:00Z",
            "evaluation_window_end": "2026-09-03T23:59:59Z",
            "as_of": "2026-09-04T00:00:00Z",
            "available_at": "2026-09-04T00:00:00Z",
            "outcome_reveal_after": "2026-09-04T00:00:00Z",
            "observation_timestamps": (
                "2026-09-01T10:00:00Z",
                "2026-09-02T10:00:00Z",
                "2026-09-03T10:00:00Z",
            ),
            "starting_bankroll": Decimal("1000"),
            "ending_bankroll": Decimal("1060"),
            "net_profit": Decimal("60"),
            "turnover": Decimal("600"),
            "bets": 10,
            "wins": 6,
            "losses": 4,
            "voids": 0,
            "brier_sum": Decimal("0.21"),
            "log_loss_sum": Decimal("1.6"),
            "prediction_count": 10,
            "max_drawdown": Decimal("30"),
            "peak_exposure": Decimal("150"),
            "risk_of_ruin": Decimal("0.01"),
            "volatility": Decimal("0.2"),
            "outcome": CampaignOutcome.POSITIVE,
        }
        values.update(overrides)
        return SessionEvidence.build(**values)

    def campaign(self) -> PaperCampaign:
        return PaperCampaign(**self.BASE)

    def test_session_evidence_hash_binds_window_and_metrics(self):
        session = self.session()
        self.assertEqual(session.evidence_sha256, session.computed_evidence_sha256)
        with self.assertRaises(CampaignFinalizedError):
            campaign = self.campaign()
            campaign.add_session(session)
            campaign.finalize(
                outcome=CampaignOutcome.POSITIVE,
                readiness=CampaignReadiness.ELIGIBLE,
                finalized_at="2026-09-11T00:00:00Z",
            )
            campaign.add_session(self.session(session_id="session-2"))

    def test_available_after_as_of_is_rejected(self):
        with self.assertRaises(ValueError):
            self.session(
                as_of="2026-09-12T00:00:00Z",
                available_at="2026-09-13T00:00:00Z",
            )

    def test_campaign_rejects_evidence_after_evaluation_as_of(self):
        campaign = self.campaign()
        session = self.session(
            as_of="2026-09-12T00:00:00Z",
            available_at="2026-09-12T00:00:00Z",
        )
        with self.assertRaises(ValueError):
            campaign.add_session(session)

    def test_sample_membership_is_mechanically_checked(self):
        with self.assertRaises(ValueError):
            self.session(observation_timestamps=("2026-08-31T23:59:59Z",) * 1)

    def test_mismatched_strategy_is_rejected_by_campaign(self):
        with self.assertRaises(ValueError):
            self.campaign().add_session(self.session(strategy_version_id="strategy-2"))

    def test_duplicate_session_membership_is_rejected(self):
        campaign = self.campaign()
        campaign.add_session(self.session())
        with self.assertRaises(ValueError):
            campaign.add_session(self.session(session_id="session-1", run_id="run-2", evidence_id="evidence-2"))

    def test_negative_and_null_outcomes_are_preserved(self):
        campaign = self.campaign()
        campaign.add_session(self.session(outcome=CampaignOutcome.NEGATIVE))
        campaign.add_session(
            self.session(
                session_id="session-2",
                run_id="run-2",
                evidence_id="evidence-2",
                outcome=CampaignOutcome.NULL,
            )
        )
        summary = campaign.finalize(
            outcome=CampaignOutcome.INCONCLUSIVE,
            readiness=CampaignReadiness.INCONCLUSIVE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        self.assertEqual(summary.outcome, CampaignOutcome.INCONCLUSIVE)
        self.assertEqual(campaign.sessions[0].outcome, CampaignOutcome.NEGATIVE)
        self.assertEqual(campaign.sessions[1].outcome, CampaignOutcome.NULL)

    def test_aggregate_metrics_are_deterministic_across_decimal_contexts(self):
        campaign_a = self.campaign()
        campaign_a.add_session(self.session())
        campaign_a.add_session(
            self.session(
                session_id="session-2",
                run_id="run-2",
                evidence_id="evidence-2",
                starting_bankroll=Decimal("2000"),
                ending_bankroll=Decimal("2110"),
                net_profit=Decimal("110"),
                turnover=Decimal("1000"),
                bets=20,
                wins=11,
                losses=9,
                brier_sum=Decimal("0.43"),
                log_loss_sum=Decimal("3.2"),
                prediction_count=20,
            )
        )
        original_context = getcontext().copy()
        getcontext().prec = 6
        first = campaign_a.finalize(
            outcome=CampaignOutcome.POSITIVE,
            readiness=CampaignReadiness.ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        getcontext().clear_flags()
        getcontext().prec = original_context.prec
        getcontext().rounding = original_context.rounding
        getcontext().Emin = original_context.Emin
        getcontext().Emax = original_context.Emax
        getcontext().capitals = original_context.capitals
        getcontext().clamp = original_context.clamp
        getcontext().traps = original_context.traps.copy()
        campaign_b = self.campaign()
        campaign_b.add_session(self.session())
        campaign_b.add_session(
            self.session(
                session_id="session-2",
                run_id="run-2",
                evidence_id="evidence-2",
                starting_bankroll=Decimal("2000"),
                ending_bankroll=Decimal("2110"),
                net_profit=Decimal("110"),
                turnover=Decimal("1000"),
                bets=20,
                wins=11,
                losses=9,
                brier_sum=Decimal("0.43"),
                log_loss_sum=Decimal("3.2"),
                prediction_count=20,
            )
        )
        second = campaign_b.finalize(
            outcome=CampaignOutcome.POSITIVE,
            readiness=CampaignReadiness.ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        self.assertEqual(campaign_a.campaign_sha256, campaign_b.campaign_sha256)
        self.assertEqual(first.campaign_sha256, second.campaign_sha256)

    def test_finalize_freezes_membership_and_fork_creates_new_version(self):
        campaign = self.campaign()
        campaign.add_session(self.session())
        campaign.finalize(
            outcome=CampaignOutcome.HARMFUL,
            readiness=CampaignReadiness.NOT_ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        with self.assertRaises(CampaignFinalizedError):
            campaign.remove_session("session-1")
        fork = campaign.fork_new_version(2)
        self.assertFalse(fork.finalized)
        self.assertEqual(fork.sessions, [])

    def test_restart_roundtrip_preserves_final_identity_and_summary(self):
        campaign = self.campaign()
        campaign.add_session(self.session())
        campaign.finalize(
            outcome=CampaignOutcome.POSITIVE,
            readiness=CampaignReadiness.ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.json"
            campaign.save(path)
            loaded = PaperCampaign.load(path)
        self.assertEqual(loaded.campaign_sha256, campaign.campaign_sha256)
        self.assertEqual(loaded.export_summary(), campaign.export_summary())

    def test_tampered_state_is_rejected(self):
        campaign = self.campaign()
        campaign.add_session(self.session())
        campaign.finalize(
            outcome=CampaignOutcome.POSITIVE,
            readiness=CampaignReadiness.ELIGIBLE,
            finalized_at="2026-09-11T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "campaign.json"
            campaign.save(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["sessions"][0]["net_profit"] = "61"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(CampaignIntegrityError):
                PaperCampaign.load(path)

    def test_export_summary_is_explicit_about_unsupported_metrics(self):
        campaign = self.campaign()
        campaign.add_session(
            self.session(
                brier_sum=None,
                log_loss_sum=None,
                prediction_count=0,
                max_drawdown=None,
                peak_exposure=None,
                risk_of_ruin=None,
                volatility=None,
            )
        )
        summary = campaign.export_summary()
        self.assertIsNone(summary["metrics"]["brier_score"])
        self.assertIsNone(summary["metrics"]["risk_of_ruin"])
        self.assertIn("brier_score=TBD", campaign.evidence_summary())


if __name__ == "__main__":
    unittest.main()
