from __future__ import annotations

import gc
import json
import weakref

import pytest

import autosport.provider_observation_authority as authority_module
from autosport.provider_observation_authority import (
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
    ProviderObservationUnsupportedError,
    assert_complete_game_board_authoritative,
)


CAPTURED_AT = "2026-09-20T08:00:00Z"


def _snapshot(index: int) -> CompleteGameBoardSnapshot:
    request = CompleteGameBoardRequest(
        sport_key="table_tennis",
        bookmakers=("bovada",),
        max_age_s=600,
    )
    frame = {
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
        "count": 1,
        "data": [
            {
                "event_id": f"event-{index}",
                "bookmaker": "bovada",
                "kind": "game",
                "market_key": "h2h",
                "home_ml": -110,
                "away_ml": 105,
            }
        ],
    }
    return CompleteGameBoardSnapshot(
        request=request,
        captured_at=CAPTURED_AT,
        frame_json=json.dumps(frame),
    )


def test_authority_registry_is_identity_exact_without_retaining_historical_frames() -> None:
    live = authority_module._remember(_snapshot(0))
    live_id = id(live)
    live_ref = weakref.ref(live)
    assert_complete_game_board_authoritative(live)

    # Dataclass value equality must not let a distinct lookalike borrow the live
    # object's opaque in-process capability.
    lookalike = CompleteGameBoardSnapshot.from_payload(live.to_payload())
    assert lookalike == live
    assert lookalike is not live
    with pytest.raises(ProviderObservationUnsupportedError):
        assert_complete_game_board_authoritative(lookalike)

    transient_refs: list[weakref.ReferenceType[CompleteGameBoardSnapshot]] = []
    transient_ids: list[int] = []
    for index in range(1, 33):
        transient = authority_module._remember(_snapshot(index))
        transient_refs.append(weakref.ref(transient))
        transient_ids.append(id(transient))
        assert_complete_game_board_authoritative(transient)

    del transient
    gc.collect()

    assert all(reference() is None for reference in transient_refs)
    assert all(snapshot_id not in authority_module._ISSUED for snapshot_id in transient_ids)

    # A still-live issued object remains authoritative while historical frames are
    # collectable, then its registry entry disappears as soon as the caller releases it.
    assert live_ref() is live
    assert_complete_game_board_authoritative(live)
    del live
    gc.collect()
    assert live_ref() is None
    assert live_id not in authority_module._ISSUED
