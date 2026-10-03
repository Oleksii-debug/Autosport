"""Seal K07 Betfair account-detail acquisition at the owning read seam.

K07 proves only a process-local authenticated SESSION_CONTEXT.  The canonical
identity authority validates the live client before and after account-details
acquisition, but ordinary Python function metadata can expose predecessor
callables retained by a composition wrapper.  A wrapper-only snapshot is
therefore insufficient: invoking the predecessor resolver directly can bypass
it.

This guard uses the existing K07 origin/context authority rather than creating a
second mutable client snapshot registry.  The K07 factory seals its product
clock before returning a client and rewrites its already-canonical origin to the
same sealed clock.  Account-detail acquisition then consumes the exact origin
held by the active K07 context, so live-client mutate/restore races and metadata
access cannot redirect the accepted provider observation to a sibling snapshot.

RPC/parser/transport clones are retained only as verified import-time templates.
Each authority-bearing read copies them into fresh per-call functions before
provider I/O and verifies the copies against frozen template metadata and global
bindings.  Frozen code/default/closure metadata is supplied explicitly to the
copy operation, so replacing an inspectable template's ``__code__`` between a
pre-check and cloning cannot become authority-bearing code.

All persistent authority-bearing cloned functions carry an identity snapshot of
their executable metadata, every directly-read global binding and
``__builtins__``.  The private JSON facade is also checked before
build/read/resolve.  No stable account identity, provider write, funds,
execution, or real-money authority is introduced.
"""
from __future__ import annotations

import json
from types import FunctionType, MethodType, SimpleNamespace

from . import betfair_account_identity as _identity
from . import betfair_account_readonly as _readonly


def _install_guard() -> None:
    client_type = _readonly.BetfairReadOnlyClient
    credentials_type = _readonly.BetfairSessionCredentials
    transport_type = _readonly.UrllibBetfairHttpTransport
    rpc_result_type = _readonly._RpcResult
    evidence_type = _readonly.BetfairEvidence
    details_type = _readonly.BetfairAccountDetailsObservation
    context_record_type = _identity._ClientContextRecord
    origin_type = _identity._CanonicalClientOrigin
    identity_error = _identity.BetfairAccountIdentityError
    account_details_rpc = _readonly._GET_ACCOUNT_DETAILS
    venue_id = _identity.VENUE_ID

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

    missing = object()

    def cell_for(value: object):
        def capture():
            return value

        closure = capture.__closure__
        if closure is None:  # pragma: no cover - Python closure invariant
            raise identity_error("cannot construct frozen K07 closure cell")
        return closure[0]

    def snapshot_metadata(function: FunctionType, label: str) -> tuple[object, ...]:
        if type(function) is not FunctionType:
            raise identity_error(f"canonical {label} is not a plain product function")
        defaults = function.__defaults__
        default_values = None if defaults is None else tuple(defaults)
        kwdefaults = function.__kwdefaults__
        kwdefault_values = (
            None
            if kwdefaults is None
            else tuple(sorted(kwdefaults.items(), key=lambda item: item[0]))
        )
        closure = function.__closure__
        closure_values: tuple[object, ...] | None
        if closure is None:
            closure_values = None
        else:
            values: list[object] = []
            for cell in closure:
                try:
                    values.append(cell.cell_contents)
                except ValueError as exc:
                    raise identity_error(
                        f"canonical {label} has an empty closure cell"
                    ) from exc
            closure_values = tuple(values)
        return (
            function.__code__,
            function.__name__,
            default_values,
            kwdefault_values,
            closure_values,
        )

    def metadata_matches(
        function: FunctionType,
        metadata: tuple[object, ...],
        label: str,
    ) -> None:
        code, _name, expected_defaults, expected_kwdefaults, expected_closure = metadata
        if function.__code__ is not code:
            raise identity_error(f"frozen {label} code was rebound")

        live_defaults = function.__defaults__
        if expected_defaults is None:
            if live_defaults is not None:
                raise identity_error(f"frozen {label} defaults were rebound")
        elif live_defaults is None or len(live_defaults) != len(expected_defaults):
            raise identity_error(f"frozen {label} defaults were rebound")
        elif any(
            live is not expected
            for live, expected in zip(live_defaults, expected_defaults, strict=True)
        ):
            raise identity_error(f"frozen {label} defaults were rebound")

        live_kwdefaults = function.__kwdefaults__
        if expected_kwdefaults is None:
            if live_kwdefaults is not None:
                raise identity_error(f"frozen {label} kwdefaults were rebound")
        else:
            expected_kw_map = dict(expected_kwdefaults)
            if live_kwdefaults is None or set(live_kwdefaults) != set(expected_kw_map):
                raise identity_error(f"frozen {label} kwdefaults were rebound")
            if any(
                live_kwdefaults[key] is not expected
                for key, expected in expected_kw_map.items()
            ):
                raise identity_error(f"frozen {label} kwdefaults were rebound")

        live_closure = function.__closure__
        if expected_closure is None:
            if live_closure is not None:
                raise identity_error(f"frozen {label} closure was rebound")
        elif live_closure is None or len(live_closure) != len(expected_closure):
            raise identity_error(f"frozen {label} closure was rebound")
        else:
            for cell, expected in zip(live_closure, expected_closure, strict=True):
                try:
                    live = cell.cell_contents
                except ValueError as exc:
                    raise identity_error(f"frozen {label} closure was rebound") from exc
                if live is not expected:
                    raise identity_error(f"frozen {label} closure was rebound")

    def clone_function(
        function: object,
        *,
        label: str,
        globals_overrides: dict[str, object] | None = None,
        metadata: tuple[object, ...] | None = None,
    ) -> FunctionType:
        if type(function) is not FunctionType:
            raise identity_error(f"canonical {label} is not a plain product function")
        frozen_metadata = snapshot_metadata(function, label) if metadata is None else metadata
        code, name, default_values, kwdefault_values, closure_values = frozen_metadata
        globals_snapshot = dict(function.__globals__)
        if globals_overrides:
            globals_snapshot.update(globals_overrides)
        closure = (
            None
            if closure_values is None
            else tuple(cell_for(value) for value in closure_values)
        )
        defaults = None if default_values is None else tuple(default_values)
        cloned = FunctionType(
            code,
            globals_snapshot,
            name,
            defaults,
            closure,
        )
        cloned.__kwdefaults__ = (
            None if kwdefault_values is None else dict(kwdefault_values)
        )
        return cloned

    def snapshot_globals(function: FunctionType) -> tuple[tuple[str, object], ...]:
        names = sorted(
            name for name in set(function.__code__.co_names) if name in function.__globals__
        )
        if "__builtins__" in function.__globals__:
            names.append("__builtins__")
        return tuple((name, function.__globals__[name]) for name in names)

    def snapshot_function(
        function: FunctionType,
        label: str,
    ) -> tuple[tuple[object, ...], tuple[tuple[str, object], ...]]:
        return snapshot_metadata(function, label), snapshot_globals(function)

    def require_snapshot(
        function: FunctionType,
        snapshot: tuple[tuple[object, ...], tuple[tuple[str, object], ...]],
        label: str,
        *,
        overrides: dict[str, object] | None = None,
    ) -> None:
        metadata, globals_snapshot = snapshot
        metadata_matches(function, metadata, label)
        expected_overrides = overrides or {}
        for name, expected in globals_snapshot:
            required = expected_overrides.get(name, expected)
            if function.__globals__.get(name, missing) is not required:
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
    decode_snapshot = snapshot_function(sealed_decode_json, "Betfair JSON decoder")
    sealed_rpc = clone_function(
        canonical_rpc,
        label="Betfair RPC",
        globals_overrides={
            "json": sealed_json,
            "_decode_json": sealed_decode_json,
        },
    )
    rpc_snapshot = snapshot_function(sealed_rpc, "Betfair RPC")
    sealed_next_request_id = clone_function(
        canonical_next_request_id,
        label="Betfair request-id allocator",
    )
    request_id_snapshot = snapshot_function(
        sealed_next_request_id,
        "Betfair request-id allocator",
    )
    sealed_observed_at = clone_function(
        canonical_observed_at,
        label="Betfair observation clock adapter",
    )
    observed_at_snapshot = snapshot_function(
        sealed_observed_at,
        "Betfair observation clock adapter",
    )
    sealed_transport_post = clone_function(
        canonical_transport_post,
        label="Betfair HTTP transport",
    )
    transport_snapshot = snapshot_function(
        sealed_transport_post,
        "Betfair HTTP transport",
    )

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

    def _freevar_value(function: FunctionType, name: str) -> object:
        closure = function.__closure__
        if closure is None or name not in function.__code__.co_freevars:
            raise identity_error(f"canonical K07 closure is missing {name!r}")
        index = function.__code__.co_freevars.index(name)
        return closure[index].cell_contents

    def _set_freevar(function: FunctionType, name: str, value: object) -> None:
        closure = function.__closure__
        if closure is None or name not in function.__code__.co_freevars:
            raise identity_error(f"canonical K07 closure is missing {name!r}")
        index = function.__code__.co_freevars.index(name)
        closure[index].cell_contents = value

    context_for = _reachable_named(original_resolve, "context_for")
    canonical_origins = _freevar_value(original_build, "canonical_client_origins")
    context_origins = _freevar_value(context_for, "canonical_client_origins")
    client_contexts = _freevar_value(context_for, "client_contexts")
    if canonical_origins is not context_origins:
        raise identity_error("K07 builder/context do not share one canonical origin registry")
    if not hasattr(canonical_origins, "get") or not isinstance(client_contexts, dict):
        raise identity_error("canonical K07 authority registries are not available")

    def sealed_product_clock(clock: object) -> FunctionType:
        clock_clone = clone_function(clock, label="Betfair product clock")
        clock_snapshot = snapshot_function(clock_clone, "Betfair product clock")

        def product_clock():
            require_snapshot(clock_clone, clock_snapshot, "K07 Betfair product clock")
            invocation_clock = clone_function(
                clock_clone,
                label="per-call K07 Betfair product clock",
                metadata=clock_snapshot[0],
            )
            require_snapshot(
                invocation_clock,
                clock_snapshot,
                "per-call K07 Betfair product clock",
            )
            return invocation_clock()

        product_clock._autosport_k07_clock_sealed = True
        return product_clock

    def fresh_call_clones() -> tuple[FunctionType, FunctionType, FunctionType, FunctionType]:
        """Copy verified templates before I/O so later metadata mutation is irrelevant."""

        require_static_snapshot()
        local_json = SimpleNamespace(
            dumps=canonical_json_dumps,
            loads=canonical_json_loads,
            JSONDecodeError=canonical_json_decode_error,
        )
        local_decode = clone_function(
            sealed_decode_json,
            label="per-call Betfair JSON decoder",
            globals_overrides={"json": local_json},
            metadata=decode_snapshot[0],
        )
        local_rpc = clone_function(
            sealed_rpc,
            label="per-call Betfair RPC",
            globals_overrides={
                "json": local_json,
                "_decode_json": local_decode,
            },
            metadata=rpc_snapshot[0],
        )
        local_next_request_id = clone_function(
            sealed_next_request_id,
            label="per-call Betfair request-id allocator",
            metadata=request_id_snapshot[0],
        )
        local_observed_at = clone_function(
            sealed_observed_at,
            label="per-call Betfair observation clock adapter",
            metadata=observed_at_snapshot[0],
        )
        local_transport_post = clone_function(
            sealed_transport_post,
            label="per-call Betfair HTTP transport",
            metadata=transport_snapshot[0],
        )

        # Validate what was copied, not only the persistent templates. Transient
        # global rebinding is caught in the local copy, while executable metadata
        # comes from the frozen snapshot rather than a late source-function read.
        require_snapshot(
            local_decode,
            decode_snapshot,
            "per-call K07 Betfair JSON decoder",
            overrides={"json": local_json},
        )
        require_snapshot(
            local_rpc,
            rpc_snapshot,
            "per-call K07 Betfair RPC",
            overrides={"json": local_json, "_decode_json": local_decode},
        )
        require_snapshot(
            local_next_request_id,
            request_id_snapshot,
            "per-call K07 Betfair request-id allocator",
        )
        require_snapshot(
            local_observed_at,
            observed_at_snapshot,
            "per-call K07 Betfair observation clock adapter",
        )
        require_snapshot(
            local_transport_post,
            transport_snapshot,
            "per-call K07 Betfair HTTP transport",
        )
        require_static_snapshot()
        return (
            local_rpc,
            local_next_request_id,
            local_observed_at,
            local_transport_post,
        )

    def guarded_client_init(self, *args, **kwargs) -> None:
        require_static_snapshot()
        original_client_init(self, *args, **kwargs)

    def guarded_read_account_details(client):
        require_static_snapshot()

        # Identity issuance creates/validates this context immediately before the
        # canonical read. Ordinary direct read-only clients have no such record and
        # continue through the unmodified adapter path.
        context = client_contexts.get(id(client))
        if (
            type(context) is not context_record_type
            or context.client_ref() is not client
            or context.revoked
        ):
            return original_read_account_details(client)
        origin = context.origin
        if type(origin) is not origin_type:
            raise identity_error("K07 active context has non-canonical origin")

        try:
            state = original_getattribute(client, "__dict__")
        except BaseException as exc:
            raise identity_error("K07 client state is not canonical") from exc
        if type(state) is not dict:
            raise identity_error("K07 client state is not canonical")
        if (
            state.get("_credentials") is not origin.credentials
            or state.get("_transport") is not origin.transport
            or state.get("_clock") is not origin.clock
        ):
            raise identity_error("K07 active context diverged before sealed read")

        credentials = origin.credentials
        transport = origin.transport
        clock = origin.clock
        timeout_seconds = state.get("_timeout_seconds")
        account_id = state.get("_account_id")
        try:
            max_response_bytes = object.__getattribute__(transport, "_max_response_bytes")
        except BaseException as exc:
            raise identity_error("K07 canonical transport lost response-size authority") from exc
        if (
            type(credentials) is not credentials_type
            or type(transport) is not transport_type
            or type(clock) is not FunctionType
            or not getattr(clock, "_autosport_k07_clock_sealed", False)
            or type(max_response_bytes) is not int
            or max_response_bytes <= 0
            or type(timeout_seconds) is not float
            or type(account_id) is not str
        ):
            raise identity_error("K07 active context is not a sealed canonical origin")

        (
            local_rpc,
            local_next_request_id,
            local_observed_at,
            local_transport_post,
        ) = fresh_call_clones()
        sealed_credentials = credentials_type(
            credentials.application_key,
            credentials.session_token,
        )
        sealed_transport = transport_type(max_response_bytes=max_response_bytes)
        sealed_transport.post = MethodType(local_transport_post, sealed_transport)
        shadow = object.__new__(client_type)
        original_client_init(
            shadow,
            sealed_credentials,
            transport=sealed_transport,
            timeout_seconds=timeout_seconds,
            clock=clock,
            venue_id=venue_id,
            account_id=account_id,
        )
        shadow._next_request_id = MethodType(local_next_request_id, shadow)
        shadow._observed_at = MethodType(local_observed_at, shadow)

        captured: list[object] = []

        def snapshot_rpc(method: str, params):
            if method != account_details_rpc or type(params) is not dict or params:
                raise identity_error(
                    "K07 acquisition attempted a non-canonical account-details RPC"
                )
            if captured:
                raise identity_error(
                    "K07 acquisition produced multiple account-details RPC results"
                )
            result = local_rpc(shadow, method, params)
            captured.append(result)
            return result

        shadow._rpc = snapshot_rpc
        details = original_read_account_details(shadow)
        require_static_snapshot()
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

    # Install at the owning read seam first. Then retarget the existing K07
    # factory's captured canonical cells so its own dispatch verifier sees these
    # exact guards as canonical. Because those outer cells are shared, the
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

    # Prove the resolver shares the retargeted read cell. If the implementation
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
        origin = canonical_origins.get(client)
        if (
            type(origin) is not origin_type
            or origin.credentials is not client._credentials
            or origin.transport is not client._transport
            or origin.clock is not client._clock
        ):
            raise identity_error("canonical K07 builder lost its registered origin")

        # Seal the exact clock before the client escapes to callers, then update the
        # existing K07 origin to that same object. This is not a sibling registry:
        # context_for() and the read guard consume this one canonical origin.
        clock = sealed_product_clock(origin.clock)
        client._clock = clock
        canonical_origins[client] = origin_type(
            transport=origin.transport,
            clock=clock,
            credentials=origin.credentials,
            credential_binding=origin.credential_binding,
        )
        sealed_origin = canonical_origins.get(client)
        if (
            type(sealed_origin) is not origin_type
            or sealed_origin.clock is not client._clock
            or sealed_origin.credentials is not client._credentials
            or sealed_origin.transport is not client._transport
        ):
            raise identity_error("canonical K07 sealed origin publication failed")
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
