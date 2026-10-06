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
    PaperLegAttempt,
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

    def test_extreme_positive_exponent_fails_before_materialization(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "decimal fixed-point representation exceeds resource limit",
        ):
            evidence(odds="1E+100000000")

    def test_extreme_negative_exponent_fails_before_materialization(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "decimal fixed-point representation exceeds resource limit",
        ):
            evidence(stake="1E-100000000")

    def test_oversized_decimal_input_text_is_rejected_before_decimal_parse(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "must be a finite Decimal",
        ):
            evidence(odds="1" * 8193)

    def test_huge_integer_ingress_is_rejected_before_decimal_construction(self) -> None:
        huge = 1 << 1_000_000

        with self.assertRaisesRegex(
            ValueError,
            "must be a finite Decimal",
        ):
            legacy._decimal(huge, "value")

    def test_boundary_sized_integer_ingress_remains_accepted(self) -> None:
        value = 10 ** 8191

        parsed = legacy._decimal(value, "value")

        self.assertEqual(parsed, Decimal(value))
        self.assertEqual(len(format(parsed, "f")), 8192)

    def test_decimal_ingress_rejects_float_and_custom_string_coercion(self) -> None:
        class DecimalLike:
            def __str__(self) -> str:
                raise AssertionError("caller-defined string coercion executed")

        with self.assertRaisesRegex(
            ValueError,
            "must be a Decimal, decimal string, or int",
        ):
            legacy._decimal(1.25, "value")
        with self.assertRaisesRegex(
            ValueError,
            "must be a Decimal, decimal string, or int",
        ):
            legacy._decimal(DecimalLike(), "value")


    def test_evidence_hash_and_id_fail_closed_for_mutated_oversized_decimal(self) -> None:
        record = evidence()
        object.__setattr__(record, "accepted_odds", Decimal("1E+100000000"))

        with self.assertRaisesRegex(
            ValueError,
            "decimal fixed-point representation exceeds resource limit",
        ):
            _ = record.evidence_sha256
        with self.assertRaisesRegex(
            ValueError,
            "decimal fixed-point representation exceeds resource limit",
        ):
            _ = record.evidence_id

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

    def test_all_attempt_decimal_siblings_preflight_before_any_formatting(self) -> None:
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
            reason="attempt resource-bound regression",
        )
        object.__setattr__(attempt, "execution_stake", Decimal("1E+8192"))
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
                attempt.to_dict()
        finally:
            if previous is sentinel:
                del legacy.format
            else:
                legacy.format = previous

        self.assertEqual(formatted, [])

    def _attempt_payload(self) -> dict[str, object]:
        return {
            "attempt_id": "attempt-durable",
            "run_id": "run-durable",
            "plan_id": "plan-durable",
            "action_id": "resource-action",
            "sequence": 0,
            "bookmaker_id": "paper-venue",
            "account_id": "paper-account",
            "event_id": "event-1",
            "market_id": "market-1",
            "selection_id": "selection-1",
            "side": "BACK",
            "decision_quote_id": "quote-1",
            "decision_odds": "2.50",
            "requested_stake": "10.00",
            "decision_observed_at": "2026-10-05T00:00:00.100000+00:00",
            "execution_observed_at": "2026-10-05T00:00:00.200000+00:00",
            "delay_ms": 100,
            "quote_age_ms": 100,
            "outcome": "ACCEPTED",
            "execution_odds": "2.40",
            "execution_stake": "10.00",
            "suspended": False,
            "evidence_grade": "EMPIRICAL",
            "evidence_source": "captured-paper-observation-v1",
            "evidence_id": "paper-evidence-1",
            "evidence_sha256": "a" * 64,
            "model_fingerprint": "b" * 64,
            "reason": "durable Decimal parser authority regression",
        }

    def test_attempt_reload_ignores_rebound_module_decimal_constructor(self) -> None:
        payload = self._attempt_payload()
        sentinel = object()
        previous = legacy.__dict__.get("Decimal", sentinel)
        calls: list[object] = []

        def forged_decimal(value: object) -> Decimal:
            calls.append(value)
            raise AssertionError("rebound module Decimal executed")

        legacy.Decimal = forged_decimal
        try:
            attempt = PaperLegAttempt.from_dict(payload)
        finally:
            if previous is sentinel:
                del legacy.Decimal
            else:
                legacy.Decimal = previous

        self.assertEqual(calls, [])
        self.assertEqual(attempt.decision_odds, Decimal("2.50"))
        self.assertEqual(attempt.requested_stake, Decimal("10.00"))
        self.assertEqual(attempt.execution_odds, Decimal("2.40"))
        self.assertEqual(attempt.execution_stake, Decimal("10.00"))

    def test_ledger_durability_ignores_rebound_filesystem_module_globals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "paper-execution.jsonl"
            names = ("os", "threading", "Path")
            sentinel = object()
            previous = {name: legacy.__dict__.get(name, sentinel) for name in names}

            class ForbiddenRuntime:
                def __getattr__(self, name: str):
                    raise AssertionError(f"rebound filesystem global executed: {name}")

                def __call__(self, *args, **kwargs):
                    raise AssertionError("rebound Path global executed")

            try:
                forbidden = ForbiddenRuntime()
                for name in names:
                    legacy.__dict__[name] = forbidden

                ledger = PaperExecutionLedger(str(ledger_path))
                record = evidence()
                ledger.register_observation_evidence(record)
                events = ledger.events()
            finally:
                for name, value in previous.items():
                    if value is sentinel:
                        legacy.__dict__.pop(name, None)
                    else:
                        legacy.__dict__[name] = value

            self.assertEqual(len(events), 1)
            self.assertEqual(
                events[0]["event_type"],
                "OBSERVATION_EVIDENCE_REGISTERED",
            )
            self.assertTrue(ledger_path.exists())

    def test_identity_helpers_ignore_rebound_module_runtime_globals(self) -> None:
        expected_record = evidence()
        expected_digest = expected_record.evidence_sha256
        expected_bucket = legacy._deterministic_int("seed", "label", 97)
        names = (
            "_text",
            "_timestamp",
            "_timestamp_text",
            "_digest",
            "_canonical",
            "json",
            "hashlib",
            "datetime",
            "timezone",
        )
        sentinel = object()
        previous = {name: legacy.__dict__.get(name, sentinel) for name in names}

        def forged(*args, **kwargs):
            raise AssertionError("rebound identity helper executed")

        try:
            for name in names:
                legacy.__dict__[name] = forged

            record = evidence()
            self.assertEqual(record.evidence_sha256, expected_digest)
            self.assertEqual(
                legacy._deterministic_int("seed", "label", 97),
                expected_bucket,
            )
            self.assertEqual(
                legacy._parse_json_object('{"value":1}', what="test"),
                {"value": 1},
            )
        finally:
            for name, value in previous.items():
                if value is sentinel:
                    legacy.__dict__.pop(name, None)
                else:
                    legacy.__dict__[name] = value

    def test_authority_dataclasses_ignore_rebound_module_enum_globals(self) -> None:
        names = ("EvidenceGrade", "PaperAttemptOutcome", "RecoveryDecision")
        sentinel = object()
        previous = {name: legacy.__dict__.get(name, sentinel) for name in names}

        class ForgedEnum:
            def __getattr__(self, name: str):
                raise AssertionError(f"rebound enum global executed: {name}")

        try:
            forged = ForgedEnum()
            for name in names:
                legacy.__dict__[name] = forged

            record = evidence()
            self.assertIs(record.outcome, PaperAttemptOutcome.ACCEPTED)
            self.assertIs(record.evidence_grade, EvidenceGrade.EMPIRICAL)

            run = PaperExecutionRun(
                run_id="run-enum-authority",
                trigger_id="trigger-enum-authority",
                plan_id="plan-enum-authority",
                plan_fingerprint="a" * 64,
                model_fingerprint="b" * 64,
                started_at="2026-10-05T00:00:00.100000+00:00",
                attempts=(),
                pending_action_ids=(),
                recovery_decision=RecoveryDecision.NONE,
                worst_case_exposure=Decimal("0"),
                completed=True,
            )
            self.assertIs(run.recovery_decision, RecoveryDecision.NONE)
        finally:
            for name, value in previous.items():
                if value is sentinel:
                    legacy.__dict__.pop(name, None)
                else:
                    legacy.__dict__[name] = value

    def test_attempt_reload_ignores_rebound_module_enum_constructors(self) -> None:
        payload = self._attempt_payload()
        names = ("PaperAttemptOutcome", "EvidenceGrade")
        sentinel = object()
        previous = {name: legacy.__dict__.get(name, sentinel) for name in names}
        calls: list[str] = []

        def forged_enum(value: object):
            calls.append(str(value))
            raise AssertionError("rebound module enum constructor executed")

        try:
            for name in names:
                legacy.__dict__[name] = forged_enum
            attempt = PaperLegAttempt.from_dict(payload)
        finally:
            for name, value in previous.items():
                if value is sentinel:
                    legacy.__dict__.pop(name, None)
                else:
                    legacy.__dict__[name] = value

        self.assertEqual(calls, [])
        self.assertIs(attempt.outcome, PaperAttemptOutcome.ACCEPTED)
        self.assertIs(attempt.evidence_grade, EvidenceGrade.EMPIRICAL)

    def test_attempt_reload_bounds_decimal_before_module_constructor_dispatch(self) -> None:
        payload = self._attempt_payload()
        payload["decision_odds"] = "1E+8192"
        sentinel = object()
        previous = legacy.__dict__.get("Decimal", sentinel)
        calls: list[object] = []

        def forged_decimal(value: object) -> Decimal:
            calls.append(value)
            raise AssertionError("rebound module Decimal executed")

        legacy.Decimal = forged_decimal
        try:
            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "invalid attempt payload",
            ):
                PaperLegAttempt.from_dict(payload)
        finally:
            if previous is sentinel:
                del legacy.Decimal
            else:
                legacy.Decimal = previous

        self.assertEqual(calls, [])

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


    def test_decimal_authorities_keep_noninjectable_call_shapes(self) -> None:
        self.assertEqual(legacy._decimal.__kwdefaults__, {"allow_zero": False})
        self.assertIsNone(legacy._decimal.__defaults__)
        self.assertIsNone(legacy._decimal_text.__defaults__)
        self.assertIsNone(legacy._decimal_text.__kwdefaults__)
        self.assertIsNone(PaperExecutionEvidenceRecord.to_dict.__defaults__)
        self.assertIsNone(PaperExecutionEvidenceRecord.to_dict.__kwdefaults__)

        with self.assertRaises(TypeError):
            legacy._decimal(
                Decimal("2"),
                "value",
                _resource_validator=lambda _value: None,
            )
        with self.assertRaises(TypeError):
            legacy._decimal_text(
                Decimal("2"),
                _preflight=lambda *_values: None,
            )
        with self.assertRaises(TypeError):
            evidence().to_dict(lambda *_values: None)

    def test_complete_run_rejects_formatter_injection_before_durable_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(ledger_path)
            with self.assertRaises(TypeError):
                ledger.complete_run(
                    run_id="run-injection",
                    pending_action_ids=(),
                    recovery_decision=RecoveryDecision.NONE,
                    worst_case_exposure=Decimal("1E+100000000"),
                    _decimal_formatter=lambda _value: "0",
                )
            self.assertFalse(ledger_path.exists())

    def test_in_place_resource_validator_code_mutation_fails_closed(self) -> None:
        validator = legacy._CANONICAL_DECIMAL_RESOURCE_VALIDATOR
        original_code = validator.__code__

        def forged_validator(value):
            raise AssertionError("mutated resource validator executed")

        validator.__code__ = forged_validator.__code__
        try:
            with self.assertRaisesRegex(
                ValueError,
                "decimal resource validator authority changed",
            ):
                evidence(odds="2.50", stake="10.00")

            record = object.__new__(PaperExecutionEvidenceRecord)
            object.__setattr__(record, "accepted_odds", Decimal("2.50"))
            object.__setattr__(record, "accepted_stake", Decimal("10.00"))
            with self.assertRaisesRegex(
                ValueError,
                "decimal resource validator authority changed",
            ):
                legacy._preflight_decimal_text_fields(
                    record.accepted_odds,
                    record.accepted_stake,
                )
        finally:
            validator.__code__ = original_code

    def test_in_place_decimal_parser_code_mutation_fails_closed(self) -> None:
        parser = legacy._CANONICAL_DECIMAL_PARSER
        original_code = parser.__code__

        def forged_parser(value, name, *, allow_zero=False):
            raise AssertionError("mutated Decimal parser executed")

        parser.__code__ = forged_parser.__code__
        try:
            with self.assertRaisesRegex(ValueError, "decimal parser authority changed"):
                evidence()
            with self.assertRaisesRegex(ValueError, "decimal parser authority changed"):
                PaperExecutionRun(
                    run_id="run-parser-mutation",
                    trigger_id="trigger-parser-mutation",
                    plan_id="plan-parser-mutation",
                    plan_fingerprint="a" * 64,
                    model_fingerprint="b" * 64,
                    started_at="2026-10-05T00:00:00.100000+00:00",
                    attempts=(),
                    pending_action_ids=(),
                    recovery_decision=RecoveryDecision.NONE,
                    worst_case_exposure=Decimal("0"),
                    completed=True,
                )
        finally:
            parser.__code__ = original_code

    def test_in_place_decimal_preflight_code_mutation_fails_closed(self) -> None:
        record = evidence()
        preflight = legacy._CANONICAL_DECIMAL_PREFLIGHT
        original_code = preflight.__code__

        def forged_preflight(*values):
            raise AssertionError("mutated Decimal preflight executed")

        preflight.__code__ = forged_preflight.__code__
        try:
            with self.assertRaisesRegex(ValueError, "decimal preflight authority changed"):
                record.to_dict()
        finally:
            preflight.__code__ = original_code

    def test_in_place_decimal_formatter_code_mutation_fails_closed(self) -> None:
        record = evidence()
        formatter = legacy._CANONICAL_DECIMAL_TEXT_FORMATTER
        original_code = formatter.__code__

        def forged_formatter(value):
            raise AssertionError("mutated Decimal formatter executed")

        formatter.__code__ = forged_formatter.__code__
        try:
            with self.assertRaisesRegex(ValueError, "decimal formatter authority changed"):
                record.to_dict()
            with tempfile.TemporaryDirectory() as tmp:
                ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
                with self.assertRaisesRegex(
                    ValueError,
                    "decimal formatter authority changed",
                ):
                    ledger.complete_run(
                        run_id="run-formatter-mutation",
                        pending_action_ids=(),
                        recovery_decision=RecoveryDecision.NONE,
                        worst_case_exposure=Decimal("1.00"),
                    )
                self.assertFalse((Path(tmp) / "paper-execution.jsonl").exists())
        finally:
            formatter.__code__ = original_code

    def test_durable_decimal_serializers_ignore_rebound_module_helpers(self) -> None:
        record = evidence()
        attempt = PaperLegAttempt(
            attempt_id="attempt-rebound",
            run_id="run-rebound",
            plan_id="plan-rebound",
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
            evidence_id="paper-evidence-rebound",
            evidence_sha256="a" * 64,
            model_fingerprint="b" * 64,
            reason="serializer rebound regression",
        )
        expected_record = record.to_dict()
        expected_attempt = attempt.to_dict()

        sentinel = object()
        names = (
            "_validate_decimal_text_resource_bound",
            "_preflight_decimal_text_fields",
            "_decimal_text",
            "format",
            "Decimal",
        )
        previous = {name: legacy.__dict__.get(name, sentinel) for name in names}

        def forged(*args, **kwargs):
            raise AssertionError("rebound Decimal serializer helper executed")

        try:
            for name in names:
                legacy.__dict__[name] = forged

            self.assertEqual(record.to_dict(), expected_record)
            self.assertEqual(attempt.to_dict(), expected_attempt)
        finally:
            for name, value in previous.items():
                if value is sentinel:
                    legacy.__dict__.pop(name, None)
                else:
                    legacy.__dict__[name] = value

    def test_complete_run_ignores_rebound_decimal_formatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")

            sentinel = object()
            previous = legacy.__dict__.get("_decimal_text", sentinel)

            def forged(*args, **kwargs):
                raise AssertionError("rebound completion Decimal formatter executed")

            legacy._decimal_text = forged
            try:
                ledger.complete_run(
                    run_id="run-complete-rebound",
                    pending_action_ids=(),
                    recovery_decision=RecoveryDecision.NONE,
                    worst_case_exposure=Decimal("12.3400"),
                )
            finally:
                if previous is sentinel:
                    del legacy._decimal_text
                else:
                    legacy._decimal_text = previous

            events = ledger.events("run-complete-rebound")
            self.assertEqual(len(events), 1)
            self.assertEqual(
                events[0]["payload"]["worst_case_exposure"],
                "12.3400",
            )


    def test_authority_dataclasses_ignore_rebound_decimal_ingress_parser(self) -> None:
        sentinel = object()
        previous = legacy.__dict__.get("_decimal", sentinel)

        def forged_decimal(value, name, *, allow_zero=False):
            return Decimal("2")

        legacy._decimal = forged_decimal
        try:
            with self.assertRaisesRegex(
                ValueError,
                "decimal fixed-point representation exceeds resource limit",
            ):
                evidence(odds="1E+8192")

            with self.assertRaisesRegex(
                ValueError,
                "decimal fixed-point representation exceeds resource limit",
            ):
                legacy.ObservedPaperExecution(
                    action_id="resource-action",
                    outcome=PaperAttemptOutcome.ACCEPTED,
                    observed_at="2026-10-05T00:00:00.250000+00:00",
                    evidence_grade=EvidenceGrade.EMPIRICAL,
                    evidence_source="captured-paper-observation-v1",
                    evidence_id="paper-evidence-ingress",
                    evidence_sha256="a" * 64,
                    accepted_odds=Decimal("1E+8192"),
                    accepted_stake=Decimal("10.00"),
                    suspended=False,
                    reason="ingress parser rebound regression",
                )

            with self.assertRaisesRegex(
                ValueError,
                "decimal fixed-point representation exceeds resource limit",
            ):
                PaperLegAttempt(
                    attempt_id="attempt-ingress",
                    run_id="run-ingress",
                    plan_id="plan-ingress",
                    action_id="resource-action",
                    sequence=0,
                    bookmaker_id="paper-venue",
                    account_id="paper-account",
                    event_id="event-1",
                    market_id="market-1",
                    selection_id="selection-1",
                    side="BACK",
                    decision_quote_id="quote-1",
                    decision_odds=Decimal("1E+8192"),
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
                    evidence_id="paper-evidence-ingress",
                    evidence_sha256="a" * 64,
                    model_fingerprint="b" * 64,
                    reason="ingress parser rebound regression",
                )

            with self.assertRaisesRegex(
                ValueError,
                "decimal fixed-point representation exceeds resource limit",
            ):
                PaperExecutionRun(
                    run_id="run-ingress",
                    trigger_id="trigger-ingress",
                    plan_id="plan-ingress",
                    plan_fingerprint="a" * 64,
                    model_fingerprint="b" * 64,
                    started_at="2026-10-05T00:00:00.100000+00:00",
                    attempts=(),
                    pending_action_ids=(),
                    recovery_decision=RecoveryDecision.NONE,
                    worst_case_exposure=Decimal("1E+8192"),
                    completed=True,
                )
        finally:
            if previous is sentinel:
                del legacy._decimal
            else:
                legacy._decimal = previous


if __name__ == "__main__":
    unittest.main()
