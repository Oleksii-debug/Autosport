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


@pytest.mark.parametrize(
    "field_name",
    ["event_id", "market_id", "selection_id", "observed_ts", "source_id"],
)
@pytest.mark.parametrize("value", [7, True, "", " padded "])
def test_deserialization_rejects_noncanonical_required_string_fields(
    field_name: str,
    value: object,
) -> None:
    payload = _event_payload()
    payload[field_name] = value

    with pytest.raises(ValueError, match=rf"{field_name} must be a non-empty trimmed string"):
        MarketEvent.from_dict(payload)


def test_deserialization_rejects_missing_source_identity() -> None:
    payload = _event_payload()
    del payload["source_id"]

    with pytest.raises(ValueError, match="source_id must be a non-empty trimmed string"):
        MarketEvent.from_dict(payload)


@pytest.mark.parametrize("sequence", [True, 1.0, "1", None])
def test_deserialization_rejects_coerced_sequence_identity(sequence: object) -> None:
    payload = _event_payload()
    payload["sequence"] = sequence

    with pytest.raises(ValueError, match="sequence must be a non-boolean int"):
        MarketEvent.from_dict(payload)


@pytest.mark.parametrize(
    "decimal_odds",
    ["NaN", "Infinity", "-Infinity", float("nan"), float("inf"), float("-inf"), "1", "0", "-1", None, True],
)
def test_deserialization_rejects_non_executable_decimal_odds(decimal_odds: object) -> None:
    payload = _event_payload()
    payload["decimal_odds"] = decimal_odds

    with pytest.raises(ValueError, match="decimal_odds must be a finite decimal greater than 1"):
        MarketEvent.from_dict(payload)


def test_deserialization_rejects_missing_decimal_odds() -> None:
    payload = _event_payload()
    del payload["decimal_odds"]

    with pytest.raises(ValueError, match="decimal_odds must be a finite decimal greater than 1"):
        MarketEvent.from_dict(payload)


def test_replay_jsonl_rejects_nonfinite_serialized_decimal_odds(tmp_path) -> None:
    payload = _event_payload()
    payload["decimal_odds"] = float("nan")
    replay_path = tmp_path / "nonfinite-replay.jsonl"
    replay_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid replay JSONL at line 1") as exc_info:
        ReplayEngine.from_jsonl(replay_path)
    cause = exc_info.value.__cause__
    assert isinstance(cause, ValueError)
    assert str(cause) == "non-finite JSON constant: NaN"


@pytest.mark.parametrize("ingest_ts", [7, True, "", " 2026-09-12T10:00:05+00:00 "])
def test_deserialization_rejects_noncanonical_explicit_ingest_timestamp(ingest_ts: object) -> None:
    payload = _event_payload()
    payload["ingest_ts"] = ingest_ts

    with pytest.raises(ValueError, match="ingest_ts must be a non-empty trimmed string"):
        MarketEvent.from_dict(payload)


def test_deserialization_preserves_canonical_identity_bytes() -> None:
    payload = _event_payload()
    payload.update(
        {
            "event_id": "parlayapi:table_tennis:event-42",
            "market_id": "parlayapi:table_tennis:book:h2h:-1.5",
            "selection_id": "parlayapi:table_tennis:player-a",
            "source_id": "parlayapi:table_tennis",
            "sequence": 42,
        }
    )

    event = MarketEvent.from_dict(payload)

    assert event.event_id == payload["event_id"]
    assert event.market_id == payload["market_id"]
    assert event.selection_id == payload["selection_id"]
    assert event.source_id == payload["source_id"]
    assert event.sequence == 42
