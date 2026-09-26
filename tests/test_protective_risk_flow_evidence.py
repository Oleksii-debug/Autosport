import copy
import unittest
from collections.abc import Sequence
from decimal import Decimal, ROUND_UP, localcontext
from unittest.mock import patch

from autosport.protective_risk_evidence import (
    PercentageEvidenceStatus,
    ProtectiveRiskEvidenceError,
    ProtectiveRiskEvent,
    ProtectiveRiskFlowEvidence,
    RiskEvidenceEventKind,
    build_protective_risk_flow_evidence,
)


PROTOCOL_SHA = "a" * 64
SOURCE_SHA = "b" * 64
T0 = "2026-09-21T10:00:00Z"
T1 = "2026-09-21T10:01:00Z"


def _valuation(sequence: int, event_id: str, equity: str, at: str = T0):
    return ProtectiveRiskEvent(
        sequence=sequence,
        event_id=event_id,
        occurred_at=at,
        kind=RiskEvidenceEventKind.VALUATION,
        raw_equity=Decimal(equity),
    )


def _flow(
    sequence: int,
    event_id: str,
    amount: str,
    source: str,
    destination: str,
    at: str = T1,
):
    return ProtectiveRiskEvent(
        sequence=sequence,
        event_id=event_id,
        occurred_at=at,
        kind=RiskEvidenceEventKind.CAPITAL_FLOW,
        amount=Decimal(amount),
        source_scope_id=source,
        destination_scope_id=destination,
    )


def _build(events):
    return build_protective_risk_flow_evidence(
        campaign_id="campaign-1",
        protocol_sha256=PROTOCOL_SHA,
        source_sha256=SOURCE_SHA,
        capital_scope_id="portfolio-A",
        currency="EUR",
        events=events,
    )


class ProtectiveRiskFlowEvidenceTests(unittest.TestCase):
    def test_deposit_does_not_erase_prior_loss(self) -> None:
        evidence = _build(
            (
                _valuation(1, "v1", "1000", T0),
                _valuation(2, "v2", "800", T1),
                _flow(3, "f1", "500", "owner", "portfolio-A", T1),
                _valuation(4, "v3", "1300", T1),
            )
        )

        self.assertEqual(
            [point.flow_adjusted_equity for point in evidence.points],
            [Decimal("1000"), Decimal("800"), Decimal("800")],
        )
        self.assertEqual(evidence.maximum_drawdown_fraction, Decimal("0.2"))
        self.assertEqual(evidence.cumulative_external_flow, Decimal("500"))

    def test_withdrawal_does_not_create_false_drawdown(self) -> None:
        evidence = _build(
            (
                _valuation(1, "v1", "1000", T0),
                _valuation(2, "v2", "1200", T1),
                _flow(3, "f1", "500", "portfolio-A", "owner", T1),
                _valuation(4, "v3", "700", T1),
            )
        )

        self.assertEqual(
            [point.flow_adjusted_equity for point in evidence.points],
            [Decimal("1000"), Decimal("1200"), Decimal("1200")],
        )
        self.assertEqual(evidence.maximum_drawdown_fraction, Decimal("0"))
        self.assertEqual(evidence.cumulative_external_flow, Decimal("-500"))

    def test_internal_transfer_is_not_fresh_owner_capital(self) -> None:
        evidence = _build(
            (
                _valuation(1, "v1", "1000", T0),
                _flow(2, "f1", "500", "portfolio-A", "portfolio-A", T1),
                _valuation(3, "v2", "1000", T1),
            )
        )

        self.assertEqual(evidence.cumulative_external_flow, Decimal("0"))
        self.assertEqual(
            evidence.points[-1].flow_adjusted_equity,
            Decimal("1000"),
        )

    def test_flow_outside_declared_scope_fails_closed(self) -> None:
        with self.assertRaises(ProtectiveRiskEvidenceError) as caught:
            _build(
                (
                    _valuation(1, "v1", "1000", T0),
                    _flow(2, "f1", "100", "wallet-X", "wallet-Y", T1),
                    _valuation(3, "v2", "1000", T1),
                )
            )

        self.assertEqual(caught.exception.code, "FLOW_SCOPE_MISMATCH")

    def test_same_timestamp_sequence_changes_identity_and_result(self) -> None:
        after_loss_then_deposit = _build(
            (
                _valuation(1, "v1", "1000", T0),
                _valuation(2, "loss", "800", T1),
                _flow(3, "deposit", "500", "owner", "portfolio-A", T1),
                _valuation(4, "after", "1300", T1),
            )
        )
        deposit_then_loss_mark = _build(
            (
                _valuation(1, "v1", "1000", T0),
                _flow(2, "deposit", "500", "owner", "portfolio-A", T1),
                _valuation(3, "loss", "800", T1),
                _valuation(4, "after", "1300", T1),
            )
        )

        self.assertNotEqual(
            after_loss_then_deposit.evidence_sha256,
            deposit_then_loss_mark.evidence_sha256,
        )
        self.assertNotEqual(
            after_loss_then_deposit.maximum_drawdown_fraction,
            deposit_then_loss_mark.maximum_drawdown_fraction,
        )

    def test_cross_zero_terminates_percentage_evidence(self) -> None:
        evidence = _build(
            (
                _valuation(1, "v1", "1000", T0),
                _valuation(2, "ruin", "0", T1),
            )
        )

        self.assertEqual(
            evidence.percentage_status,
            PercentageEvidenceStatus.TERMINATED_NONPOSITIVE_EQUITY,
        )
        self.assertEqual(evidence.ruin_event_id, "ruin")
        self.assertIsNone(evidence.maximum_drawdown_fraction)
        self.assertEqual(evidence.maximum_drawdown_absolute, Decimal("1000"))
        self.assertEqual(evidence.minimum_flow_adjusted_equity, Decimal("0"))

    def test_strategy_recovery_after_ruin_does_not_reactivate_percentage_chain(self) -> None:
        evidence = _build(
            (
                _valuation(1, "v1", "1000", T0),
                _valuation(2, "ruin", "-10", T1),
                _valuation(3, "recovery", "1200", T1),
            )
        )

        self.assertEqual(
            evidence.percentage_status,
            PercentageEvidenceStatus.TERMINATED_NONPOSITIVE_EQUITY,
        )
        self.assertIsNone(evidence.maximum_drawdown_fraction)
        self.assertEqual(evidence.ruin_event_id, "ruin")

    def test_top_up_after_ruin_requires_new_campaign(self) -> None:
        with self.assertRaises(ProtectiveRiskEvidenceError) as caught:
            _build(
                (
                    _valuation(1, "v1", "1000", T0),
                    _valuation(2, "ruin", "-10", T1),
                    _flow(3, "topup", "1000", "owner", "portfolio-A", T1),
                    _valuation(4, "v2", "990", T1),
                )
            )

        self.assertEqual(caught.exception.code, "RECAPITALIZATION_AFTER_RUIN")

    def test_restart_readback_is_exact_and_tamper_evident(self) -> None:
        original = _build(
            (
                _valuation(1, "v1", "1000", T0),
                _valuation(2, "v2", "900", T1),
            )
        )
        raw = original.to_dict()

        rebuilt = ProtectiveRiskFlowEvidence.from_dict(raw)
        self.assertEqual(rebuilt, original)

        tampered = copy.deepcopy(raw)
        tampered["maximum_drawdown_absolute"] = "99"
        with self.assertRaises(ProtectiveRiskEvidenceError) as caught:
            ProtectiveRiskFlowEvidence.from_dict(tampered)
        self.assertEqual(caught.exception.code, "EVIDENCE_INTEGRITY")

    def test_reordered_serialized_events_are_detected(self) -> None:
        original = _build(
            (
                _valuation(1, "v1", "1000", T0),
                _valuation(2, "v2", "900", T1),
            )
        )
        raw = original.to_dict()
        raw["events"] = list(reversed(raw["events"]))

        with self.assertRaises(ProtectiveRiskEvidenceError):
            ProtectiveRiskFlowEvidence.from_dict(raw)

    def test_sequence_gap_and_time_regression_fail_closed(self) -> None:
        with self.assertRaises(ProtectiveRiskEvidenceError) as gap:
            _build(
                (
                    _valuation(1, "v1", "1000", T0),
                    _valuation(3, "v2", "900", T1),
                )
            )
        self.assertEqual(gap.exception.code, "FLOW_ORDER_AMBIGUOUS")

        with self.assertRaises(ProtectiveRiskEvidenceError) as time:
            _build(
                (
                    _valuation(1, "v1", "1000", T1),
                    _valuation(2, "v2", "900", T0),
                )
            )
        self.assertEqual(time.exception.code, "FLOW_ORDER_AMBIGUOUS")

    def test_flow_before_initial_valuation_is_ambiguous(self) -> None:
        with self.assertRaises(ProtectiveRiskEvidenceError) as caught:
            _build(
                (
                    _flow(1, "f1", "100", "owner", "portfolio-A", T0),
                    _valuation(2, "v1", "1100", T1),
                )
            )

        self.assertEqual(caught.exception.code, "FLOW_ORDER_AMBIGUOUS")

    def test_unvalued_terminal_flow_is_ambiguous(self) -> None:
        with self.assertRaises(ProtectiveRiskEvidenceError) as caught:
            _build(
                (
                    _valuation(1, "v1", "1000", T0),
                    _flow(2, "f1", "100", "owner", "portfolio-A", T1),
                )
            )

        self.assertEqual(caught.exception.code, "FLOW_ORDER_AMBIGUOUS")

    def test_non_decimal_or_nonfinite_money_is_rejected(self) -> None:
        with self.assertRaises(ProtectiveRiskEvidenceError):
            ProtectiveRiskEvent(
                sequence=1,
                event_id="v1",
                occurred_at=T0,
                kind=RiskEvidenceEventKind.VALUATION,
                raw_equity="1000",
            )
        with self.assertRaises(ProtectiveRiskEvidenceError):
            ProtectiveRiskEvent(
                sequence=1,
                event_id="v1",
                occurred_at=T0,
                kind=RiskEvidenceEventKind.VALUATION,
                raw_equity=Decimal("NaN"),
            )

    def test_decimal_subclasses_cannot_forge_money_validation_or_flow_arithmetic(self) -> None:
        class HostileDecimal(Decimal):
            def is_finite(self):
                return True

            def __le__(self, _other):
                return False

            def as_tuple(self):
                return Decimal("1").as_tuple()

            def copy_negate(self):
                return Decimal("999999")

        with self.assertRaises(ProtectiveRiskEvidenceError) as valuation:
            ProtectiveRiskEvent(
                sequence=1,
                event_id="hostile-valuation",
                occurred_at=T0,
                kind=RiskEvidenceEventKind.VALUATION,
                raw_equity=HostileDecimal("1000"),
            )
        self.assertEqual(valuation.exception.code, "INVALID_EVENT")

        with self.assertRaises(ProtectiveRiskEvidenceError) as flow:
            ProtectiveRiskEvent(
                sequence=1,
                event_id="hostile-flow",
                occurred_at=T0,
                kind=RiskEvidenceEventKind.CAPITAL_FLOW,
                amount=HostileDecimal("-5"),
                source_scope_id="portfolio-A",
                destination_scope_id="owner",
            )
        self.assertEqual(flow.exception.code, "INVALID_EVENT")

    def test_unrepresentable_finite_decimal_fails_closed_with_typed_reason(self) -> None:
        for value in (
            "1e1000000",
            "1e19",
            "1e-19",
            "1" * 49,
            "0e1000000",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ProtectiveRiskEvidenceError) as caught:
                    _valuation(1, "extreme", value, T0)
                self.assertEqual(caught.exception.code, "ARITHMETIC_UNREPRESENTABLE")

    def test_event_count_resource_limit_fails_before_replay(self) -> None:
        one = _valuation(1, "v1", "1000", T0)
        with self.assertRaises(ProtectiveRiskEvidenceError) as caught:
            _build((one,) * 10_001)
        self.assertEqual(caught.exception.code, "EVIDENCE_RESOURCE_LIMIT")

    def test_event_count_bound_does_not_trust_sequence_length(self) -> None:
        class LyingSequence(Sequence):
            def __init__(self, values):
                self._values = values

            def __len__(self):
                return 1

            def __getitem__(self, index):
                return self._values[index]

            def __iter__(self):
                return iter(self._values)

        events = LyingSequence(
            (
                _valuation(1, "v1", "1000", T0),
                _valuation(2, "v2", "900", T1),
                _valuation(3, "v3", "800", T1),
            )
        )
        with patch("autosport.protective_risk_evidence._MAX_EVENTS", 2):
            with self.assertRaises(ProtectiveRiskEvidenceError) as caught:
                _build(events)

        self.assertEqual(caught.exception.code, "EVIDENCE_RESOURCE_LIMIT")

    def test_decimal_result_is_independent_of_hostile_caller_context(self) -> None:
        events = (
            _valuation(1, "v1", "123456789012345.678901234567890123", T0),
            _flow(
                2,
                "deposit",
                "0.000000000000000123",
                "owner",
                "portfolio-A",
                T1,
            ),
            _valuation(3, "v2", "123456789012345.678901234567890123", T1),
            _flow(
                4,
                "withdrawal",
                "0.000000000000000111",
                "portfolio-A",
                "owner",
                T1,
            ),
            _valuation(5, "v3", "123456789012344.678901234567890111", T1),
        )
        baseline = _build(events)

        with localcontext() as context:
            context.prec = 3
            context.rounding = ROUND_UP
            hostile = _build(events)

        self.assertEqual(hostile.to_dict(), baseline.to_dict())
        self.assertEqual(hostile.evidence_sha256, baseline.evidence_sha256)
        self.assertEqual(
            hostile.points[1].flow_adjusted_equity,
            Decimal("123456789012345.678901234567890000"),
        )
        self.assertEqual(
            hostile.points[2].flow_adjusted_equity,
            Decimal("123456789012344.678901234567890099"),
        )

    def test_evidence_cannot_claim_source_or_risk_policy_authority(self) -> None:
        evidence = _build((_valuation(1, "v1", "1000", T0),))
        self.assertFalse(evidence.source_resolved)
        self.assertFalse(evidence.risk_policy_authority)

        raw = evidence.to_dict()
        raw["risk_policy_authority"] = True
        with self.assertRaises(ProtectiveRiskEvidenceError) as caught:
            ProtectiveRiskFlowEvidence.from_dict(raw)
        self.assertEqual(caught.exception.code, "EVIDENCE_INTEGRITY")


if __name__ == "__main__":
    unittest.main()