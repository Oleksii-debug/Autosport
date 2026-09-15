from __future__ import annotations

import json
from decimal import Decimal

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
    [
        "NaN",
        "Infinity",
        "-Infinity",
        float("nan"),
        float("inf"),
        float("-inf"),
        1.62,
        2,
        Decimal("1.62"),
        "1",
        "0",
        "-1",
        None,
        True,
        " 1.62 ",
        "01.62",
        "+1.62",
        "1e2",
    ],
)
def test_deserialization_rejects_noncanonical_or_non_executable_decimal_odds(
    decimal_odds: object,
) -> None:
    payload = _event_payload()
    payload["decimal_odds"] = decimal_odds

    with pytest.raises(ValueError, match="decimal_odds must be a finite decimal greater than 1"):
        MarketEvent.from_dict(payload)


def test_deserialization_rejects_missing_decimal_odds() -> None:
    payload = _event_payload()
    del payload["decimal_odds"]

    with pytest.raises(ValueError, match="decimal_odds must be a finite decimal greater than 1"):
        MarketEvent.from_dict(payload)


def test_deserialization_preserves_exact_high_precision_decimal_text() -> None:
    payload = _event_payload()
    odds_text = "2.1234567890123456789"
    payload["decimal_odds"] = odds_text

    event = MarketEvent.from_dict(payload)

    assert event.decimal_odds == Decimal(odds_text)
    assert event.to_dict()["decimal_odds"] == odds_text


def test_replay_jsonl_rejects_high_precision_numeric_decimal_odds(tmp_path) -> None:
    serialized = json.dumps(_event_payload(), ensure_ascii=False, sort_keys=True)
    canonical_fragment = '"decimal_odds": "1.62"'
    assert canonical_fragment in serialized
    serialized = serialized.replace(
        canonical_fragment,
        '"decimal_odds": 2.1234567890123456789',
    )
    replay_path = tmp_path / "numeric-odds-replay.jsonl"
    replay_path.write_text(serialized + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid replay event schema at line 1") as exc_info:
        ReplayEngine.from_jsonl(replay_path)
    cause = exc_info.value.__cause__
    assert isinstance(cause, ValueError)
    assert str(cause) == "decimal_odds must be a finite decimal greater than 1"


def test_replay_jsonl_preserves_exact_high_precision_decimal_text(tmp_path) -> None:
    payload = _event_payload()
    odds_text = "2.1234567890123456789"
    payload["decimal_odds"] = odds_text
    replay_path = tmp_path / "text-odds-replay.jsonl"
    replay_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    replay = ReplayEngine.from_jsonl(replay_path)

    assert replay.events[0].decimal_odds == Decimal(odds_text)
    assert replay.events[0].to_dict()["decimal_odds"] == odds_text


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


def test_deserialization_preserves_canonical_optional_fields() -> None:
    payload = _event_payload()
    metadata = {
        "period": 2,
        "live": True,
        "probability": 0.5,
        "tags": ["table-tennis", None],
        "nested": {"court": "one"},
    }
    payload.update(
        {
            "market_type": "total",
            "status": "suspended",
            "source_ts": "2026-09-12T09:59:59+00:00",
            "score_state": "2-1",
            "metadata": metadata,
        }
    )

    event = MarketEvent.from_dict(payload)

    assert event.market_type.value == "total"
    assert event.status == "suspended"
    assert event.source_ts == "2026-09-12T09:59:59+00:00"
    assert event.score_state == "2-1"
    assert event.metadata == metadata
    assert event.metadata is not metadata
    assert event.metadata["tags"] is not metadata["tags"]
    assert event.metadata["nested"] is not metadata["nested"]


def test_deserialization_snapshots_nested_metadata_from_caller_mutation() -> None:
    payload = _event_payload()
    nested = {"value": 1}
    items: list[object] = [1, {"name": "verified"}]
    metadata: dict[str, object] = {"nested": nested, "items": items}
    payload["metadata"] = metadata

    event = MarketEvent.from_dict(payload)
    snapshot = event.to_dict()

    nested["value"] = float("nan")
    items.append(("not", "canonical-json"))
    metadata["late"] = {"unvalidated": True}

    assert event.metadata == {
        "nested": {"value": 1},
        "items": [1, {"name": "verified"}],
    }
    assert event.to_dict() == snapshot


@pytest.mark.parametrize(
    "source_ts",
    [
        "2026-09-12T09:59:59Z",
        "2026-09-12T09:59:59+02:30",
        "2026-09-12T09:59:59-04:00",
    ],
)
def test_deserialization_preserves_timezone_aware_source_timestamp(source_ts: str) -> None:
    payload = _event_payload()
    payload["source_ts"] = source_ts

    event = MarketEvent.from_dict(payload)

    assert event.source_ts == source_ts


@pytest.mark.parametrize(
    ("source_ts", "error"),
    [
        ("not-a-time", "source_ts must be valid ISO-8601"),
        ("2026-09-12T09:59:59", "source_ts must be timezone-aware ISO-8601"),
    ],
)
def test_deserialization_rejects_invalid_source_timestamp(
    source_ts: str,
    error: str,
) -> None:
    payload = _event_payload()
    payload["source_ts"] = source_ts

    with pytest.raises(ValueError, match=error):
        MarketEvent.from_dict(payload)


def test_deserialization_preserves_optional_defaults() -> None:
    payload = _event_payload()
    del payload["market_type"]

    event = MarketEvent.from_dict(payload)

    assert event.market_type.value == "other"
    assert event.status == "open"
    assert event.source_ts is None
    assert event.score_state is None
    assert event.metadata == {}


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("market_type", 7),
        ("market_type", True),
        ("market_type", ""),
        ("market_type", " winner "),
        ("status", 7),
        ("status", False),
        ("status", ""),
        ("status", " open "),
        ("source_ts", 7),
        ("source_ts", False),
        ("source_ts", ""),
        ("source_ts", " 2026-09-12T09:59:59+00:00 "),
        ("score_state", 7),
        ("score_state", False),
        ("score_state", ""),
        ("score_state", " 2-1 "),
    ],
)
def test_deserialization_rejects_noncanonical_optional_string_fields(
    field_name: str,
    value: object,
) -> None:
    payload = _event_payload()
    payload[field_name] = value

    with pytest.raises(ValueError, match=rf"{field_name} must be a non-empty trimmed string"):
        MarketEvent.from_dict(payload)


def test_deserialization_rejects_unknown_market_type() -> None:
    payload = _event_payload()
    payload["market_type"] = "moneyline"

    with pytest.raises(ValueError, match="market_type must be a supported market type"):
        MarketEvent.from_dict(payload)


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [["key", "value"]],
        {"tuple": (1, 2)},
        {1: "value"},
        {"nonfinite": float("nan")},
        {"infinite": float("inf")},
    ],
)
def test_deserialization_rejects_noncanonical_metadata(metadata: object) -> None:
    payload = _event_payload()
    payload["metadata"] = metadata

    with pytest.raises(ValueError):
        MarketEvent.from_dict(payload)


def test_deserialization_rejects_lone_surrogate_metadata_value() -> None:
    surrogate = json.loads(r'{"value":"\ud800"}')["value"]
    payload = _event_payload()
    payload["metadata"] = {"label": surrogate}

    with pytest.raises(ValueError, match="UTF-8 encodable"):
        MarketEvent.from_dict(payload)


def test_deserialization_rejects_lone_surrogate_metadata_key() -> None:
    metadata = json.loads(r'{"\ud800":"value"}')
    payload = _event_payload()
    payload["metadata"] = metadata

    with pytest.raises(ValueError, match="UTF-8 encodable"):
        MarketEvent.from_dict(payload)


def test_deserialization_rejects_lone_surrogate_canonical_string_field() -> None:
    surrogate = json.loads(r'{"value":"\ud800"}')["value"]
    payload = _event_payload()
    payload["event_id"] = surrogate

    with pytest.raises(ValueError, match="event_id must be UTF-8 encodable"):
        MarketEvent.from_dict(payload)


def test_deserialization_preserves_valid_unicode_strings() -> None:
    payload = _event_payload()
    payload["event_id"] = "подія-Čadca-42"
    payload["metadata"] = {"мітка": "Žilina ✓"}

    event = MarketEvent.from_dict(payload)

    assert event.event_id == "подія-Čadca-42"
    assert event.metadata == {"мітка": "Žilina ✓"}


def test_deserialization_rejects_cyclic_metadata() -> None:
    metadata: dict[str, object] = {}
    metadata["self"] = metadata
    payload = _event_payload()
    payload["metadata"] = metadata

    with pytest.raises(ValueError, match="cyclic JSON container"):
        MarketEvent.from_dict(payload)


def test_deserialization_rejects_excessive_metadata_nesting() -> None:
    metadata: dict[str, object] = {}
    cursor = metadata
    for index in range(66):
        child: dict[str, object] = {}
        cursor[f"level-{index}"] = child
        cursor = child
    payload = _event_payload()
    payload["metadata"] = metadata

    with pytest.raises(ValueError, match="metadata exceeds maximum JSON nesting depth 64"):
        MarketEvent.from_dict(payload)


def test_replay_jsonl_rejects_coerced_optional_status(tmp_path) -> None:
    payload = _event_payload()
    payload["status"] = 7
    replay_path = tmp_path / "coerced-status-replay.jsonl"
    replay_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid replay event schema at line 1") as exc_info:
        ReplayEngine.from_jsonl(replay_path)
    cause = exc_info.value.__cause__
    assert isinstance(cause, ValueError)
    assert str(cause) == "status must be a non-empty trimmed string"


@pytest.mark.parametrize(
    ("source_ts", "error"),
    [
        ("not-a-time", "source_ts must be valid ISO-8601"),
        ("2026-09-12T09:59:59", "source_ts must be timezone-aware ISO-8601"),
    ],
)
def test_replay_jsonl_rejects_invalid_source_timestamp(
    tmp_path,
    source_ts: str,
    error: str,
) -> None:
    payload = _event_payload()
    payload["source_ts"] = source_ts
    replay_path = tmp_path / "invalid-source-timestamp-replay.jsonl"
    replay_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid replay event schema at line 1") as exc_info:
        ReplayEngine.from_jsonl(replay_path)
    cause = exc_info.value.__cause__
    assert isinstance(cause, ValueError)
    assert str(cause) == error
