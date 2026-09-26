from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from autosport.agents import AgentContext
from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.forecasting import ForecastRecord
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.paper_strategy import (
    Forecast,
    PaperDecisionReconciliationRequired,
    PaperValueAgent,
)
from autosport.risk import PaperRiskPolicy


_S1 = "soccer:h2h:v1"
_S2 = "soccer:h2h:v2"
_TS = "2026-09-23T12:00:00+00:00"


def _event(semantics: str | None) -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2.10"),
        observed_ts=_TS,
        source_id="provider-1",
        sequence=1,
        sport="soccer",
        exchange_side="back",
        market_semantics_id=semantics,
    )


def _record(
    semantics: str | None,
    *,
    quote_key: str | None = None,
    forecast_id: str = "forecast-1",
) -> ForecastRecord:
    return ForecastRecord(
        quote_key=_event(semantics).quote_key if quote_key is None else quote_key,
        probability=Decimal("0.70"),
        model_id="model-1",
        model_version="1",
        strategy_version="paper-value-v1",
        model_training_cutoff_ts="2026-09-23T10:00:00+00:00",
        input_cutoff_ts="2026-09-23T11:59:00+00:00",
        generated_at="2026-09-23T11:59:30+00:00",
        uncertainty=Decimal("0.05"),
        evidence_hashes=("a" * 64,),
        market_snapshot_hash="b" * 64,
        provenance={"dataset": "frozen-1"},
        forecast_id=forecast_id,
        market_semantics_id=semantics,
    )


def _execution_config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="market-semantics-binding-test",
        model_version="1",
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


def _runtime(tmp_path, book: PaperBook) -> PaperExecutionAdoptionRuntime:
    return PaperExecutionAdoptionRuntime(
        book=book,
        ledger=PaperExecutionLedger(tmp_path / "paper-execution.jsonl"),
        config=_execution_config(),
        max_quote_age=timedelta(seconds=5),
        paper_book_path=tmp_path / "paper-book.json",
    )


def test_same_quote_key_different_market_semantics_cannot_reuse_forecast() -> None:
    first_event = _event(_S1)
    second_event = _event(_S2)
    forecast = _record(_S1)

    assert first_event.quote_key == second_event.quote_key
    assert forecast.quote_key == second_event.quote_key

    context = AgentContext(PaperBook("100"))
    PaperValueAgent(
        {second_event.quote_key: forecast},
        minimum_expected_profit_per_unit="0",
    ).on_market_event(second_event, context)

    assert context.notes == [
        "paper-value forecast withheld: forecast market identity does not "
        "match the canonical market event"
    ]
    assert not context.paper_book.tickets






def test_material_action_identity_binds_semantics_and_preserves_legacy_digest() -> None:
    context = AgentContext(PaperBook("100"), replay_run_id="replay-semantics")
    legacy_event = _event(None)
    s1_event = _event(_S1)
    s2_event = _event(_S2)

    legacy_identity = {
        "schema": "autosport.paper-value.open-ticket.v1",
        "replay_run_id": "replay-semantics",
        "agent": PaperValueAgent.name,
        "action": "OPEN_PAPER_VALUE_TICKET",
        "quote_key": legacy_event.quote_key,
    }
    canonical = json.dumps(
        legacy_identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    expected_legacy = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert PaperValueAgent._material_action_id(context, legacy_event) == expected_legacy
    assert (
        PaperValueAgent._legacy_material_action_id(context, s1_event)
        == expected_legacy
    )
    assert s1_event.quote_key == s2_event.quote_key
    assert (
        PaperValueAgent._material_action_id(context, s1_event)
        != PaperValueAgent._material_action_id(context, s2_event)
    )
    assert (
        PaperValueAgent._material_quote_identity(s1_event)
        != PaperValueAgent._material_quote_identity(s2_event)
    )


def test_successful_s1_does_not_suppress_same_quote_s2_before_identity_gate(
    tmp_path,
) -> None:
    s1_event = _event(_S1)
    s2_event = _event(_S2)
    book = PaperBook("100")
    runtime = _runtime(tmp_path, book)
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    agent = PaperValueAgent(
        {
            s1_event.quote_key: _record(
                _S1,
                forecast_id="forecast-s1",
            )
        },
        stake=Decimal("1"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=PaperRiskPolicy(max_ticket_fraction=Decimal("0.05")),
    )
    context = AgentContext(
        book,
        replay_run_id="replay-semantics",
        decision_ledger=ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-1", "account-1"),),
    )

    agent.on_market_event(s1_event, context)

    records = tuple(ledger.verified_records())
    assert len(records) == 1
    assert records[0].payload["market_semantics_id"] == _S1
    s1_action_id = records[0].payload["material_action_id"]
    assert s1_action_id == PaperValueAgent._material_action_id(context, s1_event)

    # Remove execution authority after the proven S1 commit. If S2 were still
    # suppressed by the old quote_key-only _acted entry, this call would be a
    # silent no-op. Reaching the runtime fence proves S2 owns a distinct
    # in-process material identity.
    context.paper_execution = None
    context.notes.clear()
    agent.forecasts[s2_event.quote_key] = _record(
        _S2,
        forecast_id="forecast-s2",
    )

    agent.on_market_event(s2_event, context)

    assert context.notes == [
        "paper-value material action withheld: canonical #623 execution "
        "runtime/account authority is unavailable"
    ]
    assert PaperValueAgent._material_action_id(context, s2_event) != s1_action_id


def test_semantics_bound_event_fails_closed_on_prebinding_legacy_action(
    tmp_path,
) -> None:
    event = _event(_S1)
    book = PaperBook("100")
    runtime = _runtime(tmp_path, book)
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    context = AgentContext(
        book,
        replay_run_id="replay-semantics",
        decision_ledger=ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-1", "account-1"),),
    )
    legacy_id = PaperValueAgent._legacy_material_action_id(context, event)
    ledger.append(
        DecisionRecord(
            replay_run_id=context.replay_run_id,
            agent=PaperValueAgent.name,
            observed_ts=event.observed_ts,
            action="OPEN_PAPER_VALUE_TICKET",
            payload={
                "quote_key": event.quote_key,
                "material_action_id": legacy_id,
            },
            context_hash=context.market_context_hash(),
            decision_id=legacy_id,
            recorded_at=event.observed_ts,
        )
    )
    agent = PaperValueAgent(
        {event.quote_key: _record(_S1)},
        stake=Decimal("1"),
        minimum_expected_profit_per_unit=Decimal("0"),
    )

    with pytest.raises(
        PaperDecisionReconciliationRequired,
        match="legacy paper-value material action lacks exact market-semantics identity",
    ):
        agent.on_market_event(event, context)

    assert not book.tickets
    assert not runtime.ledger.events()

def test_dictionary_key_cannot_launder_wrong_forecast_quote_identity() -> None:
    event = _event(_S1)
    wrong_quote_forecast = _record(_S1, quote_key="different|market|selection")
    context = AgentContext(PaperBook("100"))

    PaperValueAgent(
        {event.quote_key: wrong_quote_forecast},
        minimum_expected_profit_per_unit="0",
    ).on_market_event(event, context)

    assert context.notes == [
        "paper-value forecast withheld: forecast market identity does not "
        "match the canonical market event"
    ]
    assert not context.paper_book.tickets


def test_matching_market_semantics_passes_identity_gate() -> None:
    event = _event(_S1)
    context = AgentContext(PaperBook("100"))

    PaperValueAgent(
        {event.quote_key: _record(_S1)},
        minimum_expected_profit_per_unit="0",
    ).on_market_event(event, context)

    assert context.notes == [
        "paper-value material action withheld: canonical #623 execution "
        "runtime/account authority is unavailable"
    ]
    assert not context.paper_book.tickets


def test_legacy_forecast_cannot_authorize_semantics_bound_event() -> None:
    event = _event(_S1)
    legacy = Forecast(
        quote_key=event.quote_key,
        probability=Decimal("0.70"),
        model_id="legacy-model",
        as_of_ts="2026-09-23T11:59:00+00:00",
    )
    context = AgentContext(PaperBook("100"))

    PaperValueAgent(
        {event.quote_key: legacy},
        minimum_expected_profit_per_unit="0",
    ).on_market_event(event, context)

    assert context.notes == [
        "paper-value forecast withheld: forecast market identity does not "
        "match the canonical market event"
    ]


def test_legacy_forecast_still_matches_legacy_event() -> None:
    event = _event(None)
    legacy = Forecast(
        quote_key=event.quote_key,
        probability=Decimal("0.70"),
        model_id="legacy-model",
        as_of_ts="2026-09-23T11:59:00+00:00",
    )

    assert PaperValueAgent._forecast_matches_market_identity(legacy, event)


def test_no_semantics_forecast_preserves_exact_legacy_serialization_and_digest() -> None:
    record = _record(None)
    expected = {
        "forecast_id": "forecast-1",
        "quote_key": record.quote_key,
        "probability": "0.70",
        "model_id": "model-1",
        "model_version": "1",
        "strategy_version": "paper-value-v1",
        "model_training_cutoff_ts": "2026-09-23T10:00:00+00:00",
        "input_cutoff_ts": "2026-09-23T11:59:00+00:00",
        "generated_at": "2026-09-23T11:59:30+00:00",
        "uncertainty": "0.05",
        "evidence_hashes": ["a" * 64],
        "market_snapshot_hash": "b" * 64,
        "provenance": {"dataset": "frozen-1"},
    }
    canonical = json.dumps(
        expected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    assert record.to_dict() == expected
    assert record.canonical_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_concrete_semantics_is_persisted_and_changes_forecast_digest() -> None:
    legacy = _record(None)
    bound = _record(_S1)

    assert bound.to_dict()["market_semantics_id"] == _S1
    assert bound.canonical_hash != legacy.canonical_hash


@pytest.mark.parametrize(
    "identity",
    ["", " Soccer:h2h:v1", "SOCCER:H2H:V1", "soccer|h2h", "unknown", "mixed", "unspecified"],
)
def test_forecast_record_rejects_noncanonical_market_semantics(identity: str) -> None:
    with pytest.raises(ValueError, match="market_semantics_id"):
        ForecastRecord(
            quote_key=_event(None).quote_key,
            probability=Decimal("0.70"),
            model_id="model-1",
            model_version="1",
            strategy_version="paper-value-v1",
            model_training_cutoff_ts="2026-09-23T10:00:00+00:00",
            input_cutoff_ts="2026-09-23T11:59:00+00:00",
            generated_at="2026-09-23T11:59:30+00:00",
            forecast_id="forecast-invalid",
            market_semantics_id=identity,
        )
