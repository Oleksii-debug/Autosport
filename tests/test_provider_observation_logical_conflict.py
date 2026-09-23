from __future__ import annotations

import json

import pytest

from autosport.provider_observation_authority import (
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
    ProviderObservationIntegrityError,
)


def _frame(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "type": "initial_state",
        "sport_key": "table_tennis",
        "snapshot_scope": "current_game_board",
        "snapshot_complete": True,
        "truncated": False,
        "resume_mode": "replace",
        "partial": False,
        "missing_books": [],
        "truncated_books": [],
        "snapshot_partial_reasons": [],
        "count": len(rows),
        "data": rows,
    }


def _request() -> CompleteGameBoardRequest:
    return CompleteGameBoardRequest(
        sport_key="table_tennis",
        bookmakers=("bovada",),
    )


def test_complete_board_rejects_conflicting_combined_rows_for_one_market() -> None:
    rows = [
        {
            "event_id": "event-1",
            "bookmaker": "bovada",
            "kind": "game",
            "market_key": "h2h",
            "home_ml": -110,
            "away_ml": 105,
            "last_update": "2026-09-23T01:00:00Z",
        },
        {
            "event_id": "event-1",
            "bookmaker": "bovada",
            "kind": "game",
            "market_key": "h2h",
            "home_ml": -105,
            "away_ml": 100,
            "last_update": "2026-09-23T01:00:01Z",
        },
    ]

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="conflicting rows for one logical provider row",
    ):
        CompleteGameBoardSnapshot(
            request=_request(),
            captured_at="2026-09-23T01:00:02Z",
            frame_json=json.dumps(_frame(rows)),
        )


def test_complete_board_accepts_documented_per_outcome_rows_for_one_market() -> None:
    rows = [
        {
            "event_id": "event-1",
            "bookmaker": "bovada",
            "kind": "game",
            "market_key": "h2h",
            "outcome": "Player A",
            "price_american": -110,
            "last_update": "2026-09-23T01:00:00Z",
        },
        {
            "event_id": "event-1",
            "bookmaker": "bovada",
            "kind": "game",
            "market_key": "h2h",
            "outcome": "Player B",
            "price_american": 105,
            "last_update": "2026-09-23T01:00:00Z",
        },
    ]

    snapshot = CompleteGameBoardSnapshot(
        request=_request(),
        captured_at="2026-09-23T01:00:02Z",
        frame_json=json.dumps(_frame(rows)),
    )

    assert len(snapshot.row_sha256s) == 2


def test_complete_board_rejects_conflicting_rows_for_same_outcome() -> None:
    rows = [
        {
            "event_id": "event-1",
            "bookmaker": "bovada",
            "kind": "game",
            "market_key": "h2h",
            "outcome": "Player A",
            "price_american": -110,
            "last_update": "2026-09-23T01:00:00Z",
        },
        {
            "event_id": "event-1",
            "bookmaker": "bovada",
            "kind": "game",
            "market_key": "h2h",
            "outcome": "Player A",
            "price_american": -105,
            "last_update": "2026-09-23T01:00:01Z",
        },
    ]

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="conflicting rows for one logical provider row",
    ):
        CompleteGameBoardSnapshot(
            request=_request(),
            captured_at="2026-09-23T01:00:02Z",
            frame_json=json.dumps(_frame(rows)),
        )


def test_distinct_logical_markets_remain_valid() -> None:
    rows = [
        {
            "event_id": "event-1",
            "bookmaker": "bovada",
            "kind": "game",
            "market_key": "h2h",
            "home_ml": -110,
            "away_ml": 105,
        },
        {
            "event_id": "event-1",
            "bookmaker": "bovada",
            "kind": "game",
            "market_key": "spreads",
            "line": -1.5,
            "home_price": 120,
            "away_price": -125,
        },
    ]

    snapshot = CompleteGameBoardSnapshot(
        request=_request(),
        captured_at="2026-09-23T01:00:02Z",
        frame_json=json.dumps(_frame(rows)),
    )

    assert len(snapshot.row_sha256s) == 2
