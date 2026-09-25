from __future__ import annotations

import json
from threading import Event, Thread
from types import FunctionType
import urllib.request as _urllib_request

import pytest

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


def _reachable_functions(root: FunctionType) -> tuple[FunctionType, ...]:
    pending: list[object] = [root]
    seen: set[int] = set()
    found: list[FunctionType] = []
    while pending:
        value = pending.pop()
        if not isinstance(value, FunctionType) or id(value) in seen:
            continue
        seen.add(id(value))
        found.append(value)
        if value.__defaults__:
            pending.extend(value.__defaults__)
        if value.__kwdefaults__:
            pending.extend(value.__kwdefaults__.values())
        wrapped = getattr(value, "__wrapped__", None)
        if wrapped is not None:
            pending.append(wrapped)
        if value.__closure__:
            for cell in value.__closure__:
                try:
                    pending.append(cell.cell_contents)
                except ValueError:
                    pass
    return tuple(found)


def _reachable_named(root: FunctionType, name: str) -> FunctionType:
    matches = [
        function
        for function in _reachable_functions(root)
        if function is not root and function.__name__ == name
    ]
    assert len(matches) == 1, [function.__name__ for function in matches]
    return matches[0]


def test_hidden_decoder_code_rebinding_fails_closed_before_client_build() -> None:
    sealed_decode = _reachable_named(build_betfair_authenticated_client, "_decode_json")
    original_code = sealed_decode.__code__

    def forged_decode(_payload: bytes):
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"currencyCode": "GBP"},
        }

    sealed_decode.__code__ = forged_decode.__code__
    try:
        with pytest.raises(
            BetfairAccountIdentityError,
            match="frozen K07 Betfair JSON decoder code was rebound",
        ):
            build_betfair_authenticated_client(
                BetfairSessionCredentials("app-key", "session-token")
            )
    finally:
        sealed_decode.__code__ = original_code


def test_transient_hidden_decoder_code_swap_during_io_cannot_launder_identity(
    monkeypatch,
) -> None:
    read_entered = Event()
    release_payload = Event()
    original_loads = json.loads
    sealed_decode = _reachable_named(build_betfair_authenticated_client, "_decode_json")
    original_code = sealed_decode.__code__

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

    def forged_decode(_payload: bytes):
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "currencyCode": "GBP",
                "localeCode": "en",
                "region": "GBR",
                "timezone": "Europe/London",
            },
        }

    monkeypatch.setattr(_urllib_request, "_opener", Opener())
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token")
    )
    result: dict[str, object] = {}

    def resolve() -> None:
        try:
            result["value"] = resolve_betfair_authenticated_account_identity(client)
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["error"] = exc

    worker = Thread(target=resolve)
    worker.start()
    assert read_entered.wait(timeout=5)

    # The active authority-bearing call already owns a fresh decoder built from
    # frozen metadata. Mutating the inspectable persistent template now cannot
    # alter the bytes->JSON path used by this in-flight observation.
    sealed_decode.__code__ = forged_decode.__code__
    release_payload.set()
    worker.join(timeout=5)
    sealed_decode.__code__ = original_code

    assert not worker.is_alive()
    assert "error" not in result, repr(result.get("error"))
    value = result["value"]
    assert value.currency_code == "EUR"
    assert is_authoritative_betfair_account_identity(value, client=client)
