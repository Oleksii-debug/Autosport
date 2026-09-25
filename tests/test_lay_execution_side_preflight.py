from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
    execute_paper_plan,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-20T03:00:00+00:00"
STARTED_AT = "2026-09-20T03:00:00.100000+00:00"
EXPIRES_AT = "2026-09-20T03:01:00+00:00"


def _action(action_id: str, *, side: str, odds: str = "5.00", stake: str = "10.00") -> ExecutionAction:
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
        plan_id="lay-side-preflight-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="decision-1",
        approval_id="paper-only",
        created_at=QUOTE_AT,
        actions=tuple(actions),
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="lay-side-preflight-1",
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


def _empirical_acceptance(
    ledger: PaperExecutionLedger,
    action: ExecutionAction,
):
    record = PaperExecutionEvidenceRecord(
        action_id=action.action_id,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        side=action.side,
        quote_id=action.quote_id,
        outcome=PaperAttemptOutcome.ACCEPTED,
        observed_at="2026-09-20T03:00:00.250000+00:00",
        evidence_grade=EvidenceGrade.EMPIRICAL,
        evidence_source="captured-paper-observation-v1",
        accepted_odds="5.00",
        accepted_stake="10.00",
        reason="observed accepted",
    )
    registry = PaperExecutionEvidenceRegistry(ledger)
    registry.register(record)
    return record.as_observation(), registry


@pytest.mark.parametrize("side", ("lay", " LAY "))
def test_noncanonical_lay_in_multi_leg_plan_fails_before_reservation(side: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("lay", side=side)
        back = _action("back", side="BACK", odds="2.00", stake="5.00")
        observation, registry = _empirical_acceptance(ledger, lay)
        before = len(ledger.events())

        with pytest.raises(PaperExecutionStateError, match="single-leg"):
            execute_paper_plan(
                plan=_plan(lay, back),
                trigger_id=f"mixed-{side!r}",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={lay.action_id: observation},
                evidence_registry=registry,
            )

        assert len(ledger.events()) == before


@pytest.mark.parametrize("side", ("lay", " LAY "))
def test_noncanonical_single_leg_empirical_lay_uses_same_liability_classification(side: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("lay", side=side)
        observation, registry = _empirical_acceptance(ledger, lay)

        result = execute_paper_plan(
            plan=_plan(lay),
            trigger_id=f"single-{side!r}",
            config=_config(),
            ledger=ledger,
            started_at=STARTED_AT,
            observations={lay.action_id: observation},
            evidence_registry=registry,
        )

        assert result.worst_case_exposure == 40
        assert result.attempts[0].side == side
