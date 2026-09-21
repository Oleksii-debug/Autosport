from __future__ import annotations

import unittest
from decimal import Decimal

from autosport.portfolio_plan import (
    PortfolioDependencyEvidence,
    RobustPortfolioProposal,
)


class CorrelatedExposureStressTests(unittest.TestCase):
    """Adversarial stress vectors for conservative joint-dependency haircuts."""

    CANDIDATES = ("1" * 64, "2" * 64, "3" * 64)
    INTENTS = ("a" * 64, "b" * 64, "c" * 64)

    @classmethod
    def _evidence(
        cls,
        bounds: tuple[Decimal, Decimal, Decimal],
        *,
        uncertainty: Decimal = Decimal("0"),
        fee: Decimal = Decimal("0"),
        partial_fill: Decimal = Decimal("0"),
    ) -> PortfolioDependencyEvidence:
        first, second, third = cls.CANDIDATES
        return PortfolioDependencyEvidence(
            evidence_id="wp-p01-correlated-exposure-stress",
            portfolio_sha256="d" * 64,
            intent_sha256s=cls.INTENTS,
            candidate_sha256s=cls.CANDIDATES,
            population_id="wp-p01-synthetic-stress-v1",
            method="adversarial-upper-bound-matrix",
            sample_size=100,
            causal_cutoff="2026-09-21T08:00:00+00:00",
            as_of="2026-09-21T08:01:00+00:00",
            valid_until="2026-09-21T09:01:00+00:00",
            reproducibility_sha256="e" * 64,
            pairwise_dependency_upper_bounds=(
                (first, second, bounds[0]),
                (first, third, bounds[1]),
                (second, third, bounds[2]),
            ),
            uncertainty_fraction=uncertainty,
            fee_fraction=fee,
            partial_fill_stress_fraction=partial_fill,
        )

    def test_one_high_dependency_edge_dominates_low_dependency_triad(self) -> None:
        evidence = self._evidence(
            (Decimal("0.01"), Decimal("0.90"), Decimal("0.02"))
        )

        proposal = RobustPortfolioProposal.derive(
            (Decimal("100"), Decimal("80"), Decimal("60")),
            evidence,
        )

        self.assertEqual(proposal.dependency_haircut_fraction, Decimal("0.90"))
        self.assertEqual(proposal.robust_scale, Decimal("0.10"))
        self.assertEqual(
            proposal.proposed_stakes,
            (Decimal("10.00"), Decimal("8.00"), Decimal("6.00")),
        )

        averaged_dependency = (
            Decimal("0.01") + Decimal("0.90") + Decimal("0.02")
        ) / Decimal("3")
        self.assertGreater(
            Decimal("1") - averaged_dependency,
            proposal.robust_scale,
            "pairwise dependency must not be diluted by averaging across low-correlation pairs",
        )

    def test_non_worst_pair_changes_cannot_relax_worst_pair_haircut(self) -> None:
        sparse = self._evidence(
            (Decimal("0.01"), Decimal("0.90"), Decimal("0.02"))
        )
        dense = self._evidence(
            (Decimal("0.89"), Decimal("0.90"), Decimal("0.88"))
        )
        base = (Decimal("100"), Decimal("80"), Decimal("60"))

        sparse_proposal = RobustPortfolioProposal.derive(base, sparse)
        dense_proposal = RobustPortfolioProposal.derive(base, dense)

        self.assertEqual(
            sparse_proposal.dependency_haircut_fraction,
            dense_proposal.dependency_haircut_fraction,
        )
        self.assertEqual(sparse_proposal.robust_scale, dense_proposal.robust_scale)
        self.assertEqual(
            sparse_proposal.proposed_stakes,
            dense_proposal.proposed_stakes,
        )

    def test_perfect_dependency_collapses_entire_joint_vector(self) -> None:
        evidence = self._evidence(
            (Decimal("0.10"), Decimal("1"), Decimal("0.20"))
        )

        proposal = RobustPortfolioProposal.derive(
            (Decimal("100"), Decimal("80"), Decimal("60")),
            evidence,
        )

        self.assertEqual(proposal.dependency_haircut_fraction, Decimal("1"))
        self.assertEqual(proposal.robust_scale, Decimal("0"))
        self.assertEqual(
            proposal.proposed_stakes,
            (Decimal("0.00"), Decimal("0.00"), Decimal("0.00")),
        )

    def test_dependency_and_other_stress_factors_compound_without_netting(self) -> None:
        evidence = self._evidence(
            (Decimal("0.10"), Decimal("0.60"), Decimal("0.20")),
            uncertainty=Decimal("0.25"),
            fee=Decimal("0.10"),
            partial_fill=Decimal("0.20"),
        )
        expected_scale = (
            Decimal("0.40")
            * Decimal("0.75")
            * Decimal("0.90")
            * Decimal("0.80")
        )

        proposal = RobustPortfolioProposal.derive(
            (Decimal("100"), Decimal("50"), Decimal("25")),
            evidence,
        )

        self.assertEqual(proposal.dependency_haircut_fraction, Decimal("0.60"))
        self.assertEqual(proposal.robust_scale, expected_scale)
        self.assertEqual(
            proposal.proposed_stakes,
            tuple(
                (stake * expected_scale).quantize(Decimal("0.01"))
                for stake in (Decimal("100"), Decimal("50"), Decimal("25"))
            ),
        )
        self.assertTrue(
            all(
                proposed <= base
                for proposed, base in zip(
                    proposal.proposed_stakes,
                    proposal.base_stakes,
                    strict=True,
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
