from __future__ import annotations

import hashlib
import json
import unittest
from decimal import Decimal, localcontext

from autosport.campaign_evidence import (
    CampaignError,
    CampaignIntegrityError,
    CampaignOutcome,
    SessionEvidence,
    _session_from_payload,
)


class CampaignSessionBankrollConservationTests(unittest.TestCase):
    def _session(self, **overrides: object) -> SessionEvidence:
        values: dict[str, object] = {
            "session_id": "session-bankroll-1",
            "run_id": "run-bankroll-1",
            "evidence_id": "evidence-bankroll-1",
            "source_sha256": "11" * 32,
            "research_protocol_id": "protocol-1",
            "protocol_sha256": "22" * 32,
            "dataset_snapshot_id": "dataset-1",
            "dataset_manifest_sha256": "33" * 32,
            "strategy_version_id": "strategy-1",
            "model_version_id": "model-1",
            "config_sha256": "44" * 32,
            "evaluation_window_start": "2026-09-01T00:00:00Z",
            "evaluation_window_end": "2026-09-01T01:00:00Z",
            "as_of": "2026-09-01T02:00:00Z",
            "available_at": "2026-09-01T02:00:00Z",
            "outcome_reveal_after": "2026-09-01T01:30:00Z",
            "observation_timestamps": (
                "2026-09-01T00:10:00Z",
                "2026-09-01T00:50:00Z",
            ),
            "observation_membership_sha256": "55" * 32,
            "starting_bankroll": Decimal("1000"),
            "ending_bankroll": Decimal("1060"),
            "net_profit": Decimal("60"),
            "turnover": Decimal("600"),
            "bets": 10,
            "wins": 6,
            "losses": 4,
            "voids": 0,
            "brier_sum": None,
            "log_loss_sum": None,
            "prediction_count": 0,
            "max_drawdown": None,
            "peak_exposure": None,
            "risk_of_ruin": None,
            "volatility": None,
            "outcome": CampaignOutcome.POSITIVE,
        }
        values.update(overrides)
        return SessionEvidence.build(**values)

    def test_consistent_session_is_accepted(self) -> None:
        session = self._session()

        self.assertEqual(session.ending_bankroll, Decimal("1060"))
        self.assertEqual(session.net_profit, Decimal("60"))

    def test_positive_profit_cannot_disagree_with_ending_bankroll(self) -> None:
        with self.assertRaisesRegex(
            CampaignError,
            "ending_bankroll must equal starting_bankroll",
        ):
            self._session(
                ending_bankroll=Decimal("1059"),
                net_profit=Decimal("60"),
            )

    def test_loss_cannot_disagree_with_ending_bankroll(self) -> None:
        with self.assertRaisesRegex(
            CampaignError,
            "ending_bankroll must equal starting_bankroll",
        ):
            self._session(
                ending_bankroll=Decimal("991"),
                net_profit=Decimal("-10"),
            )

    def test_missing_net_profit_is_rejected(self) -> None:
        with self.assertRaisesRegex(CampaignError, "net_profit is required"):
            self._session(net_profit=None)

    def test_campaign_money_ingress_requires_exact_decimal(self) -> None:
        invalid_values = (
            ("starting_bankroll", 1000.0),
            ("ending_bankroll", 1060.0),
            ("net_profit", 60.0),
            ("turnover", 600.0),
            ("starting_bankroll", 1000),
            ("starting_bankroll", "1000"),
        )
        for field, value in invalid_values:
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(
                    CampaignError,
                    rf"{field} must be an exact Decimal",
                ):
                    self._session(**{field: value})

    def test_rehashed_inconsistent_durable_payload_fails_closed(self) -> None:
        raw = self._session().to_payload()
        raw["ending_bankroll"] = "1059"
        payload_without_evidence_sha = dict(raw)
        payload_without_evidence_sha.pop("evidence_sha256")
        raw["evidence_sha256"] = hashlib.sha256(
            json.dumps(
                payload_without_evidence_sha,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

        with self.assertRaisesRegex(
            CampaignIntegrityError,
            "invalid session evidence payload",
        ):
            _session_from_payload(raw)

    def test_conservation_is_independent_of_ambient_decimal_precision(self) -> None:
        with localcontext() as context:
            context.prec = 2
            session = self._session(
                starting_bankroll=Decimal(
                    "1.2345678901234567890123456788"
                ),
                net_profit=Decimal(
                    "0.0000000000000000000000000001"
                ),
                ending_bankroll=Decimal(
                    "1.2345678901234567890123456789"
                ),
                turnover=Decimal("1"),
            )

        self.assertEqual(
            session.ending_bankroll,
            Decimal("1.2345678901234567890123456789"),
        )


if __name__ == "__main__":
    unittest.main()
