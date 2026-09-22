import unittest
from decimal import Decimal

from autosport.domain import TicketLeg
from autosport.risk import ProposedTicketRiskContext, RiskOfRuinEvidence


class _RiskOfRuinEvidenceSubclass(RiskOfRuinEvidence):
    """Adversarial lookalike that must not inherit canonical evidence authority."""


class RiskOfRuinEvidenceCanonicalTypeTests(unittest.TestCase):
    @staticmethod
    def _evidence() -> RiskOfRuinEvidence:
        return _RiskOfRuinEvidenceSubclass(
            evidence_id="subclass-lookalike",
            research_protocol_sha256="a" * 64,
            reproducibility_bundle_sha256="b" * 64,
            producer_identity="adversarial-test",
            causal_cutoff="2026-09-22T18:59:00+00:00",
            evaluated_at="2026-09-22T19:00:00+00:00",
            bankroll_id="paper-bankroll",
            currency="USD",
            base_portfolio_sha256="c" * 64,
            candidate_sha256="d" * 64,
            evaluated_stake=Decimal("1"),
            upper_bound=Decimal("0.01"),
        )

    def test_proposed_context_rejects_risk_evidence_subclass(self) -> None:
        leg = TicketLeg(
            "event-1",
            "market-1",
            "selection-1",
            Decimal("2"),
        )

        with self.assertRaisesRegex(
            ValueError,
            "risk_of_ruin_evidence must be canonical RiskOfRuinEvidence",
        ):
            ProposedTicketRiskContext(
                legs=(leg,),
                bankroll_id="paper-bankroll",
                currency="USD",
                proposal_ts="2026-09-22T19:00:01+00:00",
                risk_of_ruin_evidence=self._evidence(),
            )


if __name__ == "__main__":
    unittest.main()
