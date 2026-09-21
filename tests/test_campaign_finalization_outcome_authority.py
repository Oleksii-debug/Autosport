from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from autosport.campaign_evidence import (
    CampaignError,
    CampaignOutcome,
    CampaignReadiness,
    PaperCampaign,
    SessionEvidence,
)


class CampaignFinalizationOutcomeAuthorityFalsifiers(unittest.TestCase):
    def _negative_session(self) -> SessionEvidence:
        return SessionEvidence.build(
            session_id="negative-session-1",
            run_id="negative-run-1",
            evidence_id="negative-evidence-1",
            source_sha256="11" * 32,
            research_protocol_id="protocol-1",
            protocol_sha256="22" * 32,
            dataset_snapshot_id="dataset-1",
            dataset_manifest_sha256="33" * 32,
            strategy_version_id="strategy-1",
            model_version_id="model-1",
            config_sha256="44" * 32,
            evaluation_window_start="2026-09-01T00:00:00Z",
            evaluation_window_end="2026-09-01T01:00:00Z",
            as_of="2026-09-01T02:00:00Z",
            available_at="2026-09-01T02:00:00Z",
            outcome_reveal_after="2026-09-01T01:30:00Z",
            observation_timestamps=(
                "2026-09-01T00:10:00Z",
                "2026-09-01T00:50:00Z",
            ),
            observation_membership_sha256="55" * 32,
            starting_bankroll=Decimal("1000"),
            ending_bankroll=Decimal("990"),
            net_profit=Decimal("-10"),
            turnover=Decimal("100"),
            bets=2,
            wins=0,
            losses=2,
            voids=0,
            brier_sum=None,
            log_loss_sum=None,
            prediction_count=0,
            max_drawdown=None,
            peak_exposure=None,
            risk_of_ruin=None,
            volatility=None,
            outcome=CampaignOutcome.NEGATIVE,
        )

    def _campaign_with_negative_authoritative_session(self) -> PaperCampaign:
        campaign = PaperCampaign(
            campaign_id="campaign-negative-only",
            campaign_version=1,
            research_protocol_id="protocol-1",
            protocol_sha256="22" * 32,
            hypothesis_id="hypothesis-1",
            primary_metric="net_profit",
            protective_metrics=("max_drawdown", "risk_of_ruin"),
            evaluation_window_start="2026-09-01T00:00:00Z",
            evaluation_window_end="2026-09-02T00:00:00Z",
            evaluation_as_of="2026-09-03T00:00:00Z",
            readiness_rule="frozen campaign outcome rule must be product-evaluated",
            source_sha256="11" * 32,
            created_at="2026-08-31T00:00:00Z",
            strategy_version_id="strategy-1",
            model_version_id="model-1",
        )
        campaign.sessions.append(self._negative_session())

        # This falsifier isolates the finalization authority boundary. Upstream
        # ScientificRegistry/RunRegistry/session checks are assumed to have
        # succeeded; the finalizer must still not let its caller mint a
        # contradictory campaign-level POSITIVE classification.
        object.__setattr__(campaign, "_scientific_registry", object())
        object.__setattr__(campaign, "_run_registry", object())
        return campaign

    def test_caller_cannot_mint_positive_campaign_outcome_after_negative_evidence(
        self,
    ) -> None:
        campaign = self._campaign_with_negative_authoritative_session()

        with (
            patch(
                "autosport.campaign_evidence._validate_registry_bindings",
                return_value=None,
            ),
            patch(
                "autosport.campaign_evidence._validate_authoritative_session",
                return_value=None,
            ),
        ):
            with self.assertRaises(CampaignError):
                campaign.finalize(
                    outcome=CampaignOutcome.POSITIVE,
                    readiness=CampaignReadiness.ELIGIBLE,
                    finalized_at="2026-09-03T00:00:00Z",
                )

        self.assertFalse(campaign.finalized)
        self.assertIsNone(campaign.campaign_sha256)


if __name__ == "__main__":
    unittest.main()
