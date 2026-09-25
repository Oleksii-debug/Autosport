"""Seal K07 Betfair account-detail acquisition at the owning read seam.

K07 proves only a process-local authenticated SESSION_CONTEXT.  The canonical
identity authority validates the live client before and after account-details
acquisition, but ordinary Python function metadata can expose predecessor
callables retained by a composition wrapper.  A wrapper-only snapshot is
therefore insufficient: invoking the predecessor resolver directly can bypass
it.

This guard snapshots the exact canonical client's credential/transport/clock
inputs when the client itself is constructed, installs a sealed
``read_account_details`` implementation, and retargets the existing K07
factory's captured canonical dispatch cells to those guarded callables.  The
underlying resolver is therefore safe even when obtained through ``__closure__``
and invoked directly.

All authority-bearing cloned functions carry an identity snapshot of every
directly-read global binding plus ``__builtins__``.  The private JSON facade is
also checked before build/read/resolve.  No stable account identity, provider
write, funds, execution, or real-money authority is introduced.
"""
from __future__ import annotations

import json
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
    details_type = _readonly.BetfairAccountDetailsObservation
    identity_error = _identity.BetfairAccountIdentityError
    account_details_rpc = _readonly._GET_ACCOUNT_DETAILS

    original_build = _identity.build_betfair_authenticated_client
    original_resolve = _identity.resolve_betfair_authenticated_account_identity
    original_client_init = client_type.__init__
    original_read_account_details = client_type.read_account_details
    original_getattribute = client_type.__getattribute__

    canonical_rpc = client_type._rpc
    canonical_next_request_id = client_type._next_request_id
    canonical_observed_at = client_type._observed_at
    canonical_transport_post = transport_type.post
    canonical_json_dumps = json.dumps
    canonical_json_loads = json.loads
    canonical_json_decode_error = json.JSONDecodeError

    snapshots: WeakKeyDictionary = WeakKeyDictionary()
    missing = object()

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

    def snapshot_globals(function: FunctionType) -> tuple[tuple[str, object], ...]:
        names = sorted(
            name for name in set(function.__code__.co_names) if name in function.__globals__
        )
        if "__builtins__" in function.__globals__:
            names.append("__builtins__")
        return tuple((name, function.__globals__[name]) for name in names)

    def require_snapshot(
        function: FunctionType,
        snapshot: tuple[tuple[str, object], ...],
        label: str,
    ) -> None:
        for name, expected in snapshot:
            if function.__globals__.get(name, missing) is not expected:
                raise identity_error(f"frozen {label} global {name!r} was rebound")

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
    decode_snapshot = snapshot_globals(sealed_decode_json)
    sealed_rpc = clone_function(
        canonical_rpc,
        label="Betfair RPC",
        globals_overrides={
            "json": sealed_json,
            "_decode_json": sealed_decode_json,
        },
    )
    rpc_snapshot = snapshot_globals(sealed_rpc)
    sealed_next_request_id = clone_function(
        canonical_next_request_id,
        label="Betfair request-id allocator",
    )
    request_id_snapshot = snapshot_globals(sealed_next_request_id)
    sealed_observed_at = clone_function(
        canonical_observed_at,
        label="Betfair observation clock adapter",
    )
    observed_at_snapshot = snapshot_globals(sealed_observed_at)
    sealed_transport_post = clone_function(
        canonical_transport_post,
        label="Betfair HTTP transport",
    )
    transport_snapshot = snapshot_globals(sealed_transport_post)

    def require_static_snapshot() -> None:
        if sealed_json.dumps is not canonical_json_dumps:
            raise identity_error("frozen K07 JSON encode dispatch was rebound")
        if sealed_json.loads is not canonical_json_loads:
            raise identity_error("frozen K07 JSON decode dispatch was rebound")
        if sealed_json.JSONDecodeError is not canonical_json_decode_error:
            raise identity_error("frozen K07 JSON error authority was rebound")
        require_snapshot(sealed_decode_json, decode_snapshot, "K07 Betfair JSON decoder")
        require_snapshot(sealed_rpc, rpc_snapshot, "K07 Betfair RPC")
        require_snapshot(
            sealed_next_request_id,
            request_id_snapshot,
            "K07 Betfair request-id allocator",
        )
        require_snapshot(
            sealed_observed_at,
            observed_at_snapshot,
            "K07 Betfair observation clock adapter",
        )
        require_snapshot(
            sealed_transport_post,
            transport_snapshot,
            "K07 Betfair HTTP transport",
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
            if function.__name__ == name
        ]
        if len(matches) != 1:
            raise identity_error(
                f"canonical K07 closure graph has ambiguous {name!r} dispatch"
            )
        return matches[0]

    def _set_freevar(function: FunctionType, name: str, value: object) -> None:
        closure = function.__closure__
        if closure is None or name not in function.__code__.co_freevars:
            raise identity_error(f"canonical K07 closure is missing {name!r}")
        index = function.__code__.co_freevars.index(name)
        closure[index].cell_contents = value

    def guarded_client_init(self, *args, **kwargs) -> None:
        require_static_snapshot()
        original_client_init(self, *args, **kwargs)
        state = original_getattribute(self, "__dict__")
        credentials = state.get("_credentials")
        transport = state.get("_transport")
        clock = state.get("_clock")
        timeout_seconds = state.get("_timeout_seconds")
        venue_id = state.get("_venue_id")
        account_id = state.get("_account_id")

        # Noncanonical/custom transports are legitimate for the ordinary read-only
        # adapter and tests, but they can never become K07 positive origin.  Leave
        # those reads on the original adapter path.  K07's own builder requires the
        # exact canonical transport and will therefore always receive a snapshot.
        if type(transport) is not transport_type or type(clock) is not FunctionType:
            snapshots.pop(self, None)
            return
        max_response_bytes = original_getattribute(transport, "_max_response_bytes")
        if (
            type(credentials) is not credentials_type
            or type(max_response_bytes) is not int
            or max_response_bytes <= 0
            or type(timeout_seconds) is not float
            or type(venue_id) is not str
            or type(account_id) is not str
        ):
            snapshots.pop(self, None)
            return

        sealed_credentials = credentials_type(
            credentials.application_key,
            credentials.session_token,
        )
        sealed_clock = clone_function(clock, label="Betfair product clock")
        clock_snapshot = snapshot_globals(sealed_clock)
        snapshots[self] = (
            sealed_credentials,
            sealed_clock,
            clock_snapshot,
            max_response_bytes,
            timeout_seconds,
            venue_id,
            account_id,
        )

    def guarded_read_account_details(client):
        require_static_snapshot()
        record = snapshots.get(client)
        if record is None:
            return original_read_account_details(client)
        (
            sealed_credentials,
            sealed_clock,
            clock_snapshot,
            max_response_bytes,
            timeout_seconds,
            venue_id,
            account_id,
        ) = record
        require_snapshot(sealed_clock, clock_snapshot, "K07 Betfair product clock")

        sealed_transport = transport_type(max_response_bytes=max_response_bytes)
        sealed_transport.post = MethodType(sealed_transport_post, sealed_transport)
        shadow = object.__new__(client_type)
        original_client_init(
            shadow,
            sealed_credentials,
            transport=sealed_transport,
            timeout_seconds=timeout_seconds,
            clock=sealed_clock,
            venue_id=venue_id,
            account_id=account_id,
        )
        shadow._next_request_id = MethodType(sealed_next_request_id, shadow)
        shadow._observed_at = MethodType(sealed_observed_at, shadow)

        captured: list[object] = []

        def snapshot_rpc(method: str, params):
            require_static_snapshot()
            require_snapshot(sealed_clock, clock_snapshot, "K07 Betfair product clock")
            if method != account_details_rpc or type(params) is not dict or params:
                raise identity_error(
                    "K07 acquisition attempted a non-canonical account-details RPC"
                )
            if captured:
                raise identity_error(
                    "K07 acquisition produced multiple account-details RPC results"
                )
            result = sealed_rpc(shadow, method, params)
            captured.append(result)
            return result

        shadow._rpc = snapshot_rpc
        details = original_read_account_details(shadow)
        require_static_snapshot()
        require_snapshot(sealed_clock, clock_snapshot, "K07 Betfair product clock")
        if type(details) is not details_type or len(captured) != 1:
            raise identity_error(
                "K07 identity is not bound to one captured account-details result"
            )
        rpc_result = captured[0]
        if type(rpc_result) is not rpc_result_type:
            raise identity_error(
                "K07 identity is not bound to canonical account-details RPC evidence"
            )
        result = rpc_result.result
        evidence = rpc_result.evidence
        if type(result) is not dict:
            raise identity_error("K07 captured account-details result is not canonical JSON")
        currency = result.get("currencyCode")
        if type(currency) is not str or currency != details.currency_code:
            raise identity_error(
                "K07 identity currency diverged from captured provider response"
            )
        if type(evidence) is not evidence_type:
            raise identity_error("K07 captured account-details evidence is not canonical")
        if (
            details.evidence.source_payload_sha256 != evidence.source_payload_sha256
            or details.evidence.observed_at != evidence.observed_at
        ):
            raise identity_error(
                "K07 identity evidence diverged from captured provider response"
            )
        return details

    # Install at the owning read seam first.  Then retarget the existing K07
    # factory's captured canonical cells so its own dispatch verifier sees these
    # exact guards as canonical.  Because those outer cells are shared, the
    # predecessor resolver itself now invokes guarded_read_account_details.
    client_type.__init__ = guarded_client_init
    client_type.read_account_details = guarded_read_account_details
    dispatch_check = _reachable_named(original_build, "client_class_dispatch_is_current")
    _set_freevar(dispatch_check, "canonical_client_init", guarded_client_init)
    _set_freevar(
        dispatch_check,
        "canonical_read_account_details",
        guarded_read_account_details,
    )

    # Prove the resolver shares the retargeted read cell.  If the implementation
    # topology changes, fail closed at import rather than silently reinstalling a
    # wrapper that leaves a predecessor bypass reachable.
    resolve_freevars = original_resolve.__code__.co_freevars
    if "canonical_read_account_details" not in resolve_freevars:
        raise identity_error("canonical K07 resolver lost its account-details read seam")
    resolve_cell = original_resolve.__closure__[  # type: ignore[index]
        resolve_freevars.index("canonical_read_account_details")
    ]
    if resolve_cell.cell_contents is not guarded_read_account_details:
        raise identity_error("canonical K07 resolver did not adopt sealed read authority")

    def build_client(
        credentials,
        *,
        timeout_seconds: float = 10.0,
        account_label: str = "authenticated-account",
    ):
        require_static_snapshot()
        if (
            client_type.__init__ is not guarded_client_init
            or client_type.read_account_details is not guarded_read_account_details
        ):
            raise identity_error("K07 owning client/read dispatch was rebound")
        client = original_build(
            credentials,
            timeout_seconds=timeout_seconds,
            account_label=account_label,
        )
        if snapshots.get(client) is None:
            raise identity_error("canonical K07 client lacks construction-time IO snapshot")
        return client

    def resolve_identity(
        client,
        *,
        mode=_identity.BetfairAccountIdentityMode.PERSONAL_DEVELOPER,
    ):
        require_static_snapshot()
        if (
            client_type.__init__ is not guarded_client_init
            or client_type.read_account_details is not guarded_read_account_details
        ):
            raise identity_error("K07 owning client/read dispatch was rebound")
        value = original_resolve(client, mode=mode)
        require_static_snapshot()
        return value

    guarded_client_init._autosport_k07_io_snapshot_sealed = True
    guarded_read_account_details._autosport_k07_io_snapshot_sealed = True
    build_client._autosport_k07_io_snapshot_sealed = True
    resolve_identity._autosport_k07_io_snapshot_sealed = True
    _identity.build_betfair_authenticated_client = build_client
    _identity.resolve_betfair_authenticated_account_identity = resolve_identity


_install_guard()
del _install_guard
