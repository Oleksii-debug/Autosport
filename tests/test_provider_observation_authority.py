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


@pytest.fixture(autouse=True)
def _private_test_acquisition_origin():
    with authority_module._test_acquisition_origin(
        _capability=authority_module._TEST_ACQUISITION_CAPABILITY,
    ):
        yield


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



def test_test_acquisition_origin_rejects_wrong_capability():
    with pytest.raises(TypeError, match="private capability"):
        with authority_module._test_acquisition_origin(_capability=object()):
            pass


@pytest.mark.parametrize(
    "name",
    [
        "urlopen",
        "Request",
        "strict_json_loads",
        "_parse_sse_event",
        "_read_production_initial_state",
        "_default_clock",
        "_canonical_json",
        "_remember",
    ],
)
def test_production_origin_guard_rejects_global_rebind(
    monkeypatch,
    name,
):
    monkeypatch.setattr(authority_module, name, object())
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="production acquisition origin is rebound",
    ):
        authority_module._require_production_capture_origin_integrity()


def test_production_origin_guard_rejects_request_url_surface_rebind(monkeypatch):
    monkeypatch.setattr(
        CompleteGameBoardRequest,
        "sse_url",
        lambda _self: "https://attacker.invalid/",
    )
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="production acquisition origin code changed",
    ):
        authority_module._require_production_capture_origin_integrity()


def test_production_origin_guard_rejects_reader_code_mutation():
    target = authority_module._CANONICAL_READ_PRODUCTION_INITIAL_STATE
    original = target.__code__

    def hostile(*_args, **_kwargs):
        raise AssertionError("hostile provider reader executed")

    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            ProviderObservationIntegrityError,
            match="production acquisition origin code changed",
        ):
            authority_module._require_production_capture_origin_integrity()
    finally:
        target.__code__ = original


def test_production_reader_defaults_capture_transport_origin():
    defaults = authority_module._CANONICAL_READ_PRODUCTION_INITIAL_STATE.__kwdefaults__
    assert defaults is not None
    assert defaults["_request_factory"] is authority_module._CANONICAL_HTTP_REQUEST
    assert defaults["_open_url"] is authority_module._CANONICAL_URLOPEN
    assert defaults["_parse_event"] is authority_module._CANONICAL_PARSE_SSE_EVENT
    assert defaults["_sse_url"] is authority_module._CANONICAL_REQUEST_SSE_URL
    assert defaults["_loads"] is authority_module._CANONICAL_STRICT_JSON_LOADS
    assert defaults["_max_sse_bytes"] == authority_module._MAX_SSE_BYTES


def test_public_capture_has_no_transport_or_clock_injection_parameters():
    import inspect

    parameters = inspect.signature(capture_parlay_complete_game_board).parameters
    assert set(parameters) == {"api_key", "request", "timeout_seconds"}



def test_capture_rejects_request_subclass_even_in_test_origin():
    class HostileRequest(CompleteGameBoardRequest):
        pass

    with pytest.raises(TypeError, match="exact CompleteGameBoardRequest"):
        capture_parlay_complete_game_board(
            api_key="secret-value",
            request=HostileRequest(
                sport_key="table_tennis",
                bookmakers=("bovada",),
            ),
        )


def test_sealed_capture_rejects_clock_and_witness_double_rebind(monkeypatch):
    hostile = lambda: "2200-01-01T00:00:00Z"
    monkeypatch.setattr(authority_module, "_default_clock", hostile)
    monkeypatch.setattr(authority_module, "_CANONICAL_DEFAULT_CLOCK", hostile)

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="production acquisition witness changed",
    ):
        capture_parlay_complete_game_board(
            api_key="secret-value",
            request=_request(),
        )


def test_sealed_capture_rejects_transport_and_witness_double_rebind(monkeypatch):
    def hostile(*_args, **_kwargs):
        raise AssertionError("hostile transport executed")

    monkeypatch.setattr(authority_module, "urlopen", hostile)
    monkeypatch.setattr(authority_module, "_CANONICAL_URLOPEN", hostile)

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="production acquisition witness changed",
    ):
        capture_parlay_complete_game_board(
            api_key="secret-value",
            request=_request(),
        )


def test_sealed_capture_rejects_guard_rebind_before_execution(monkeypatch):
    calls: list[str] = []

    def hostile_guard():
        calls.append("guard")
        raise AssertionError("hostile guard executed")

    monkeypatch.setattr(
        authority_module,
        "_require_production_capture_origin_integrity",
        hostile_guard,
    )
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="production acquisition guard changed",
    ):
        capture_parlay_complete_game_board(
            api_key="secret-value",
            request=_request(),
        )
    assert calls == []


def test_sealed_capture_does_not_expose_unsealed_delegate():
    sealed = authority_module.capture_parlay_complete_game_board

    assert not hasattr(sealed, "__wrapped__")
    assert sealed.__name__ == "capture_parlay_complete_game_board"


def test_sealed_capture_rejects_public_surface_rebind(monkeypatch):
    saved = capture_parlay_complete_game_board
    monkeypatch.setattr(
        authority_module,
        "capture_parlay_complete_game_board",
        lambda **_kwargs: None,
    )
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="public surface changed",
    ):
        saved(api_key="secret-value", request=_request())



def test_sealed_capture_rejects_mid_call_public_surface_rebind(monkeypatch):
    saved = capture_parlay_complete_game_board
    hostile_calls: list[str] = []

    def hostile_capture(**_kwargs):
        hostile_calls.append("capture")
        raise AssertionError("hostile public capture executed")

    def mutating_urlopen(_request, _timeout):
        monkeypatch.setattr(
            authority_module,
            "capture_parlay_complete_game_board",
            hostile_capture,
        )
        return _FakeSseResponse(_complete_frame())

    monkeypatch.setattr(authority_module, "urlopen", mutating_urlopen)
    monkeypatch.setattr(authority_module, "_default_clock", lambda: CAPTURED_AT)

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="public surface changed",
    ):
        saved(
            api_key="secret-value",
            request=_request(),
            timeout_seconds=3.0,
        )

    assert hostile_calls == []


def test_ephemeral_issuance_registry_is_not_module_mutable():
    assert not hasattr(authority_module, "_ISSUED")


def test_store_save_uses_captured_assertion_not_live_public_alias(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    hostile_calls: list[str] = []

    def hostile_assert(_snapshot) -> None:
        hostile_calls.append("assert")
        raise AssertionError("hostile public assertion executed")

    monkeypatch.setattr(
        authority_module,
        "assert_complete_game_board_authoritative",
        hostile_assert,
    )
    path = _store(tmp_path).save(snapshot)
    assert path.name == f"{snapshot.evidence_sha256}.json"
    assert hostile_calls == []


def test_store_save_rejects_canonical_assert_witness_rebind(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    hostile_calls: list[str] = []

    def hostile_assert(_snapshot) -> None:
        hostile_calls.append("assert")
        raise AssertionError("hostile canonical assertion executed")

    monkeypatch.setattr(
        authority_module,
        "_CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE",
        hostile_assert,
    )
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider evidence store authority witness changed",
    ):
        _store(tmp_path).save(snapshot)
    assert hostile_calls == []


def test_store_save_requires_exact_store_type(tmp_path, monkeypatch):
    snapshot = _capture(monkeypatch)

    class HostileStore(CompleteGameBoardEvidenceStore):
        pass

    store = HostileStore(
        tmp_path / "workspace",
        authority_root=tmp_path / "machine-authority",
    )
    with pytest.raises(
        TypeError,
        match="save requires exact CompleteGameBoardEvidenceStore",
    ):
        store.save(snapshot)


def test_store_load_requires_exact_store_type(tmp_path):
    class HostileStore(CompleteGameBoardEvidenceStore):
        pass

    store = HostileStore(
        tmp_path / "workspace",
        authority_root=tmp_path / "machine-authority",
    )
    with pytest.raises(
        TypeError,
        match="load requires exact CompleteGameBoardEvidenceStore",
    ):
        store.load("a" * 64)


def test_store_load_implementation_uses_canonical_remember_without_unsealed_delegate():
    import inspect

    load_descriptor = inspect.getattr_static(CompleteGameBoardEvidenceStore, "load")
    save_descriptor = inspect.getattr_static(CompleteGameBoardEvidenceStore, "save")
    assert not hasattr(load_descriptor, "__wrapped__")
    assert not hasattr(save_descriptor, "__wrapped__")
    original_code = authority_module._CANONICAL_EVIDENCE_STORE_LOAD_IMPLEMENTATION_CODE
    assert "_CANONICAL_REMEMBER" in original_code.co_names
    assert "_remember" not in original_code.co_names


def test_store_save_rejects_atomic_writer_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    hostile_calls: list[str] = []

    def hostile_writer(*_args, **_kwargs):
        hostile_calls.append("write")
        raise AssertionError("hostile atomic writer executed")

    monkeypatch.setattr(authority_module, "atomic_write_json", hostile_writer)
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider evidence store authority witness changed",
    ):
        _store(tmp_path).save(snapshot)
    assert hostile_calls == []


def test_store_save_rejects_hmac_dispatch_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    hostile_calls: list[str] = []

    def hostile_hmac(*_args, **_kwargs):
        hostile_calls.append("hmac")
        raise AssertionError("hostile HMAC executed")

    monkeypatch.setattr(authority_module.hmac, "new", hostile_hmac)
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider evidence store authority witness changed",
    ):
        _store(tmp_path).save(snapshot)
    assert hostile_calls == []


def test_store_save_rejects_internal_receipt_writer_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    hostile_calls: list[str] = []

    def hostile_write_receipt(_self, _snapshot):
        hostile_calls.append("receipt")
        raise AssertionError("hostile receipt writer executed")

    monkeypatch.setattr(
        CompleteGameBoardEvidenceStore,
        "_write_receipt",
        hostile_write_receipt,
    )
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider evidence store internal dispatch changed",
    ):
        _store(tmp_path).save(snapshot)
    assert hostile_calls == []


def test_store_save_rejects_monotonic_commit_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    hostile_calls: list[str] = []

    def hostile_commit(_self, **_kwargs):
        hostile_calls.append("commit")
        raise AssertionError("hostile monotonic commit executed")

    monkeypatch.setattr(
        authority_module.MonotonicWorkspaceAuthority,
        "commit",
        hostile_commit,
    )
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider evidence monotonic authority dispatch changed",
    ):
        _store(tmp_path).save(snapshot)
    assert hostile_calls == []


def test_store_save_rejects_workspace_lock_enter_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    hostile_calls: list[str] = []

    def hostile_enter(_self):
        hostile_calls.append("lock")
        raise AssertionError("hostile workspace lock executed")

    monkeypatch.setattr(
        authority_module.WorkspaceEconomicLock,
        "__enter__",
        hostile_enter,
    )
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider evidence workspace lock dispatch changed",
    ):
        _store(tmp_path).save(snapshot)
    assert hostile_calls == []


def test_store_save_rejects_snapshot_payload_dispatch_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    hostile_calls: list[str] = []

    def hostile_to_payload(_self):
        hostile_calls.append("snapshot")
        raise AssertionError("hostile snapshot payload executed")

    monkeypatch.setattr(
        CompleteGameBoardSnapshot,
        "to_payload",
        hostile_to_payload,
    )
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider evidence snapshot dispatch changed",
    ):
        _store(tmp_path).save(snapshot)
    assert hostile_calls == []


def test_store_load_rejects_json_reader_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    store = _store(tmp_path)
    store.save(snapshot)
    hostile_calls: list[str] = []

    def hostile_json_reader(_payload):
        hostile_calls.append("json")
        raise AssertionError("hostile JSON reader executed")

    monkeypatch.setattr(authority_module, "strict_json_loads", hostile_json_reader)
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider evidence store authority witness changed",
    ):
        store.load(snapshot.evidence_sha256)
    assert hostile_calls == []

def test_store_save_rejects_surface_reader_rebind_before_hostile_dispatch(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    store = _store(tmp_path)
    hostile_calls: list[str] = []

    def hostile_getattr_static(*_args, **_kwargs):
        hostile_calls.append("reflect")
        raise AssertionError("hostile reflection reader executed")

    monkeypatch.setattr(
        authority_module,
        "_CANONICAL_GETATTR_STATIC",
        hostile_getattr_static,
    )
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider evidence store authority witness changed",
    ):
        store.save(snapshot)
    assert hostile_calls == []


def test_store_save_rejects_path_constructor_rebind_before_hostile_dispatch(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    store = _store(tmp_path)
    hostile_calls: list[str] = []
    original_new = authority_module.Path.__new__

    def hostile_new(cls, *args, **kwargs):
        hostile_calls.append("path-new")
        return original_new(cls, *args, **kwargs)

    monkeypatch.setattr(authority_module.Path, "__new__", hostile_new)
    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider evidence filesystem path dispatch changed",
    ):
        store.save(snapshot)
    assert hostile_calls == []




def test_store_save_rejects_shadowed_runtime_builtin_before_dispatch(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    store = _store(tmp_path)
    hostile_calls: list[str] = []

    def hostile_any(*_args, **_kwargs):
        hostile_calls.append("any")
        raise AssertionError("hostile any executed")

    monkeypatch.setattr(authority_module, "any", hostile_any, raising=False)

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider evidence store builtin dispatch shadowed",
    ):
        store.save(snapshot)

    assert hostile_calls == []


def test_store_save_uses_captured_type_and_getattr_primitives(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    store = _store(tmp_path)
    hostile_calls: list[str] = []

    def hostile(*_args, **_kwargs):
        hostile_calls.append("primitive")
        raise AssertionError("hostile primitive executed")

    monkeypatch.setattr(authority_module, "type", hostile, raising=False)
    monkeypatch.setattr(authority_module, "getattr", hostile, raising=False)

    path = store.save(snapshot)

    assert path.name == f"{snapshot.evidence_sha256}.json"
    assert hostile_calls == []


def test_store_save_rejects_getattr_static_helper_rebind_before_execution(
    tmp_path,
    monkeypatch,
):
    snapshot = _capture(monkeypatch)
    store = _store(tmp_path)
    reader = authority_module._CANONICAL_GETATTR_STATIC
    helper_globals = reader.__globals__
    name = next(
        candidate
        for candidate in reader.__code__.co_names
        if candidate in helper_globals
        and getattr(helper_globals[candidate], "__code__", None) is not None
    )
    original = helper_globals[name]
    hostile_calls: list[str] = []

    def hostile(*_args, **_kwargs):
        hostile_calls.append(name)
        raise AssertionError("hostile inspect helper executed")

    helper_globals[name] = hostile
    try:
        with pytest.raises(
            ProviderObservationIntegrityError,
            match="provider evidence store authority witness changed",
        ):
            store.save(snapshot)
    finally:
        helper_globals[name] = original

    assert hostile_calls == []



def test_sealed_capture_rejects_shadowed_isinstance_before_execution(
    monkeypatch,
):
    saved = authority_module.capture_parlay_complete_game_board
    hostile_calls: list[str] = []

    def hostile_isinstance(*_args, **_kwargs):
        hostile_calls.append("isinstance")
        raise AssertionError("hostile isinstance executed")

    monkeypatch.setattr(
        authority_module,
        "isinstance",
        hostile_isinstance,
        raising=False,
    )

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider production acquisition builtin dispatch shadowed",
    ):
        saved(api_key="secret-value", request=_request())

    assert hostile_calls == []


def test_sealed_capture_rejects_math_rebind_before_isfinite_dispatch(
    monkeypatch,
):
    saved = authority_module.capture_parlay_complete_game_board
    hostile_calls: list[str] = []

    class HostileMath:
        @staticmethod
        def isfinite(_value):
            hostile_calls.append("isfinite")
            raise AssertionError("hostile isfinite executed")

    monkeypatch.setattr(authority_module, "math", HostileMath)

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="provider production acquisition numeric validation changed",
    ):
        saved(api_key="secret-value", request=_request())

    assert hostile_calls == []


def test_production_origin_guard_rejects_getattr_static_helper_rebind(
    monkeypatch,
):
    reader = authority_module._CANONICAL_GETATTR_STATIC
    helper_globals = reader.__globals__
    name = next(
        candidate
        for candidate in reader.__code__.co_names
        if candidate in helper_globals
        and getattr(helper_globals[candidate], "__code__", None) is not None
    )
    original = helper_globals[name]
    hostile_calls: list[str] = []

    def hostile(*_args, **_kwargs):
        hostile_calls.append(name)
        raise AssertionError("hostile inspect helper executed")

    helper_globals[name] = hostile
    try:
        with pytest.raises(
            ProviderObservationIntegrityError,
            match="provider production acquisition origin is rebound",
        ):
            authority_module._require_production_capture_origin_integrity()
    finally:
        helper_globals[name] = original

    assert hostile_calls == []
