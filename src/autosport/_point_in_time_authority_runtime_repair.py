"""Exact capability and stale-instance repair for point-in-time evidence authority.

This compatibility guard composes with the canonical point-in-time provenance and
monotonic-workspace authorities.  It adds no parallel source of truth: positive
feature evidence must receive the exact DatasetSnapshotLineageAuthority concrete
capability before any registry/record dispatch, and each holdout ledger reload
re-resolves the same durable workspace identity/domain/key/root so a process view
created while the workspace was pristine can safely observe a later valid binding.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import sys

from . import point_in_time_evidence as evidence
from .dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from .monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .scientific_registry import ScientificRegistry


_ORIGINAL_BIND = evidence.PointInTimeFeatureAuthority.bind
_ORIGINAL_LOAD = evidence.HoldoutConsumptionLedger._load


def _fsync_directory_fail_closed(path: Path) -> None:
    """Require rename-directory durability on supported non-Windows filesystems."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise evidence.EvidenceLedgerCorruptError(
            "cannot open holdout ledger directory for durability"
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise evidence.EvidenceLedgerCorruptError(
            "cannot fsync holdout ledger directory for durability"
        ) from exc
    finally:
        os.close(descriptor)


def _reject_instance_method_shadows(
    instance: object,
    concrete_type: type,
    *,
    authority_name: str,
) -> None:
    """Fail closed when an exact capability shadows trusted class methods.

    Exact-type checks prevent subclass dispatch, but both accepted authority classes
    intentionally carry mutable instance state.  A caller must not be able to place a
    same-named callable in ``__dict__`` (for example ``record``, ``get`` or an
    internal read/verify method) and thereby redirect the later authority call while
    still satisfying ``type(instance) is concrete_type``.
    """

    shadowed = sorted(
        name
        for name in vars(instance)
        if callable(getattr(concrete_type, name, None))
    )
    if shadowed:
        raise evidence.PointInTimeEvidenceError(
            f"{authority_name} shadows trusted concrete authority method: {shadowed[0]}"
        )


def _bind_exact_lineage_authority(*, lineage_authority, **kwargs):
    """Reject caller-polymorphic lineage/registry authority before any positive read."""

    if type(lineage_authority) is not DatasetSnapshotLineageAuthority:
        raise evidence.PointInTimeEvidenceError(
            "lineage_authority must be an exact DatasetSnapshotLineageAuthority"
        )
    if type(lineage_authority.registry) is not ScientificRegistry:
        raise evidence.PointInTimeEvidenceError(
            "lineage_authority.registry must be an exact ScientificRegistry"
        )
    _reject_instance_method_shadows(
        lineage_authority,
        DatasetSnapshotLineageAuthority,
        authority_name="lineage_authority",
    )
    _reject_instance_method_shadows(
        lineage_authority.registry,
        ScientificRegistry,
        authority_name="lineage_authority.registry",
    )
    return _ORIGINAL_BIND(lineage_authority=lineage_authority, **kwargs)


def _refresh_monotonic_authority(ledger: evidence.HoldoutConsumptionLedger) -> None:
    """Re-resolve the same durable authority identity under the ledger lock.

    Two HoldoutConsumptionLedger objects may both be constructed while a workspace is
    pristine, before either has a durable workspace marker.  Their initial authority
    objects therefore carry provisional random workspace ids.  Once one writer
    performs PREPARE it durably binds the canonical id.  A stale process view must
    resolve that durable binding before validating the newly published local state;
    retaining its provisional id would incorrectly report identity corruption.

    Reconstructing the authority from the same workspace/domain/key/resolved external
    root cannot reset freshness: surviving path/marker/history evidence is still
    validated by MonotonicWorkspaceAuthority and rollback/deletion/move/copy/tamper
    checks remain fail-closed.
    """

    previous = ledger._authority
    try:
        ledger._authority = MonotonicWorkspaceAuthority(
            workspace=ledger._workspace,
            domain=evidence._HOLDOUT_AUTHORITY_DOMAIN,
            key=f"holdout-ledger:{ledger._path.name}",
            authority_root=previous.authority_root,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise evidence.EvidenceLedgerCorruptError(
            "holdout ledger workspace authority identity is invalid"
        ) from exc


def _load_with_fresh_authority(self: evidence.HoldoutConsumptionLedger) -> None:
    _refresh_monotonic_authority(self)
    _ORIGINAL_LOAD(self)


def _install_runtime_guards() -> None:
    """Reinstall every authority-bearing point-in-time patch on the live module.

    ``importlib.reload`` re-executes a submodule without re-executing package
    ``__init__`` or already-cached guard modules.  Without an explicit reinstall, a
    reload of ``point_in_time_evidence`` can therefore resurrect its legacy
    caller-constructible feature bind.  Keep the reload target fail-closed by restoring
    the canonical provenance classes and exact capability fences immediately after
    the underlying source module executes.
    """

    from . import _point_in_time_feature_provenance_guard as provenance_guard

    evidence.FeatureArtifactProvenance = provenance_guard.FeatureArtifactProvenance
    evidence.FeatureAvailabilityEvidence = provenance_guard.FeatureAvailabilityEvidence
    evidence.PointInTimeFeatureAuthority.bind = staticmethod(
        _bind_exact_lineage_authority
    )
    evidence.HoldoutConsumptionLedger._load = _load_with_fresh_authority
    evidence._fsync_directory = _fsync_directory_fail_closed


class _PointInTimeReloadLoader(importlib.abc.Loader):
    """Wrap only reload execution of the point-in-time authority module."""

    def __init__(self, wrapped: importlib.abc.Loader) -> None:
        self._wrapped = wrapped

    def create_module(self, spec):
        create = getattr(self._wrapped, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module) -> None:
        self._wrapped.exec_module(module)
        if module is evidence:
            _install_runtime_guards()


class _PointInTimeReloadFinder(importlib.abc.MetaPathFinder):
    """Intercept only explicit reload of the already-loaded authority submodule."""

    def find_spec(self, fullname, path, target=None):
        if fullname != evidence.__name__ or target is not evidence:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _PointInTimeReloadLoader(spec.loader)
        return spec


def _install_reload_finder() -> None:
    if any(isinstance(finder, _PointInTimeReloadFinder) for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _PointInTimeReloadFinder())


# The provenance guard is imported first by autosport.__init__, so wrapping here
# preserves all of its canonical DatasetSnapshot/FeatureSet/provenance/publication
# checks while fencing the capabilities before they can dispatch to caller code.
_install_runtime_guards()
_install_reload_finder()


__all__: list[str] = []