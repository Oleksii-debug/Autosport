"""Seal the product-owned risk-randomization issuer against mutable dispatch.

The public issuer captures product entropy, but its implementation and helper
functions originally still resolved cryptographic/serialization helpers through
the mutable module globals dictionary.  Rebinding ``hashlib`` (or a helper that
uses it) could therefore steer the persisted randomization root or its durable
binding without changing the public issuer function identity.

Reuse the monotonic-root dispatch-sealing pattern: clone the authority-bearing
implementation and its integrity helpers over private dependency snapshots, keep
the public signature unchanged, and fail closed when the public entropy/hash/root
surfaces are rebound.  No estimator, membership, persistence schema, or money
authority is added here.
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
    # Keep the target module itself in this installation frame so every runtime
    # wrapper below closes over the exact object.  The guard module's public/global
    # ``_precommit`` binding is deleted after installation and cannot redirect checks.
    precommit_module = _precommit
    implementation_name = "_issue_risk_randomization_precommit"
    implementation = getattr(precommit_module, implementation_name, None)
    if implementation is None:
        return

    public_issuer = precommit_module.issue_risk_randomization_precommit
    closure = getattr(public_issuer, "__closure__", None)
    if not closure or not any(cell.cell_contents is implementation for cell in closure):
        raise RuntimeError(
            "risk randomization public issuer is not bound to the canonical implementation"
        )

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

    # Private facades hold exact callable objects rather than mutable public module
    # dispatch.  A later ``precommit.hashlib = ...`` or attribute replacement cannot
    # steer any authority-bearing hash after this package guard has installed.
    frozen_hashlib = SimpleNamespace(sha256=canonical_sha256)
    frozen_json = SimpleNamespace(dumps=canonical_json_dumps)
    frozen_os = SimpleNamespace(stat=canonical_os_stat, fstat=canonical_os_fstat)
    frozen_stat = SimpleNamespace(S_ISREG=canonical_s_isreg)
    frozen_uuid = SimpleNamespace(uuid4=canonical_uuid4)
    frozen_secrets = SimpleNamespace(token_bytes=canonical_token_bytes)

    frozen_text = _clone_function(precommit_module._text)
    frozen_sha256_text = _clone_function(
        precommit_module._sha256_text,
        globals_overrides={"_text": frozen_text},
    )
    frozen_canonical_bytes = _clone_function(
        precommit_module._canonical_bytes,
        globals_overrides={"json": frozen_json},
    )
    frozen_pretty_bytes = _clone_function(
        precommit_module._pretty_bytes,
        globals_overrides={"json": frozen_json},
    )
    frozen_sha256_bytes = _clone_function(
        precommit_module._sha256_bytes,
        globals_overrides={"hashlib": frozen_hashlib},
    )
    frozen_experiment_key = _clone_function(
        precommit_module._experiment_key,
        globals_overrides={"hashlib": frozen_hashlib},
    )
    frozen_workspace_path = _clone_function(precommit_module._workspace_path)
    frozen_read_regular_bytes = _clone_function(
        precommit_module._read_regular_bytes,
        globals_overrides={"os": frozen_os, "stat": frozen_stat},
    )
    frozen_membership_binding = _clone_function(
        precommit_module._membership_binding,
        globals_overrides={
            "_sha256_text": frozen_sha256_text,
            "_text": frozen_text,
        },
    )
    frozen_decode_state = _clone_function(
        precommit_module._decode_state,
        globals_overrides={
            "_read_regular_bytes": frozen_read_regular_bytes,
            "_sha256_text": frozen_sha256_text,
            "_pretty_bytes": frozen_pretty_bytes,
            "_sha256_bytes": frozen_sha256_bytes,
        },
    )
    frozen_semantic_binding = _clone_function(
        precommit_module._semantic_binding_sha256,
        globals_overrides={
            "_sha256_bytes": frozen_sha256_bytes,
            "_canonical_bytes": frozen_canonical_bytes,
        },
    )
    frozen_receipt = _clone_function(
        precommit_module._receipt,
        globals_overrides={
            "_semantic_binding_sha256": frozen_semantic_binding,
            "_experiment_key": frozen_experiment_key,
            "_text": frozen_text,
            "_sha256_text": frozen_sha256_text,
            "_sha256_bytes": frozen_sha256_bytes,
            "_canonical_bytes": frozen_canonical_bytes,
        },
    )

    # Keep the existing upstream membership-publication seam unchanged in this
    # bounded repair.  The receipt still passes the exact-type/content checks in
    # ``frozen_membership_binding``; sealing upstream membership issuance is owned
    # by that authority family rather than creating a second registry here.
    def membership_resolver(*args, **kwargs):
        return precommit_module.resolve_fixed_n_membership_publication(*args, **kwargs)

    frozen_implementation = _clone_function(
        implementation,
        globals_overrides={
            "hashlib": frozen_hashlib,
            "json": frozen_json,
            "secrets": frozen_secrets,
            "uuid": frozen_uuid,
            "_ROOT_BYTES": canonical_root_bytes,
            "_text": frozen_text,
            "_workspace_path": frozen_workspace_path,
            "resolve_fixed_n_membership_publication": membership_resolver,
            "_membership_binding": frozen_membership_binding,
            "_experiment_key": frozen_experiment_key,
            "_decode_state": frozen_decode_state,
            "_semantic_binding_sha256": frozen_semantic_binding,
            "_sha256_bytes": frozen_sha256_bytes,
            "_pretty_bytes": frozen_pretty_bytes,
            "_receipt": frozen_receipt,
        },
    )

    def sealed_issue_risk_randomization_precommit(
        registry_path,
        *,
        workspace,
        research_protocol_id,
        dataset_snapshot_id,
        experiment_id,
        authority_root=None,
    ):
        # Persistent replacement is integrity loss.  Races after these checks are
        # harmless to root selection because ``frozen_implementation`` owns private
        # callable snapshots rather than late-reading these public surfaces.
        if precommit_module.issue_risk_randomization_precommit is not sealed_issue_risk_randomization_precommit:
            raise canonical_error("risk randomization public issuer was rebound")
        if precommit_module.hashlib is not canonical_hashlib or canonical_hashlib.sha256 is not canonical_sha256:
            raise canonical_error("randomization cryptographic digest dispatch was rebound")
        if precommit_module.secrets is not canonical_secrets or canonical_secrets.token_bytes is not canonical_token_bytes:
            raise canonical_error("randomization entropy source was rebound")
        if type(precommit_module._ROOT_BYTES) is not int or precommit_module._ROOT_BYTES != canonical_root_bytes:
            raise canonical_error("randomization root size authority was rebound")
        return frozen_implementation(
            registry_path,
            workspace=workspace,
            research_protocol_id=research_protocol_id,
            dataset_snapshot_id=dataset_snapshot_id,
            experiment_id=experiment_id,
            authority_root=authority_root,
            _product_token_bytes=canonical_token_bytes,
        )

    sealed_issue_risk_randomization_precommit._autosport_randomization_dispatch_sealed = True
    precommit_module.issue_risk_randomization_precommit = sealed_issue_risk_randomization_precommit

    # The injectable implementation must not remain an ordinary module capability.
    delattr(precommit_module, implementation_name)


_install_guard()
del _install_guard
del _precommit