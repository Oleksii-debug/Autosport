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
_TS = "2026-10-06T09:00:00+00:00"


def _event(semantics: str | None) -> MarketEvent:
    return MarketEvent(
        event_id="event-semantics",
        market_id="market-semantics",
        selection_id="selection-semantics",
        decimal_odds=Decimal("2.10"),
        observed_ts=_TS,
        source_id="provider-semantics",
        sequence=1,
        sport="soccer",
        exchange_side="back",
        market_semantics_id=semantics,
    )


def _record(
    semantics: str | None,
    *,
    quote_key: str | None = None,
) -> ForecastRecord:
    return ForecastRecord(
        quote_key=_event(semantics).quote_key if quote_key is None else quote_key,
        probability=Decimal("0.70"),
        model_id="model-semantics",
        model_version="1",
        strategy_version="paper-value-v1",
        model_training_cutoff_ts="2026-10-06T08:00:00+00:00",
        input_cutoff_ts="2026-10-06T08:59:00+00:00",
        generated_at="2026-10-06T08:59:30+00:00",
        uncertainty=Decimal("0.05"),
        evidence_hashes=("a" * 64,),
        market_snapshot_hash="b" * 64,
        forecast_id="forecast-semantics",
        market_semantics_id=semantics,
    )


def test_same_quote_key_different_market_semantics_cannot_reuse_forecast() -> None:
    event = _event(_S2)
    forecast = _record(_S1)
    context = AgentContext(PaperBook("100"))

    PaperValueAgent(
        {event.quote_key: forecast},
        minimum_expected_profit_per_unit="0",
    ).on_market_event(event, context)

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
    identity = {
        "schema": "autosport.paper-value.open-ticket.v1",
        "replay_run_id": "replay-semantics",
        "agent": PaperValueAgent.name,
        "action": "OPEN_PAPER_VALUE_TICKET",
        "quote_key": legacy_event.quote_key,
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    expected_legacy = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert PaperValueAgent._material_action_id(context, legacy_event) == expected_legacy
    assert PaperValueAgent._legacy_material_action_id(context, s1_event) == expected_legacy
    assert PaperValueAgent._material_action_id(context, s1_event) != expected_legacy
    assert (
        PaperValueAgent._material_action_id(context, s1_event)
        != PaperValueAgent._material_action_id(context, s2_event)
    )
    assert (
        PaperValueAgent._material_quote_identity(s1_event)
        != PaperValueAgent._material_quote_identity(s2_event)
    )


def test_matching_semantics_reaches_execution_authority_boundary() -> None:
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


def test_legacy_forecast_cannot_authorize_semantics_bound_event() -> None:
    event = _event(_S1)
    legacy = Forecast(
        quote_key=event.quote_key,
        probability=Decimal("0.70"),
        model_id="legacy-model",
        as_of_ts="2026-10-06T08:59:00+00:00",
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


def test_legacy_forecast_record_serialization_and_digest_remain_exact() -> None:
    record = _record(None)
    payload = record.to_dict()

    assert "market_semantics_id" not in payload
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert record.canonical_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_concrete_semantics_persists_and_changes_forecast_digest() -> None:
    legacy = _record(None)
    bound = _record(_S1)

    assert bound.to_dict()["market_semantics_id"] == _S1
    assert bound.canonical_hash != legacy.canonical_hash


@pytest.mark.parametrize(
    "identity",
    ("", " Soccer:h2h:v1", "SOCCER:H2H:V1", "soccer|h2h", "unknown", "mixed"),
)
def test_forecast_record_rejects_noncanonical_market_semantics(identity: str) -> None:
    with pytest.raises(ValueError, match="market_semantics_id"):
        _record(identity)
