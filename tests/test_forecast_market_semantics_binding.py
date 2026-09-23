from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from autosport.agents import AgentContext
from autosport.domain import MarketEvent
from autosport.forecasting import ForecastRecord
from autosport.paper import PaperBook
from autosport.paper_strategy import Forecast, PaperValueAgent


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


def _record(semantics: str | None) -> ForecastRecord:
    return ForecastRecord(
        quote_key=_event(semantics).quote_key,
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
        forecast_id="forecast-1",
        market_semantics_id=semantics,
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
        "paper-value forecast withheld: forecast market semantics do not "
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
        "paper-value forecast withheld: forecast market semantics do not "
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

    assert PaperValueAgent._forecast_matches_market_semantics(legacy, event)


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
