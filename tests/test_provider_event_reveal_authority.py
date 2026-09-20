from __future__ import annotations

import json

import pytest

from autosport.provider_event_reveal_authority import (
    ProviderEventRevealAuthorityError,
    resolve_provider_event_reveal,
)
from autosport.provider_observation_authority import (
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
)


def _snapshot(*, rows: list[dict[str, object]]) -> CompleteGameBoardSnapshot:
    request = CompleteGameBoardRequest(
        sport_key="tennis_atp",
        bookmakers=("pinnacle",),
    )
    frame = {
        "type": "initial_state",
        "sport_key": request.sport_key,
        "snapshot_scope": "current_game_board",
        "snapshot_complete": True,
        "truncated": False,
        "resume_mode": "replace",
        "count": len(rows),
        "data": rows,
    }
    return CompleteGameBoardSnapshot(
        request=request,
        captured_at="2026-09-20T10:00:00Z",
        frame_json=json.dumps(frame),
    )


def _row(
    *,
    event_id: str = "event-1",
    market_key: str = "h2h",
    commence_time: str | None = "2026-09-20T12:00:00Z",
    commence_time_reported: bool = True,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "bookmaker": "pinnacle",
        "kind": "game",
        "market_key": market_key,
        "last_update": "2026-09-20T09:59:00Z",
        "commence_time": commence_time,
        "commence_time_reported": commence_time_reported,
    }


def _allow_constructed_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unit-test only: upstream provider-observation tests separately prove that
    # production positive authority is minted only by canonical live acquisition.
    monkeypatch.setattr(
        "autosport.provider_event_reveal_authority.assert_complete_game_board_authoritative",
        lambda snapshot: None,
    )


def test_reveal_boundary_is_derived_from_exact_provider_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_constructed_snapshot(monkeypatch)
    snapshot = _snapshot(
        rows=[
            _row(market_key="h2h"),
            _row(market_key="spreads"),
            _row(market_key="totals"),
        ]
    )

    evidence = resolve_provider_event_reveal(snapshot, event_id="event-1")

    assert evidence.event_id == "event-1"
    assert evidence.outcome_reveal_not_before == "2026-09-20T12:00:00Z"
    assert evidence.authority_id.startswith(
        f"parlay-complete-board-commence-time-v1:{snapshot.evidence_sha256}:event-1"
    )
    assert len(evidence.authority_sha256) == 64


def test_reveal_boundary_rejects_unreported_provider_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_constructed_snapshot(monkeypatch)
    snapshot = _snapshot(
        rows=[_row(commence_time=None, commence_time_reported=False)]
    )

    with pytest.raises(
        ProviderEventRevealAuthorityError,
        match="start time is not source-reported",
    ):
        resolve_provider_event_reveal(snapshot, event_id="event-1")


def test_reveal_boundary_rejects_conflicting_event_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_constructed_snapshot(monkeypatch)
    snapshot = _snapshot(
        rows=[
            _row(market_key="h2h", commence_time="2026-09-20T12:00:00Z"),
            _row(market_key="spreads", commence_time="2026-09-20T12:05:00Z"),
        ]
    )

    with pytest.raises(
        ProviderEventRevealAuthorityError,
        match="disagree on event commence_time",
    ):
        resolve_provider_event_reveal(snapshot, event_id="event-1")


def test_reveal_boundary_rejects_started_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_constructed_snapshot(monkeypatch)
    snapshot = _snapshot(
        rows=[_row(commence_time="2026-09-20T10:00:00Z")]
    )

    with pytest.raises(
        ProviderEventRevealAuthorityError,
        match="not strictly before the reported event start",
    ):
        resolve_provider_event_reveal(snapshot, event_id="event-1")


def test_reveal_boundary_rejects_event_absent_from_complete_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_constructed_snapshot(monkeypatch)
    snapshot = _snapshot(rows=[_row(event_id="event-1")])

    with pytest.raises(
        ProviderEventRevealAuthorityError,
        match="absent from the authoritative complete board",
    ):
        resolve_provider_event_reveal(snapshot, event_id="event-2")
