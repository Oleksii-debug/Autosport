from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)
from autosport.settlement_execution_basis import (
    SettlementExecutionBasisError,
    derive_settlement_execution_basis,
)


T0 = "2026-09-21T09:00:00+00:00"
T1 = "2026-09-21T09:00:01+00:00"
T2 = "2026-09-21T09:00:02+00:00"
EXP = "2026-09-21T09:10:00+00:00"


def _ledger_with_unfinalized_partial(path: Path) -> RealExecutionLedger:
    ledger = RealExecutionLedger(path)
    action = ExecutionAction(
        action_id="action-partial-live-1",
        bookmaker_id="betfair",
        account_id="account-a",
        event_id="event-a",
        market_id="market-a",
        selection_id="selection-a",
        side="BACK",
        requested_odds="2.00",
        requested_stake="10",
        quote_id="quote-a",
        quote_observed_at=T0,
        expires_at=EXP,
    )
    plan = ExecutionPlan(
        plan_id="plan-partial-live-1",
        bookmaker_profile_version="betfair-profile-v1",
        decision_id="decision-partial-live-1",
        approval_id="approval-partial-live-1",
        created_at=T0,
        actions=(action,),
    )
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-partial-live-1",
        reserved_at=T0,
    )
    ledger.mark_submitted(
        "attempt-partial-live-1",
        submitted_at=T1,
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-partial-live-1",
            external_receipt_id="bet-id-partial-live-1",
            status=AcknowledgementStatus.PARTIAL,
            acknowledged_at=T2,
            accepted_odds="2.10",
            accepted_stake="3",
        )
    )
    return ledger


class SettlementPartialFinalizationTests(unittest.TestCase):
    def test_unfinalized_partial_ack_cannot_mint_settlement_fill_basis(self) -> None:
        """PARTIAL amount is not proof that the provider order can no longer match.

        A direct SUBMITTED -> PARTIAL acknowledgement contains an immediate matched
        fragment but no canonical provider-final / cleared-order authority for the
        remaining requested stake. Settlement must therefore fail closed until a
        provider-terminal realization is resolved.
        """

        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger_with_unfinalized_partial(
                Path(directory) / "execution.jsonl"
            )

            with self.assertRaisesRegex(
                SettlementExecutionBasisError,
                "partial|final|terminal|provider",
            ):
                derive_settlement_execution_basis(
                    ledger,
                    attempt_id="attempt-partial-live-1",
                )


if __name__ == "__main__":
    unittest.main()
