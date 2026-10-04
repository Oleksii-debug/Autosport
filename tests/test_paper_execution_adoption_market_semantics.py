from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.domain import MarketEvent, TicketLeg
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)


_QUOTE_AT = "2026-10-05T00:00:00+00:00"
_STARTED_AT = "2026-10-05T00:00:00.100000+00:00"
_S1 = "soccer:h2h:v1"
_S2 = "soccer:h2h:v2"


def _event(semantics: str | None = _S1) -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2.50"),
        observed_ts=_QUOTE_AT,
        source_id="paper-venue",
        sequence=1,
        source_ts=_QUOTE_AT,
        ingest_ts=_QUOTE_AT,
        sport="soccer",
        exchange_side="back",
        market_semantics_id=semantics,
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="semantics-test",
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


def _runtime(tmp_path: Path) -> PaperExecutionAdoptionRuntime:
    return PaperExecutionAdoptionRuntime(
        book=PaperBook("100.00"),
        ledger=PaperExecutionLedger(tmp_path / "paper-execution.jsonl"),
        config=_config(),
        max_quote_age=timedelta(seconds=5),
        paper_book_path=tmp_path / "paper-book.json",
    )


def _prepared(
    runtime: PaperExecutionAdoptionRuntime,
    semantics: str | None = _S1,
):
    return runtime.prepare_paper_value_action(
        event=_event(semantics),
        stake=Decimal("10.00"),
        decision_id="decision-1",
        account_id="paper-account",
        bankroll_id="paper-bankroll",
        currency="EUR",
    )


@pytest.mark.parametrize(
    "identity",
    ["", " Soccer:h2h:v1", "SOCCER:H2H:V1", "unknown", "mixed", "unspecified"],
)
def test_exposure_binding_rejects_noncanonical_market_semantics(identity: str) -> None:
    with pytest.raises(ValueError, match="market_semantics_id"):
        PaperExposureBinding(
            action_id="action-1",
            sport="soccer",
            bankroll_id="paper-bankroll",
            currency="EUR",
            market_semantics_id=identity,
        )


def test_leg_quote_identity_rejects_semantics_substitution() -> None:
    event = _event(_S2)
    leg = TicketLeg(
        event.event_id,
        event.market_id,
        event.selection_id,
        event.decimal_odds,
        sport=event.sport,
        exchange_side=event.exchange_side,
        market_semantics_id=_S1,
    )
    assert leg.quote_key == event.quote_key

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="ticket leg identity does not match canonical execution quote",
    ):
        PaperExecutionAdoptionRuntime._require_leg_quote_identity(leg, event)


def test_paper_value_prepared_binding_carries_market_semantics(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    prepared = _prepared(runtime)

    assert prepared.exposure_bindings[0].market_semantics_id == _S1


def test_paper_value_action_identity_changes_with_market_semantics(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    first = _prepared(runtime, _S1)
    second = _prepared(runtime, _S2)

    first_action = first.execution_plan.actions[0]
    second_action = second.execution_plan.actions[0]
    assert first_action.quote_id != second_action.quote_id
    assert first_action.action_id != second_action.action_id
    assert first.execution_plan.plan_id != second.execution_plan.plan_id


def test_paper_value_materialization_preserves_market_semantics(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime)

    result = runtime.execute(
        prepared=prepared,
        trigger_id="trigger-1",
        started_at=_STARTED_AT,
        materialize_exposure=True,
    )

    assert len(result.ticket_ids) == 1
    ticket = runtime.book.tickets[result.ticket_ids[0]]
    assert ticket.legs[0].market_semantics_id == _S1

    attempt = result.run.attempts[0]
    action = prepared.execution_plan.actions[0]
    binding = prepared.exposure_bindings[0]
    assert runtime._ticket_matches_attempt(
        ticket=ticket,
        attempt=attempt,
        action=action,
        binding=binding,
    )

    object.__setattr__(ticket.legs[0], "market_semantics_id", _S2)
    assert not runtime._ticket_matches_attempt(
        ticket=ticket,
        attempt=attempt,
        action=action,
        binding=binding,
    )


def test_restart_materialization_preserves_same_semantics_identity(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime)
    first = runtime.execute(
        prepared=prepared,
        trigger_id="trigger-restart",
        started_at=_STARTED_AT,
        materialize_exposure=False,
    )
    assert first.ticket_ids == ()

    book_path = tmp_path / "paper-book.json"
    reloaded = PaperBook.load(book_path)
    restarted = PaperExecutionAdoptionRuntime(
        book=reloaded,
        ledger=runtime.ledger,
        config=runtime.config,
        max_quote_age=runtime.max_quote_age,
        paper_book_path=book_path,
    )
    restarted_prepared = _prepared(restarted)
    resumed = restarted.execute(
        prepared=restarted_prepared,
        trigger_id="trigger-restart",
        started_at=_STARTED_AT,
        materialize_exposure=True,
    )

    assert len(resumed.ticket_ids) == 1
    ticket = reloaded.tickets[resumed.ticket_ids[0]]
    assert ticket.legs[0].market_semantics_id == _S1
