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
    missing = object()

    lock_body = exact_getattr(lock_factory, "__wrapped__", None)
    lock_body_code = exact_getattr(lock_body, "__code__", None)
    lock_globals = exact_getattr(lock_body, "__globals__", None)
    if lock_body is None or lock_body_code is None or exact_type(lock_globals) is not dict:
        raise RuntimeError("provider sport durable lock body is unavailable")

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
    canonical_lock_module_name = "msvcrt" if exact_getattr(canonical_os, "name", None) == "nt" else "fcntl"
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
            or exact_getattr(lock_body, "__code__", None) is not lock_body_code
            or exact_getattr(lock_body, "__globals__", None) is not lock_globals
        ):
            raise failure_type("provider sport durable lock authority changed")
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
            or lock_globals.get(canonical_lock_module_name, missing) is not canonical_lock_module
        ):
            raise failure_type("provider sport durable lock platform authority changed")
        for name, expected in platform_members:
            if exact_getattr(canonical_lock_module, name, None) is not expected:
                raise failure_type(
                    "provider sport durable lock platform authority changed: " + name
                )

    @contextmanager
    def guarded_durable_path_lock(path):
        require_lock_authority()
        with lock_factory(path):
            require_lock_authority()
            yield
            require_lock_authority()
        require_lock_authority()

    mapping_module.durable_path_lock = guarded_durable_path_lock


_install()
del _install
