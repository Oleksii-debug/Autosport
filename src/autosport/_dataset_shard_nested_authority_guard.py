"""Fence nested authorities used by registered DatasetSnapshot shard reads.

The registered-shard API already exact-fences DatasetSnapshotLineageAuthority itself.
Its canonical ``record`` reader, however, crosses two further mutable Python objects:
ScientificRegistry and MonotonicWorkspaceAuthority.  A caller that can shadow a method
on either exact instance must not gain an authority-bearing callback inside the
otherwise non-virtual lineage reader.

This module adds no new storage or lineage semantics.  It snapshots the final product
class dispatch surfaces already installed for those two existing authorities, rejects
instance method shadows, and wraps only the two registered-shard public entrypoints.
The broader shard authority guard imported afterwards then captures/seals these exact
wrappers as part of its normal Wave M authority witness.
"""

from __future__ import annotations

from types import FunctionType
from typing import Any

from . import dataset_shard_manifest as _manifest
from .dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from .monotonic_workspace_authority import MonotonicWorkspaceAuthority
from .scientific_registry import ScientificRegistry


def _install() -> None:
    manifest_module = _manifest
    error_type = manifest_module.DatasetShardManifestError
    lineage_type = DatasetSnapshotLineageAuthority
    registry_type = ScientificRegistry
    monotonic_type = MonotonicWorkspaceAuthority
    exact_type = type
    exact_getattr = getattr
    exact_object_getattribute = object.__getattribute__
    missing = object()

    canonical_verify = manifest_module.verify_registered_dataset_shards
    canonical_read = manifest_module.read_registered_shard_bytes

    def executable(slot: object) -> object | None:
        if exact_type(slot) in {staticmethod, classmethod}:
            return slot.__func__
        if exact_type(slot) is property:
            return slot.fget
        if exact_type(slot) is FunctionType:
            return slot
        return None

    def capture_callable_surface(owner: type) -> tuple[tuple[str, object, object, object], ...]:
        surface: list[tuple[str, object, object, object]] = []
        namespace = type.__getattribute__(owner, "__dict__")
        for name, slot in namespace.items():
            target = executable(slot)
            if target is None:
                continue
            surface.append((name, slot, target, exact_getattr(target, "__code__", None)))
        return tuple(surface)

    registry_surface = capture_callable_surface(registry_type)
    monotonic_surface = capture_callable_surface(monotonic_type)
    registry_shadow_names = frozenset(name for name, *_rest in registry_surface)
    monotonic_shadow_names = frozenset(name for name, *_rest in monotonic_surface)

    def require_surface(
        owner: type,
        surface: tuple[tuple[str, object, object, object], ...],
        *,
        label: str,
    ) -> None:
        namespace = type.__getattribute__(owner, "__dict__")
        for name, expected_slot, expected_target, expected_code in surface:
            if namespace.get(name, missing) is not expected_slot:
                raise error_type(
                    f"canonical dataset shard nested {label} dispatch changed: {name}"
                )
            current_target = executable(expected_slot)
            if current_target is not expected_target:
                raise error_type(
                    f"canonical dataset shard nested {label} executable changed: {name}"
                )
            if (
                expected_code is not None
                and exact_getattr(expected_target, "__code__", None) is not expected_code
            ):
                raise error_type(
                    f"canonical dataset shard nested {label} executable changed: {name}"
                )

    def require_nested_authority(authority: Any) -> None:
        if exact_type(authority) is not lineage_type:
            raise error_type("canonical DatasetSnapshotLineageAuthority is required")

        authority_namespace = exact_object_getattribute(authority, "__dict__")
        registry = authority_namespace.get("registry", missing)
        monotonic = authority_namespace.get("monotonic_authority", missing)
        if exact_type(registry) is not registry_type:
            raise error_type(
                "canonical DatasetSnapshotLineageAuthority ScientificRegistry dependency changed"
            )
        if exact_type(monotonic) is not monotonic_type:
            raise error_type(
                "canonical DatasetSnapshotLineageAuthority monotonic dependency changed"
            )

        require_surface(registry_type, registry_surface, label="ScientificRegistry")
        require_surface(
            monotonic_type,
            monotonic_surface,
            label="MonotonicWorkspaceAuthority",
        )

        registry_namespace = exact_object_getattribute(registry, "__dict__")
        if any(name in registry_namespace for name in registry_shadow_names):
            raise error_type(
                "canonical dataset shard nested ScientificRegistry instance dispatch shadowed"
            )
        monotonic_namespace = exact_object_getattribute(monotonic, "__dict__")
        if any(name in monotonic_namespace for name in monotonic_shadow_names):
            raise error_type(
                "canonical dataset shard nested MonotonicWorkspaceAuthority instance dispatch shadowed"
            )

    def guarded_verify_registered_dataset_shards(
        authority: Any,
        *,
        snapshot_id: str,
        shard_root: Any,
        shards: Any,
    ) -> Any:
        require_nested_authority(authority)
        result = canonical_verify(
            authority,
            snapshot_id=snapshot_id,
            shard_root=shard_root,
            shards=shards,
        )
        require_nested_authority(authority)
        return result

    def guarded_read_registered_shard_bytes(
        authority: Any,
        *,
        snapshot_id: str,
        shard_root: Any,
        shards: Any,
        shard_id: str,
    ) -> bytes:
        require_nested_authority(authority)
        result = canonical_read(
            authority,
            snapshot_id=snapshot_id,
            shard_root=shard_root,
            shards=shards,
            shard_id=shard_id,
        )
        require_nested_authority(authority)
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
