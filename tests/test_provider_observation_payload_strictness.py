from __future__ import annotations

import json

import pytest

from autosport.provider_observation_authority import (
    CompleteGameBoardEvidenceStore,
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
    ProviderObservationIntegrityError,
)


def _snapshot() -> CompleteGameBoardSnapshot:
    request = CompleteGameBoardRequest(
        sport_key="table_tennis",
        bookmakers=("betfair",),
    )
    frame = {
        "type": "initial_state",
        "sport_key": "table_tennis",
        "snapshot_scope": "current_game_board",
        "snapshot_complete": True,
        "truncated": False,
        "resume_mode": "replace",
        "partial": False,
        "data": [
            {
                "event_id": "event_1",
                "bookmaker": "betfair",
                "kind": "game",
                "market_key": "h2h",
            }
        ],
        "count": 1,
    }
    return CompleteGameBoardSnapshot(
        request=request,
        captured_at="2026-09-20T09:00:00Z",
        frame_json=json.dumps(frame),
    )


def test_persisted_snapshot_rejects_unknown_top_level_v1_field(tmp_path) -> None:
    payload = _snapshot().to_payload()
    payload["unexpected_local_field"] = "must-not-normalize-away"
    path = tmp_path / "provider-evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="evidence payload fields mismatch",
    ):
        CompleteGameBoardEvidenceStore._read_path(path)


def test_persisted_snapshot_rejects_unknown_request_v1_field(tmp_path) -> None:
    payload = _snapshot().to_payload()
    request = payload["request"]
    assert isinstance(request, dict)
    request["unexpected_request_field"] = "must-not-normalize-away"
    path = tmp_path / "provider-evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="request payload fields mismatch",
    ):
        CompleteGameBoardEvidenceStore._read_path(path)
