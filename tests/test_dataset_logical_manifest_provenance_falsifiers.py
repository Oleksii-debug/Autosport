from __future__ import annotations

import hashlib
from pathlib import Path

from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    DatasetSnapshotUnprovenError,
    membership_manifest_sha256,
)
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _machine_root(tmp_path: Path) -> Path:
    return tmp_path.parent / f"{tmp_path.name}-lineage-machine-state"


def test_public_manifest_and_caller_snapshot_cannot_mint_durable_lineage_authority(
    tmp_path: Path,
) -> None:
    """Consistency hashes must not become dataset provenance issuance authority.

    A caller can currently compute the typed membership manifest from arbitrary
    member hashes, append a matching public DatasetSnapshot DTO, and ask the
    lineage authority to persist that assertion. The lineage authority must
    either reject that unissued snapshot immediately or ensure it can never
    survive restart as a proven dataset lineage record.
    """

    registry_path = tmp_path / "scientific-registry.json"
    registry = ScientificRegistry.initialize_pristine(registry_path)

    caller_members = (
        _sha("caller-row-a"),
        _sha("caller-row-b"),
    )
    caller_manifest = membership_manifest_sha256(caller_members)

    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="caller-minted-snapshot",
            manifest_sha256=caller_manifest,
            source_identity="caller-asserted:source",
            license_identity="caller-asserted:license",
            causal_cutoff="2026-09-01T00:00:00Z",
            available_at_utc="2026-09-01T00:01:00Z",
        )
    )

    authority = DatasetSnapshotLineageAuthority.initialize_pristine(
        tmp_path / "dataset-snapshot-lineage.json",
        registry,
        authority_root=_machine_root(tmp_path),
    )

    try:
        authority.register(
            snapshot_id="caller-minted-snapshot",
            member_sha256=caller_members,
        )
    except DatasetSnapshotUnprovenError:
        # Preferred repair: the caller-constructed registry DTO never crosses
        # the provenance-issuance boundary.
        return

    restarted_registry = ScientificRegistry(registry_path)
    restarted = DatasetSnapshotLineageAuthority(
        authority.path,
        restarted_registry,
        authority_root=authority.monotonic_authority.authority_root,
    )

    assert restarted.record("caller-minted-snapshot") is None, (
        "caller-computed member hashes plus a caller-constructed DatasetSnapshot "
        "became durable product dataset authority; manifest equality proves "
        "self-consistency, not product-issued provenance"
    )
