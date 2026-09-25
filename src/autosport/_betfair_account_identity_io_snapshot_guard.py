"""Seal K07 Betfair account-detail acquisition against transient client mutation.

The K07 identity authority already proves that the live client is the exact
product-built authenticated context before and after acquisition. A second
thread could nevertheless transiently replace mutable client dispatch/state
while provider I/O was in flight and restore it before the post-check. This
composition layer leaves the canonical Betfair parser/RPC implementation in
place, but routes the authority-bearing RPC through a closure-hidden snapshot
client built from product-captured credentials, clock and transport code.
"""
from __future__ import annotations

from threading import local
from types import FunctionType, MethodType
from weakref import WeakKeyDictionary

from . import betfair_account_identity as _identity
from . import betfair_account_readonly as _readonly


def _install_guard() -> None:
    client_type = _readonly.BetfairReadOnlyClient
    credentials_type = _readonly.BetfairSessionCredentials
    transport_type = _readonly.UrllibBetfairHttpTransport
    identity_error = _identity.BetfairAccountIdentityError

    original_build = _identity.build_betfair_authenticated_client
    original_resolve = _identity.resolve_betfair_authenticated_account_identity
    original_getattribute = client_type.__getattribute__

    canonical_rpc = client_type._rpc
    canonical_next_request_id = client_type._next_request_id
    canonical_observed_at = client_type._observed_at
    canonical_transport_post = transport_type.post

    records: WeakKeyDictionary = WeakKeyDictionary()
    active = local()

    def clone_function(function: object, *, label: str) -> FunctionType:
        if type(function) is not FunctionType:
            raise identity_error(f"canonical {label} is not a plain product function")
        cloned = FunctionType(
            function.__code__,
            dict(function.__globals__),
            function.__name__,
            function.__defaults__,
            function.__closure__,
        )
        cloned.__kwdefaults__ = (
            None if function.__kwdefaults__ is None else dict(function.__kwdefaults__)
        )
        return cloned

    sealed_rpc = clone_function(canonical_rpc, label="Betfair RPC")
    sealed_next_request_id = clone_function(
        canonical_next_request_id,
        label="Betfair request-id allocator",
    )
    sealed_observed_at = clone_function(
        canonical_observed_at,
        label="Betfair observation clock adapter",
    )
    sealed_transport_post = clone_function(
        canonical_transport_post,
        label="Betfair HTTP transport",
    )

    def guarded_getattribute(client: object, name: str):
        if name == "_rpc":
            mapping = getattr(active, "by_client_id", None)
            if mapping is not None:
                record = mapping.get(id(client))
                if record is not None and record[0]() is client:
                    return record[1]
        return original_getattribute(client, name)

    client_type.__getattribute__ = guarded_getattribute

    def build_client(
        credentials,
        *,
        timeout_seconds: float = 10.0,
        account_label: str = "authenticated-account",
    ):
        if client_type.__getattribute__ is not guarded_getattribute:
            raise identity_error("K07 acquisition snapshot dispatch was rebound")
        if transport_type.post is not canonical_transport_post:
            raise identity_error(
                "invalid origin: canonical Betfair client/network implementation changed"
            )
        client = original_build(
            credentials,
            timeout_seconds=timeout_seconds,
            account_label=account_label,
        )

        state = original_getattribute(client, "__dict__")
        live_credentials = state.get("_credentials")
        live_transport = state.get("_transport")
        live_clock = state.get("_clock")
        live_timeout = state.get("_timeout_seconds")
        live_venue = state.get("_venue_id")
        live_account = state.get("_account_id")
        if (
            type(live_credentials) is not credentials_type
            or type(live_transport) is not transport_type
            or type(live_timeout) is not float
            or type(live_venue) is not str
            or type(live_account) is not str
        ):
            raise identity_error("canonical K07 client origin cannot be snapshotted")

        # Copy secret values into a closure-hidden credentials object. The public
        # live credentials object may be transiently mutated in-place; the request
        # snapshot must not share that mutable object identity.
        sealed_credentials = credentials_type(
            live_credentials.application_key,
            live_credentials.session_token,
        )
        sealed_clock = clone_function(live_clock, label="Betfair product clock")

        max_response_bytes = original_getattribute(
            live_transport, "_max_response_bytes"
        )
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise identity_error("canonical Betfair response bound is invalid")
        sealed_transport = transport_type(max_response_bytes=max_response_bytes)
        # Use the exact canonical transport code object with a frozen globals
        # snapshot (notably the canonical urlopen function). The object itself is
        # closure-hidden so caller code cannot transiently shadow its ``post``.
        sealed_transport.post = MethodType(sealed_transport_post, sealed_transport)

        shadow = client_type(
            sealed_credentials,
            transport=sealed_transport,
            timeout_seconds=live_timeout,
            clock=sealed_clock,
            venue_id=live_venue,
            account_id=live_account,
        )
        shadow._next_request_id = MethodType(sealed_next_request_id, shadow)
        shadow._observed_at = MethodType(sealed_observed_at, shadow)

        def snapshot_rpc(method: str, params):
            return sealed_rpc(shadow, method, params)

        # WeakKeyDictionary prevents the guard from extending the real client's
        # lifetime; the shadow is reachable only through this closure-hidden value.
        import weakref

        records[client] = (weakref.ref(client), snapshot_rpc)
        return client

    def resolve_identity(
        client,
        *,
        mode=_identity.BetfairAccountIdentityMode.PERSONAL_DEVELOPER,
    ):
        if client_type.__getattribute__ is not guarded_getattribute:
            raise identity_error("K07 acquisition snapshot dispatch was rebound")
        record = records.get(client)
        if record is None:
            # Preserve canonical error semantics for direct/non-product clients.
            return original_resolve(client, mode=mode)

        mapping = getattr(active, "by_client_id", None)
        if mapping is None:
            mapping = {}
            active.by_client_id = mapping
        previous = mapping.get(id(client))
        mapping[id(client)] = record
        try:
            return original_resolve(client, mode=mode)
        finally:
            if previous is None:
                mapping.pop(id(client), None)
            else:
                mapping[id(client)] = previous

    build_client._autosport_k07_io_snapshot_sealed = True
    resolve_identity._autosport_k07_io_snapshot_sealed = True
    _identity.build_betfair_authenticated_client = build_client
    _identity.resolve_betfair_authenticated_account_identity = resolve_identity


_install_guard()
del _install_guard
