from __future__ import annotations

import unittest

from autosport.champion_challenger_gate import (
    PairedLoss,
    PromotionEvidenceError,
    PromotionPolicy,
    evaluate_promotion,
)


class ChampionChallengerEvidenceAuthorityFalsifierTests(unittest.TestCase):
    def test_caller_created_losses_cannot_mint_positive_promotion_authority(self) -> None:
        # Ordinary callers can fabricate a statistically perfect cohort with no
        # canonical EvaluationBundle / ScientificRegistry / holdout evidence at all.
        pairs = [
            PairedLoss(
                evaluation_id=f"caller-fabricated-{index:02d}",
                champion_loss=1.0,
                challenger_loss=0.0,
            )
            for index in range(20)
        ]
        policy = PromotionPolicy(
            min_pairs=20,
            min_effective_pairs=20,
            min_mean_improvement=0.5,
            min_win_rate=0.9,
            alpha=0.01,
            tie_tolerance=0.0,
        )

        try:
            decision = evaluate_promotion(pairs, policy)
        except PromotionEvidenceError:
            # A canonical owner may choose to fail closed immediately until exact
            # product-owned evaluation evidence is supplied/resolved.
            return

        # If descriptive statistics remain available for assertion-only PairedLoss
        # rows, the authority gap must stay explicit and positive promotion must
        # remain unavailable. This keeps the test disjoint from the separate
        # multiplicity child: a multiplicity-only reason does not satisfy it.
        self.assertIn(
            "evaluation_evidence_authority_unresolved",
            decision.reasons,
        )
        try:
            promoted = decision.promote
        except PromotionEvidenceError:
            return
        self.assertFalse(promoted)


if __name__ == "__main__":
    unittest.main()
