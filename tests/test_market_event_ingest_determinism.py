from __future__ import annotations

import json

import pytest

from autosport.domain import MarketEvent
from autosport.replay import ReplayEngine


OBSERVED_TS = "2026-09-12T10:00:00+00:00"


def _event_payload() -> dict[str, object]:
    return {
        "event_id": "tt-demo-1",
        "market_id": "winner",
        "selection_id": "player-a",
        "decimal_odds": "1.62",
        "observed_ts": OBSERVED_TS,
        "source_id": "fixture",
        "sequence": 1,
        "market_type": "winner",
    }


def test_missing_ingest_timestamp_uses_deterministic_observed_timestamp() -> None:
    event = MarketEvent.from_dict(_event_payload())

    assert event.ingest_ts == OBSERVED_TS


def test_explicit_ingest_timestamp_is_preserved() -> None:
    payload = _event_payload()
    payload["ingest_ts"] = "2026-09-12T10:00:05+00:00"

    event = MarketEvent.from_dict(payload)

    assert event.ingest_ts == "2026-09-12T10:00:05+00:00"


def test_replay_jsonl_without_ingest_timestamp_has_stable_dataset_hash(tmp_path) -> None:
    replay_path = tmp_path / "replay.jsonl"
    replay_path.write_text(
        json.dumps(_event_payload(), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    first = ReplayEngine.from_jsonl(replay_path)
    second = ReplayEngine.from_jsonl(replay_path)

    assert first.events[0].ingest_ts == OBSERVED_TS
    assert second.events[0].ingest_ts == OBSERVED_TS
    assert first.dataset_hash == second.dataset_hash


@pytest.mark.parametrize("source_id", [7, True, "", " fixture "])
def test_deserialization_rejects_noncanonical_source_identity(source_id: object) -> None:
    payload = _event_payload()
    payload["source_id"] = source_id

    with pytest.raises(ValueError, match="source_id must be a non-empty trimmed string"):
        MarketEvent.from_dict(payload)


def test_deserialization_preserves_canonical_source_identity_byte_for_byte() -> None:
    payload = _event_payload()
    payload["source_id"] = "parlayapi:table_tennis"

    event = MarketEvent.from_dict(payload)

    assert event.source_id == "parlayapi:table_tennis"
