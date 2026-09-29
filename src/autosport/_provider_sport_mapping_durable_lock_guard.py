"""Seal the existing provider-sport durable lock graph before curation captures it."""

from __future__ import annotations

from contextlib import contextmanager

from . import integrity as _integrity
from . import provider_sport_mapping as _mapping


def _install() -> None:
    mapping_module = _mapping
    integrity_module = _integrity
    failure_type = mapping_module.ProviderSportMappingError
    lock_factory = mapping_module.durable_path_lock

    exact_getattr = getattr
    exact_type = type
    exact_vars = vars
    exact_len = len
    exact_zip = zip
    missing = object()

    lock_body = exact_getattr(lock_factory, "__wrapped__", None)
    lock_body_code = exact_getattr(lock_body, "__code__", None)
    lock_globals = exact_getattr(lock_body, "__globals__", None)
    lock_factory_code = exact_getattr(lock_factory, "__code__", None)
    lock_factory_globals = exact_getattr(lock_factory, "__globals__", None)
    lock_factory_closure = exact_getattr(lock_factory, "__closure__", None)
    lock_factory_closure_values = tuple(
        cell.cell_contents for cell in (lock_factory_closure or ())
    )
    if (
        lock_body is None
        or lock_body_code is None
        or exact_type(lock_globals) is not dict
        or lock_factory_code is None
        or exact_type(lock_factory_globals) is not dict
        or not lock_factory_closure
    ):
        raise RuntimeError("provider sport durable lock body is unavailable")

    generator_contextmanager_type = lock_factory_globals.get(
        "_GeneratorContextManager", missing
    )
    if generator_contextmanager_type is missing:
        raise RuntimeError("provider sport contextmanager runtime is unavailable")
    generator_contextmanager_mro = generator_contextmanager_type.__mro__

    def _resolve_contextmanager_slot(name: str):
        for owner in generator_contextmanager_mro:
            slot = exact_vars(owner).get(name, missing)
            if slot is missing:
                continue
            return owner, slot, exact_getattr(slot, "__code__", None)
        raise RuntimeError(
            "provider sport contextmanager runtime member is unavailable: " + name
        )

    generator_contextmanager_surface = tuple(
        (name, *_resolve_contextmanager_slot(name))
        for name in ("__new__", "__init__", "__enter__", "__exit__")
    )

    dependency_names = (
        "Path",
        "_resolved_key",
        "_thread_lock_for",
        "_PATH_LOCK_LOCAL",
        "_PATH_LOCKS_GUARD",
        "_PATH_LOCKS",
        "_lock_handle",
        "_unlock_handle",
        "os",
        "threading",
    )
    dependencies = tuple(
        (name, lock_globals.get(name, missing)) for name in dependency_names
    )
    if any(value is missing for _name, value in dependencies):
        raise RuntimeError("provider sport durable lock dependency is unavailable")

    executable_dependencies = tuple(
        (name, value, code)
        for name, value in dependencies
        if (code := exact_getattr(value, "__code__", None)) is not None
    )

    canonical_os = lock_globals["os"]
    canonical_threading = lock_globals["threading"]
    canonical_rlock = exact_getattr(canonical_threading, "RLock", None)
    canonical_fsync = exact_getattr(canonical_os, "fsync", None)
    canonical_lock_module_name = (
        "msvcrt" if exact_getattr(canonical_os, "name", None) == "nt" else "fcntl"
    )
    canonical_lock_module = lock_globals.get(canonical_lock_module_name, missing)
    if canonical_lock_module is missing or canonical_rlock is None or canonical_fsync is None:
        raise RuntimeError("provider sport durable platform lock authority is unavailable")

    if canonical_lock_module_name == "msvcrt":
        platform_members = (
            ("locking", exact_getattr(canonical_lock_module, "locking", None)),
            ("LK_LOCK", exact_getattr(canonical_lock_module, "LK_LOCK", None)),
            ("LK_UNLCK", exact_getattr(canonical_lock_module, "LK_UNLCK", None)),
        )
    else:
        platform_members = (
            ("flock", exact_getattr(canonical_lock_module, "flock", None)),
            ("LOCK_EX", exact_getattr(canonical_lock_module, "LOCK_EX", None)),
            ("LOCK_UN", exact_getattr(canonical_lock_module, "LOCK_UN", None)),
        )
    if any(value is None for _name, value in platform_members):
        raise RuntimeError("provider sport durable platform lock member is unavailable")

    def require_lock_authority() -> None:
        if (
            exact_getattr(lock_factory, "__wrapped__", None) is not lock_body
            or exact_getattr(lock_factory, "__code__", None) is not lock_factory_code
            or exact_getattr(lock_factory, "__globals__", None) is not lock_factory_globals
            or exact_getattr(lock_body, "__code__", None) is not lock_body_code
            or exact_getattr(lock_body, "__globals__", None) is not lock_globals
        ):
            raise failure_type("provider sport durable lock authority changed")
        current_factory_closure = exact_getattr(lock_factory, "__closure__", None)
        if (
            current_factory_closure is not lock_factory_closure
            or exact_len(current_factory_closure or ())
            != exact_len(lock_factory_closure_values)
        ):
            raise failure_type("provider sport durable lock delegation authority changed")
        for current_cell, expected_cell, expected_value in exact_zip(
            current_factory_closure or (),
            lock_factory_closure or (),
            lock_factory_closure_values,
        ):
            if (
                current_cell is not expected_cell
                or current_cell.cell_contents is not expected_value
            ):
                raise failure_type(
                    "provider sport durable lock delegation authority changed"
                )
        if (
            lock_factory_globals.get("_GeneratorContextManager", missing)
            is not generator_contextmanager_type
            or generator_contextmanager_type.__mro__ is not generator_contextmanager_mro
        ):
            raise failure_type(
                "provider sport contextmanager runtime authority changed"
            )
        for name, owner, expected_slot, expected_code in generator_contextmanager_surface:
            if exact_vars(owner).get(name, missing) is not expected_slot:
                raise failure_type(
                    "provider sport contextmanager runtime authority changed: " + name
                )
            if (
                expected_code is not None
                and exact_getattr(expected_slot, "__code__", None) is not expected_code
            ):
                raise failure_type(
                    "provider sport contextmanager executable authority changed: " + name
                )
        for name, expected in dependencies:
            if lock_globals.get(name, missing) is not expected:
                raise failure_type(
                    "provider sport durable lock dependency authority changed: " + name
                )
        for name, expected, expected_code in executable_dependencies:
            if exact_getattr(expected, "__code__", None) is not expected_code:
                raise failure_type(
                    "provider sport durable lock executable authority changed: " + name
                )
        if (
            exact_getattr(canonical_threading, "RLock", None) is not canonical_rlock
            or exact_getattr(canonical_os, "fsync", None) is not canonical_fsync
            or lock_globals.get(canonical_lock_module_name, missing)
            is not canonical_lock_module
        ):
            raise failure_type("provider sport durable lock platform authority changed")
        for name, expected in platform_members:
            if exact_getattr(canonical_lock_module, name, None) is not expected:
                raise failure_type(
                    "provider sport durable lock platform authority changed: " + name
                )

    require_code = require_lock_authority.__code__
    require_closure = require_lock_authority.__closure__
    require_closure_values = tuple(
        cell.cell_contents for cell in (require_closure or ())
    )

    body_marker = "__AUTOSPORT_PROVIDER_SPORT_DURABLE_LOCK_BODY_WITNESS__"

    def guarded_durable_path_lock_body(path):
        (
            trusted_lock_factory,
            trusted_lock_factory_code,
            trusted_lock_body,
            trusted_lock_body_code,
            trusted_lock_factory_globals,
            trusted_lock_factory_closure,
            trusted_lock_factory_closure_values,
            trusted_contextmanager_type,
            trusted_contextmanager_mro,
            trusted_contextmanager_surface,
            trusted_require,
            trusted_require_code,
            trusted_require_closure,
            trusted_require_closure_values,
            trusted_getattr,
            trusted_vars,
            trusted_len,
            trusted_zip,
            trusted_missing,
            trusted_failure_type,
        ) = "__AUTOSPORT_PROVIDER_SPORT_DURABLE_LOCK_BODY_WITNESS__"

        if (
            trusted_getattr(trusted_lock_factory, "__code__", None)
            is not trusted_lock_factory_code
            or trusted_getattr(trusted_lock_factory, "__wrapped__", None)
            is not trusted_lock_body
            or trusted_getattr(trusted_lock_factory, "__globals__", None)
            is not trusted_lock_factory_globals
            or trusted_getattr(trusted_lock_body, "__code__", None)
            is not trusted_lock_body_code
            or trusted_getattr(trusted_require, "__code__", None)
            is not trusted_require_code
            or trusted_getattr(trusted_require, "__closure__", None)
            is not trusted_require_closure
        ):
            raise trusted_failure_type(
                "provider sport durable lock executable authority changed"
            )

        current_factory_closure = trusted_getattr(
            trusted_lock_factory, "__closure__", None
        )
        if (
            current_factory_closure is not trusted_lock_factory_closure
            or trusted_len(current_factory_closure or ())
            != trusted_len(trusted_lock_factory_closure_values)
        ):
            raise trusted_failure_type(
                "provider sport durable lock delegation authority changed"
            )
        for current_cell, expected_cell, expected_value in trusted_zip(
            current_factory_closure or (),
            trusted_lock_factory_closure or (),
            trusted_lock_factory_closure_values,
        ):
            if (
                current_cell is not expected_cell
                or current_cell.cell_contents is not expected_value
            ):
                raise trusted_failure_type(
                    "provider sport durable lock delegation authority changed"
                )

        if (
            trusted_lock_factory_globals.get(
                "_GeneratorContextManager", trusted_missing
            )
            is not trusted_contextmanager_type
            or trusted_contextmanager_type.__mro__ is not trusted_contextmanager_mro
        ):
            raise trusted_failure_type(
                "provider sport contextmanager runtime authority changed"
            )
        for (
            name,
            owner,
            expected_slot,
            expected_code,
        ) in trusted_contextmanager_surface:
            if trusted_vars(owner).get(name, trusted_missing) is not expected_slot:
                raise trusted_failure_type(
                    "provider sport contextmanager runtime authority changed: " + name
                )
            if (
                expected_code is not None
                and trusted_getattr(expected_slot, "__code__", None)
                is not expected_code
            ):
                raise trusted_failure_type(
                    "provider sport contextmanager executable authority changed: "
                    + name
                )

        current_require_closure = trusted_getattr(
            trusted_require, "__closure__", None
        )
        if trusted_len(current_require_closure or ()) != trusted_len(
            trusted_require_closure_values
        ):
            raise trusted_failure_type(
                "provider sport durable lock guard authority changed"
            )
        for current_cell, expected_cell, expected_value in trusted_zip(
            current_require_closure or (),
            trusted_require_closure or (),
            trusted_require_closure_values,
        ):
            if (
                current_cell is not expected_cell
                or current_cell.cell_contents is not expected_value
            ):
                raise trusted_failure_type(
                    "provider sport durable lock guard authority changed"
                )

        trusted_require()
        with trusted_lock_factory(path):
            trusted_require()
            yield
            trusted_require()
        trusted_require()

    body_constants = guarded_durable_path_lock_body.__code__.co_consts
    if sum(item == body_marker for item in body_constants) != 1:
        raise RuntimeError("provider sport durable lock body witness anchor is ambiguous")
    body_witness = (
        lock_factory,
        lock_factory_code,
        lock_body,
        lock_body_code,
        lock_factory_globals,
        lock_factory_closure,
        lock_factory_closure_values,
        generator_contextmanager_type,
        generator_contextmanager_mro,
        generator_contextmanager_surface,
        require_lock_authority,
        require_code,
        require_closure,
        require_closure_values,
        exact_getattr,
        exact_vars,
        exact_len,
        exact_zip,
        missing,
        failure_type,
    )
    guarded_durable_path_lock_body.__code__ = (
        guarded_durable_path_lock_body.__code__.replace(
            co_consts=tuple(
                body_witness if item == body_marker else item
                for item in body_constants
            )
        )
    )
    if guarded_durable_path_lock_body.__closure__ is not None:
        raise RuntimeError("provider sport durable lock body must not retain a closure")

    # Build the contextmanager adapter once while this module is installing. The
    # public authority never calls contextmanager()/functools.wraps at runtime.
    guarded_contextmanager_factory = contextmanager(guarded_durable_path_lock_body)
    guarded_contextmanager_factory_code = exact_getattr(
        guarded_contextmanager_factory, "__code__", None
    )
    guarded_contextmanager_factory_globals = exact_getattr(
        guarded_contextmanager_factory, "__globals__", None
    )
    guarded_contextmanager_factory_closure = exact_getattr(
        guarded_contextmanager_factory, "__closure__", None
    )
    guarded_contextmanager_factory_closure_values = tuple(
        cell.cell_contents
        for cell in (guarded_contextmanager_factory_closure or ())
    )
    if (
        guarded_contextmanager_factory_code is None
        or guarded_contextmanager_factory_globals is not lock_factory_globals
        or not guarded_contextmanager_factory_closure
        or exact_getattr(guarded_contextmanager_factory, "__wrapped__", None)
        is not guarded_durable_path_lock_body
    ):
        raise RuntimeError("provider sport guarded contextmanager is unavailable")

    public_marker = "__AUTOSPORT_PROVIDER_SPORT_DURABLE_LOCK_PUBLIC_WITNESS__"

    def guarded_durable_path_lock(path):
        (
            trusted_factory,
            trusted_factory_code,
            trusted_factory_globals,
            trusted_factory_closure,
            trusted_factory_closure_values,
            trusted_body,
            trusted_body_code,
            trusted_contextmanager_type,
            trusted_contextmanager_mro,
            trusted_contextmanager_surface,
            trusted_getattr,
            trusted_vars,
            trusted_len,
            trusted_zip,
            trusted_missing,
            trusted_failure_type,
        ) = "__AUTOSPORT_PROVIDER_SPORT_DURABLE_LOCK_PUBLIC_WITNESS__"
        if (
            trusted_getattr(trusted_factory, "__code__", None)
            is not trusted_factory_code
            or trusted_getattr(trusted_factory, "__globals__", None)
            is not trusted_factory_globals
            or trusted_getattr(trusted_factory, "__wrapped__", None)
            is not trusted_body
            or trusted_getattr(trusted_body, "__code__", None) is not trusted_body_code
            or trusted_getattr(trusted_body, "__closure__", None) is not None
        ):
            raise trusted_failure_type(
                "provider sport durable lock wrapper authority changed"
            )

        current_factory_closure = trusted_getattr(
            trusted_factory, "__closure__", None
        )
        if (
            current_factory_closure is not trusted_factory_closure
            or trusted_len(current_factory_closure or ())
            != trusted_len(trusted_factory_closure_values)
        ):
            raise trusted_failure_type(
                "provider sport durable lock wrapper delegation authority changed"
            )
        for current_cell, expected_cell, expected_value in trusted_zip(
            current_factory_closure or (),
            trusted_factory_closure or (),
            trusted_factory_closure_values,
        ):
            if (
                current_cell is not expected_cell
                or current_cell.cell_contents is not expected_value
            ):
                raise trusted_failure_type(
                    "provider sport durable lock wrapper delegation authority changed"
                )

        if (
            trusted_factory_globals.get(
                "_GeneratorContextManager", trusted_missing
            )
            is not trusted_contextmanager_type
            or trusted_contextmanager_type.__mro__ is not trusted_contextmanager_mro
        ):
            raise trusted_failure_type(
                "provider sport contextmanager runtime authority changed"
            )
        for (
            name,
            owner,
            expected_slot,
            expected_code,
        ) in trusted_contextmanager_surface:
            if trusted_vars(owner).get(name, trusted_missing) is not expected_slot:
                raise trusted_failure_type(
                    "provider sport contextmanager runtime authority changed: " + name
                )
            if (
                expected_code is not None
                and trusted_getattr(expected_slot, "__code__", None)
                is not expected_code
            ):
                raise trusted_failure_type(
                    "provider sport contextmanager executable authority changed: "
                    + name
                )

        return trusted_factory(path)

    public_constants = guarded_durable_path_lock.__code__.co_consts
    if sum(item == public_marker for item in public_constants) != 1:
        raise RuntimeError("provider sport durable lock public witness anchor is ambiguous")
    public_witness = (
        guarded_contextmanager_factory,
        guarded_contextmanager_factory_code,
        guarded_contextmanager_factory_globals,
        guarded_contextmanager_factory_closure,
        guarded_contextmanager_factory_closure_values,
        guarded_durable_path_lock_body,
        guarded_durable_path_lock_body.__code__,
        generator_contextmanager_type,
        generator_contextmanager_mro,
        generator_contextmanager_surface,
        exact_getattr,
        exact_vars,
        exact_len,
        exact_zip,
        missing,
        failure_type,
    )
    guarded_durable_path_lock.__code__ = guarded_durable_path_lock.__code__.replace(
        co_consts=tuple(
            public_witness if item == public_marker else item
            for item in public_constants
        )
    )
    if guarded_durable_path_lock.__closure__ is not None:
        raise RuntimeError("provider sport durable lock wrapper must not retain a closure")

    mapping_module.durable_path_lock = guarded_durable_path_lock


_install()
del _install
