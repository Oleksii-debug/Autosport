"""Plan 2 post-terminal #2184: fail-closed PAPER Decimal resource regressions."""
from __future__ import annotations

import unittest
from decimal import Decimal, localcontext
from unittest.mock import patch

from autosport import _paper_execution_reality_legacy as legacy
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperLegAttempt,
)


def evidence(*, odds: object = "2.50", stake: object = "10.00") -> PaperExecutionEvidenceRecord:
    return PaperExecutionEvidenceRecord(
        action_id="resource-action",
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        quote_id="quote-1",
        outcome=PaperAttemptOutcome.ACCEPTED,
        observed_at="2026-10-05T00:00:00.250000+00:00",
        evidence_grade=EvidenceGrade.EMPIRICAL,
        evidence_source="captured-paper-observation-v1",
        accepted_odds=odds,
        accepted_stake=stake,
        reason="resource-bound regression",
    )


class PaperExecutionDecimalIngressTests(unittest.TestCase):
    def test_extreme_exponents_reject_before_formatting(self):
        for field, value in (
            ("odds", "1E+100000000"),
            ("stake", "1E-100000000"),
            ("odds", "1E+8192"),
            ("stake", "1E-8192"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, "fixed-point representation exceeds resource limit"):
                    evidence(**{field: value})

    def test_long_input_string_rejected_before_decimal_materialization(self):
        with self.assertRaisesRegex(ValueError, "input text exceeds resource limit"):
            evidence(odds="1" * 8193)

    def test_huge_integer_rejected_before_decimal_materialization(self):
        with self.assertRaisesRegex(ValueError, "integer input exceeds resource limit"):
            evidence(odds=10 ** 12000)

    def test_float_and_custom_string_side_effects_are_not_economic_transport(self):
        with self.assertRaises(ValueError):
            evidence(odds=2.5)
        class Sneaky:
            def __str__(self):
                raise AssertionError("untrusted __str__ must not run")
        with self.assertRaises(ValueError):
            evidence(odds=Sneaky())

    def test_sibling_preflight_precedes_any_formatted_evidence_field(self):
        record = evidence()
        object.__setattr__(record, "accepted_stake", Decimal("1E+8192"))
        formatted = []
        def track(value, spec):
            formatted.append(value)
            return value.__format__(spec)
        with patch.dict(legacy.__dict__, {"format": track}):
            with self.assertRaisesRegex(ValueError, "fixed-point representation exceeds resource limit"):
                record.to_dict()
        self.assertEqual(formatted, [])

    def test_sibling_preflight_precedes_any_formatted_attempt_field(self):
        attempt = PaperLegAttempt(
            attempt_id="attempt-resource",
            run_id="run-resource",
            plan_id="plan-resource",
            action_id="resource-action",
            sequence=0,
            bookmaker_id="paper-venue",
            account_id="paper-account",
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            side="BACK",
            decision_quote_id="quote-1",
            decision_odds=Decimal("2.50"),
            requested_stake=Decimal("10.00"),
            decision_observed_at="2026-10-05T00:00:00.100000+00:00",
            execution_observed_at="2026-10-05T00:00:00.200000+00:00",
            delay_ms=100,
            quote_age_ms=100,
            outcome=PaperAttemptOutcome.ACCEPTED,
            execution_odds=Decimal("2.40"),
            execution_stake=Decimal("10.00"),
            suspended=False,
            evidence_grade=EvidenceGrade.EMPIRICAL,
            evidence_source="captured-paper-observation-v1",
            evidence_id="paper-evidence-1",
            evidence_sha256="a" * 64,
            model_fingerprint="b" * 64,
            reason="resource-bound regression",
        )
        object.__setattr__(attempt, "execution_stake", Decimal("1E+8192"))
        formatted = []
        def track(value, spec):
            formatted.append(value)
            return value.__format__(spec)
        with patch.dict(legacy.__dict__, {"format": track}):
            with self.assertRaisesRegex(ValueError, "fixed-point representation exceeds resource limit"):
                attempt.to_dict()
        self.assertEqual(formatted, [])

    def test_boundary_and_context_preserve_exact_fixed_point_evidence(self):
        boundary = evidence(odds="1E+8191", stake="1.00")
        self.assertEqual(len(boundary.to_dict()["accepted_odds"]), 8192)
        self.assertEqual(boundary.to_dict()["accepted_odds"], "1" + "0" * 8191)
        sample = evidence(odds="123.4500", stake="7.500")
        with localcontext() as context:
            context.prec = 5
            digest1 = sample.evidence_sha256
        with localcontext() as context:
            context.prec = 80
            digest2 = sample.evidence_sha256
        self.assertEqual(digest1, digest2)
        self.assertEqual(sample.to_dict()["accepted_odds"], "123.4500")
        self.assertEqual(sample.to_dict()["accepted_stake"], "7.500")


if __name__ == "__main__":
    unittest.main()
