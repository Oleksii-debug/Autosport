from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal, localcontext
from pathlib import Path

from autosport import _paper_execution_reality_legacy as legacy
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperExecutionRun,
    RecoveryDecision,
)

_MAX_FIXED_POINT_CHARS = 8192


def evidence(*, odds: str = "2.50", stake: str = "10.00") -> PaperExecutionEvidenceRecord:
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


class PaperExecutionDecimalResourceBoundTests(unittest.TestCase):
    def test_oversized_empirical_odds_is_rejected_at_construction(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "decimal fixed-point representation exceeds resource limit",
        ):
            evidence(odds="1E+8192")

    def test_oversized_empirical_stake_is_rejected_at_construction(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "decimal fixed-point representation exceeds resource limit",
        ):
            evidence(stake="1E-8192")

    def test_mutated_evidence_fails_before_fixed_point_formatting(self) -> None:
        record = evidence()
        object.__setattr__(record, "accepted_odds", Decimal("1E+8192"))

        with self.assertRaisesRegex(
            ValueError,
            "decimal fixed-point representation exceeds resource limit",
        ):
            record.to_dict()

    def test_all_evidence_decimal_siblings_preflight_before_any_formatting(self) -> None:
        record = evidence()
        object.__setattr__(record, "accepted_stake", Decimal("1E+8192"))
        formatted: list[Decimal] = []

        def tracking_format(value: Decimal, spec: str) -> str:
            formatted.append(value)
            return value.__format__(spec)

        sentinel = object()
        previous = legacy.__dict__.get("format", sentinel)
        legacy.format = tracking_format
        try:
            with self.assertRaisesRegex(
                ValueError,
                "decimal fixed-point representation exceeds resource limit",
            ):
                record.to_dict()
        finally:
            if previous is sentinel:
                del legacy.format
            else:
                legacy.format = previous

        self.assertEqual(formatted, [])

    def test_reload_rejects_oversized_evidence_under_same_resource_law(self) -> None:
        payload = evidence().to_dict()
        payload["accepted_stake"] = "1E+8192"

        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "invalid evidence record",
        ):
            PaperExecutionEvidenceRecord.from_dict(payload)

    def test_mutated_oversized_evidence_cannot_append_durable_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            record = evidence()
            object.__setattr__(record, "accepted_odds", Decimal("1E+8192"))

            with self.assertRaisesRegex(
                ValueError,
                "decimal fixed-point representation exceeds resource limit",
            ):
                ledger.register_observation_evidence(record)

            self.assertEqual(ledger.events(), ())
            self.assertFalse((Path(tmp) / "paper-execution.jsonl").exists())

    def test_derived_exposure_is_bound_before_durable_serialization(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "decimal fixed-point representation exceeds resource limit",
        ):
            PaperExecutionRun(
                run_id="run-resource",
                trigger_id="trigger-resource",
                plan_id="plan-resource",
                plan_fingerprint="a" * 64,
                model_fingerprint="b" * 64,
                started_at="2026-10-05T00:00:00.100000+00:00",
                attempts=(),
                pending_action_ids=(),
                recovery_decision=RecoveryDecision.NONE,
                worst_case_exposure=Decimal("1E+8192"),
                completed=True,
            )

    def test_boundary_size_is_accepted_and_formatting_is_exact(self) -> None:
        record = evidence(odds="1E+8191", stake="1.00")

        payload = record.to_dict()

        self.assertEqual(len(payload["accepted_odds"]), _MAX_FIXED_POINT_CHARS)
        self.assertEqual(payload["accepted_odds"], "1" + ("0" * 8191))

    def test_digest_is_ambient_context_independent_inside_bound(self) -> None:
        record = evidence(odds="123.4500", stake="7.500")
        with localcontext() as context:
            context.prec = 5
            first = record.evidence_sha256
        with localcontext() as context:
            context.prec = 80
            second = record.evidence_sha256

        self.assertEqual(first, second)
        self.assertEqual(record.to_dict()["accepted_odds"], "123.4500")
        self.assertEqual(record.to_dict()["accepted_stake"], "7.500")

    def test_legacy_decimal_formatter_now_uses_the_canonical_resource_policy(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "decimal fixed-point representation exceeds resource limit",
        ):
            legacy._decimal_text(Decimal("1E+8192"))

        self.assertEqual(legacy._decimal_text(Decimal("12.3400")), "12.3400")


if __name__ == "__main__":
    unittest.main()
