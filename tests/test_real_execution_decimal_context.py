from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
from pathlib import Path

from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionLedgerIntegrityError,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)


_REQUESTED_ODDS = "1.23456789012345678901234567890123456789"
_REQUESTED_STAKE = "12345678901234567890.1234567890123456789"
_ACCEPTED_ODDS = "1.2345678901234567890123456789012345"
_ACCEPTED_STAKE = "9876543210.1234567890123456789012345"


def _plan() -> ExecutionPlan:
    action = ExecutionAction(
        action_id="action-decimal-context",
        bookmaker_id="betfair",
        account_id="account-decimal-context",
        event_id="event-decimal-context",
        market_id="market-decimal-context",
        selection_id="selection-decimal-context",
        side="BACK",
        requested_odds=Decimal(_REQUESTED_ODDS),
        requested_stake=Decimal(_REQUESTED_STAKE),
        quote_id="quote-decimal-context",
        quote_observed_at="2026-09-21T20:00:00+00:00",
        expires_at="2026-09-21T20:05:00+00:00",
    )
    return ExecutionPlan(
        plan_id="plan-decimal-context",
        bookmaker_profile_version="profile-decimal-context-v1",
        decision_id="decision-decimal-context",
        approval_id="approval-decimal-context",
        created_at="2026-09-21T20:00:01+00:00",
        actions=(action,),
    )


class _HostileDecimal(Decimal):
    validation_calls = 0
    comparison_calls = 0
    format_calls = 0

    @classmethod
    def reset_calls(cls) -> None:
        cls.validation_calls = 0
        cls.comparison_calls = 0
        cls.format_calls = 0

    def is_finite(self) -> bool:
        type(self).validation_calls += 1
        return True

    def __le__(self, other: object) -> bool:
        type(self).comparison_calls += 1
        return False

    def __format__(self, format_spec: str) -> str:
        type(self).format_calls += 1
        return "999999"


class RealExecutionDecimalContextTests(unittest.TestCase):
    def test_plan_payload_and_fingerprint_ignore_ambient_decimal_context(self) -> None:
        plan = _plan()

        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_DOWN
            low_payload = plan.to_dict()
            low_fingerprint = plan.fingerprint

        with localcontext() as context:
            context.prec = 80
            context.rounding = ROUND_UP
            high_payload = plan.to_dict()
            high_fingerprint = plan.fingerprint

        self.assertEqual(low_payload, high_payload)
        self.assertEqual(low_fingerprint, high_fingerprint)
        action = low_payload["actions"][0]
        self.assertEqual(action["requested_odds"], _REQUESTED_ODDS)
        self.assertEqual(action["requested_stake"], _REQUESTED_STAKE)

    def test_acknowledgement_payload_preserves_exact_provider_decimals(self) -> None:
        acknowledgement = ExternalAcknowledgement(
            attempt_id="attempt-decimal-context",
            external_receipt_id="receipt-decimal-context",
            status=AcknowledgementStatus.PARTIAL,
            acknowledged_at="2026-09-21T20:00:03+00:00",
            accepted_odds=Decimal(_ACCEPTED_ODDS),
            accepted_stake=Decimal(_ACCEPTED_STAKE),
        )

        with localcontext() as context:
            context.prec = 5
            context.rounding = ROUND_DOWN
            low_payload = acknowledgement.to_dict()

        with localcontext() as context:
            context.prec = 90
            context.rounding = ROUND_UP
            high_payload = acknowledgement.to_dict()

        self.assertEqual(low_payload, high_payload)
        self.assertEqual(low_payload["accepted_odds"], _ACCEPTED_ODDS)
        self.assertEqual(low_payload["accepted_stake"], _ACCEPTED_STAKE)

    def test_equivalent_decimal_scales_keep_one_semantic_plan_identity(self) -> None:
        base = _plan()
        scaled_action = replace(
            base.actions[0],
            requested_odds=Decimal("2.5000"),
            requested_stake=Decimal("10.000"),
        )
        canonical_action = replace(
            base.actions[0],
            requested_odds=Decimal("2.5"),
            requested_stake=Decimal("10"),
        )
        scaled_plan = replace(base, actions=(scaled_action,))
        canonical_plan = replace(base, actions=(canonical_action,))

        self.assertEqual(scaled_plan.to_dict(), canonical_plan.to_dict())
        self.assertEqual(scaled_plan.fingerprint, canonical_plan.fingerprint)
        self.assertEqual(
            scaled_plan.to_dict()["actions"][0]["requested_odds"],
            "2.5",
        )
        self.assertEqual(
            scaled_plan.to_dict()["actions"][0]["requested_stake"],
            "10",
        )

    def test_binary_float_and_bool_action_ingress_fails_closed(self) -> None:
        action = _plan().actions[0]

        for field, value in (
            ("requested_odds", 0.3 - 0.2),
            ("requested_stake", 1.25),
            ("requested_odds", True),
            ("requested_stake", False),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(
                    ValueError, rf"{field} must be a finite Decimal"
                ):
                    replace(action, **{field: value})

    def test_binary_float_acknowledgement_ingress_fails_closed(self) -> None:
        acknowledgement = ExternalAcknowledgement(
            attempt_id="attempt-decimal-ingress",
            external_receipt_id="receipt-decimal-ingress",
            status=AcknowledgementStatus.PARTIAL,
            acknowledged_at="2026-09-21T20:00:03+00:00",
            accepted_odds=Decimal("2.5"),
            accepted_stake=Decimal("10"),
        )

        for field, value in (
            ("accepted_odds", 2.5),
            ("accepted_stake", 0.3 - 0.2),
            ("accepted_odds", True),
            ("accepted_stake", False),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(
                    ValueError, rf"{field} must be a finite Decimal"
                ):
                    replace(acknowledgement, **{field: value})

    def test_hostile_decimal_subclass_action_ingress_fails_before_virtual_hooks(
        self,
    ) -> None:
        action = _plan().actions[0]
        _HostileDecimal.reset_calls()

        with self.assertRaisesRegex(
            ValueError, r"requested_stake must be a finite Decimal"
        ):
            replace(
                action,
                requested_stake=_HostileDecimal("NaN"),
            ).to_dict()

        self.assertEqual(_HostileDecimal.validation_calls, 0)
        self.assertEqual(_HostileDecimal.comparison_calls, 0)
        self.assertEqual(_HostileDecimal.format_calls, 0)

    def test_hostile_decimal_subclass_acknowledgement_ingress_fails_before_virtual_hooks(
        self,
    ) -> None:
        acknowledgement = ExternalAcknowledgement(
            attempt_id="attempt-hostile-decimal",
            external_receipt_id="receipt-hostile-decimal",
            status=AcknowledgementStatus.PARTIAL,
            acknowledged_at="2026-09-21T20:00:03+00:00",
            accepted_odds=Decimal("2.5"),
            accepted_stake=Decimal("10"),
        )
        _HostileDecimal.reset_calls()

        with self.assertRaisesRegex(
            ValueError, r"accepted_odds must be a finite Decimal"
        ):
            replace(
                acknowledgement,
                accepted_odds=_HostileDecimal("NaN"),
            ).to_dict()

        self.assertEqual(_HostileDecimal.validation_calls, 0)
        self.assertEqual(_HostileDecimal.comparison_calls, 0)
        self.assertEqual(_HostileDecimal.format_calls, 0)

    def test_declared_decimal_input_types_remain_supported(self) -> None:
        action = replace(
            _plan().actions[0],
            requested_odds="2.5000",
            requested_stake=10,
        )

        self.assertEqual(action.to_dict()["requested_odds"], "2.5")
        self.assertEqual(action.to_dict()["requested_stake"], "10")

    def test_hash_valid_persisted_float_money_is_rejected_on_reopen(self) -> None:
        plan = _plan()

        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "real-execution.jsonl"
            RealExecutionLedger(ledger_path).reserve_plan(plan)

            record = json.loads(ledger_path.read_text(encoding="utf-8").strip())
            event = record["event"]
            durable_plan = event["payload"]["plan"]
            durable_plan["actions"][0]["requested_stake"] = 0.1

            canonical_plan = json.dumps(
                durable_plan,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            event["payload"]["plan_fingerprint"] = hashlib.sha256(
                canonical_plan.encode("utf-8")
            ).hexdigest()
            canonical_event = json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            record["sha256"] = hashlib.sha256(
                canonical_event.encode("utf-8")
            ).hexdigest()
            ledger_path.write_text(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ExecutionLedgerIntegrityError):
                RealExecutionLedger(ledger_path).verify_integrity()

    def test_low_precision_persistence_reopens_idempotently_at_high_precision(self) -> None:
        plan = _plan()

        with tempfile.TemporaryDirectory() as temporary_directory:
            ledger_path = Path(temporary_directory) / "real-execution.jsonl"

            with localcontext() as context:
                context.prec = 6
                context.rounding = ROUND_DOWN
                ledger = RealExecutionLedger(ledger_path)
                persisted_fingerprint = ledger.reserve_plan(plan)

            original_bytes = ledger_path.read_bytes()
            record = json.loads(original_bytes.decode("utf-8").splitlines()[0])
            durable_payload = record["event"]["payload"]
            durable_action = durable_payload["plan"]["actions"][0]

            self.assertEqual(durable_action["requested_odds"], _REQUESTED_ODDS)
            self.assertEqual(durable_action["requested_stake"], _REQUESTED_STAKE)

            with localcontext() as context:
                context.prec = 80
                context.rounding = ROUND_UP
                reopened = RealExecutionLedger(ledger_path)
                self.assertEqual(reopened.verify_integrity(), 1)
                self.assertEqual(reopened.reserve_plan(plan), persisted_fingerprint)

            self.assertEqual(ledger_path.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
