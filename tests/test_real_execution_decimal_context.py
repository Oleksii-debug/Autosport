from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
from pathlib import Path

from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
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
