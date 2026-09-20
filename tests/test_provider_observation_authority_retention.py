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
    capture_parlay_complete_game_board,
)


CAPTURED_AT = "2026-09-20T08:00:00Z"


def _request() -> CompleteGameBoardRequest:
    return CompleteGameBoardRequest(
        sport_key="table_tennis",
        bookmakers=("bovada",),
        max_age_s=600,
    )


def _frame(index: int) -> dict[str, object]:
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


def _snapshot(index: int) -> CompleteGameBoardSnapshot:
    return CompleteGameBoardSnapshot(
        request=_request(),
        captured_at=CAPTURED_AT,
        frame_json=json.dumps(_frame(index)),
    )


class _FakeSseResponse:
    def __init__(self, frame: dict[str, object]) -> None:
        self.status = 200
        self.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        payload = json.dumps(frame, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self._lines = [b"event: initial_state\n", b"data: " + payload + b"\n", b"\n"]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def __iter__(self):
        return iter(self._lines)


def test_authority_registry_is_identity_exact_without_retaining_historical_frames(
    monkeypatch,
) -> None:
    next_index = {"value": 0}

    def fake_urlopen(request, timeout):
        del request, timeout
        return _FakeSseResponse(_frame(next_index["value"]))

    monkeypatch.setattr(authority_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(authority_module, "_default_clock", lambda: CAPTURED_AT)

    def issue(index: int) -> CompleteGameBoardSnapshot:
        next_index["value"] = index
        return capture_parlay_complete_game_board(
            api_key="secret-value",
            request=_request(),
            timeout_seconds=3.0,
        )

    live = issue(0)
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

    # The old module-global issuer was itself a forgery primitive. It is now a
    # fail-closed compatibility tombstone rather than a capability minting API.
    with pytest.raises(
        ProviderObservationUnsupportedError,
        match="issuance is not a consumer API",
    ):
        authority_module._remember(_snapshot(999))

    transient_refs: list[weakref.ReferenceType[CompleteGameBoardSnapshot]] = []
    transient_ids: list[int] = []
    for index in range(1, 33):
        transient = issue(index)
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
