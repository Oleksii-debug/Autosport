from __future__ import annotations

"""Fail-closed backing identity guard for forward provider-universe authority.

The provider universe already persists its denominator under EvaluationUniverseStore,
which is protected by MonotonicWorkspaceAuthority and a durable workspace identity.
This guard does not create another store or chronology. It derives one stable locator
from those existing product-owned bindings and pins the first locator observed for an
exact ProviderEvaluationUniverseStore object. The locator digest is also carried by
forward source receipts so restart/reconstruction evidence remains bound to the same
workspace authority rather than only to Python object identity.
"""

import hashlib
import json
from dataclasses import dataclass
from threading import RLock
from weakref import WeakKeyDictionary

from .evaluation_universe import EvaluationUniverseLedger, EvaluationUniverseStore
from .monotonic_workspace_authority import MonotonicWorkspaceAuthority
from .provider_evaluation_universe import ProviderEvaluationUniverseStore


_CANONICAL_EVALUATION_UNIVERSE_LOAD = EvaluationUniverseStore.load


class ForwardUniverseBackingGuardError(RuntimeError):
    """The provider-universe backing no longer matches its bound authority locator."""


@dataclass(frozen=True, slots=True)
class ForwardUniverseBackingLocator:
    authority_id: str
    source_id: str
    workspace_instance_id: str
    workspace_locator_sha256: str
    monotonic_namespace_sha256: str

    @property
    def locator_sha256(self) -> str:
        payload = {
            "authority_id": self.authority_id,
            "kind": "autosport.forward-provider-universe-backing-locator.v1",
            "monotonic_namespace_sha256": self.monotonic_namespace_sha256,
            "source_id": self.source_id,
            "workspace_instance_id": self.workspace_instance_id,
            "workspace_locator_sha256": self.workspace_locator_sha256,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


_LOCK = RLock()
_ORIGIN_BY_STORE: WeakKeyDictionary[
    ProviderEvaluationUniverseStore, ForwardUniverseBackingLocator
] = WeakKeyDictionary()


def _current_locator(
    store: ProviderEvaluationUniverseStore,
) -> ForwardUniverseBackingLocator:
    if type(store) is not ProviderEvaluationUniverseStore:
        raise TypeError("store must be exact ProviderEvaluationUniverseStore")

    backing = getattr(store, "_store", None)
    intake = getattr(store, "_intake", None)
    if type(backing) is not EvaluationUniverseStore:
        raise ForwardUniverseBackingGuardError(
            "provider-universe backing store is not canonical EvaluationUniverseStore"
        )
    if backing.intake_ledger is not intake:
        raise ForwardUniverseBackingGuardError(
            "provider-universe backing intake authority changed"
        )
    if (
        getattr(intake, "authority_id", None) != store.authority_id
        or getattr(intake, "source_id", None) != store.source_id
    ):
        raise ForwardUniverseBackingGuardError(
            "provider-universe backing authority/source identity changed"
        )
    if backing.path != backing.workspace / EvaluationUniverseStore.FILE_NAME:
        raise ForwardUniverseBackingGuardError(
            "provider-universe backing ledger path changed"
        )

    authority = getattr(backing, "monotonic_authority", None)
    if type(authority) is not MonotonicWorkspaceAuthority:
        raise ForwardUniverseBackingGuardError(
            "provider-universe backing lacks canonical monotonic authority"
        )
    if (
        authority.workspace != backing.workspace
        or authority.domain != "evaluation-universe"
        or authority.key != store.authority_id
    ):
        raise ForwardUniverseBackingGuardError(
            "provider-universe monotonic authority locator changed"
        )

    binding = authority.workspace_binding
    if (
        binding.workspace_instance_id != authority.workspace_instance_id
        or binding.workspace != backing.workspace
    ):
        raise ForwardUniverseBackingGuardError(
            "provider-universe workspace identity binding changed"
        )

    return ForwardUniverseBackingLocator(
        authority_id=store.authority_id,
        source_id=store.source_id,
        workspace_instance_id=authority.workspace_instance_id,
        workspace_locator_sha256=binding.workspace_locator_sha256,
        monotonic_namespace_sha256=authority.namespace_sha256,
    )


def resolve_forward_universe_backing_locator(
    store: ProviderEvaluationUniverseStore,
) -> ForwardUniverseBackingLocator:
    """Resolve and pin the existing durable backing locator for ``store``.

    The durable components (workspace instance/path binding and monotonic namespace)
    are restart-stable. The per-object pin prevents coherent in-process replacement of
    both private backing members after positive authority has already been resolved.
    Downstream receipt hashes include ``locator_sha256`` so restart evidence keeps the
    same durable locator commitment.
    """

    current = _current_locator(store)
    with _LOCK:
        origin = _ORIGIN_BY_STORE.get(store)
        if origin is None:
            _ORIGIN_BY_STORE[store] = current
            return current
        if origin != current:
            raise ForwardUniverseBackingGuardError(
                "provider-universe backing locator changed after authority binding"
            )
        return origin


def _build_guarded_load(
    *,
    store_type: type[ProviderEvaluationUniverseStore],
    backing_type: type[EvaluationUniverseStore],
    canonical_load,
    locator_resolver,
):
    """Capture exact load/locator executables so module aliases cannot retarget them."""

    canonical_load_code = canonical_load.__code__
    locator_code = locator_resolver.__code__

    def guarded_load(
        store: ProviderEvaluationUniverseStore,
    ) -> tuple[EvaluationUniverseLedger | None, ForwardUniverseBackingLocator]:
        if type(store) is not store_type:
            raise TypeError("store must be exact ProviderEvaluationUniverseStore")
        if locator_resolver.__code__ is not locator_code:
            raise ForwardUniverseBackingGuardError(
                "provider-universe backing locator resolver changed"
            )
        origin = locator_resolver(store)
        backing = getattr(store, "_store", None)
        if type(backing) is not backing_type:
            raise ForwardUniverseBackingGuardError(
                "provider-universe backing store changed during guarded load"
            )
        if locator_resolver.__code__ is not locator_code:
            raise ForwardUniverseBackingGuardError(
                "provider-universe backing locator resolver changed"
            )
        if locator_resolver(store) != origin:
            raise ForwardUniverseBackingGuardError(
                "provider-universe backing locator changed before durable load"
            )
        if canonical_load.__code__ is not canonical_load_code:
            raise ForwardUniverseBackingGuardError(
                "canonical evaluation-universe load authority changed"
            )

        ledger = canonical_load(backing)

        if canonical_load.__code__ is not canonical_load_code:
            raise ForwardUniverseBackingGuardError(
                "canonical evaluation-universe load authority changed"
            )
        if locator_resolver.__code__ is not locator_code:
            raise ForwardUniverseBackingGuardError(
                "provider-universe backing locator resolver changed"
            )
        if locator_resolver(store) != origin:
            raise ForwardUniverseBackingGuardError(
                "provider-universe backing locator changed during durable load"
            )
        return ledger, origin

    return guarded_load


load_guarded_provider_evaluation_universe = _build_guarded_load(
    store_type=ProviderEvaluationUniverseStore,
    backing_type=EvaluationUniverseStore,
    canonical_load=_CANONICAL_EVALUATION_UNIVERSE_LOAD,
    locator_resolver=resolve_forward_universe_backing_locator,
)


__all__ = [
    "ForwardUniverseBackingGuardError",
    "ForwardUniverseBackingLocator",
    "load_guarded_provider_evaluation_universe",
    "resolve_forward_universe_backing_locator",
]
