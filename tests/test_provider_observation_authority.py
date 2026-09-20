from __future__ import annotations

import json
from copy import deepcopy
from urllib.parse import parse_qs, urlparse

import pytest

import autosport.provider_observation_authority as authority_module
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


class _FakeSseResponse:
    def __init__(
        self,
        frame: dict[str, object],
        *,
        status: int = 200,
        content_type: str = "text/event-stream; charset=utf-8",
    ) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        encoded = json.dumps(frame, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self._lines = [b"event: initial_state\n", b"data: " + encoded + b"\n", b"\n"]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def __iter__(self):
        return iter(self._lines)


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


def _store(tmp_path) -> CompleteGameBoardEvidenceStore:
    return CompleteGameBoardEvidenceStore(
        tmp_path / "workspace",
        authority_root=tmp_path / "machine-authority",
    )


def _install_fake_sse(
    monkeypatch,
    frame: dict[str, object],
    *,
    status: int = 200,
    content_type: str = "text/event-stream; charset=utf-8",
):
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {
            key.lower(): value for key, value in request.header_items()
        }
        captured["timeout"] = timeout
        return _FakeSseResponse(
            frame,
            status=status,
            content_type=content_type,
        )

    monkeypatch.setattr(authority_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(authority_module, "_default_clock", lambda: CAPTURED_AT)
    return captured


def _capture(monkeypatch, frame: dict[str, object] | None = None):
    expected = _complete_frame() if frame is None else frame
    captured = _install_fake_sse(monkeypatch, expected)
    snapshot = capture_parlay_complete_game_board(
        api_key="secret-value",
        request=_request(),
        timeout_seconds=3.0,
    )
    parsed = urlparse(captured["url"])
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "parlay-api.com"
    assert parsed.path == "/v1/sse/odds/table_tennis"
    assert query["bookmakers"] == ["bovada,tenbet"]
    assert query["kinds"] == ["game"]
    assert query["markets"] == [",".join(GAME_LINE_MARKETS)]
    assert query["limit"] == ["1000"]
    assert query["max_age_s"] == ["600"]
    assert "secret-value" not in captured["url"]
    assert captured["headers"]["x-api-key"] == "secret-value"
    assert captured["headers"]["accept"] == "text/event-stream"
    assert captured["timeout"] == 3.0
    return snapshot


def test_capture_mints_authority_only_for_exact_complete_provider_frame(tmp_path, monkeypatch):
    snapshot = _capture(monkeypatch)
    assert snapshot.request.bookmakers == ("bovada", "tenbet")
    assert snapshot.request.markets == GAME_LINE_MARKETS
    assert len(snapshot.row_sha256s) == 3
    assert len(snapshot.frame_sha256) == 64
    assert len(snapshot.evidence_sha256) == 64
    assert "secret-value" not in json.dumps(snapshot.to_payload(), sort_keys=True)
    assert_complete_game_board_authoritative(snapshot)

    store = _store(tmp_path)
    path = store.save(snapshot)
    assert path.name == f"{snapshot.evidence_sha256}.json"

    # Local durable bytes/journals are integrity evidence, not remote-origin
    # attestation. A restarted process must reacquire before positive authority.
    with pytest.raises(
        ProviderObservationUnsupportedError,
        match="restart cannot reissue provider-origin authority",
    ):
        _store(tmp_path).load(snapshot.evidence_sha256)

    # The still-live exact object keeps its ephemeral production-capture capability.
    assert_complete_game_board_authoritative(snapshot)


def test_caller_constructed_lookalike_does_not_have_production_authority():
    snapshot = CompleteGameBoardSnapshot(
        request=_request(),
        captured_at=CAPTURED_AT,
        frame_json=json.dumps(_complete_frame()),
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
        _store(tmp_path).save(snapshot)


def test_manually_written_self_consistent_bytes_cannot_regain_authority(tmp_path):
    forged = CompleteGameBoardSnapshot(
        request=_request(),
        captured_at=CAPTURED_AT,
        frame_json=json.dumps(_complete_frame()),
    )
    store = _store(tmp_path)
    forged_path = store.root / f"{forged.evidence_sha256}.json"
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_text(
        json.dumps(forged.to_payload(), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="independent machine-state acquisition authority",
    ):
        _store(tmp_path).load(forged.evidence_sha256)
    with pytest.raises(ProviderObservationUnsupportedError):
        assert_complete_game_board_authoritative(forged)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"snapshot_scope": "recent_rows"}, "snapshot scope"),
        ({"snapshot_complete": False}, "snapshot_complete"),
        ({"truncated": True}, "truncated provider snapshot"),
        ({"resume_mode": "diff"}, "replacement semantics"),
        ({"partial": True}, "partial provider snapshot"),
        ({"partial": "false"}, "partial provider snapshot"),
        ({"partial_reason": "upstream_gap"}, "partial_reason"),
        ({"missing_books": ["bovada"]}, "missing_books"),
        ({"truncated_books": ["tenbet"]}, "truncated_books"),
        ({"snapshot_partial_reasons": ["fetch_cap"]}, "snapshot_partial_reasons"),
    ],
)
def test_incomplete_provider_contract_never_mints_authority(
    monkeypatch,
    mutation,
    message,
):
    frame = _complete_frame()
    frame.update(mutation)
    _install_fake_sse(monkeypatch, frame)
    with pytest.raises(ProviderObservationUnsupportedError, match=message):
        capture_parlay_complete_game_board(
            api_key="secret-value",
            request=_request(),
        )


def test_out_of_scope_row_never_mints_authority(monkeypatch):
    frame = _complete_frame()
    rows = deepcopy(frame["data"])
    assert isinstance(rows, list)
    rows[0]["bookmaker"] = "unrequested_book"
    frame["data"] = rows
    _install_fake_sse(monkeypatch, frame)
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="escapes requested bookmaker scope",
    ):
        capture_parlay_complete_game_board(api_key="secret-value", request=_request())


def test_duplicate_exact_provider_row_fails_closed(monkeypatch):
    frame = _complete_frame()
    rows = deepcopy(frame["data"])
    assert isinstance(rows, list)
    rows.append(deepcopy(rows[0]))
    frame["data"] = rows
    frame["count"] = len(rows)
    _install_fake_sse(monkeypatch, frame)
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="duplicate exact rows",
    ):
        capture_parlay_complete_game_board(api_key="secret-value", request=_request())


def test_count_must_bind_exact_frame_rows(monkeypatch):
    frame = _complete_frame()
    frame["count"] = 999
    _install_fake_sse(monkeypatch, frame)
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="count must equal exact data length",
    ):
        capture_parlay_complete_game_board(api_key="secret-value", request=_request())


def test_wrong_content_type_fails_before_snapshot_authority(monkeypatch):
    _install_fake_sse(monkeypatch, _complete_frame(), content_type="application/json")
    with pytest.raises(
        ProviderObservationUnsupportedError,
        match="text/event-stream",
    ):
        capture_parlay_complete_game_board(api_key="secret-value", request=_request())


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
    with pytest.raises(ProviderObservationUnsupportedError, match="limit=1000"):
        CompleteGameBoardRequest(
            sport_key="table_tennis",
            bookmakers=("bovada",),
            limit=500,
        )


def test_persisted_digest_tamper_is_rejected(tmp_path, monkeypatch):
    snapshot = _capture(monkeypatch)
    store = _store(tmp_path)
    path = store.save(snapshot)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["frame_sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="frame_sha256 does not bind exact provider frame",
    ):
        _store(tmp_path).load(snapshot.evidence_sha256)
