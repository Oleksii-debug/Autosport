from decimal import Decimal

import pytest

from autosport.domain import MarketEvent
from autosport.market_mirror import MirrorSnapshot
from autosport.registered_strategy_live_feature import (
    RegisteredStrategyLiveFeatureError,
    market_snapshot_sha256,
)


def _snapshot() -> MirrorSnapshot:
    event = MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2.0"),
        observed_ts="2026-09-24T20:00:00Z",
        ingest_ts="2026-09-24T20:00:01Z",
        source_id="provider-1",
        sequence=1,
    )
    return MirrorSnapshot(revision=1, events=(event,))


def test_market_event_to_dict_rebind_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot()
    market_snapshot_sha256(snapshot)
    called = False

    def forged_to_dict(self: MarketEvent) -> dict[str, object]:
        nonlocal called
        called = True
        return {"forged": True}

    monkeypatch.setattr(MarketEvent, "to_dict", forged_to_dict)
    with pytest.raises(
        RegisteredStrategyLiveFeatureError,
        match="canonical MarketEvent dispatch changed",
    ):
        market_snapshot_sha256(snapshot)
    assert called is False


def test_market_event_from_dict_rebind_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot()
    market_snapshot_sha256(snapshot)
    called = False

    def forged_from_dict(cls: type[MarketEvent], raw: dict[str, object]) -> MarketEvent:
        nonlocal called
        called = True
        return snapshot.events[0]

    monkeypatch.setattr(MarketEvent, "from_dict", classmethod(forged_from_dict))
    with pytest.raises(
        RegisteredStrategyLiveFeatureError,
        match="canonical MarketEvent dispatch changed",
    ):
        market_snapshot_sha256(snapshot)
    assert called is False


def test_market_event_quote_key_rebind_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot()
    market_snapshot_sha256(snapshot)
    called = False

    def forged_quote_key(self: MarketEvent) -> str:
        nonlocal called
        called = True
        return "forged-quote"

    monkeypatch.setattr(MarketEvent, "quote_key", property(forged_quote_key))
    with pytest.raises(
        RegisteredStrategyLiveFeatureError,
        match="canonical MarketEvent dispatch changed",
    ):
        market_snapshot_sha256(snapshot)
    assert called is False
