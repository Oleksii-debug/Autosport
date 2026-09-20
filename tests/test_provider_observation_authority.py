from __future__ import annotations

import json
from copy import deepcopy
from urllib.parse import parse_qs, urlparse

import pytest

from autosport.provider_observation_authority import (
    GAME_LINE_MARKETS,
    CompleteGameBoardEvidenceStore,
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
    ProviderObservationIntegrityError,
    ProviderObservationUnsupportedError,
    assert_complete_game_board_authoritative,
    capture_parlay_complete_game_board,
)


CAPTURED_AT = "2026-09-20T08:00:00Z"


def _request() -> CompleteGameBoardRequest:
    return CompleteGameBoardRequest(
        sport_key="table_tennis",
        bookmakers=("tenbet", "bovada"),
        max_age_s=600,
    )


def _complete_frame() -> dict[str, object]:
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
        "count": 3,
        "timestamp": 1789891200,
        "data": [
            {
                "event_id": "event-1",
                "bookmaker": "bovada",
                "kind": "game",
                "market_key": "h2h",
                "home_ml": -110,
                "away_ml": 105,
                "last_update": "2026-09-20T07:59:55Z",
            },
            {
                "event_id": "event-1",
                "bookmaker": "bovada",
                "kind": "game",
                "market_key": "spreads",
                "line": -1.5,
                "home_price": 120,
                "away_price": -125,
                "last_update": "2026-09-20T07:59:55Z",
            },
            {
                "event_id": "event-1",
                "bookmaker": "tenbet",
                "kind": "game",
                "market_key": "totals",
                "line": 74.5,
                "over_price": -105,
                "under_price": -110,
                "last_update": "2026-09-20T07:59:54Z",
            },
        ],
    }


def _capture(frame: dict[str, object] | None = None):
    expected = _complete_frame() if frame is None else frame

    def transport(url, headers, timeout, max_bytes):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert parsed.scheme == "https"
        assert parsed.path == "/v1/sse/odds/table_tennis"
        assert query["bookmakers"] == ["bovada,tenbet"]
        assert query["kinds"] == ["game"]
        assert query["markets"] == [",".join(GAME_LINE_MARKETS)]
        assert query["limit"] == ["1000"]
        assert query["max_age_s"] == ["600"]
        assert "secret-value" not in url
        assert headers["X-API-Key"] == "secret-value"
        assert headers["Accept"] == "text/event-stream"
        assert timeout == 3.0
        assert max_bytes > 1_000_000
        return expected

    return capture_parlay_complete_game_board(
        api_key="secret-value",
        request=_request(),
        timeout_seconds=3.0,
        transport=transport,
        clock=lambda: CAPTURED_AT,
    )


def test_capture_mints_authority_only_for_exact_complete_provider_frame(tmp_path):
    snapshot = _capture()
    assert snapshot.request.bookmakers == ("bovada", "tenbet")
    assert snapshot.request.markets == GAME_LINE_MARKETS
    assert len(snapshot.row_sha256s) == 3
    assert len(snapshot.frame_sha256) == 64
    assert len(snapshot.evidence_sha256) == 64
    assert "secret-value" not in json.dumps(snapshot.to_payload(), sort_keys=True)
    assert_complete_game_board_authoritative(snapshot)

    store = CompleteGameBoardEvidenceStore(tmp_path)
    path = store.save(snapshot)
    assert path.name == f"{snapshot.evidence_sha256}.json"

    restarted = CompleteGameBoardEvidenceStore(tmp_path).load(snapshot.evidence_sha256)
    assert restarted.to_payload() == snapshot.to_payload()
    assert restarted is not snapshot
    assert_complete_game_board_authoritative(restarted)


def test_caller_constructed_lookalike_does_not_have_production_authority():
    frame = _complete_frame()
    snapshot = CompleteGameBoardSnapshot(
        request=_request(),
        captured_at=CAPTURED_AT,
        frame_json=json.dumps(frame),
    )
    with pytest.raises(
        ProviderObservationUnsupportedError,
        match="not issued by canonical provider acquisition evidence",
    ):
        assert_complete_game_board_authoritative(snapshot)


def test_store_rejects_caller_constructed_lookalike(tmp_path):
    snapshot = CompleteGameBoardSnapshot(
        request=_request(),
        captured_at=CAPTURED_AT,
        frame_json=json.dumps(_complete_frame()),
    )
    with pytest.raises(ProviderObservationUnsupportedError):
        CompleteGameBoardEvidenceStore(tmp_path).save(snapshot)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"snapshot_scope": "recent_rows"}, "snapshot scope"),
        ({"snapshot_complete": False}, "snapshot_complete"),
        ({"truncated": True}, "truncated provider snapshot"),
        ({"resume_mode": "diff"}, "replacement semantics"),
        ({"partial": True}, "partial provider snapshot"),
        ({"missing_books": ["bovada"]}, "missing_books"),
        ({"truncated_books": ["tenbet"]}, "truncated_books"),
        ({"snapshot_partial_reasons": ["fetch_cap"]}, "snapshot_partial_reasons"),
    ],
)
def test_incomplete_provider_contract_never_mints_authority(mutation, message):
    frame = _complete_frame()
    frame.update(mutation)
    with pytest.raises(ProviderObservationUnsupportedError, match=message):
        _capture(frame)


def test_out_of_scope_row_never_mints_authority():
    frame = _complete_frame()
    rows = deepcopy(frame["data"])
    assert isinstance(rows, list)
    rows[0]["bookmaker"] = "unrequested_book"
    frame["data"] = rows
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="escapes requested bookmaker scope",
    ):
        _capture(frame)


def test_duplicate_exact_provider_row_fails_closed():
    frame = _complete_frame()
    rows = deepcopy(frame["data"])
    assert isinstance(rows, list)
    rows.append(deepcopy(rows[0]))
    frame["data"] = rows
    frame["count"] = len(rows)
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="duplicate exact rows",
    ):
        _capture(frame)


def test_count_must_bind_exact_frame_rows():
    frame = _complete_frame()
    frame["count"] = 999
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="count must equal exact data length",
    ):
        _capture(frame)


def test_only_documented_complete_game_line_scope_is_admitted():
    with pytest.raises(
        ProviderObservationUnsupportedError,
        match="requires h2h, spreads and totals together",
    ):
        CompleteGameBoardRequest(
            sport_key="table_tennis",
            bookmakers=("bovada",),
            markets=("h2h",),
        )

    with pytest.raises(
        ProviderObservationUnsupportedError,
        match="limit=1000",
    ):
        CompleteGameBoardRequest(
            sport_key="table_tennis",
            bookmakers=("bovada",),
            limit=500,
        )


def test_persisted_digest_tamper_is_rejected(tmp_path):
    snapshot = _capture()
    store = CompleteGameBoardEvidenceStore(tmp_path)
    path = store.save(snapshot)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["frame_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="frame_sha256 does not bind exact provider frame",
    ):
        CompleteGameBoardEvidenceStore(tmp_path).load(snapshot.evidence_sha256)
