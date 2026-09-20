"""Exact capability and stale-instance repair for point-in-time evidence authority.

This compatibility guard composes with the canonical point-in-time provenance,
dataset-lineage, and monotonic-workspace authorities. Positive feature evidence
remains fail-closed without independent source-owned artifact authority. Holdout
freshness is resolved from an exact canonical DatasetSnapshotLineageAuthority under
the holdout lock, so caller-constructed DatasetSnapshot fields cannot mint a new
physical confirmation identity.
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
from .scientific_registry import DatasetSnapshot, ScientificRegistry


# ``importlib.reload`` reuses this module's globals dictionary. Preserve the
# pre-wrapper implementations only on first execution so self-reload cannot
# recapture our wrappers and turn them into recursive "originals".
if "_PRISTINE_BIND" not in globals():
    _PRISTINE_BIND = evidence.PointInTimeFeatureAuthority.bind
if "_PRISTINE_LOAD" not in globals():
    _PRISTINE_LOAD = evidence.HoldoutConsumptionLedger._load
if "_PRISTINE_LEDGER_INIT" not in globals():
    _PRISTINE_LEDGER_INIT = evidence.HoldoutConsumptionLedger.__init__
if "_PRISTINE_ACCESS_ID" not in globals():
    _PRISTINE_ACCESS_ID = evidence.HoldoutConsumptionLedger.access_id
if "_PRISTINE_FRESHNESS_ID" not in globals():
    _PRISTINE_FRESHNESS_ID = evidence.HoldoutConsumptionLedger.freshness_id

_PROVENANCE_GUARD_MODULE_NAME = (
    f"{__package__}._point_in_time_feature_provenance_guard"
)
_RELOAD_FINDER_MARKER = "_autosport_point_in_time_reload_finder_v1"
_SOURCE_AUTHORITY_REQUIRED = (
    "positive point-in-time feature evidence requires an independent "
    "source-owned feature artifact authority"
)
_HOLDOUT_LINEAGE_REQUIRED = (
    "holdout consumption requires an exact canonical DatasetSnapshotLineageAuthority"
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


def _require_exact_lineage_authority(lineage_authority) -> DatasetSnapshotLineageAuthority:
    if type(lineage_authority) is not DatasetSnapshotLineageAuthority:
        raise evidence.PointInTimeEvidenceError(_HOLDOUT_LINEAGE_REQUIRED)
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
    return lineage_authority


def _bind_exact_lineage_authority(*, lineage_authority, **kwargs):
    """Validate canonical lineage facts, then fail closed without source authority."""

    _require_exact_lineage_authority(lineage_authority)
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


def _holdout_init_with_lineage(
    self: evidence.HoldoutConsumptionLedger,
    path: str | Path,
    *,
    authority_root: str | Path | None = None,
    lineage_authority=None,
) -> None:
    if lineage_authority is not None:
        lineage_authority = _require_exact_lineage_authority(lineage_authority)
    self._dataset_lineage_authority = lineage_authority
    _PRISTINE_LEDGER_INIT(self, path, authority_root=authority_root)


def _resolve_canonical_snapshot(
    ledger: evidence.HoldoutConsumptionLedger,
    dataset_snapshot: DatasetSnapshot,
) -> DatasetSnapshot:
    """Treat caller snapshot fields as assertions against durable lineage truth."""

    if type(dataset_snapshot) is not DatasetSnapshot:
        raise evidence.PointInTimeEvidenceError(
            "dataset_snapshot must be an exact DatasetSnapshot"
        )
    lineage = _require_exact_lineage_authority(
        getattr(ledger, "_dataset_lineage_authority", None)
    )
    try:
        record = lineage.record(dataset_snapshot.dataset_snapshot_id)
    except ValueError as exc:
        raise evidence.PointInTimeEvidenceError(
            "canonical dataset lineage authority could not resolve dataset_snapshot"
        ) from exc
    if record is None:
        raise evidence.PointInTimeEvidenceError(
            "dataset_snapshot is not registered in canonical dataset lineage authority"
        )

    asserted = {
        "dataset_snapshot_id": dataset_snapshot.dataset_snapshot_id,
        "manifest_sha256": dataset_snapshot.manifest_sha256.lower(),
        "source_identity": dataset_snapshot.source_identity,
        "license_identity": dataset_snapshot.license_identity,
        "causal_cutoff": dataset_snapshot.causal_cutoff,
        "available_at_utc": dataset_snapshot.available_at_utc,
    }
    canonical = {
        "dataset_snapshot_id": record.snapshot_id,
        "manifest_sha256": record.manifest_sha256,
        "source_identity": record.source_identity,
        "license_identity": record.license_identity,
        "causal_cutoff": record.causal_cutoff,
        "available_at_utc": record.available_at,
    }
    if asserted != canonical:
        raise evidence.PointInTimeEvidenceError(
            "dataset_snapshot assertion does not match canonical dataset lineage authority"
        )
    return DatasetSnapshot(
        dataset_snapshot_id=record.snapshot_id,
        manifest_sha256=record.manifest_sha256,
        source_identity=record.source_identity,
        license_identity=record.license_identity,
        causal_cutoff=record.causal_cutoff,
        available_at_utc=record.available_at,
    )


def _access_id_from_lineage(
    self: evidence.HoldoutConsumptionLedger,
    *,
    dataset_snapshot: DatasetSnapshot,
    research_protocol_id: str,
    confirmation_trial_family_id: str,
) -> str:
    with self._lock:
        with evidence._HoldoutLedgerLock(self._path.parent):
            canonical = _resolve_canonical_snapshot(self, dataset_snapshot)
            return _PRISTINE_ACCESS_ID(
                dataset_snapshot=canonical,
                research_protocol_id=research_protocol_id,
                confirmation_trial_family_id=confirmation_trial_family_id,
            )


def _freshness_id_from_lineage(
    self: evidence.HoldoutConsumptionLedger,
    *,
    dataset_snapshot: DatasetSnapshot,
    confirmation_trial_family_id: str,
) -> str:
    with self._lock:
        with evidence._HoldoutLedgerLock(self._path.parent):
            canonical = _resolve_canonical_snapshot(self, dataset_snapshot)
            return _PRISTINE_FRESHNESS_ID(
                dataset_snapshot=canonical,
                confirmation_trial_family_id=confirmation_trial_family_id,
            )


def _assert_unused_from_lineage(
    self: evidence.HoldoutConsumptionLedger,
    *,
    dataset_snapshot: DatasetSnapshot,
    research_protocol_id: str,
    confirmation_trial_family_id: str,
) -> None:
    evidence._text(research_protocol_id, "research_protocol_id")
    with self._lock:
        with evidence._HoldoutLedgerLock(self._path.parent):
            self._load()
            canonical = _resolve_canonical_snapshot(self, dataset_snapshot)
            freshness_id = _PRISTINE_FRESHNESS_ID(
                dataset_snapshot=canonical,
                confirmation_trial_family_id=confirmation_trial_family_id,
            )
            existing = self._records.get(freshness_id)
            if existing is not None:
                raise evidence.HoldoutAlreadyConsumedError(
                    f"holdout {existing.holdout_access_id} was already consumed by "
                    f"{existing.consumer_identity}"
                )


def _consume_from_lineage(
    self: evidence.HoldoutConsumptionLedger,
    *,
    dataset_snapshot: DatasetSnapshot,
    research_protocol_id: str,
    confirmation_trial_family_id: str,
    consumer_identity: str,
    purpose: str,
    consumed_at_utc: str,
) -> evidence.HoldoutConsumption:
    with self._lock:
        with evidence._HoldoutLedgerLock(self._path.parent):
            self._load()
            canonical = _resolve_canonical_snapshot(self, dataset_snapshot)
            access_id = _PRISTINE_ACCESS_ID(
                dataset_snapshot=canonical,
                research_protocol_id=research_protocol_id,
                confirmation_trial_family_id=confirmation_trial_family_id,
            )
            freshness_id = _PRISTINE_FRESHNESS_ID(
                dataset_snapshot=canonical,
                confirmation_trial_family_id=confirmation_trial_family_id,
            )
            consumed_at = evidence._instant(consumed_at_utc, "consumed_at_utc")
            if consumed_at < evidence._instant(
                canonical.available_at_utc, "dataset_snapshot.available_at"
            ):
                raise evidence.FutureEvidenceError(
                    "holdout cannot be consumed before the dataset is available"
                )

            record = evidence.HoldoutConsumption(
                holdout_access_id=access_id,
                holdout_freshness_id=freshness_id,
                research_protocol_id=evidence._text(
                    research_protocol_id, "research_protocol_id"
                ),
                confirmation_trial_family_id=evidence._text(
                    confirmation_trial_family_id, "confirmation_trial_family_id"
                ),
                dataset_manifest_sha256=evidence._sha256(
                    canonical.manifest_sha256, "dataset_snapshot.manifest_sha256"
                ),
                source_identity=evidence._text(
                    canonical.source_identity, "dataset_snapshot.source_identity"
                ),
                license_identity=evidence._text(
                    canonical.license_identity, "dataset_snapshot.license_identity"
                ),
                consumer_identity=evidence._text(consumer_identity, "consumer_identity"),
                purpose=evidence._text(purpose, "purpose"),
                consumed_at_utc=evidence._utc_text(consumed_at_utc, "consumed_at_utc"),
            )

            existing = self._records.get(freshness_id)
            if existing is not None:
                if (
                    existing.holdout_access_id == record.holdout_access_id
                    and existing.consumer_identity == record.consumer_identity
                    and existing.purpose == record.purpose
                ):
                    return existing
                raise evidence.HoldoutAlreadyConsumedError(
                    f"holdout {existing.holdout_access_id} was already consumed by "
                    f"{existing.consumer_identity}"
                )

            previous_state_sha256 = self._state_sha256
            self._records[freshness_id] = record
            tx_id = self._next_transition_tx_id(record.consumption_id)
            payload, semantic_binding = self._payload_for_transition(
                added_record=record,
                previous_state_sha256=previous_state_sha256,
                tx_id=tx_id,
            )
            intended_state_sha256 = evidence._digest(payload)
            try:
                self._authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=previous_state_sha256,
                    intended_state_sha256=intended_state_sha256,
                    semantic_binding_sha256=semantic_binding,
                )
                evidence._atomic_write_json(self._path, payload)
                self._load()
            except MonotonicWorkspaceAuthorityError as exc:
                self._records.pop(freshness_id, None)
                self._state_sha256 = previous_state_sha256
                raise evidence.EvidenceLedgerCorruptError(
                    "holdout ledger monotonic transition was rejected"
                ) from exc
            except BaseException:
                self._records.pop(freshness_id, None)
                self._state_sha256 = previous_state_sha256
                raise
            persisted = self._records.get(freshness_id)
            if persisted != record:
                raise evidence.EvidenceLedgerCorruptError(
                    "holdout ledger publish did not re-resolve the intended consumption"
                )
            return persisted


def _install_runtime_guards() -> None:
    """Reinstall every authority-bearing point-in-time patch on the live module."""

    from . import _point_in_time_feature_provenance_guard as provenance_guard

    evidence.FeatureArtifactProvenance = provenance_guard.FeatureArtifactProvenance
    evidence.FeatureAvailabilityEvidence = provenance_guard.FeatureAvailabilityEvidence
    evidence.PointInTimeFeatureAuthority.bind = staticmethod(
        _bind_exact_lineage_authority
    )
    evidence.HoldoutConsumptionLedger.__init__ = _holdout_init_with_lineage
    evidence.HoldoutConsumptionLedger._load = _load_with_fresh_authority
    evidence.HoldoutConsumptionLedger.access_id = _access_id_from_lineage
    evidence.HoldoutConsumptionLedger.freshness_id = _freshness_id_from_lineage
    evidence.HoldoutConsumptionLedger.assert_unused = _assert_unused_from_lineage
    evidence.HoldoutConsumptionLedger.consume = _consume_from_lineage
    evidence._fsync_directory = _fsync_directory_fail_closed


class _PointInTimeReloadLoader(importlib.abc.Loader):
    """Wrap reload execution of either already-loaded authority-bearing point-in-time submodule."""

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


if "_CANONICAL_RELOAD_FINDER" not in globals():
    _CANONICAL_RELOAD_FINDER = _PointInTimeReloadFinder()


def _install_reload_finder() -> None:
    if any(finder is _CANONICAL_RELOAD_FINDER for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _CANONICAL_RELOAD_FINDER)


_install_runtime_guards()
_install_reload_finder()


__all__: list[str] = []
