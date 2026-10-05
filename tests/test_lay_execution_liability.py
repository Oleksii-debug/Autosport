from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

import autosport.paper as paper_module
from autosport.domain import TicketLeg, TicketStatus
from autosport.exchange_exposure import locked_capital_for_exchange_side
from autosport.paper import PaperBook
import autosport.paper_execution_reality as paper_reality
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperLegAttempt,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
    RecoveryDecision,
    execute_paper_plan,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-20T03:00:00+00:00"
STARTED_AT = "2026-09-20T03:00:00.100000+00:00"
EXPIRES_AT = "2026-09-20T03:01:00+00:00"


def _action(
    action_id: str = "lay-1",
    *,
    side: str = "LAY",
    odds: str = "5.00",
    stake: str = "10.00",
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-exchange",
        account_id="paper-account",
        event_id="event-1",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side=side,
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="lay-paper-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="decision-1",
        approval_id="paper-only",
        created_at=QUOTE_AT,
        actions=tuple(actions),
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="lay-liability-1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="test-seeded-model",
        seed="fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _registered_observation(
    ledger: PaperExecutionLedger,
    source_action: ExecutionAction,
    outcome: PaperAttemptOutcome,
    *,
    odds: str | None = None,
    stake: str | None = None,
    grade: EvidenceGrade = EvidenceGrade.EMPIRICAL,
    suspended: bool = False,
):
    record = PaperExecutionEvidenceRecord(
        action_id=source_action.action_id,
        bookmaker_id=source_action.bookmaker_id,
        account_id=source_action.account_id,
        event_id=source_action.event_id,
        market_id=source_action.market_id,
        selection_id=source_action.selection_id,
        side=source_action.side,
        quote_id=source_action.quote_id,
        outcome=outcome,
        observed_at="2026-09-20T03:00:00.250000+00:00",
        evidence_grade=grade,
        evidence_source="captured-paper-observation-v1",
        accepted_odds=odds,
        accepted_stake=stake,
        suspended=suspended,
        reason=f"observed {outcome.value.lower()}",
    )
    registry = PaperExecutionEvidenceRegistry(ledger)
    registry.register(record)
    return record.as_observation(), registry


class ExchangeLockedCapitalTests(unittest.TestCase):
    def test_back_locks_stake_exactly(self):
        stake = Decimal("10.000")
        self.assertEqual(
            locked_capital_for_exchange_side(
                stake=stake,
                odds=Decimal("5.00"),
                exchange_side="BACK",
            ),
            stake,
        )

    def test_lay_locks_exact_liability_without_quantization(self):
        self.assertEqual(
            locked_capital_for_exchange_side(
                stake=Decimal("10.00"),
                odds=Decimal("5.00"),
                exchange_side="lay",
            ),
            Decimal("40.0000"),
        )

    def test_canonical_lay_liability_matrix(self):
        for odds, expected in (
            ("1.5", "5.0"),
            ("2", "10"),
            ("5", "40"),
        ):
            with self.subTest(odds=odds):
                self.assertEqual(
                    locked_capital_for_exchange_side(
                        stake=Decimal("10"),
                        odds=Decimal(odds),
                        exchange_side="LAY",
                    ),
                    Decimal(expected),
                )

    def test_invalid_or_inexact_inputs_fail_closed(self):
        with self.assertRaises(TypeError):
            locked_capital_for_exchange_side(
                stake=10.0,
                odds=Decimal("2"),
                exchange_side="LAY",
            )
        with self.assertRaises(ValueError):
            locked_capital_for_exchange_side(
                stake=Decimal("10"),
                odds=Decimal("2"),
                exchange_side="UNKNOWN",
            )
        with self.assertRaises(ValueError):
            locked_capital_for_exchange_side(
                stake=Decimal("10"),
                odds=Decimal("NaN"),
                exchange_side="LAY",
            )


class DurableSideIntegrityTests(unittest.TestCase):
    @staticmethod
    def _attempt(*, outcome: PaperAttemptOutcome, side: str) -> PaperLegAttempt:
        return PaperLegAttempt(
            attempt_id="attempt-1",
            run_id="run-1",
            plan_id="plan-1",
            action_id="action-1",
            sequence=0,
            bookmaker_id="paper-exchange",
            account_id="paper-account",
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            side=side,
            decision_quote_id="quote-1",
            decision_odds=Decimal("2.00"),
            requested_stake=Decimal("10.00"),
            decision_observed_at=QUOTE_AT,
            execution_observed_at=STARTED_AT,
            delay_ms=100,
            quote_age_ms=100,
            outcome=outcome,
            execution_odds=None,
            execution_stake=None,
            suspended=False,
            evidence_grade=EvidenceGrade.SYNTHETIC,
            evidence_source="test",
            evidence_id=None,
            evidence_sha256=None,
            model_fingerprint="model-1",
            reason="test",
        )

    def test_rejected_durable_attempt_does_not_normalize_side(self):
        attempt = self._attempt(
            outcome=PaperAttemptOutcome.REJECTED,
            side="BACK",
        )
        object.__setattr__(attempt, "side", " back ")
        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "noncanonical exchange side",
        ):
            paper_reality._derive_run_economics(("action-1",), (attempt,))

    def test_durable_attempt_rejects_numeric_decimal_token_laundering(self):
        attempt = self._attempt(
            outcome=PaperAttemptOutcome.REJECTED,
            side="BACK",
        )
        payload = attempt.to_dict()
        payload["decision_odds"] = 2

        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "canonical serialized decimal string",
        ):
            PaperLegAttempt.from_dict(payload)

    def test_registered_evidence_rejects_numeric_decimal_token_laundering(self):
        source_action = _action()
        record = PaperExecutionEvidenceRecord(
            action_id=source_action.action_id,
            bookmaker_id=source_action.bookmaker_id,
            account_id=source_action.account_id,
            event_id=source_action.event_id,
            market_id=source_action.market_id,
            selection_id=source_action.selection_id,
            side=source_action.side,
            quote_id=source_action.quote_id,
            outcome=PaperAttemptOutcome.ACCEPTED,
            observed_at=STARTED_AT,
            evidence_grade=EvidenceGrade.EMPIRICAL,
            evidence_source="captured-paper-observation-v1",
            accepted_odds="5.00",
            accepted_stake="10.00",
        )
        payload = record.to_dict()
        payload["accepted_stake"] = 10.0

        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "canonical serialized decimal string",
        ):
            PaperExecutionEvidenceRecord.from_dict(payload)

    def test_unknown_durable_attempt_does_not_normalize_lay_side(self):
        attempt = self._attempt(
            outcome=PaperAttemptOutcome.UNKNOWN,
            side="LAY",
        )
        object.__setattr__(attempt, "side", " lay ")
        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "noncanonical exchange side",
        ):
            paper_reality._derive_run_economics(("action-1",), (attempt,))


    def test_durable_unknown_back_rejects_oversized_requested_stake(self):
        attempt = self._attempt(
            outcome=PaperAttemptOutcome.UNKNOWN,
            side="BACK",
        )
        object.__setattr__(attempt, "requested_stake", Decimal("1E+9000"))

        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "canonical resource bounds",
        ):
            paper_reality._derive_run_economics(("action-1",), (attempt,))

    def test_durable_side_subclass_is_rejected_without_hash_or_equality_hooks(self):
        class HostileSide(str):
            calls = 0

            def __hash__(self) -> int:
                type(self).calls += 1
                return super().__hash__()

            def __eq__(self, other: object) -> bool:
                type(self).calls += 1
                return super().__eq__(other)

        attempt = self._attempt(
            outcome=PaperAttemptOutcome.UNKNOWN,
            side="BACK",
        )
        object.__setattr__(attempt, "side", HostileSide("BACK"))

        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "noncanonical exchange side",
        ):
            paper_reality._derive_run_economics(("action-1",), (attempt,))

        self.assertEqual(HostileSide.calls, 0)


class LayExecutionLiabilityTests(unittest.TestCase):
    def test_registry_revalidates_mutated_decimal_before_durable_write(self):
        class HostileDecimal(Decimal):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            registry = PaperExecutionEvidenceRegistry(ledger)
            source_action = _action()
            record = PaperExecutionEvidenceRecord(
                action_id=source_action.action_id,
                bookmaker_id=source_action.bookmaker_id,
                account_id=source_action.account_id,
                event_id=source_action.event_id,
                market_id=source_action.market_id,
                selection_id=source_action.selection_id,
                side=source_action.side,
                quote_id=source_action.quote_id,
                outcome=PaperAttemptOutcome.ACCEPTED,
                observed_at=STARTED_AT,
                evidence_grade=EvidenceGrade.EMPIRICAL,
                evidence_source="captured-paper-observation-v1",
                accepted_odds="5.00",
                accepted_stake="10.00",
            )
            object.__setattr__(record, "accepted_odds", HostileDecimal("5.00"))

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "accepted_odds must retain exact Decimal authority",
            ):
                registry.register(record)

            self.assertEqual(ledger.events(), [])

    def test_attempt_write_revalidates_mutated_exponent_before_serialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            attempt = DurableSideIntegrityTests._attempt(
                outcome=PaperAttemptOutcome.REJECTED,
                side="BACK",
            )
            object.__setattr__(attempt, "requested_stake", Decimal("1E+9000"))

            with self.assertRaisesRegex(ValueError, "resource limit"):
                ledger.record_attempt(attempt)

            self.assertEqual(ledger.events(), [])

    def test_empirical_decimal_ingress_rejects_arbitrary_str_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            _HostileStake.calls = 0

            with self.assertRaisesRegex(ValueError, "exact Decimal"):
                _registered_observation(
                    ledger,
                    source_action,
                    PaperAttemptOutcome.ACCEPTED,
                    odds="5.00",
                    stake=_HostileStake(),  # type: ignore[arg-type]
                )

            self.assertEqual(_HostileStake.calls, 0)
            self.assertEqual(ledger.events(), [])

    def test_empirical_decimal_ingress_rejects_decimal_subclass(self):
        class HostileDecimal(Decimal):
            calls = 0

            def __str__(self) -> str:
                type(self).calls += 1
                return super().__str__()

        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            value = HostileDecimal("5.00")

            with self.assertRaisesRegex(ValueError, "exact Decimal"):
                _registered_observation(
                    ledger,
                    source_action,
                    PaperAttemptOutcome.ACCEPTED,
                    odds=value,  # type: ignore[arg-type]
                    stake="10.00",
                )

            self.assertEqual(HostileDecimal.calls, 0)
            self.assertEqual(ledger.events(), [])

    def test_observation_digest_subclass_fails_before_equality_hooks_or_reservation(self):
        class HostileDigest(str):
            calls = 0

            def __eq__(self, other: object) -> bool:
                type(self).calls += 1
                return super().__eq__(other)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
            )
            before = len(ledger.events())
            object.__setattr__(
                observation,
                "evidence_sha256",
                HostileDigest(observation.evidence_sha256),
            )

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "evidence_sha256 must retain exact canonical text authority",
            ):
                execute_paper_plan(
                    plan=_plan(source_action),
                    trigger_id="hostile-observation-digest",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={source_action.action_id: observation},
                    evidence_registry=registry,
                )

            self.assertEqual(HostileDigest.calls, 0)
            self.assertEqual(len(ledger.events()), before)

    def test_action_identity_subclass_fails_before_equality_hooks_or_reservation(self):
        class HostileIdentity(str):
            calls = 0

            def __eq__(self, other: object) -> bool:
                type(self).calls += 1
                return super().__eq__(other)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
            )
            before = len(ledger.events())
            object.__setattr__(
                source_action,
                "bookmaker_id",
                HostileIdentity(source_action.bookmaker_id),
            )

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "bookmaker_id must retain exact canonical text authority",
            ):
                execute_paper_plan(
                    plan=_plan(source_action),
                    trigger_id="hostile-action-identity",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={source_action.action_id: observation},
                    evidence_registry=registry,
                )

            self.assertEqual(HostileIdentity.calls, 0)
            self.assertEqual(len(ledger.events()), before)

    def test_empirical_decimal_ingress_rejects_oversized_exponent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()

            with self.assertRaisesRegex(ValueError, "resource limit"):
                _registered_observation(
                    ledger,
                    source_action,
                    PaperAttemptOutcome.ACCEPTED,
                    odds=Decimal("1E+9000"),  # type: ignore[arg-type]
                    stake="10.00",
                )

            self.assertEqual(ledger.events(), [])

    def test_empirical_accepted_lay_uses_liability_and_survives_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action(odds="5.00", stake="10.00")
            current = _plan(source_action)
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
            )
            first = execute_paper_plan(
                plan=current,
                trigger_id="lay-accepted",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={source_action.action_id: observation},
                evidence_registry=registry,
            )
            event_count = len(ledger.events())
            resumed = execute_paper_plan(
                plan=current,
                trigger_id="lay-accepted",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={source_action.action_id: observation},
                evidence_registry=registry,
            )

            self.assertEqual(first.attempts[0].side, "LAY")
            self.assertEqual(first.attempts[0].execution_stake, Decimal("10.00"))
            self.assertEqual(first.attempts[0].execution_odds, Decimal("5.00"))
            self.assertEqual(first.worst_case_exposure, Decimal("40.0000"))
            self.assertEqual(first.recovery_decision, RecoveryDecision.NONE)
            self.assertEqual(resumed, first)
            self.assertEqual(len(ledger.events()), event_count)

    def test_empirical_partial_lay_uses_accepted_partial_liability(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action(odds="5.00", stake="10.00")
            current = _plan(source_action)
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.PARTIAL,
                odds="4.50",
                stake="4.00",
            )
            result = execute_paper_plan(
                plan=current,
                trigger_id="lay-partial",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={source_action.action_id: observation},
                evidence_registry=registry,
            )

            self.assertEqual(result.attempts[0].execution_stake, Decimal("4.00"))
            self.assertEqual(result.worst_case_exposure, Decimal("14.0000"))
            self.assertEqual(
                result.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )

    def test_synthetic_lay_fails_before_run_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "explicit empirical execution evidence",
            ):
                execute_paper_plan(
                    plan=_plan(_action()),
                    trigger_id="lay-synthetic",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                )
            self.assertEqual(ledger.events(), ())

    def test_configured_lay_and_unknown_lay_fail_before_run_reservation(self):
        for grade, outcome, odds, stake in (
            (EvidenceGrade.CONFIGURED, PaperAttemptOutcome.ACCEPTED, "5.00", "10.00"),
            (EvidenceGrade.EMPIRICAL, PaperAttemptOutcome.UNKNOWN, None, None),
        ):
            with self.subTest(grade=grade, outcome=outcome):
                with tempfile.TemporaryDirectory() as tmp:
                    ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
                    source_action = _action()
                    observation, registry = _registered_observation(
                        ledger,
                        source_action,
                        outcome,
                        odds=odds,
                        stake=stake,
                        grade=grade,
                    )
                    before = len(ledger.events())
                    with self.assertRaises(PaperExecutionStateError):
                        execute_paper_plan(
                            plan=_plan(source_action),
                            trigger_id=f"lay-{grade.value}-{outcome.value}",
                            config=_config(),
                            ledger=ledger,
                            started_at=STARTED_AT,
                            observations={source_action.action_id: observation},
                            evidence_registry=registry,
                        )
                    self.assertEqual(len(ledger.events()), before)

    def test_suspended_empirical_fill_fails_before_run_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
                suspended=True,
            )
            before = len(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "suspended observation cannot claim a fill",
            ):
                execute_paper_plan(
                    plan=_plan(source_action),
                    trigger_id="lay-suspended-fill",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={source_action.action_id: observation},
                    evidence_registry=registry,
                )

            self.assertEqual(len(ledger.events()), before)

    def test_empirical_overfill_fails_before_run_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action(stake="10.00")
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="11.00",
            )
            before = len(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "accepted stake exceeds requested stake",
            ):
                execute_paper_plan(
                    plan=_plan(source_action),
                    trigger_id="lay-overfill",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={source_action.action_id: observation},
                    evidence_registry=registry,
                )

            self.assertEqual(len(ledger.events()), before)

    def test_mixed_or_multileg_lay_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            lay = _action("lay")
            back = _action("back", side="BACK", odds="2.00", stake="5.00")
            observation, registry = _registered_observation(
                ledger,
                lay,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
            )
            before = len(ledger.events())
            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "single-leg",
            ):
                execute_paper_plan(
                    plan=_plan(lay, back),
                    trigger_id="mixed-lay",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={lay.action_id: observation},
                    evidence_registry=registry,
                )
            self.assertEqual(len(ledger.events()), before)

    def test_back_regression_keeps_stake_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            result = execute_paper_plan(
                plan=_plan(_action("back", side="BACK", odds="5.00", stake="10.00")),
                trigger_id="back-regression",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )
            self.assertEqual(result.worst_case_exposure, Decimal("10.00"))


class _HostileStake:
    calls = 0

    def __str__(self) -> str:
        type(self).calls += 1
        raise AssertionError("hostile stake __str__ must not execute")


class _HostileLegIterable:
    calls = 0

    def __iter__(self):
        type(self).calls += 1
        raise AssertionError("hostile leg iterable must not execute")


class _HostileSettlementKey:
    calls = 0

    def __hash__(self) -> int:
        type(self).calls += 1
        return object.__hash__(self)


class _HostileComparableText(str):
    comparisons = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)


class _HostileExchangeSide(str):
    comparisons = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)


class PaperBookLayEconomicsTests(unittest.TestCase):
    @staticmethod
    def _lay_leg() -> TicketLeg:
        return TicketLeg(
            event_id="event-lay",
            market_id="market-lay",
            selection_id="selection-lay",
            locked_odds=Decimal("5.00"),
            sport="football",
            exchange_side="lay",
        )

    def test_mutated_exchange_side_subclass_is_rejected_before_equality_dispatch(self):
        book = PaperBook(Decimal("100"))
        leg = self._lay_leg()
        object.__setattr__(leg, "exchange_side", _HostileExchangeSide("lay"))
        _HostileExchangeSide.comparisons = 0

        with self.assertRaisesRegex(ValueError, "exchange_side"):
            book.open_ticket(
                [leg],
                Decimal("10"),
                placed_at=QUOTE_AT,
            )

        self.assertEqual(_HostileExchangeSide.comparisons, 0)
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_lay_public_read_validates_visible_state_before_authority_comparison(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            reason="safe",
            placed_at=QUOTE_AT,
        )
        ticket.strategy_reason = _HostileComparableText("safe")
        _HostileComparableText.comparisons = 0

        def exact_text(value, label):
            if type(value) is not str:
                raise ValueError(f"PaperBook {label} must be a string")
            return value

        with patch.object(
            PaperBook,
            "_require_utf8_string",
            staticmethod(exact_text),
        ):
            with self.assertRaisesRegex(ValueError, "strategy_reason must be a string"):
                _ = book.committed_capital

        self.assertEqual(_HostileComparableText.comparisons, 0)

    def test_lay_lifecycle_rejects_noncanonical_settlement_key_before_rehash(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )
        hostile = _HostileSettlementKey()
        book._settlement_times[hostile] = None
        _HostileSettlementKey.calls = 0

        with self.assertRaisesRegex(
            ValueError,
            "settlement-time witness keys must be canonical strings",
        ):
            _ = book.committed_capital

        self.assertEqual(_HostileSettlementKey.calls, 0)

    def test_lay_open_reuses_canonical_stake_ingress_when_available(self):
        book = PaperBook(Decimal("100"))
        _HostileStake.calls = 0

        def canonical_decimal(value, label):
            self.assertEqual(label, "stake")
            if type(value) is not Decimal:
                raise ValueError("canonical stake rejected")
            return value

        with patch.object(
            PaperBook,
            "_canonical_decimal_input",
            canonical_decimal,
            create=True,
        ):
            with self.assertRaisesRegex(ValueError, "canonical stake rejected"):
                book.open_ticket(
                    [self._lay_leg()],
                    _HostileStake(),
                    placed_at=QUOTE_AT,
                )

        self.assertEqual(_HostileStake.calls, 0)
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_lay_open_preserves_exact_leg_container_ingress_when_available(self):
        book = PaperBook(Decimal("100"))
        _HostileLegIterable.calls = 0

        def canonical_decimal(value, _label):
            return Decimal(str(value))

        with patch.object(
            PaperBook,
            "_canonical_decimal_input",
            canonical_decimal,
            create=True,
        ):
            with self.assertRaisesRegex(ValueError, "exact list or tuple"):
                book.open_ticket(
                    _HostileLegIterable(),
                    Decimal("10"),
                    placed_at=QUOTE_AT,
                )

        self.assertEqual(_HostileLegIterable.calls, 0)
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_lay_open_reuses_canonical_paperbook_operation_lock_when_available(self):
        book = PaperBook(Decimal("100"))
        entered = 0
        exited = 0

        @contextmanager
        def operation_lock(current):
            nonlocal entered, exited
            self.assertIs(current, book)
            entered += 1
            try:
                yield
            finally:
                exited += 1

        with patch.object(
            paper_module,
            "_require_paperbook_operation_lock",
            operation_lock,
            create=True,
        ):
            ticket = book.open_ticket(
                [self._lay_leg()],
                Decimal("10"),
                placed_at=QUOTE_AT,
            )

        self.assertEqual(ticket.legs[0].exchange_side, "lay")
        self.assertEqual(entered, 1)
        self.assertEqual(exited, 1)
        self.assertEqual(book.balance, Decimal("60.00"))

    def test_lay_committed_capital_reuses_canonical_operation_lock_when_available(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )
        entered = 0
        exited = 0

        @contextmanager
        def operation_lock(current):
            nonlocal entered, exited
            self.assertIs(current, book)
            entered += 1
            try:
                yield
            finally:
                exited += 1

        with patch.object(
            paper_module,
            "_require_paperbook_operation_lock",
            operation_lock,
            create=True,
        ):
            committed = book.committed_capital

        self.assertEqual(committed, Decimal("40.00"))
        self.assertEqual(entered, 1)
        self.assertEqual(exited, 1)

    def test_invalid_canonical_operation_lock_authority_fails_closed(self):
        book = PaperBook(Decimal("100"))

        with patch.object(
            paper_module,
            "_require_paperbook_operation_lock",
            object(),
            create=True,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "operation lock authority is invalid",
            ):
                book.open_ticket(
                    [self._lay_leg()],
                    Decimal("10"),
                    placed_at=QUOTE_AT,
                )

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_open_lay_reserves_liability_and_reports_committed_capital(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )

        self.assertEqual(ticket.stake, Decimal("10"))
        self.assertEqual(book.balance, Decimal("60.00"))
        self.assertEqual(book.committed_capital, Decimal("40.00"))
        self.assertEqual(ticket.status, TicketStatus.OPEN)

    def test_lay_open_accepts_exact_liability_bankroll(self):
        book = PaperBook(Decimal("40"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )

        self.assertEqual(ticket.stake, Decimal("10"))
        self.assertEqual(book.balance, Decimal("0"))
        self.assertEqual(book.committed_capital, Decimal("40.00"))

    def test_lay_open_rejects_bankroll_below_liability_without_mutation(self):
        book = PaperBook(Decimal("39.99"))

        with self.assertRaisesRegex(ValueError, "insufficient virtual bankroll"):
            book.open_ticket(
                [self._lay_leg()],
                Decimal("10"),
                placed_at=QUOTE_AT,
            )

        self.assertEqual(book.balance, Decimal("39.99"))
        self.assertEqual(book.tickets, {})
        self.assertEqual(book.committed_capital, Decimal("0"))

    def test_lay_selection_loses_returns_liability_plus_lay_stake(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )

        settled = book.settle(ticket.ticket_id, set())

        self.assertEqual(settled.status, TicketStatus.WON)
        self.assertEqual(settled.payout, Decimal("50.00"))
        self.assertEqual(book.balance, Decimal("110.00"))
        self.assertEqual(book.committed_capital, Decimal("0"))

    def test_lay_selection_wins_consumes_locked_liability(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )

        settled = book.settle(ticket.ticket_id, {ticket.legs[0].quote_key})

        self.assertEqual(settled.status, TicketStatus.LOST)
        self.assertEqual(settled.payout, Decimal("0"))
        self.assertEqual(book.balance, Decimal("60.00"))
        self.assertEqual(book.committed_capital, Decimal("0"))

    def test_lay_void_releases_exact_locked_liability(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )

        settled = book.settle(
            ticket.ticket_id,
            set(),
            {ticket.legs[0].quote_key},
        )

        self.assertEqual(settled.status, TicketStatus.VOID)
        self.assertEqual(settled.payout, Decimal("40.00"))
        self.assertEqual(book.balance, Decimal("100.00"))
        self.assertEqual(book.committed_capital, Decimal("0"))

    def test_open_lay_snapshot_round_trip_preserves_liability_and_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-book.json"
            book = PaperBook(Decimal("100"))
            ticket = book.open_ticket(
                [self._lay_leg()],
                Decimal("10"),
                placed_at=QUOTE_AT,
            )
            book.save(path)

            loaded = PaperBook.load(path)
            loaded_ticket = loaded.tickets[ticket.ticket_id]

            self.assertEqual(loaded_ticket.legs[0].exchange_side, "lay")
            self.assertEqual(loaded_ticket.stake, Decimal("10"))
            self.assertEqual(loaded.balance, Decimal("60.00"))
            self.assertEqual(loaded.committed_capital, Decimal("40.00"))


if __name__ == "__main__":
    unittest.main()
