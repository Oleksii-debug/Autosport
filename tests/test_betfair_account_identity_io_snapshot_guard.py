from __future__ import annotations

from datetime import datetime, timezone
import json
from threading import Event, Thread
import urllib.request as _urllib_request

import pytest

from autosport import betfair_account_readonly as _readonly
from autosport.betfair_account_identity import (
    BetfairAccountIdentityError,
    build_betfair_authenticated_client,
    is_authoritative_betfair_account_identity,
    resolve_betfair_authenticated_account_identity,
)
from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BetfairSessionCredentials,
)


def test_transient_clock_substitution_during_provider_io_cannot_backdate_identity(
    monkeypatch,
) -> None:
    read_entered = Event()
    release_payload = Event()
    after_clock_sample = Event()
    release_parse = Event()

    original_loads = json.loads
    original_provider_text = _readonly._provider_text

    class Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit: int) -> bytes:
            assert limit >= len(self._payload)
            read_entered.set()
            assert release_payload.wait(timeout=5)
            return self._payload

    def fake_open(request, timeout: float):
        assert request.full_url == ACCOUNT_JSON_RPC_ENDPOINT
        decoded = original_loads(request.data.decode("utf-8"))
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": decoded["id"],
                "result": {
                    "currencyCode": "EUR",
                    "localeCode": "en",
                    "region": "GBR",
                    "timezone": "Europe/London",
                },
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return Response(payload)

    class Opener:
        def open(self, request, data=None, timeout: float = 0):
            assert data is None
            return fake_open(request, timeout)

    def fenced_provider_text(*args, **kwargs):
        after_clock_sample.set()
        assert release_parse.wait(timeout=5)
        return original_provider_text(*args, **kwargs)

    monkeypatch.setattr(_urllib_request, "_opener", Opener())
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token")
    )
    original_clock = client._clock

    result: dict[str, object] = {}

    def resolve() -> None:
        try:
            result["value"] = resolve_betfair_authenticated_account_identity(client)
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["error"] = exc

    worker = Thread(target=resolve)
    worker.start()
    assert read_entered.wait(timeout=5)

    # The old implementation dereferenced the mutable live clock after provider
    # I/O. Keep the forged clock installed through the sealed RPC evidence sample,
    # then pause in the still-live parser and restore it before K07's after-
    # acquisition context check.
    client._clock = lambda: datetime(1900, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(_readonly, "_provider_text", fenced_provider_text)
    release_payload.set()
    assert after_clock_sample.wait(timeout=5)
    client._clock = original_clock
    monkeypatch.setattr(_readonly, "_provider_text", original_provider_text)
    release_parse.set()

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert "error" not in result, repr(result.get("error"))
    value = result["value"]
    assert value.observed_at != "1900-01-01T00:00:00+00:00"
    assert value.currency_code == "EUR"
    assert is_authoritative_betfair_account_identity(value, client=client)


def test_transient_parser_currency_substitution_cannot_launder_k07_identity(
    monkeypatch,
) -> None:
    read_entered = Event()
    release_payload = Event()
    forged_parser_entered = Event()
    release_forged_parser = Event()
    original_loads = json.loads
    original_provider_text = _readonly._provider_text

    class Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit: int) -> bytes:
            assert limit >= len(self._payload)
            read_entered.set()
            assert release_payload.wait(timeout=5)
            return self._payload

    def fake_open(request, timeout: float):
        assert request.full_url == ACCOUNT_JSON_RPC_ENDPOINT
        decoded = original_loads(request.data.decode("utf-8"))
        return Response(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": decoded["id"],
                    "result": {
                        "currencyCode": "EUR",
                        "localeCode": "en",
                        "region": "GBR",
                        "timezone": "Europe/London",
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        )

    class Opener:
        def open(self, request, data=None, timeout: float = 0):
            assert data is None
            return fake_open(request, timeout)

    def forged_provider_text(raw, key: str, field: str):
        if field == "currency_code":
            forged_parser_entered.set()
            assert release_forged_parser.wait(timeout=5)
            return "GBP"
        return original_provider_text(raw, key, field)

    monkeypatch.setattr(_urllib_request, "_opener", Opener())
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token")
    )
    result: dict[str, object] = {}

    def resolve() -> None:
        try:
            result["value"] = resolve_betfair_authenticated_account_identity(client)
        except BaseException as exc:  # pragma: no cover - asserted below
            result["error"] = exc

    worker = Thread(target=resolve)
    worker.start()
    assert read_entered.wait(timeout=5)
    monkeypatch.setattr(_readonly, "_provider_text", forged_provider_text)
    release_payload.set()
    assert forged_parser_entered.wait(timeout=5)

    # Restore the public parser before the original K07 post-acquisition checks.
    # The executing forged helper still returns GBP, reproducing the old TOCTOU.
    monkeypatch.setattr(_readonly, "_provider_text", original_provider_text)
    release_forged_parser.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert "value" not in result
    error = result.get("error")
    assert isinstance(error, BetfairAccountIdentityError)
    assert "currency diverged from captured provider response" in str(error)


def test_json_codec_rebinding_cannot_redirect_or_forge_k07_rpc(monkeypatch) -> None:
    original_dumps = json.dumps
    original_loads = json.loads
    observed_methods: list[str] = []

    class Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit: int) -> bytes:
            assert limit >= len(self._payload)
            return self._payload

    def fake_open(request, timeout: float):
        assert request.full_url == ACCOUNT_JSON_RPC_ENDPOINT
        decoded = original_loads(request.data.decode("utf-8"))
        observed_methods.append(decoded["method"])
        assert decoded["method"] == "AccountAPING/v1.0/getAccountDetails"
        return Response(
            original_dumps(
                {
                    "jsonrpc": "2.0",
                    "id": decoded["id"],
                    "result": {
                        "currencyCode": "EUR",
                        "localeCode": "en",
                        "region": "GBR",
                        "timezone": "Europe/London",
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        )

    class Opener:
        def open(self, request, data=None, timeout: float = 0):
            assert data is None
            return fake_open(request, timeout)

    def forged_dumps(value, *args, **kwargs):
        if type(value) is dict and value.get("jsonrpc") == "2.0" and "method" in value:
            forged = dict(value)
            forged["method"] = "AccountAPING/v1.0/transferFunds"
            return original_dumps(forged, *args, **kwargs)
        return original_dumps(value, *args, **kwargs)

    def forged_loads(value, *args, **kwargs):
        decoded = original_loads(value, *args, **kwargs)
        if type(decoded) is dict and type(decoded.get("result")) is dict:
            forged = dict(decoded)
            forged_result = dict(decoded["result"])
            forged_result["currencyCode"] = "GBP"
            forged["result"] = forged_result
            return forged
        return decoded

    monkeypatch.setattr(_urllib_request, "_opener", Opener())
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token")
    )
    monkeypatch.setattr(json, "dumps", forged_dumps)
    monkeypatch.setattr(json, "loads", forged_loads)

    value = resolve_betfair_authenticated_account_identity(client)

    assert observed_methods == ["AccountAPING/v1.0/getAccountDetails"]
    assert value.currency_code == "EUR"
    assert is_authoritative_betfair_account_identity(value, client=client)


def test_k07_public_entrypoints_report_io_snapshot_seal() -> None:
    assert getattr(
        build_betfair_authenticated_client,
        "_autosport_k07_io_snapshot_sealed",
        False,
    )
    assert getattr(
        resolve_betfair_authenticated_account_identity,
        "_autosport_k07_io_snapshot_sealed",
        False,
    )
