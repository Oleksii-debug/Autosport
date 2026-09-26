from __future__ import annotations

from datetime import datetime, timezone
import json
from threading import Event, Thread
from types import FunctionType
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


def _predecessor_resolver() -> FunctionType:
    matches = [
        function
        for function in _reachable_functions(resolve_betfair_authenticated_account_identity)
        if function is not resolve_betfair_authenticated_account_identity
        and function.__name__ == "resolve_identity"
    ]
    assert len(matches) == 1, [function.__name__ for function in matches]
    return matches[0]


def test_recovered_predecessor_resolver_rejects_read_cell_rebinding() -> None:
    """A recovered predecessor cannot be rewired back to the unguarded read seam."""

    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token")
    )
    predecessor = _predecessor_resolver()
    freevars = predecessor.__code__.co_freevars
    assert "canonical_read_account_details" in freevars
    read_cell = predecessor.__closure__[
        freevars.index("canonical_read_account_details")
    ]
    guarded_read = read_cell.cell_contents

    original_reads = [
        function
        for function in _reachable_functions(resolve_betfair_authenticated_account_identity)
        if function.__name__ == "read_account_details"
        and function is not guarded_read
    ]
    assert len(original_reads) == 1, [function.__name__ for function in original_reads]

    read_cell.cell_contents = original_reads[0]
    try:
        with pytest.raises(
            BetfairAccountIdentityError,
            match="canonical Betfair account-details read authority changed",
        ):
            predecessor(client)
    finally:
        read_cell.cell_contents = guarded_read


def test_recovered_predecessor_resolver_uses_construction_time_snapshot(
    monkeypatch,
) -> None:
    """Function-metadata access cannot resurrect the old mutate/restore race."""

    read_entered = Event()
    release_payload = Event()
    parser_entered = Event()
    release_parser = Event()
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
        parser_entered.set()
        assert release_parser.wait(timeout=5)
        return original_provider_text(*args, **kwargs)

    monkeypatch.setattr(_urllib_request, "_opener", Opener())
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token")
    )
    predecessor = _predecessor_resolver()
    original_clock = client._clock
    result: dict[str, object] = {}

    def resolve() -> None:
        try:
            result["value"] = predecessor(client)
        except BaseException as exc:  # pragma: no cover - surfaced below
            result["error"] = exc

    worker = Thread(target=resolve)
    worker.start()
    assert read_entered.wait(timeout=5)

    # Mutate the live client only while provider I/O is in flight, then restore it
    # before the predecessor resolver's post-acquisition context check.  The old
    # implementation could accept the transient clock; the owning read-seam
    # snapshot must remain bound to the construction-time clock instead.
    client._clock = lambda: datetime(1900, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(_readonly, "_provider_text", fenced_provider_text)
    release_payload.set()
    assert parser_entered.wait(timeout=5)
    client._clock = original_clock
    monkeypatch.setattr(_readonly, "_provider_text", original_provider_text)
    release_parser.set()

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert "error" not in result, repr(result.get("error"))
    value = result["value"]
    assert value.currency_code == "EUR"
    assert value.observed_at != "1900-01-01T00:00:00+00:00"
    assert is_authoritative_betfair_account_identity(value, client=client)
