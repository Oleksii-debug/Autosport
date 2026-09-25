"""Seal K07 Betfair account-detail acquisition against transient client mutation.

The K07 identity authority already proves that the live client is the exact
product-built authenticated context before and after acquisition. A second
thread could nevertheless transiently replace mutable client dispatch/state
while provider I/O was in flight and restore it before the post-check. This
composition layer leaves the canonical Betfair parser/RPC implementation in
place, but routes the authority-bearing RPC through a closure-hidden snapshot
client built from product-captured credentials, clock and transport code.

The snapshot also freezes JSON encode/decode dispatch and binds the final
identity fields back to the exact captured canonical RPC result. This prevents
a transient live-parser/global substitution from laundering caller-selected
currency or evidence metadata while preserving the real provider payload hash.
"""
from __future__ import annotations

import json
from threading import local
from types import FunctionType, MethodType, SimpleNamespace
from weakref import WeakKeyDictionary

from . import betfair_account_identity as _identity
from . import betfair_account_readonly as _readonly


def _install_guard() -> None:
    client_type = _readonly.BetfairReadOnlyClient
    credentials_type = _readonly.BetfairSessionCredentials
    transport_type = _readonly.UrllibBetfairHttpTransport
    rpc_result_type = _readonly._RpcResult
    evidence_type = _readonly.BetfairEvidence
    identity_error = _identity.BetfairAccountIdentityError
    account_details_rpc = _readonly._GET_ACCOUNT_DETAILS

    original_build = _identity.build_betfair_authenticated_client
    original_resolve = _identity.resolve_betfair_authenticated_account_identity
    original_getattribute = client_type.__getattribute__

    canonical_rpc = client_type._rpc
    canonical_next_request_id = client_type._next_request_id
    canonical_observed_at = client_type._observed_at
    canonical_transport_post = transport_type.post
    canonical_json_dumps = json.dumps
    canonical_json_loads = json.loads
    canonical_json_decode_error = json.JSONDecodeError

    records: WeakKeyDictionary = WeakKeyDictionary()
    active = local()

    def clone_function(
        function: object,
        *,
        label: str,
        globals_overrides: dict[str, object] | None = None,
    ) -> FunctionType:
        if type(function) is not FunctionType:
            raise identity_error(f"canonical {label} is not a plain product function")
        globals_snapshot = dict(function.__globals__)
        if globals_overrides:
            globals_snapshot.update(globals_overrides)
        cloned = FunctionType(
            function.__code__,
            globals_snapshot,
            function.__name__,
            function.__defaults__,
            function.__closure__,
        )
        cloned.__kwdefaults__ = (
            None if function.__kwdefaults__ is None else dict(function.__kwdefaults__)
        )
        return cloned

    # A copied globals dictionary alone is not enough for ``json`` because the
    # module object itself is mutable. Keep only the import-time functions/error
    # class behind a closure-hidden namespace and inject that into the sealed
    # encode/decode functions.
    sealed_json = SimpleNamespace(
        dumps=canonical_json_dumps,
        loads=canonical_json_loads,
        JSONDecodeError=canonical_json_decode_error,
    )
    sealed_decode_json = clone_function(
        _readonly._decode_json,
        label="Betfair JSON decoder",
        globals_overrides={"json": sealed_json},
    )
    sealed_rpc = clone_function(
        canonical_rpc,
        label="Betfair RPC",
        globals_overrides={
            "json": sealed_json,
            "_decode_json": sealed_decode_json,
        },
    )
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
            # Identity resolution owns exactly one empty-parameter account-details
            # RPC. A transient parser/global rebind must not be able to redirect
            # the sealed transport to a different read (or any future method).
            if method != account_details_rpc or type(params) is not dict or params:
                raise identity_error(
                    "K07 acquisition attempted a non-canonical account-details RPC"
                )
            result = sealed_rpc(shadow, method, params)
            captures = getattr(active, "rpc_result_by_client_id", None)
            if captures is not None:
                identity = id(client)
                if identity in captures:
                    raise identity_error(
                        "K07 acquisition produced multiple account-details RPC results"
                    )
                captures[identity] = result
            return result

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
        captures = getattr(active, "rpc_result_by_client_id", None)
        if captures is None:
            captures = {}
            active.rpc_result_by_client_id = captures

        identity = id(client)
        previous = mapping.get(identity)
        had_previous_capture = identity in captures
        previous_capture = captures.get(identity)
        captures.pop(identity, None)
        mapping[identity] = record
        try:
            value = original_resolve(client, mode=mode)
            captured = captures.pop(identity, None)
            if type(captured) is not rpc_result_type:
                raise identity_error(
                    "K07 identity is not bound to one captured account-details result"
                )
            result = captured.result
            evidence = captured.evidence
            if type(result) is not dict:
                raise identity_error(
                    "K07 captured account-details result is not canonical JSON"
                )
            currency = result.get("currencyCode")
            if type(currency) is not str or currency != value.currency_code:
                raise identity_error(
                    "K07 identity currency diverged from captured provider response"
                )
            if type(evidence) is not evidence_type:
                raise identity_error("K07 captured account-details evidence is not canonical")
            if (
                value.account_details_sha256 != evidence.source_payload_sha256
                or value.observed_at != evidence.observed_at
            ):
                raise identity_error(
                    "K07 identity evidence diverged from captured provider response"
                )
            return value
        finally:
            captures.pop(identity, None)
            if had_previous_capture:
                captures[identity] = previous_capture
            if previous is None:
                mapping.pop(identity, None)
            else:
                mapping[identity] = previous

    build_client._autosport_k07_io_snapshot_sealed = True
    resolve_identity._autosport_k07_io_snapshot_sealed = True
    _identity.build_betfair_authenticated_client = build_client
    _identity.resolve_betfair_authenticated_account_identity = resolve_identity


_install_guard()
del _install_guard
