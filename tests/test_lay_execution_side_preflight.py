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


class _HostileSide(str):
    comparisons = 0

    def __hash__(self) -> int:
        type(self).comparisons += 1
        return super().__hash__()

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)


def _action(action_id: str, *, side: str, odds: str = "5.00", stake: str = "10.00") -> ExecutionAction:
    constructor_side = side if side.strip() == side else "BACK"
    action = ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-exchange",
        account_id="paper-account",
        event_id="event-1",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side=constructor_side,
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )
    if constructor_side != side:
        object.__setattr__(action, "side", side)
    return action


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


def test_mutated_side_subclass_fails_before_dispatch_hooks_or_reservation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        current = _action("hostile-side", side="BACK", odds="2.00", stake="5.00")
        plan = _plan(current)
        _HostileSide.comparisons = 0
        object.__setattr__(current, "side", _HostileSide("LAY"))
        before = len(ledger.events())

        with pytest.raises(
            PaperExecutionStateError,
            match="canonical ExecutionAction side authority",
        ):
            execute_paper_plan(
                plan=plan,
                trigger_id="hostile-side",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )

        assert _HostileSide.comparisons == 0
        assert len(ledger.events()) == before


@pytest.mark.parametrize("side", ("lay", " LAY "))
def test_noncanonical_lay_in_multi_leg_plan_fails_before_reservation(side: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("lay", side=side)
        back = _action("back", side="BACK", odds="2.00", stake="5.00")
        observation, registry = _empirical_acceptance(ledger, lay)
        before = len(ledger.events())

        with pytest.raises(PaperExecutionStateError, match="canonical BACK or LAY"):
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
def test_noncanonical_single_leg_lay_fails_before_run_reservation(side: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("lay", side=side)
        observation, registry = _empirical_acceptance(ledger, lay)
        before = len(ledger.events())

        with pytest.raises(PaperExecutionStateError, match="canonical BACK or LAY"):
            execute_paper_plan(
                plan=_plan(lay),
                trigger_id=f"single-{side!r}",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={lay.action_id: observation},
                evidence_registry=registry,
            )

        assert len(ledger.events()) == before




def test_unsuspended_lay_without_empirical_observation_fails_before_reservation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("lay-no-evidence", side="LAY")
        before = len(ledger.events())

        with pytest.raises(
            PaperExecutionStateError,
            match="requires explicit empirical execution evidence",
        ):
            execute_paper_plan(
                plan=_plan(lay),
                trigger_id="lay-no-evidence",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )

        assert len(ledger.events()) == before


def test_suspended_lay_rejects_mutated_action_identity_before_reservation() -> None:
    class HostileIdentity(str):
        calls = 0

        def __eq__(self, other: object) -> bool:
            type(self).calls += 1
            return super().__eq__(other)

    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("lay-hostile-identity", side="LAY")
        HostileIdentity.calls = 0
        object.__setattr__(
            lay,
            "bookmaker_id",
            HostileIdentity(lay.bookmaker_id),
        )

        with pytest.raises(
            PaperExecutionStateError,
            match="bookmaker_id must retain exact canonical text authority",
        ):
            execute_paper_plan(
                plan=_plan(lay),
                trigger_id="lay-hostile-identity",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids=frozenset({lay.action_id}),
            )

        assert HostileIdentity.calls == 0
        assert ledger.events() == []


def test_suspended_lay_is_durable_no_exposure_without_empirical_fill_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("lay-suspended", side="LAY")

        first = execute_paper_plan(
            plan=_plan(lay),
            trigger_id="lay-suspended",
            config=_config(),
            ledger=ledger,
            started_at=STARTED_AT,
            suspended_action_ids=frozenset({lay.action_id}),
        )
        second = execute_paper_plan(
            plan=_plan(lay),
            trigger_id="lay-suspended",
            config=_config(),
            ledger=ledger,
            started_at=STARTED_AT,
            suspended_action_ids=frozenset({lay.action_id}),
        )

        assert first == second
        assert first.completed is True
        assert len(first.attempts) == 1
        assert first.attempts[0].side == "LAY"
        assert first.attempts[0].outcome is PaperAttemptOutcome.REJECTED
        assert first.attempts[0].suspended is True
        assert first.attempts[0].execution_odds is None
        assert first.attempts[0].execution_stake is None
        assert first.worst_case_exposure == 0
        assert first.recovery_decision.value == "NO_EXPOSURE"
        assert first.pending_action_ids == ()
        assert [event["event_type"] for event in ledger.events()] == [
            "RUN_RESERVED",
            "ATTEMPT_RECORDED",
            "RUN_COMPLETED",
        ]




def test_noncanonical_suspension_container_fails_before_reservation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        back = _action("back-bad-suspension-container", side="BACK", odds="2.00", stake="5.00")
        before = len(ledger.events())

        with pytest.raises(TypeError, match="frozenset"):
            execute_paper_plan(
                plan=_plan(back),
                trigger_id="bad-suspension-container",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids={back.action_id},  # type: ignore[arg-type]
            )

        assert len(ledger.events()) == before


def test_suspension_state_is_bound_to_durable_run_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        back = _action(
            "back-suspension-identity",
            side="BACK",
            odds="2.00",
            stake="5.00",
        )

        first = execute_paper_plan(
            plan=_plan(back),
            trigger_id="back-suspension-identity",
            config=_config(),
            ledger=ledger,
            started_at=STARTED_AT,
            suspended_action_ids=frozenset({back.action_id}),
        )
        assert first.attempts[0].suspended is True
        assert first.attempts[0].outcome is PaperAttemptOutcome.REJECTED

        with pytest.raises(
            PaperExecutionStateError,
            match="execution-control state conflicts with durable reservation",
        ):
            execute_paper_plan(
                plan=_plan(back),
                trigger_id="back-suspension-identity",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )

        events = ledger.events()
        assert [event["event_type"] for event in events] == [
            "RUN_RESERVED",
            "ATTEMPT_RECORDED",
            "RUN_COMPLETED",
        ]
        reserve = events[0]
        assert reserve["payload"]["suspended_action_ids"] == [back.action_id]


@pytest.mark.parametrize("side", ("SIDEWAYS", "back", " BACK "))
def test_unsupported_or_noncanonical_nonlay_side_fails_before_reservation(side: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        current = _action("bad-side", side=side, odds="2.00", stake="5.00")
        before = len(ledger.events())

        with pytest.raises(PaperExecutionStateError, match="canonical BACK or LAY"):
            execute_paper_plan(
                plan=_plan(current),
                trigger_id=f"bad-{side!r}",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )

        assert len(ledger.events()) == before
