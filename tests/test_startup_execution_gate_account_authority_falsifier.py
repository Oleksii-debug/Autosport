from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.real_execution_ledger import RealExecutionLedger
from autosport.startup_execution_gate import (
    AccountExposureSnapshot,
    ExecutionAccount,
    StartupExecutionGate,
)


class StartupExecutionGateAccountAuthorityFalsifierTests(unittest.TestCase):
    def _evaluate_with_caller_snapshot(
        self,
        *,
        observed_at: str,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
            account = ExecutionAccount("betfair", "caller-selected-account")
            forged = AccountExposureSnapshot(
                bookmaker_id=account.bookmaker_id,
                account_id=account.account_id,
                currency="EUR",
                available_bankroll="999999999.99",
                open_exposure="0",
                observed_at=observed_at,
            )
            return StartupExecutionGate(
                ledger=ledger,
                expected_accounts=(account,),
                reconcile_unresolved=None,
                rebuild_account_state=lambda: (forged,),
            ).evaluate()

    def test_caller_minted_account_snapshot_cannot_enable_execution(self) -> None:
        status = self._evaluate_with_caller_snapshot(
            observed_at="2026-09-21T14:30:00+00:00"
        )

        self.assertFalse(
            status.execution_enabled,
            "a plain caller-constructed account snapshot must not mint "
            "provider/account-state authority for execution startup",
        )

    def test_stale_caller_snapshot_cannot_enable_execution(self) -> None:
        status = self._evaluate_with_caller_snapshot(
            observed_at="2000-01-01T00:00:00+00:00"
        )

        self.assertFalse(
            status.execution_enabled,
            "caller-selected historical observation time must not satisfy "
            "startup account-state freshness/authority",
        )


if __name__ == "__main__":
    unittest.main()
