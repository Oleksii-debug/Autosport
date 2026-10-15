from __future__ import annotations

from datetime import datetime, timezone
import json
from threading import Event, Thread
from types import FunctionType
import urllib.request as _urllib_request
from weakref import WeakKeyDictionary

import pytest

from autosport import betfair_account_identity as _identity
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
    if name == "_rpc" and len(matches) > 1:
        # The guard intentionally retains the canonical predecessor RPC and one
        # sealed persistent RPC template. Target the sealed template rather than
        # treating reachable helper multiplicity as duplicate product authority.
        matches = [
            function
            for function in matches
            if type(function.__globals__.get("_decode_json")) is FunctionType
            and function.__globals__.get("_decode_json") is not _readonly._decode_json
        ]
    assert len(matches) == 1, [function.__name__ for function in matches]
    return matches[0]


def _reachable_weak_registries(root: FunctionType) -> tuple[WeakKeyDictionary, ...]:
    found: dict[int, WeakKeyDictionary] = {}
    for function in _reachable_functions(root):
        if function.__closure__ is None:
            continue
        for cell in function.__closure__:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if isinstance(value, WeakKeyDictionary):
                found[id(value)] = value
    return tuple(found.values())



def test_transient_clock_substitution_during_provider_io_cannot_backdate_identity(
    monkeypatch,
) -> None:
    read_entered = Event()
    release_payload = Event()
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

    # The provider read is already in flight and the per-call shadow owns the
    # construction-time product clock. A mutate/restore race on the escaped live
    # client must therefore be irrelevant to the observation timestamp.
    client._clock = lambda: datetime(1900, 1, 1, tzinfo=timezone.utc)
    client._clock = original_clock
    release_payload.set()

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
        forged_parser_entered.set()
        if field == "currency_code":
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

    # fresh_call_clones() completed before transport.read() blocked. Mutating the
    # module parser now must not enter the already-sealed per-call reader at all.
    monkeypatch.setattr(_readonly, "_provider_text", forged_provider_text)
    release_payload.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert not forged_parser_entered.is_set()
    assert "error" not in result, repr(result.get("error"))
    value = result["value"]
    assert value.currency_code == "EUR"
    assert is_authoritative_betfair_account_identity(value, client=client)

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


def test_k07_uses_one_existing_origin_registry_not_parallel_snapshot_state() -> None:
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token")
    )
    registries = [
        registry
        for registry in _reachable_weak_registries(build_betfair_authenticated_client)
        if client in registry
    ]
    origin_registries = [
        registry
        for registry in registries
        if type(registry.get(client)) is _identity._CanonicalClientOrigin
    ]

    # The closure graph may legitimately expose unrelated weak registries owned by
    # adjacent helpers. K07 must still have exactly one canonical-origin registry;
    # a second acquisition-origin registry would violate the authority contract.
    assert len(origin_registries) == 1



def test_transient_private_rpc_decode_swap_during_io_cannot_launder_identity(
    monkeypatch,
) -> None:
    read_entered = Event()
    release_payload = Event()
    original_loads = json.loads
    sealed_rpc = _reachable_named(build_betfair_authenticated_client, "_rpc")
    original_decode = sealed_rpc.__globals__["_decode_json"]

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

    def forged_decode(payload: bytes):
        decoded = original_decode(payload)
        forged = dict(decoded)
        forged_result = dict(decoded["result"])
        forged_result["currencyCode"] = "GBP"
        forged["result"] = forged_result
        return forged

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

    # The active call already owns a private RPC clone. A transient mutation of
    # the persistent template during blocked provider I/O cannot redirect it.
    sealed_rpc.__globals__["_decode_json"] = forged_decode
    sealed_rpc.__globals__["_decode_json"] = original_decode
    release_payload.set()

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert "error" not in result, repr(result.get("error"))
    value = result["value"]
    assert value.currency_code == "EUR"
    assert is_authoritative_betfair_account_identity(value, client=client)

def test_private_json_facade_mutation_fails_closed_before_build(monkeypatch) -> None:
    sealed_rpc = _reachable_named(build_betfair_authenticated_client, "_rpc")
    private_json = sealed_rpc.__globals__["json"]
    monkeypatch.setattr(private_json, "loads", lambda *_args, **_kwargs: {"forged": True})

    with pytest.raises(
        BetfairAccountIdentityError,
        match="frozen K07 JSON decode dispatch was rebound",
    ):
        build_betfair_authenticated_client(
            BetfairSessionCredentials("app-key", "session-token")
        )


def test_private_rpc_clone_global_mutation_fails_closed_before_build(monkeypatch) -> None:
    sealed_rpc = _reachable_named(build_betfair_authenticated_client, "_rpc")
    monkeypatch.setitem(
        sealed_rpc.__globals__,
        "_decode_json",
        lambda _payload: {"forged": True},
    )

    with pytest.raises(
        BetfairAccountIdentityError,
        match=r"frozen K07 Betfair RPC global '_decode_json' was rebound",
    ):
        build_betfair_authenticated_client(
            BetfairSessionCredentials("app-key", "session-token")
        )


def test_private_decode_clone_global_mutation_fails_closed_before_build(monkeypatch) -> None:
    sealed_decode = _reachable_named(build_betfair_authenticated_client, "_decode_json")
    monkeypatch.setitem(sealed_decode.__globals__, "json", object())

    with pytest.raises(
        BetfairAccountIdentityError,
        match=r"frozen K07 Betfair JSON decoder global 'json' was rebound",
    ):
        build_betfair_authenticated_client(
            BetfairSessionCredentials("app-key", "session-token")
        )


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
