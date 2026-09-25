from __future__ import annotations

from datetime import datetime, timezone
import json
from threading import Event, Thread
import urllib.request as _urllib_request

from autosport.betfair_account_identity import (
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
    release_decode = Event()

    original_loads = json.loads

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

    def fenced_loads(*args, **kwargs):
        after_clock_sample.set()
        assert release_decode.wait(timeout=5)
        return original_loads(*args, **kwargs)

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
    # I/O. Keep the forged clock installed through the evidence sampling point,
    # then restore it before K07's after-acquisition context check.
    client._clock = lambda: datetime(1900, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(json, "loads", fenced_loads)
    release_payload.set()
    assert after_clock_sample.wait(timeout=5)
    client._clock = original_clock
    release_decode.set()

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert "error" not in result, repr(result.get("error"))
    value = result["value"]
    assert value.observed_at != "1900-01-01T00:00:00+00:00"
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
