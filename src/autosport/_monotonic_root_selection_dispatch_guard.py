"""Seal monotonic root-selection executable dispatch and resolver dependencies.

The durable selector store is a trust root only if callers cannot replace either the
public resolver function or the dependencies consumed by its frozen implementation.
A ``FunctionType`` globals copy is private from the source module but is still publicly
mutable through ``function.__globals__``. Product entry points therefore wrap every
frozen implementation with an exact globals snapshot check before authority-bearing
execution.

Root-selection receipts are also read through one verified file handle. A separate
``lstat`` followed by ``Path.read_text`` leaves a replace/repoint race between the
metadata check and the authority-bearing bytes. The sealed reader verifies the path,
opened handle, post-read handle, and path-after-read identities before decoding JSON.

No new root, registry, journal, or persistence format is introduced.
"""
from __future__ import annotations

from types import FunctionType, SimpleNamespace
from typing import Callable

from . import monotonic_authority_root_binding as _root
from . import monotonic_workspace_authority as _authority


def _clone_function(
    function: Callable[..., object],
    *,
    globals_overrides: dict[str, object] | None = None,
) -> FunctionType:
    if type(function) is not FunctionType:
        raise RuntimeError("monotonic authority canonical dispatch is not a plain function")
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
    root_module = _root
    authority_module = _authority

    original_store = root_module.stable_root_selection_store
    original_lexical = root_module._lexical_locator
    original_resolved = root_module._resolved_locator
    original_context = root_module._selection_context
    original_preflight = root_module.preflight_authority_root_selection
    original_resolve = root_module.AuthorityRootSelectionBinding.resolve.__func__
    original_authority_init = authority_module.MonotonicWorkspaceAuthority.__init__

    canonical_os = root_module.os
    canonical_path = root_module.Path
    canonical_hashlib = root_module.hashlib
    canonical_sha256 = canonical_hashlib.sha256
    canonical_stat = root_module.stat
    canonical_s_isreg = canonical_stat.S_ISREG
    canonical_strict_json_loads = root_module.strict_json_loads
    canonical_os_stat = canonical_os.stat
    canonical_os_open = canonical_os.open
    canonical_os_fstat = canonical_os.fstat
    canonical_os_fdopen = canonical_os.fdopen
    canonical_os_close = canonical_os.close
    canonical_configuration_error = root_module.AuthorityRootSelectionConfigurationError
    canonical_integrity_error = root_module.AuthorityRootSelectionIntegrityError
    canonical_selection_context = root_module._SelectionContext
    canonical_binding_type = root_module.AuthorityRootSelectionBinding
    canonical_authority_root_resolver = authority_module.resolve_monotonic_authority_root
    frozen_hashlib = SimpleNamespace(sha256=canonical_sha256)
    max_receipt_bytes = 64 * 1024

    missing = object()

    def snapshot_globals(function: FunctionType) -> tuple[tuple[str, object], ...]:
        """Capture every global binding this exact code object can directly read."""

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
                raise canonical_configuration_error(
                    f"frozen {label} global {name!r} was rebound"
                )

    def require_hash_dispatch() -> None:
        # Snapshotting the hashlib module object is insufficient because callers can
        # replace its sha256 attribute without changing module identity. The three
        # locator digests select the durable receipt path/root universe, so that
        # attribute is itself authority-bearing dispatch.
        if (
            root_module.hashlib is not canonical_hashlib
            or canonical_hashlib.sha256 is not canonical_sha256
            or frozen_hashlib.sha256 is not canonical_sha256
        ):
            raise canonical_configuration_error(
                "product root-selection SHA-256 dispatch was rebound"
            )

    def require_stable_read_dispatch() -> None:
        if (
            root_module.os is not canonical_os
            or canonical_os.stat is not canonical_os_stat
            or canonical_os.open is not canonical_os_open
            or canonical_os.fstat is not canonical_os_fstat
            or canonical_os.fdopen is not canonical_os_fdopen
            or canonical_os.close is not canonical_os_close
            or root_module.stat is not canonical_stat
            or canonical_stat.S_ISREG is not canonical_s_isreg
            or root_module.strict_json_loads is not canonical_strict_json_loads
        ):
            raise canonical_integrity_error(
                "product root-selection stable-read dispatch was rebound"
            )

    def sealed_read_strict_object(
        path,
        *,
        expected_keys,
        label,
    ):
        if root_module._read_strict_object is not sealed_read_strict_object:
            raise canonical_integrity_error(
                "product root-selection stable reader was rebound"
            )
        require_stable_read_dispatch()
        descriptor = None
        try:
            before = canonical_os_stat(path, follow_symlinks=False)
            if not canonical_s_isreg(before.st_mode) or before.st_nlink != 1:
                raise canonical_integrity_error(
                    f"{label} must be a single-link regular file"
                )
            if before.st_size < 0 or before.st_size > max_receipt_bytes:
                raise canonical_integrity_error(
                    f"{label} exceeds bounded root-selection receipt size"
                )

            flags = canonical_os.O_RDONLY | getattr(canonical_os, "O_BINARY", 0)
            if canonical_os.name != "nt":
                flags |= getattr(canonical_os, "O_NOFOLLOW", 0)
            descriptor = canonical_os_open(path, flags)
            opened = canonical_os_fstat(descriptor)
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or not canonical_s_isreg(opened.st_mode)
                or opened.st_nlink != 1
            ):
                raise canonical_integrity_error(
                    f"{label} changed during stable open"
                )
            if opened.st_size < 0 or opened.st_size > max_receipt_bytes:
                raise canonical_integrity_error(
                    f"{label} exceeds bounded root-selection receipt size"
                )

            with canonical_os_fdopen(descriptor, "rb") as handle:
                descriptor = None
                payload = handle.read(max_receipt_bytes + 1)
                if len(payload) > max_receipt_bytes:
                    raise canonical_integrity_error(
                        f"{label} exceeds bounded root-selection receipt size"
                    )
                after_open = canonical_os_fstat(handle.fileno())
            after = canonical_os_stat(path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise canonical_integrity_error(f"cannot stably read {label}") from exc
        except canonical_integrity_error:
            raise
        except OSError as exc:
            raise canonical_integrity_error(f"cannot stably read {label}") from exc
        finally:
            if descriptor is not None:
                try:
                    canonical_os_close(descriptor)
                except OSError:
                    pass

        path_identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_nlink,
        )
        handle_identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_nlink,
        )
        if (
            path_identity(before) != path_identity(after)
            or handle_identity(opened) != handle_identity(after_open)
        ):
            raise canonical_integrity_error(
                f"{label} changed during stable read"
            )

        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise canonical_integrity_error(f"cannot decode {label}") from exc
        try:
            raw = canonical_strict_json_loads(text)
        except (TypeError, ValueError) as exc:
            raise canonical_integrity_error(
                f"invalid strict JSON in {label}"
            ) from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise canonical_integrity_error(
                f"{label} must be a JSON object"
            )
        if frozenset(raw) != expected_keys:
            raise canonical_integrity_error(
                f"{label} keys must match schema exactly"
            )
        return raw

    # Clone the three OS/path primitives over product-owned dependency identities.
    frozen_store = _clone_function(
        original_store,
        globals_overrides={
            "os": canonical_os,
            "Path": canonical_path,
            "AuthorityRootSelectionConfigurationError": canonical_configuration_error,
        },
    )
    store_snapshot = snapshot_globals(frozen_store)

    frozen_lexical = _clone_function(
        original_lexical,
        globals_overrides={
            "os": canonical_os,
            "AuthorityRootSelectionConfigurationError": canonical_configuration_error,
        },
    )
    lexical_snapshot = snapshot_globals(frozen_lexical)

    frozen_resolved = _clone_function(
        original_resolved,
        globals_overrides={
            "os": canonical_os,
            "Path": canonical_path,
            "AuthorityRootSelectionConfigurationError": canonical_configuration_error,
        },
    )
    resolved_snapshot = snapshot_globals(frozen_resolved)

    def sealed_store():
        if root_module.stable_root_selection_store is not sealed_store:
            raise canonical_configuration_error(
                "product root-selection store resolver was rebound"
            )
        require_snapshot(frozen_store, store_snapshot, "root-selection store")
        return frozen_store()

    def sealed_lexical(path):
        if root_module._lexical_locator is not sealed_lexical:
            raise canonical_configuration_error(
                "product root-selection lexical locator was rebound"
            )
        require_snapshot(frozen_lexical, lexical_snapshot, "root-selection lexical locator")
        return frozen_lexical(path)

    def sealed_resolved(path):
        if root_module._resolved_locator is not sealed_resolved:
            raise canonical_configuration_error(
                "product root-selection resolved locator was rebound"
            )
        require_snapshot(frozen_resolved, resolved_snapshot, "root-selection resolved locator")
        return frozen_resolved(path)

    # Context now consumes only the verified public wrappers and a private SHA-256
    # facade. It never late-reads a caller-mutable hashlib attribute after the
    # canonical digest identity has been selected.
    frozen_context_impl = _clone_function(
        original_context,
        globals_overrides={
            "stable_root_selection_store": sealed_store,
            "_lexical_locator": sealed_lexical,
            "_resolved_locator": sealed_resolved,
            "hashlib": frozen_hashlib,
            "Path": canonical_path,
            "_SelectionContext": canonical_selection_context,
            "AuthorityRootSelectionConfigurationError": canonical_configuration_error,
        },
    )
    context_snapshot = snapshot_globals(frozen_context_impl)

    def sealed_context(*, workspace, authority_root):
        if root_module.stable_root_selection_store is not sealed_store:
            raise canonical_configuration_error(
                "product root-selection store resolver was rebound"
            )
        if root_module._lexical_locator is not sealed_lexical:
            raise canonical_configuration_error(
                "product root-selection lexical locator was rebound"
            )
        if root_module._resolved_locator is not sealed_resolved:
            raise canonical_configuration_error(
                "product root-selection resolved locator was rebound"
            )
        if root_module._selection_context is not sealed_context:
            raise canonical_configuration_error(
                "product root-selection context resolver was rebound"
            )
        require_hash_dispatch()
        require_snapshot(frozen_context_impl, context_snapshot, "root-selection context")
        return frozen_context_impl(workspace=workspace, authority_root=authority_root)

    frozen_preflight_impl = _clone_function(
        original_preflight,
        globals_overrides={
            "_selection_context": sealed_context,
            "_read_strict_object": sealed_read_strict_object,
        },
    )
    preflight_snapshot = snapshot_globals(frozen_preflight_impl)

    def sealed_preflight(
        *,
        workspace,
        authority_root,
        requested_workspace_instance_id,
    ):
        if root_module.preflight_authority_root_selection is not sealed_preflight:
            raise canonical_configuration_error(
                "product root-selection preflight was rebound"
            )
        if root_module._read_strict_object is not sealed_read_strict_object:
            raise canonical_integrity_error(
                "product root-selection stable reader was rebound"
            )
        require_hash_dispatch()
        require_snapshot(
            frozen_preflight_impl,
            preflight_snapshot,
            "root-selection preflight",
        )
        return frozen_preflight_impl(
            workspace=workspace,
            authority_root=authority_root,
            requested_workspace_instance_id=requested_workspace_instance_id,
        )

    frozen_resolve_impl = _clone_function(
        original_resolve,
        globals_overrides={"_selection_context": sealed_context},
    )
    resolve_snapshot = snapshot_globals(frozen_resolve_impl)

    def sealed_resolve(
        cls,
        *,
        workspace,
        workspace_instance_id,
        authority_root,
    ):
        live_descriptor = root_module.AuthorityRootSelectionBinding.__dict__.get("resolve")
        if (
            not isinstance(live_descriptor, classmethod)
            or live_descriptor.__func__ is not sealed_resolve
        ):
            raise canonical_configuration_error(
                "product root-selection binding resolver was rebound"
            )
        if cls is not canonical_binding_type:
            raise canonical_configuration_error(
                "product root-selection binding requires exact canonical class"
            )
        if root_module._read_strict_object is not sealed_read_strict_object:
            raise canonical_integrity_error(
                "product root-selection stable reader was rebound"
            )
        require_hash_dispatch()
        require_snapshot(
            frozen_resolve_impl,
            resolve_snapshot,
            "root-selection binding resolve",
        )
        return frozen_resolve_impl(
            cls,
            workspace=workspace,
            workspace_instance_id=workspace_instance_id,
            authority_root=authority_root,
        )

    root_module._read_strict_object = sealed_read_strict_object
    root_module.stable_root_selection_store = sealed_store
    root_module._lexical_locator = sealed_lexical
    root_module._resolved_locator = sealed_resolved
    root_module._selection_context = sealed_context
    root_module.preflight_authority_root_selection = sealed_preflight
    root_module.AuthorityRootSelectionBinding.resolve = classmethod(sealed_resolve)

    # monotonic_workspace_authority imported these names before this package guard
    # ran. Update that local view, then freeze construction over the exact verified
    # public entry points. Its implementation clone is itself globals-checked before
    # execution, closing direct ``__globals__`` mutation without a function rebind.
    authority_module.preflight_authority_root_selection = sealed_preflight
    authority_module.AuthorityRootSelectionBinding = canonical_binding_type
    frozen_authority_init_impl = _clone_function(
        original_authority_init,
        globals_overrides={
            "preflight_authority_root_selection": sealed_preflight,
            "AuthorityRootSelectionBinding": canonical_binding_type,
            "resolve_monotonic_authority_root": canonical_authority_root_resolver,
        },
    )
    authority_init_snapshot = snapshot_globals(frozen_authority_init_impl)

    def sealed_authority_init(self, *args, **kwargs):
        if authority_module.MonotonicWorkspaceAuthority.__init__ is not sealed_authority_init:
            raise canonical_configuration_error(
                "monotonic workspace authority constructor was rebound"
            )
        if authority_module.preflight_authority_root_selection is not sealed_preflight:
            raise canonical_configuration_error(
                "monotonic workspace authority preflight dispatch was rebound"
            )
        if authority_module.AuthorityRootSelectionBinding is not canonical_binding_type:
            raise canonical_configuration_error(
                "monotonic workspace authority binding class was rebound"
            )
        if root_module._read_strict_object is not sealed_read_strict_object:
            raise canonical_integrity_error(
                "product root-selection stable reader was rebound"
            )
        require_hash_dispatch()
        require_snapshot(
            frozen_authority_init_impl,
            authority_init_snapshot,
            "monotonic workspace authority constructor",
        )
        return frozen_authority_init_impl(self, *args, **kwargs)

    authority_module.MonotonicWorkspaceAuthority.__init__ = sealed_authority_init

    for method in (
        sealed_read_strict_object,
        sealed_store,
        sealed_lexical,
        sealed_resolved,
        sealed_context,
        sealed_preflight,
        sealed_resolve,
        sealed_authority_init,
    ):
        method._autosport_root_selection_dispatch_sealed = True


_install_guard()
del _install_guard