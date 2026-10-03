"""Seal Wave M shard-lineage entrypoints against mutable Python dispatch metadata.

The owning ``dataset_shard_manifest`` module remains the only byte/manifest/lineage
implementation. This guard captures its already-installed public entrypoints and
rejects mutation of the function metadata, module aliases, or builtins those
entrypoints use to decide positive registered-shard authority.
"""

from __future__ import annotations

import builtins
from typing import Any

from . import dataset_shard_manifest as _manifest
from .dataset_snapshot_lineage import DatasetSnapshotLineageRecord as _DatasetSnapshotLineageRecord


def _install() -> None:
    manifest_module = _manifest
    namespace = manifest_module.__dict__
    error_type = manifest_module.DatasetShardManifestError

    canonical_verify = manifest_module.verify_registered_dataset_shards
    canonical_verify_code = canonical_verify.__code__
    canonical_read = manifest_module.read_registered_shard_bytes
    canonical_read_code = canonical_read.__code__
    canonical_require = manifest_module._require_registered_manifest
    canonical_require_code = canonical_require.__code__
    canonical_require_kwdefaults = canonical_require.__kwdefaults__
    if canonical_require_kwdefaults is None:
        raise RuntimeError("registered shard authority defaults are unavailable")
    require_kwdefault_witnesses = tuple(canonical_require_kwdefaults.items())

    helper_guard = manifest_module._require_shard_helper_authority
    helper_guard_code = helper_guard.__code__
    helper_guard_defaults = helper_guard.__defaults__
    if helper_guard_defaults is None:
        raise RuntimeError("dataset shard helper witnesses are unavailable")
    helper_default_witnesses = tuple(helper_guard_defaults)

    exact_getattr = getattr
    exact_len = len
    exact_any = any
    exact_zip = zip
    exact_tuple = tuple
    exact_type = type
    exact_object_getattribute = object.__getattribute__
    missing_external_global = object()
    canonical_path = manifest_module.Path
    canonical_pure_posix_path = manifest_module.PurePosixPath
    canonical_concrete_path_type = exact_type(canonical_path())
    builtins_module = builtins

    canonical_path_open = exact_getattr(canonical_concrete_path_type, "open")
    canonical_path_open_globals = exact_getattr(canonical_path_open, "__globals__", None)
    if exact_type(canonical_path_open_globals) is not dict:
        raise RuntimeError("canonical Path.open globals are unavailable")
    canonical_pathlib_io = canonical_path_open_globals.get("io")
    canonical_pathlib_io_open = exact_getattr(canonical_pathlib_io, "open", None)
    if canonical_pathlib_io_open is None:
        raise RuntimeError("canonical pathlib io.open authority is unavailable")
    canonical_pathlib_io_open_code = exact_getattr(
        canonical_pathlib_io_open, "__code__", None
    )

    builtin_names = (
        "any",
        "getattr",
        "globals",
        "int",
        "isinstance",
        "len",
        "list",
        "next",
        "object",
        "ord",
        "range",
        "set",
        "sorted",
        "str",
        "tuple",
        "type",
        "zip",
        "FileNotFoundError",
        "OSError",
        "RuntimeError",
        "ValueError",
    )
    builtin_witnesses = tuple(
        (name, exact_getattr(builtins_module, name)) for name in builtin_names
    )

    alias_names = (
        "DatasetShardManifestError",
        "DatasetShardDescriptor",
        "DatasetShardManifest",
        "_DatasetSnapshotLineageAuthority",
        "_LINEAGE_RECORD",
        "_membership_manifest_sha256",
        "_text",
        "_sha256",
        "_canonical_json",
        "_digest",
        "_utc_instant",
        "_canonical_utc",
        "_relative_path",
        "_byte_size",
        "_ordinal",
        "_resolve_shard_root",
        "canonical_shard_manifest",
        "_verify_descriptor_bytes",
        "_candidate_path",
        "_observe_stable_regular_file",
        "_stat_identity",
        "_require_shard_helper_authority",
        "hashlib",
        "json",
        "os",
        "stat",
        "datetime",
        "timezone",
        "Path",
        "PurePosixPath",
        "_HEX",
        "_SHARD_KIND",
        "_DESCRIPTOR_SET_KIND",
        "_WINDOWS_RESERVED_STEMS",
        "_WINDOWS_FORBIDDEN_FILENAME_CHARS",
        "_READ_CHUNK_BYTES",
    )
    alias_witnesses = tuple((name, namespace[name]) for name in alias_names)
    alias_executable_witnesses = tuple(
        (name, expected, code)
        for name, expected in alias_witnesses
        if (code := exact_getattr(expected, "__code__", None)) is not None
    )

    descriptor_type = manifest_module.DatasetShardDescriptor
    manifest_type = manifest_module.DatasetShardManifest
    lineage_authority_type = manifest_module._DatasetSnapshotLineageAuthority
    lineage_record_type = _DatasetSnapshotLineageRecord
    missing_class_slot = object()
    lineage_dispatch_slots = (
        "record",
        "_read_and_verify",
        "_read",
        "_recover_monotonic",
        "_verify_registry_records",
        "_verify_registry_record",
        "_verify_entry_binding",
        "_record_from_raw",
        "_validate_graph",
        "_state_payload",
        "_state_sha256",
        "_semantic_binding_sha256",
    )
    class_slot_specs = (
        (
            "DatasetShardDescriptor",
            descriptor_type,
            (
                "__getattribute__",
                "__init__",
                "__post_init__",
                "ordinal",
                "shard_id",
                "relative_path",
                "byte_size",
                "content_sha256",
                "event_start_utc",
                "event_end_utc",
                "payload",
                "member_sha256",
            ),
        ),
        (
            "DatasetShardManifest",
            manifest_type,
            (
                "__getattribute__",
                "__init__",
                "__post_init__",
                "shards",
                "member_sha256",
                "payload",
                "membership_manifest_sha256",
            ),
        ),
        (
            "DatasetSnapshotLineageAuthority",
            lineage_authority_type,
            ("__getattribute__", *lineage_dispatch_slots),
        ),
        (
            "DatasetSnapshotLineageRecord",
            lineage_record_type,
            (
                "__getattribute__",
                "__init__",
                "__post_init__",
                "snapshot_id",
                "dataset_record_sha256",
                "manifest_sha256",
                "source_identity",
                "license_identity",
                "causal_cutoff",
                "available_at",
                "proof_registered_at",
                "member_sha256",
                "parent_snapshot_id",
                "parent_dataset_record_sha256",
                "parent_proof_sha256",
                "proof_sha256",
                "proof_payload",
                "to_payload",
            ),
        ),
    )
    class_slot_witnesses: list[tuple[str, type, str, Any, Any, Any]] = []
    for owner_label, owner, slot_names in class_slot_specs:
        owner_namespace = owner.__dict__
        for slot_name in slot_names:
            slot = owner_namespace.get(slot_name, missing_class_slot)
            if slot is missing_class_slot:
                executable = None
                executable_code = None
            else:
                if exact_type(slot) is property:
                    executable = slot.fget
                elif exact_type(slot) in {staticmethod, classmethod}:
                    executable = slot.__func__
                else:
                    executable = slot
                executable_code = exact_getattr(executable, "__code__", None)
            class_slot_witnesses.append(
                (
                    owner_label,
                    owner,
                    slot_name,
                    slot,
                    executable,
                    executable_code,
                )
            )
    frozen_class_slot_witnesses = tuple(class_slot_witnesses)

    external_dispatch_specs = (
        ("hashlib.sha256", manifest_module.hashlib, "sha256"),
        ("json.dumps", manifest_module.json, "dumps"),
        ("os.fspath", manifest_module.os, "fspath"),
        ("os.getcwd", manifest_module.os, "getcwd"),
        ("os.getcwdb", manifest_module.os, "getcwdb"),
        ("os.environ", manifest_module.os, "environ"),
        ("os.fsdecode", manifest_module.os, "fsdecode"),
        ("os.fsencode", manifest_module.os, "fsencode"),
        ("os.stat", manifest_module.os, "stat"),
        ("os.lstat", manifest_module.os, "lstat"),
        ("os.readlink", manifest_module.os, "readlink"),
        ("os.fstat", manifest_module.os, "fstat"),
        ("os.path.realpath", manifest_module.os.path, "realpath"),
        ("os.path.expanduser", manifest_module.os.path, "expanduser"),
        ("stat.S_ISREG", manifest_module.stat, "S_ISREG"),
        ("stat.S_ISLNK", manifest_module.stat, "S_ISLNK"),
        ("stat.S_ISDIR", manifest_module.stat, "S_ISDIR"),
        ("Path.__new__", canonical_path, "__new__"),
        ("Path.__eq__", canonical_concrete_path_type, "__eq__"),
        ("Path.__ne__", canonical_concrete_path_type, "__ne__"),
        ("Path.parent", canonical_concrete_path_type, "parent"),
        ("Path.name", canonical_concrete_path_type, "name"),
        ("Path.expanduser", canonical_concrete_path_type, "expanduser"),
        ("Path.is_symlink", canonical_concrete_path_type, "is_symlink"),
        ("Path.resolve", canonical_concrete_path_type, "resolve"),
        ("Path.is_dir", canonical_concrete_path_type, "is_dir"),
        ("Path.stat", canonical_concrete_path_type, "stat"),
        ("Path.open", canonical_concrete_path_type, "open"),
        ("Path.relative_to", canonical_concrete_path_type, "relative_to"),
        ("Path.__truediv__", canonical_concrete_path_type, "__truediv__"),
        ("PurePosixPath.__new__", canonical_pure_posix_path, "__new__"),
        ("PurePosixPath.is_absolute", canonical_pure_posix_path, "is_absolute"),
        ("PurePosixPath.parts", canonical_pure_posix_path, "parts"),
    )
    external_dispatch_witnesses: list[tuple[Any, ...]] = []
    for label, owner, attr_name in external_dispatch_specs:
        expected = exact_getattr(owner, attr_name)
        executable = expected.fget if exact_type(expected) is property else expected
        executable_code = exact_getattr(executable, "__code__", None)
        executable_globals = exact_getattr(executable, "__globals__", None)
        if exact_type(executable_globals) is dict and executable_code is not None:
            global_witnesses = exact_tuple(
                (name, executable_globals[name])
                for name in executable_code.co_names
                if name in executable_globals
            )
        else:
            executable_globals = None
            global_witnesses = ()
        external_dispatch_witnesses.append(
            (
                label,
                owner,
                attr_name,
                expected,
                executable,
                executable_code,
                executable_globals,
                global_witnesses,
            )
        )
    frozen_external_dispatch_witnesses = tuple(external_dispatch_witnesses)

    def require_authority(authority: Any = None) -> None:
        if (
            manifest_module.verify_registered_dataset_shards
            is not guarded_verify_registered_dataset_shards
            or manifest_module.read_registered_shard_bytes
            is not guarded_read_registered_shard_bytes
        ):
            raise error_type("canonical registered shard public dispatch changed")
        if (
            canonical_verify.__code__ is not canonical_verify_code
            or canonical_read.__code__ is not canonical_read_code
            or canonical_require.__code__ is not canonical_require_code
            or helper_guard.__code__ is not helper_guard_code
        ):
            raise error_type("canonical registered shard executable changed")

        current_kwdefaults = canonical_require.__kwdefaults__
        if (
            current_kwdefaults is not canonical_require_kwdefaults
            or exact_len(current_kwdefaults) != exact_len(require_kwdefault_witnesses)
            or exact_any(
                current_kwdefaults.get(name) is not expected
                for name, expected in require_kwdefault_witnesses
            )
        ):
            raise error_type("canonical registered shard authority defaults changed")

        current_helper_defaults = helper_guard.__defaults__
        if (
            current_helper_defaults is not helper_guard_defaults
            or exact_len(current_helper_defaults) != exact_len(helper_default_witnesses)
            or exact_any(
                current is not expected
                for current, expected in exact_zip(
                    current_helper_defaults,
                    helper_default_witnesses,
                    strict=True,
                )
            )
        ):
            raise error_type("canonical dataset shard helper defaults changed")

        for name, expected in builtin_witnesses:
            if exact_getattr(builtins_module, name, None) is not expected:
                raise error_type(
                    f"canonical dataset shard builtin dispatch changed: {name}"
                )
            if namespace.get(name, expected) is not expected:
                raise error_type(
                    f"canonical dataset shard builtin dispatch changed: {name}"
                )
        for name, expected in alias_witnesses:
            if namespace.get(name) is not expected:
                raise error_type(
                    f"canonical dataset shard module authority changed: {name}"
                )
        for name, expected, expected_code in alias_executable_witnesses:
            if exact_getattr(expected, "__code__", None) is not expected_code:
                raise error_type(
                    f"canonical dataset shard module executable changed: {name}"
                )
        for (
            owner_label,
            owner,
            slot_name,
            expected_slot,
            expected_executable,
            expected_code,
        ) in frozen_class_slot_witnesses:
            if owner.__dict__.get(slot_name, missing_class_slot) is not expected_slot:
                raise error_type(
                    "canonical dataset shard class dispatch changed: "
                    f"{owner_label}.{slot_name}"
                )
            if (
                expected_executable is not None
                and expected_code is not None
                and exact_getattr(expected_executable, "__code__", None)
                is not expected_code
            ):
                raise error_type(
                    "canonical dataset shard class executable changed: "
                    f"{owner_label}.{slot_name}"
                )

        if (
            exact_getattr(canonical_path_open, "__globals__", None)
            is not canonical_path_open_globals
            or canonical_path_open_globals.get("io") is not canonical_pathlib_io
            or exact_getattr(canonical_pathlib_io, "open", None)
            is not canonical_pathlib_io_open
            or (
                canonical_pathlib_io_open_code is not None
                and exact_getattr(canonical_pathlib_io_open, "__code__", None)
                is not canonical_pathlib_io_open_code
            )
        ):
            raise error_type(
                "canonical dataset shard dependency dispatch changed: Path.open.io.open"
            )

        for (
            label,
            owner,
            attr_name,
            expected,
            expected_executable,
            expected_code,
            expected_globals,
            global_witnesses,
        ) in frozen_external_dispatch_witnesses:
            if exact_getattr(owner, attr_name, None) is not expected:
                raise error_type(
                    f"canonical dataset shard dependency dispatch changed: {label}"
                )
            if (
                expected_code is not None
                and exact_getattr(expected_executable, "__code__", None)
                is not expected_code
            ):
                raise error_type(
                    f"canonical dataset shard dependency executable changed: {label}"
                )
            if expected_globals is not None:
                if (
                    exact_getattr(expected_executable, "__globals__", None)
                    is not expected_globals
                    or exact_any(
                        expected_globals.get(name, missing_external_global)
                        is not expected_value
                        for name, expected_value in global_witnesses
                    )
                ):
                    raise error_type(
                        "canonical dataset shard dependency globals changed: "
                        f"{label}"
                    )

        if exact_type(authority) is lineage_authority_type:
            authority_namespace = exact_object_getattribute(authority, "__dict__")
            if exact_any(name in authority_namespace for name in lineage_dispatch_slots):
                raise error_type(
                    "canonical DatasetSnapshotLineageAuthority instance dispatch shadowed"
                )

    def detach_public_inputs(shard_root: Any, shards: Any) -> tuple[Any, tuple[Any, ...]]:
        """Run caller callbacks before entering the trusted positive-authority interval."""

        detached_shards = exact_tuple(shards)
        detached_root = canonical_path(shard_root)
        return detached_root, detached_shards

    def guarded_verify_registered_dataset_shards(
        authority: Any,
        *,
        snapshot_id: str,
        shard_root: Any,
        shards: Any,
    ) -> Any:
        require_authority(authority)
        detached_root, detached_shards = detach_public_inputs(shard_root, shards)
        require_authority(authority)
        result = canonical_verify(
            authority,
            snapshot_id=snapshot_id,
            shard_root=detached_root,
            shards=detached_shards,
        )
        require_authority(authority)
        return result

    def guarded_read_registered_shard_bytes(
        authority: Any,
        *,
        snapshot_id: str,
        shard_root: Any,
        shards: Any,
        shard_id: str,
    ) -> bytes:
        require_authority(authority)
        detached_root, detached_shards = detach_public_inputs(shard_root, shards)
        require_authority(authority)
        result = canonical_read(
            authority,
            snapshot_id=snapshot_id,
            shard_root=detached_root,
            shards=detached_shards,
            shard_id=shard_id,
        )
        require_authority(authority)
        return result

    guarded_verify_registered_dataset_shards.__name__ = canonical_verify.__name__
    guarded_verify_registered_dataset_shards.__qualname__ = canonical_verify.__qualname__
    guarded_verify_registered_dataset_shards.__doc__ = canonical_verify.__doc__
    guarded_verify_registered_dataset_shards.__annotations__ = canonical_verify.__annotations__
    guarded_read_registered_shard_bytes.__name__ = canonical_read.__name__
    guarded_read_registered_shard_bytes.__qualname__ = canonical_read.__qualname__
    guarded_read_registered_shard_bytes.__doc__ = canonical_read.__doc__
    guarded_read_registered_shard_bytes.__annotations__ = canonical_read.__annotations__

    manifest_module.verify_registered_dataset_shards = guarded_verify_registered_dataset_shards
    manifest_module.read_registered_shard_bytes = guarded_read_registered_shard_bytes


_install()
del _install