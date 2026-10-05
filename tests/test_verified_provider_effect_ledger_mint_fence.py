from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    EventType,
    ExecutionAction,
    ExecutionLedgerIntegrityError,
    ExecutionPlan,
    RealExecutionLedger,
    VerifiedProviderEffectBindingView,
)
from autosport.supervised_provider_evidence import VerifiedProviderEffectEvidence


QUOTE = "2026-10-05T05:00:00+00:00"
RESERVED = "2026-10-05T05:00:01+00:00"
SUBMITTED = "2026-10-05T05:00:02+00:00"
OBSERVED = "2026-10-05T05:00:03+00:00"


def _submitted_ledger(tmp_path) -> RealExecutionLedger:
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="provider-1",
        account_id="account-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="1",
        side="BACK",
        requested_odds=Decimal("2.10"),
        requested_stake=Decimal("10.00"),
        quote_id="quote-1",
        quote_observed_at=QUOTE,
        expires_at="2026-10-05T05:01:00+00:00",
    )
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=QUOTE,
        actions=(action,),
    )
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id="plan-1",
        action_id="action-1",
        attempt_id="attempt-1",
        reserved_at=RESERVED,
    )
    ledger.mark_submitted("attempt-1", submitted_at=SUBMITTED)
    return ledger


def _caller_constructed_effect() -> VerifiedProviderEffectEvidence:
    return VerifiedProviderEffectEvidence(
        bookmaker_id="provider-1",
        account_id="account-1",
        action_id="action-1",
        adapter_id="caller-forged-adapter",
        adapter_version="caller-forged-version",
        profile_version=1,
        event_id="event-1",
        market_id="market-1",
        selection_id="1",
        external_receipt_id="receipt-1",
        observed_at=OBSERVED,
        source_payload_sha256="a" * 64,
        status=AcknowledgementStatus.ACCEPTED,
        accepted_odds=Decimal("2.08"),
        accepted_stake=Decimal("10.00"),
        evidence_id="b" * 64,
        provider_order_ref=None,
    )


def _raw_effect_payload() -> dict[str, object]:
    return VerifiedProviderEffectBindingView(
        evidence_id="b" * 64,
        observed_at=OBSERVED,
        source_payload_sha256="a" * 64,
        external_receipt_id="receipt-1",
        status=AcknowledgementStatus.ACCEPTED,
        accepted_odds=Decimal("2.08"),
        accepted_stake=Decimal("10.00"),
        provider_order_ref=None,
    ).to_dict()


def test_caller_constructed_verified_effect_cannot_open_writer_grant(tmp_path) -> None:
    ledger = _submitted_ledger(tmp_path)
    before = ledger.verified_snapshot()

    with pytest.raises(
        ExecutionLedgerIntegrityError,
        match="verified provider effect evidence is not authoritative",
    ):
        ledger._bind_verified_provider_effect_evidence(
            attempt_id="attempt-1",
            evidence=_caller_constructed_effect(),
        )

    after = ledger.verified_snapshot()
    assert after.sha256 == before.sha256
    assert after.event_count == before.event_count


def test_failed_writer_verification_does_not_leak_raw_append_grant(tmp_path) -> None:
    ledger = _submitted_ledger(tmp_path)

    with pytest.raises(ExecutionLedgerIntegrityError):
        ledger._bind_verified_provider_effect_evidence(
            attempt_id="attempt-1",
            evidence=_caller_constructed_effect(),
        )

    before = ledger.verified_snapshot()
    with pytest.raises(
        ExecutionLedgerIntegrityError,
        match="requires canonical origin-verifying ledger writer",
    ):
        ledger._append(
            EventType.VERIFIED_PROVIDER_EFFECT_BOUND,
            "plan-1",
            "action-1",
            "attempt-1",
            _raw_effect_payload(),
        )
    after = ledger.verified_snapshot()

    assert after.sha256 == before.sha256
    assert after.event_count == before.event_count
