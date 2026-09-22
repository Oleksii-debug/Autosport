from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betfair_live_capital_at_risk import (
    BetfairLiveCapitalAtRiskError,
    BetfairLiveCapitalAtRiskEvidence,
    BetfairLiveCapitalAtRiskReason,
    BetfairLiveCapitalAtRiskTruth,
)
from autosport.real_execution_ledger import AttemptState


def test_class_method_rebinding_cannot_authorize_caller_exact_risk(monkeypatch) -> None:
    """Authority must not be replaceable through the public evidence class."""

    forged = BetfairLiveCapitalAtRiskEvidence(
        truth=BetfairLiveCapitalAtRiskTruth.EXACT,
        reason=BetfairLiveCapitalAtRiskReason.CURRENT_ORDER,
        capital_at_risk=Decimal("500.00"),
        plan_id="caller-plan",
        attempt_id="caller-attempt",
        attempt_state=AttemptState.SUBMITTED,
        action_id="caller-action",
        provider_order_ref="caller-provider-ref",
        bet_id=None,
        readback_observed_at="2026-09-22T14:49:59Z",
        provider_row_observed_at="2026-09-22T14:49:59Z",
        readback_request_scope_sha256="1" * 64,
        readback_evidence_sha256="2" * 64,
        ledger_snapshot_sha256="3" * 64,
    )

    # The current module installs its positive authority verifier by replacing
    # this public class method. Ordinary process code can replace it again.
    monkeypatch.setattr(
        BetfairLiveCapitalAtRiskEvidence,
        "assert_authoritative",
        lambda self: None,
    )

    with pytest.raises(BetfairLiveCapitalAtRiskError):
        forged.assert_authoritative()
