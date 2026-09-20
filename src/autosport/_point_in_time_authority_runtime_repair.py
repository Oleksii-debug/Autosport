"""Exact capability and stale-instance repair for point-in-time evidence authority.

This compatibility guard composes with the canonical point-in-time provenance and
monotonic-workspace authorities.  It adds no parallel source of truth: positive
feature evidence must receive the exact DatasetSnapshotLineageAuthority concrete
capability before any registry/record dispatch, and each holdout ledger reload
re-resolves the same durable workspace identity/domain/key/root so a process view
created while the workspace was pristine can safely observe a later valid binding.
"""

from __future__ import annotations

from . import point_in_time_evidence as evidence
from .dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from .monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .scientific_registry import ScientificRegistry


_ORIGINAL_BIND = evidence.PointInTimeFeatureAuthority.bind
_ORIGINAL_LOAD = evidence.HoldoutConsumptionLedger._load


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


# The provenance guard is imported first by autosport.__init__, so wrapping here
# preserves all of its canonical DatasetSnapshot/FeatureSet/provenance/publication
# checks while fencing the capabilities before they can dispatch to caller code.
evidence.PointInTimeFeatureAuthority.bind = staticmethod(_bind_exact_lineage_authority)
evidence.HoldoutConsumptionLedger._load = _load_with_fresh_authority


__all__: list[str] = []
