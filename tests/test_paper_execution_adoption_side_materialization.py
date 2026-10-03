from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


_QUOTE_AT = "2026-09-27T19:50:00+00:00"
_STARTED_AT = "2026-09-27T19:50:00.100000+00:00"
_EXPIRES_AT = "2026-09-27T19:51:00+00:00"


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="side-materialization-test",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="side-materialization-test",
        seed="fixed-side-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="side-action",
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="side-event",
        market_id="side-market",
        selection_id="side-selection",
        side="BACK",
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id="side-quote",
        quote_observed_at=_QUOTE_AT,
        expires_at=_EXPIRES_AT,
    )


def _binding() -> PaperExposureBinding:
    return PaperExposureBinding(
        action_id="side-action",
        sport="soccer",
        bankroll_id="paper-bankroll",
        currency="EUR",
    )


def _prepared(runtime: PaperExecutionAdoptionRuntime) -> PreparedPaperExecution:
    action = _action()
    return runtime._mint_prepared(
        PreparedPaperExecution(
            execution_plan=ExecutionPlan(
                plan_id="side-plan",
                bookmaker_profile_version="paper-profile-v1",
                decision_id="side-decision",
                approval_id="paper-only-no-real-money",
                created_at=_QUOTE_AT,
                actions=(action,),
            ),
            exposure_bindings=(_binding(),),
            intent_evidence_json='{"schema":"side-materialization-test"}',
        )
    )


def _attempt(*, side: str = "BACK", **overrides):
    action = _action()
    values = {
        "action_id": action.action_id,
        "attempt_id": "side-attempt",
        "run_id": "side-run",
        "outcome": PaperAttemptOutcome.ACCEPTED,
        "event_id": action.event_id,
        "market_id": action.market_id,
        "selection_id": action.selection_id,
        "bookmaker_id": action.bookmaker_id,
        "account_id": action.account_id,
        "side": side,
        "execution_odds": Decimal("2.25"),
        "execution_stake": Decimal("10.00"),
        "execution_observed_at": _STARTED_AT,
        "decision_quote_id": action.quote_id,
        "decision_odds": action.requested_odds,
        "requested_stake": action.requested_stake,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _runtime(tmp_path: Path, book: PaperBook | None = None):
    ledger = PaperExecutionLedger(tmp_path / "paper-execution.jsonl")
    runtime = PaperExecutionAdoptionRuntime(
        book=book or PaperBook("100.00"),
        ledger=ledger,
        config=_config(),
        max_quote_age=__import__("datetime").timedelta(seconds=5),
        paper_book_path=tmp_path / "paper-book.json",
    )
    return ledger, runtime


def test_back_attempt_materialization_preserves_side_identity_durably(tmp_path: Path) -> None:
    _ledger, runtime = _runtime(tmp_path)
    attempt = _attempt()
    action = _action()
    binding = _binding()

    ticket = runtime._materialize_attempt(
        attempt=attempt,
        action=action,
        binding=binding,
        decision_id="side-decision",
    )

    assert ticket.legs[0].exchange_side == "back"
    assert ticket.legs[0].quote_key.startswith("exchange-side-v1-")
    assert runtime._ticket_matches_attempt(
        ticket=ticket,
        attempt=attempt,
        action=action,
        binding=binding,
    )

    runtime.book.save(runtime.paper_book_path)
    restored = PaperBook.load(runtime.paper_book_path)
    restored_ticket = next(iter(restored.tickets.values()))
    assert restored_ticket.legs[0].exchange_side == "back"
    assert restored_ticket.legs[0].quote_key == ticket.legs[0].quote_key
    assert runtime._ticket_matches_attempt(
        ticket=restored_ticket,
        attempt=attempt,
        action=action,
        binding=binding,
    )


def test_attempt_side_must_match_back_action_before_book_mutation(tmp_path: Path) -> None:
    _ledger, runtime = _runtime(tmp_path)
    attempt = _attempt(side="LAY")
    action = _action()
    binding = _binding()
    balance_before = runtime.book.balance

    with pytest.raises(PaperExecutionAdoptionError, match="side|BACK"):
        runtime._materialize_attempt(
            attempt=attempt,
            action=action,
            binding=binding,
            decision_id="side-decision",
        )

    assert runtime.book.balance == balance_before
    assert runtime.book.tickets == {}


@pytest.mark.parametrize(
    ("field", "hostile"),
    (
        ("bookmaker_id", "other-venue"),
        ("account_id", "other-account"),
        ("event_id", "other-event"),
        ("market_id", "other-market"),
        ("selection_id", "other-selection"),
    ),
)
def test_attempt_execution_identity_must_match_prepared_action_before_book_mutation(
    tmp_path: Path,
    field: str,
    hostile: str,
) -> None:
    """An action_id alone cannot authorize a different venue/account/selection exposure."""

    _ledger, runtime = _runtime(tmp_path)
    attempt = _attempt(**{field: hostile})
    action = _action()
    binding = _binding()
    balance_before = runtime.book.balance

    with pytest.raises(PaperExecutionAdoptionError, match="attempt|action|identity"):
        runtime._materialize_attempt(
            attempt=attempt,
            action=action,
            binding=binding,
            decision_id="side-decision",
        )

    assert runtime.book.balance == balance_before
    assert runtime.book.tickets == {}


def test_restart_recovery_reconstructs_back_side_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger, runtime = _runtime(tmp_path)
    prepared = _prepared(runtime)
    pre_action_book = PaperBook.load(runtime.paper_book_path)
    attempt = _attempt()

    ticket = runtime._materialize_attempt(
        attempt=attempt,
        action=prepared.execution_plan.actions[0],
        binding=prepared.exposure_bindings[0],
        decision_id=prepared.execution_plan.decision_id,
    )
    runtime.book.save(runtime.paper_book_path)
    assert ticket.legs[0].exchange_side == "back"

    restarted_book = PaperBook.load(runtime.paper_book_path)
    restarted = PaperExecutionAdoptionRuntime(
        book=restarted_book,
        ledger=ledger,
        config=runtime.config,
        max_quote_age=runtime.max_quote_age,
        paper_book_path=runtime.paper_book_path,
    )
    restarted_prepared = restarted._mint_prepared(prepared)
    monkeypatch.setattr(
        ledger,
        "load_run",
        lambda **_kwargs: SimpleNamespace(attempts=(attempt,)),
    )

    restarted.assert_recoverable_book_state(
        pre_action_book=pre_action_book,
        prepared=restarted_prepared,
        trigger_id="side-trigger",
        started_at=_STARTED_AT,
        materialize_exposure=True,
    )

    recovered_ticket = next(iter(restarted.book.tickets.values()))
    assert recovered_ticket.legs[0].exchange_side == "back"
    assert recovered_ticket.legs[0].quote_key.startswith("exchange-side-v1-")
