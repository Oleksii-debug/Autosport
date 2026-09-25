"""Seal product-owned risk-randomization authority against mutable dispatch.

The public issuer captures product entropy, but its implementation and helper
functions originally still resolved cryptographic/serialization helpers through
the mutable module globals dictionary. Rebinding those helpers, the upstream
membership resolver, or the public receipt resolver could otherwise steer or
relabel durable randomization authority without changing persisted truth.

A copied ``FunctionType.__globals__`` dictionary is also directly inspectable and
mutable. Reuse the monotonic-root dispatch-sealing pattern for the supported API:
every authority-bearing clone reached through that API is invoked only through a
wrapper that checks an exact globals snapshot, while private facades are checked
for exact captured callable attributes before use.

This is not a same-process capability-security boundary. Python code allowed to
extract private slots with ``object.__getattribute__``, mutate arbitrary function
globals/defaults/closures, or otherwise rewrite interpreter objects is explicitly
outside the authority threat model and is reported as unproven by the receipt.
Adding another Python wrapper cannot make that stronger claim true.
No estimator, membership, persistence schema, or money authority is added here.
"""
from __future__ import annotations

from types import FunctionType, SimpleNamespace
from typing import Callable

from . import risk_randomization_precommit as _precommit


def _clone_function(
    function: Callable[..., object],
    *,
    globals_overrides: dict[str, object] | None = None,
) -> FunctionType:
    if type(function) is not FunctionType:
        raise RuntimeError("risk randomization canonical dispatch is not a plain function")
    globals_copy = dict(function.__globals__)
    if globals_overrides:
        globals_copy.update(globals_overrides)
    cloned = FunctionType(
        function.__code__,
        globals_copy,
        function.__name__,
        function.__defaults__,
        function.__closure__,
    )
    cloned.__kwdefaults__ = (
        None if function.__kwdefaults__ is None else dict(function.__kwdefaults__)
    )
    cloned.__annotations__ = dict(function.__annotations__)
    return cloned


def _install_guard() -> None:
    precommit_module = _precommit
    implementation_name = "_issue_risk_randomization_precommit"
    implementation = getattr(precommit_module, implementation_name, None)
    if implementation is None:
        return

    public_issuer = precommit_module.issue_risk_randomization_precommit
    public_resolver = precommit_module.resolve_risk_randomization_precommit
    membership_resolution = precommit_module.resolve_fixed_n_membership_publication
    closure = getattr(public_issuer, "__closure__", None)
    if not closure or not any(cell.cell_contents is implementation for cell in closure):
        raise RuntimeError(
            "risk randomization public issuer is not bound to the canonical implementation"
        )
    if type(public_resolver) is not FunctionType or type(membership_resolution) is not FunctionType:
        raise RuntimeError("risk randomization resolver dispatch is not canonical")

    canonical_error = precommit_module.RiskRandomizationPrecommitError
    canonical_hashlib = precommit_module.hashlib
    canonical_sha256 = canonical_hashlib.sha256
    canonical_json = precommit_module.json
    canonical_json_dumps = canonical_json.dumps
    canonical_os = precommit_module.os
    canonical_os_stat = canonical_os.stat
    canonical_os_fstat = canonical_os.fstat
    canonical_stat = precommit_module.stat
    canonical_s_isreg = canonical_stat.S_ISREG
    canonical_uuid = precommit_module.uuid
    canonical_uuid4 = canonical_uuid.uuid4
    canonical_secrets = precommit_module.secrets
    canonical_token_bytes = canonical_secrets.token_bytes
    canonical_root_bytes = precommit_module._ROOT_BYTES

    frozen_hashlib = SimpleNamespace(sha256=canonical_sha256)
    frozen_json = SimpleNamespace(dumps=canonical_json_dumps)
    frozen_os = SimpleNamespace(stat=canonical_os_stat, fstat=canonical_os_fstat)
    frozen_stat = SimpleNamespace(S_ISREG=canonical_s_isreg)
    frozen_uuid = SimpleNamespace(uuid4=canonical_uuid4)
    frozen_secrets = SimpleNamespace(token_bytes=canonical_token_bytes)

    missing = object()

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
                raise canonical_error(f"frozen {label} global {name!r} was rebound")

    def require_private_facades() -> None:
        if frozen_hashlib.sha256 is not canonical_sha256:
            raise canonical_error("frozen randomization SHA-256 dispatch was rebound")
        if frozen_json.dumps is not canonical_json_dumps:
            raise canonical_error("frozen randomization JSON dispatch was rebound")
        if frozen_os.stat is not canonical_os_stat or frozen_os.fstat is not canonical_os_fstat:
            raise canonical_error("frozen randomization file-stat dispatch was rebound")
        if frozen_stat.S_ISREG is not canonical_s_isreg:
            raise canonical_error("frozen randomization regular-file dispatch was rebound")
        if frozen_uuid.uuid4 is not canonical_uuid4:
            raise canonical_error("frozen randomization transaction-id dispatch was rebound")
        if frozen_secrets.token_bytes is not canonical_token_bytes:
            raise canonical_error("frozen randomization entropy dispatch was rebound")

    def require_membership_resolver() -> None:
        if precommit_module.resolve_fixed_n_membership_publication is not membership_resolution:
            raise canonical_error("randomization membership resolver was rebound")

    def sealed_clone(function: FunctionType, label: str):
        snapshot = snapshot_globals(function)

        def sealed(*args, **kwargs):
            require_private_facades()
            require_snapshot(function, snapshot, label)
            return function(*args, **kwargs)

        return sealed

    frozen_text_impl = _clone_function(precommit_module._text)
    sealed_text = sealed_clone(frozen_text_impl, "randomization text validator")

    frozen_sha256_text_impl = _clone_function(
        precommit_module._sha256_text,
        globals_overrides={"_text": sealed_text},
    )
    sealed_sha256_text = sealed_clone(
        frozen_sha256_text_impl,
        "randomization SHA-256 text validator",
    )

    frozen_canonical_bytes_impl = _clone_function(
        precommit_module._canonical_bytes,
        globals_overrides={"json": frozen_json},
    )
    sealed_canonical_bytes = sealed_clone(
        frozen_canonical_bytes_impl,
        "randomization canonical serializer",
    )

    frozen_pretty_bytes_impl = _clone_function(
        precommit_module._pretty_bytes,
        globals_overrides={"json": frozen_json},
    )
    sealed_pretty_bytes = sealed_clone(
        frozen_pretty_bytes_impl,
        "randomization state serializer",
    )

    frozen_sha256_bytes_impl = _clone_function(
        precommit_module._sha256_bytes,
        globals_overrides={"hashlib": frozen_hashlib},
    )
    sealed_sha256_bytes = sealed_clone(
        frozen_sha256_bytes_impl,
        "randomization byte hasher",
    )

    frozen_experiment_key_impl = _clone_function(
        precommit_module._experiment_key,
        globals_overrides={"hashlib": frozen_hashlib},
    )
    sealed_experiment_key = sealed_clone(
        frozen_experiment_key_impl,
        "randomization experiment-key hasher",
    )

    frozen_workspace_path_impl = _clone_function(precommit_module._workspace_path)
    sealed_workspace_path = sealed_clone(
        frozen_workspace_path_impl,
        "randomization workspace resolver",
    )

    frozen_state_path_impl = _clone_function(precommit_module._state_path)
    sealed_state_path = sealed_clone(
        frozen_state_path_impl,
        "randomization state-path resolver",
    )

    frozen_read_regular_bytes_impl = _clone_function(
        precommit_module._read_regular_bytes,
        globals_overrides={"os": frozen_os, "stat": frozen_stat},
    )
    sealed_read_regular_bytes = sealed_clone(
        frozen_read_regular_bytes_impl,
        "randomization stable-file reader",
    )

    frozen_membership_binding_impl = _clone_function(
        precommit_module._membership_binding,
        globals_overrides={
            "_sha256_text": sealed_sha256_text,
            "_text": sealed_text,
        },
    )
    sealed_membership_binding = sealed_clone(
        frozen_membership_binding_impl,
        "randomization membership binder",
    )

    frozen_decode_state_impl = _clone_function(
        precommit_module._decode_state,
        globals_overrides={
            "_read_regular_bytes": sealed_read_regular_bytes,
            "_sha256_text": sealed_sha256_text,
            "_pretty_bytes": sealed_pretty_bytes,
            "_sha256_bytes": sealed_sha256_bytes,
        },
    )
    sealed_decode_state = sealed_clone(
        frozen_decode_state_impl,
        "randomization state decoder",
    )

    frozen_semantic_binding_impl = _clone_function(
        precommit_module._semantic_binding_sha256,
        globals_overrides={
            "_sha256_bytes": sealed_sha256_bytes,
            "_canonical_bytes": sealed_canonical_bytes,
        },
    )
    sealed_semantic_binding = sealed_clone(
        frozen_semantic_binding_impl,
        "randomization semantic binder",
    )

    frozen_receipt_impl = _clone_function(
        precommit_module._receipt,
        globals_overrides={
            "_semantic_binding_sha256": sealed_semantic_binding,
            "_experiment_key": sealed_experiment_key,
            "_text": sealed_text,
            "_sha256_text": sealed_sha256_text,
            "_sha256_bytes": sealed_sha256_bytes,
            "_canonical_bytes": sealed_canonical_bytes,
        },
    )
    sealed_receipt = sealed_clone(
        frozen_receipt_impl,
        "randomization receipt builder",
    )

    def membership_resolver(*args, **kwargs):
        require_membership_resolver()
        return membership_resolution(*args, **kwargs)

    common_overrides = {
        "_text": sealed_text,
        "_workspace_path": sealed_workspace_path,
        "resolve_fixed_n_membership_publication": membership_resolver,
        "_membership_binding": sealed_membership_binding,
        "_experiment_key": sealed_experiment_key,
        "_state_path": sealed_state_path,
        "_decode_state": sealed_decode_state,
        "_semantic_binding_sha256": sealed_semantic_binding,
        "_sha256_bytes": sealed_sha256_bytes,
        "_receipt": sealed_receipt,
    }

    frozen_implementation = _clone_function(
        implementation,
        globals_overrides={
            **common_overrides,
            "hashlib": frozen_hashlib,
            "json": frozen_json,
            "secrets": frozen_secrets,
            "uuid": frozen_uuid,
            "_ROOT_BYTES": canonical_root_bytes,
            "_pretty_bytes": sealed_pretty_bytes,
        },
    )
    implementation_snapshot = snapshot_globals(frozen_implementation)

    class CheckedImplementation:
        """Seal the raw issuer on supported API calls, not against interpreter reflection."""

        __slots__ = ("_function", "_snapshot")

        def __init__(
            self,
            function: FunctionType,
            snapshot: tuple[tuple[str, object], ...],
        ) -> None:
            self._function = function
            self._snapshot = snapshot

        def __call__(self, *args, **kwargs):
            require_private_facades()
            require_snapshot(
                self._function,
                self._snapshot,
                "randomization implementation",
            )
            return self._function(*args, **kwargs)

    checked_implementation = CheckedImplementation(
        frozen_implementation,
        implementation_snapshot,
    )
    del frozen_implementation
    del implementation_snapshot

    frozen_resolver = _clone_function(
        public_resolver,
        globals_overrides=common_overrides,
    )
    resolver_snapshot = snapshot_globals(frozen_resolver)

    def require_public_crypto_dispatch() -> None:
        if precommit_module.hashlib is not canonical_hashlib or canonical_hashlib.sha256 is not canonical_sha256:
            raise canonical_error("randomization cryptographic digest dispatch was rebound")
        if precommit_module.secrets is not canonical_secrets or canonical_secrets.token_bytes is not canonical_token_bytes:
            raise canonical_error("randomization entropy source was rebound")
        if type(precommit_module._ROOT_BYTES) is not int or precommit_module._ROOT_BYTES != canonical_root_bytes:
            raise canonical_error("randomization root size authority was rebound")

    def sealed_issue_risk_randomization_precommit(
        registry_path,
        *,
        workspace,
        research_protocol_id,
        dataset_snapshot_id,
        experiment_id,
        authority_root=None,
    ):
        if precommit_module.issue_risk_randomization_precommit is not sealed_issue_risk_randomization_precommit:
            raise canonical_error("risk randomization public issuer was rebound")
        require_membership_resolver()
        require_public_crypto_dispatch()
        require_private_facades()
        return checked_implementation(
            registry_path,
            workspace=workspace,
            research_protocol_id=research_protocol_id,
            dataset_snapshot_id=dataset_snapshot_id,
            experiment_id=experiment_id,
            authority_root=authority_root,
        )

    def sealed_resolve_risk_randomization_precommit(
        registry_path,
        *,
        workspace,
        research_protocol_id,
        dataset_snapshot_id,
        experiment_id,
        authority_root=None,
    ):
        if precommit_module.resolve_risk_randomization_precommit is not sealed_resolve_risk_randomization_precommit:
            raise canonical_error("risk randomization public resolver was rebound")
        require_membership_resolver()
        require_public_crypto_dispatch()
        require_private_facades()
        require_snapshot(
            frozen_resolver,
            resolver_snapshot,
            "randomization resolver",
        )
        return frozen_resolver(
            registry_path,
            workspace=workspace,
            research_protocol_id=research_protocol_id,
            dataset_snapshot_id=dataset_snapshot_id,
            experiment_id=experiment_id,
            authority_root=authority_root,
        )

    sealed_issue_risk_randomization_precommit._autosport_randomization_dispatch_sealed = True
    sealed_resolve_risk_randomization_precommit._autosport_randomization_dispatch_sealed = True
    precommit_module.issue_risk_randomization_precommit = sealed_issue_risk_randomization_precommit
    precommit_module.resolve_risk_randomization_precommit = sealed_resolve_risk_randomization_precommit

    delattr(precommit_module, implementation_name)


_install_guard()
del _install_guard
del _precommit
