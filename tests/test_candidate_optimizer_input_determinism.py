import unittest
from decimal import Context, Decimal, localcontext

from autosport.candidate_optimizer import (
    PortfolioAwareCandidateOptimizer,
    _decimal_identity_key,
)
from autosport.candidate_search import BeamParlayCandidateSearch, CandidateLeg, ParlayCandidate
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome, ScenarioSearchEngine


def _candidate(event_id: str) -> ParlayCandidate:
    leg = CandidateLeg(
        f"{event_id}|winner|a",
        event_id,
        Decimal("2"),
        Decimal("0.5"),
    )
    return ParlayCandidate(
        (leg,),
        leg.decimal_odds,
        leg.probability,
        Decimal("0"),
    )


def _two_leg_permutations() -> tuple[ParlayCandidate, ParlayCandidate]:
    legs = (
        CandidateLeg(
            "e1|winner|a",
            "e1",
            Decimal("2"),
            Decimal("0.5"),
        ),
        CandidateLeg(
            "e2|winner|a",
            "e2",
            Decimal("2"),
            Decimal("0.5"),
        ),
    )
    canonical = BeamParlayCandidateSearch._to_candidate(legs)
    permuted = ParlayCandidate(
        tuple(reversed(legs)),
        canonical.combined_odds,
        canonical.independent_probability,
        canonical.expected_profit_per_unit,
    )
    return canonical, permuted


def _scale_equivalent_two_leg_candidates() -> tuple[ParlayCandidate, ParlayCandidate]:
    compact_legs = (
        CandidateLeg(
            "e1|winner|a",
            "e1",
            Decimal("2.0"),
            Decimal("0.5"),
        ),
        CandidateLeg(
            "e2|winner|a",
            "e2",
            Decimal("2.0"),
            Decimal("0.5"),
        ),
    )
    scaled_legs = (
        CandidateLeg(
            "e1|winner|a",
            "e1",
            Decimal("2.00"),
            Decimal("0.50"),
        ),
        CandidateLeg(
            "e2|winner|a",
            "e2",
            Decimal("2.00"),
            Decimal("0.50"),
        ),
    )
    return (
        BeamParlayCandidateSearch._to_candidate(compact_legs),
        BeamParlayCandidateSearch._to_candidate(scaled_legs),
    )


def _groups() -> list[ScenarioGroup]:
    return [
        ScenarioGroup(
            event_id,
            (
                ScenarioOutcome(f"{event_id}|winner|a", Decimal("0.5")),
                ScenarioOutcome(f"{event_id}|winner|b", Decimal("0.5")),
            ),
        )
        for event_id in ("e1", "e2")
    ]


class _RecordingScenarioEngine(ScenarioSearchEngine):
    def __init__(self) -> None:
        super().__init__()
        self.ticket_id_snapshots: list[tuple[str, ...]] = []

    def analyse(self, tickets, groups):
        self.ticket_id_snapshots.append(tuple(ticket.ticket_id for ticket in tickets))
        return super().analyse(tickets, groups)


class CandidateOptimizerInputDeterminismTests(unittest.TestCase):
    def test_result_limit_requires_positive_non_boolean_integer(self) -> None:
        for invalid in (True, False, 1.5, Decimal("2"), "2", None):
            with self.subTest(value=repr(invalid)):
                with self.assertRaisesRegex(
                    ValueError,
                    "result_limit must be a positive non-boolean integer",
                ):
                    PortfolioAwareCandidateOptimizer(result_limit=invalid)  # type: ignore[arg-type]

        for invalid in (0, -1):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(
                    ValueError,
                    "result_limit must be a positive non-boolean integer",
                ):
                    PortfolioAwareCandidateOptimizer(result_limit=invalid)

    def test_generated_candidate_validation_ignores_caller_decimal_context(self) -> None:
        legs = [
            CandidateLeg(
                "e1|winner|a",
                "e1",
                Decimal("1.23456789"),
                Decimal("0.87654321"),
            ),
            CandidateLeg(
                "e2|winner|a",
                "e2",
                Decimal("1.98765432"),
                Decimal("0.76543219"),
            ),
        ]
        optimizer = PortfolioAwareCandidateOptimizer(
            generator=BeamParlayCandidateSearch(
                beam_width=10,
                max_legs=2,
                result_limit=10,
            )
        )

        with localcontext(Context(prec=5)):
            impacts = optimizer.optimize(
                [],
                legs,
                _groups(),
                stake="1",
                minimum_legs=2,
            )

        self.assertEqual(len(impacts), 1)
        self.assertEqual(
            impacts[0].candidate.combined_odds,
            Decimal("2.4538941998917848"),
        )
        self.assertEqual(
            impacts[0].candidate.independent_probability,
            Decimal("0.6709343888599299"),
        )
        self.assertEqual(
            impacts[0].candidate.expected_profit_per_unit,
            Decimal("0.646402005331321294939224914"),
        )

    def test_equal_rank_candidates_have_input_order_independent_limit_selection(self) -> None:
        first = _candidate("e1")
        second = _candidate("e2")
        optimizer = PortfolioAwareCandidateOptimizer(result_limit=1)

        forward = optimizer.evaluate_candidates(
            [],
            [first, second],
            _groups(),
            stake="1",
        )
        reversed_input = optimizer.evaluate_candidates(
            [],
            [second, first],
            _groups(),
            stake="1",
        )

        self.assertEqual(len(forward), 1)
        self.assertEqual(len(reversed_input), 1)
        self.assertEqual(
            forward[0].candidate.legs[0].quote_key,
            reversed_input[0].candidate.legs[0].quote_key,
        )

    def test_leg_permutations_share_canonical_identity_before_limit(self) -> None:
        canonical, permuted = _two_leg_permutations()
        forward_engine = _RecordingScenarioEngine()
        reversed_engine = _RecordingScenarioEngine()

        forward = PortfolioAwareCandidateOptimizer(
            scenario_engine=forward_engine,
            result_limit=2,
        ).evaluate_candidates(
            [],
            [canonical, permuted],
            _groups(),
            stake="1",
        )
        reversed_input = PortfolioAwareCandidateOptimizer(
            scenario_engine=reversed_engine,
            result_limit=2,
        ).evaluate_candidates(
            [],
            [permuted, canonical],
            _groups(),
            stake="1",
        )

        self.assertEqual(len(forward), 1)
        self.assertEqual(len(reversed_input), 1)
        self.assertEqual(len(forward_engine.ticket_id_snapshots), 2)
        self.assertEqual(len(reversed_engine.ticket_id_snapshots), 2)
        self.assertEqual(
            tuple(leg.quote_key for leg in forward[0].candidate.legs),
            tuple(leg.quote_key for leg in reversed_input[0].candidate.legs),
        )
        self.assertEqual(
            forward_engine.ticket_id_snapshots[-1],
            reversed_engine.ticket_id_snapshots[-1],
        )

    def test_decimal_identity_is_scale_equivalent_without_exponent_expansion(self) -> None:
        self.assertEqual(
            _decimal_identity_key(Decimal("2.0")),
            _decimal_identity_key(Decimal("2.00")),
        )
        self.assertEqual(
            _decimal_identity_key(Decimal("-0.00")),
            _decimal_identity_key(Decimal("0")),
        )

        huge = Decimal("1E+1000000")
        self.assertEqual(_decimal_identity_key(huge), "0:1:1000000")
        leg = CandidateLeg(
            "e1|winner|a",
            "e1",
            huge,
            Decimal("0.5"),
        )
        candidate = ParlayCandidate(
            (leg,),
            huge,
            Decimal("0.5"),
            Decimal("0"),
        )
        with self.assertRaisesRegex(
            ValueError,
            "candidate combined economics exceed Decimal range",
        ):
            PortfolioAwareCandidateOptimizer().evaluate_candidates(
                [],
                [candidate],
                _groups(),
                stake="1",
            )

    def test_scale_equivalent_decimals_share_candidate_and_ticket_identity(self) -> None:
        compact, scaled = _scale_equivalent_two_leg_candidates()
        compact_first_engine = _RecordingScenarioEngine()
        scaled_first_engine = _RecordingScenarioEngine()

        compact_first = PortfolioAwareCandidateOptimizer(
            scenario_engine=compact_first_engine,
            result_limit=2,
        ).evaluate_candidates(
            [],
            [compact, scaled],
            _groups(),
            stake=Decimal("1.0"),
        )
        scaled_first = PortfolioAwareCandidateOptimizer(
            scenario_engine=scaled_first_engine,
            result_limit=2,
        ).evaluate_candidates(
            [],
            [scaled, compact],
            _groups(),
            stake=Decimal("1.00"),
        )

        self.assertEqual(len(compact_first), 1)
        self.assertEqual(len(scaled_first), 1)
        self.assertEqual(len(compact_first_engine.ticket_id_snapshots), 2)
        self.assertEqual(len(scaled_first_engine.ticket_id_snapshots), 2)
        self.assertEqual(
            compact_first_engine.ticket_id_snapshots[-1],
            scaled_first_engine.ticket_id_snapshots[-1],
        )


if __name__ == "__main__":
    unittest.main()
