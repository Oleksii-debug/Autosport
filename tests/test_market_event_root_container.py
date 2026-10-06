from __future__ import annotations

import pytest

from autosport.domain import MarketEvent


class _ExplosiveRootDict(dict):
    def get(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("root dict subclass get() must not be invoked")


def test_market_event_rejects_root_dict_subclass_before_virtual_get() -> None:
    payload = _ExplosiveRootDict(
        {
            "event_id": "tt-demo-1",
            "market_id": "winner",
            "selection_id": "player-a",
            "decimal_odds": "1.62",
            "observed_ts": "2026-09-12T10:00:00+00:00",
            "source_id": "fixture",
            "sequence": 1,
            "market_type": "winner",
        }
    )

    with pytest.raises(ValueError, match="serialized market event must be a JSON object"):
        MarketEvent.from_dict(payload)
