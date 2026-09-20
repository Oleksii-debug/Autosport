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


# ``importlib.reload`` reuses this module's globals dictionary.  Preserve the
# pre-wrapper implementations only on the first execution so self-reload cannot
# recapture our own wrappers and turn them into recursive "originals".
if "_PRISTINE_BIND" not in globals():
    _PRISTINE_BIND = evidence.PointInTimeFeatureAuthority.bind
if "_PRISTINE_LOAD" not in globals():
    _PRISTINE_LOAD = evidence.HoldoutConsumptionLedger._load

_PROVENANCE_GUARD_MODULE_NAME = (
    f"{__package__}._point_in_time_feature_provenance_guard"
)
# Diagnostic only.  Installation/deduplication below never trusts this public
# marker as possession proof; the exact first finder object is the authority.
_RELOAD_FINDER_MARKER = "_autosport_point_in_time_reload_finder_v1"
_SOURCE_AUTHORITY_REQUIRED = (
    "positive point-in-time feature evidence requires an independent "
    "source-owned feature artifact authority"
)


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
    """Fail closed when an exact capability shadows trusted class methods."""

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
    """Validate canonical lineage facts, then fail closed without source authority.

    ``FeatureArtifactProvenance.issue`` hashes caller-provided bytes and
    ``DatasetSnapshotLineageAuthority.register`` accepts caller-provided member
    digests.  Those two durable objects therefore cannot, by themselves, establish
    that the feature artifact was independently owned by ingestion/dataset source
    authority.  Keep every existing negative/cutoff/provenance check active by
    delegating first, but never turn a syntactically valid self-authored lineage into
    positive evidence until a source-owned artifact/member capability is available.
    """

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
    _PRISTINE_BIND(lineage_authority=lineage_authority, **kwargs)
    raise evidence.PointInTimeEvidenceError(_SOURCE_AUTHORITY_REQUIRED)


def _refresh_monotonic_authority(ledger: evidence.HoldoutConsumptionLedger) -> None:
    """Re-resolve the same durable authority identity under the ledger lock."""

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
    _PRISTINE_LOAD(self)


def _install_runtime_guards() -> None:
    """Reinstall every authority-bearing point-in-time patch on the live module."""

    from . import _point_in_time_feature_provenance_guard as provenance_guard

    evidence.FeatureArtifactProvenance = provenance_guard.FeatureArtifactProvenance
    evidence.FeatureAvailabilityEvidence = provenance_guard.FeatureAvailabilityEvidence
    evidence.PointInTimeFeatureAuthority.bind = staticmethod(
        _bind_exact_lineage_authority
    )
    evidence.HoldoutConsumptionLedger._load = _load_with_fresh_authority
    evidence._fsync_directory = _fsync_directory_fail_closed


class _PointInTimeReloadLoader(importlib.abc.Loader):
    """Wrap reload execution of either authority-bearing point-in-time submodule."""

    def __init__(self, wrapped: importlib.abc.Loader) -> None:
        self._wrapped = wrapped

    def create_module(self, spec):
        create = getattr(self._wrapped, "create_module", None)
        if create is None:
            return None
        return create(spec)

    def exec_module(self, module) -> None:
        self._wrapped.exec_module(module)
        if module is evidence or module.__name__ == _PROVENANCE_GUARD_MODULE_NAME:
            _install_runtime_guards()


class _PointInTimeReloadFinder(importlib.abc.MetaPathFinder):
    """Intercept explicit reload of either already-loaded authority submodule."""

    # Exposed only for diagnostics/tests.  A caller can forge this attribute, so
    # canonical installation is fenced by exact finder object identity instead.
    _autosport_point_in_time_reload_finder_v1 = True

    def find_spec(self, fullname, path, target=None):
        is_evidence_reload = fullname == evidence.__name__ and target is evidence
        is_provenance_reload = (
            fullname == _PROVENANCE_GUARD_MODULE_NAME
            and target is sys.modules.get(_PROVENANCE_GUARD_MODULE_NAME)
        )
        if not (is_evidence_reload or is_provenance_reload):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _PointInTimeReloadLoader(spec.loader)
        return spec


# Keep the first real finder instance across ``importlib.reload``.  Its methods
# resolve globals through this reused module dictionary, so the old object safely
# dispatches to the newest loader/guard implementations.  Object identity cannot
# be pre-seeded by a caller before the module has executed.
if "_CANONICAL_RELOAD_FINDER" not in globals():
    _CANONICAL_RELOAD_FINDER = _PointInTimeReloadFinder()


def _install_reload_finder() -> None:
    if any(finder is _CANONICAL_RELOAD_FINDER for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _CANONICAL_RELOAD_FINDER)


# The provenance guard is imported first by autosport.__init__, so wrapping here
# preserves all of its canonical DatasetSnapshot/FeatureSet/provenance/publication
# checks while fencing the capabilities before they can dispatch to caller code.
_install_runtime_guards()
_install_reload_finder()


__all__: list[str] = []