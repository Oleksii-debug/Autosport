from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    DatasetSnapshotUnprovenError,
    membership_manifest_sha256,
)
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_public_hash_and_registry_append_cannot_mint_dataset_lineage_authority(
    tmp_path: Path,
) -> None:
    members = (_sha("caller-chosen-row-a"), _sha("caller-chosen-row-b"))
    manifest_sha256 = membership_manifest_sha256(members)

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="caller-minted-snapshot",
            manifest_sha256=manifest_sha256,
            source_identity="provider:caller-asserted-source",
            license_identity="license:caller-asserted",
            causal_cutoff="2026-09-01T00:00:00Z",
            available_at_utc="2026-09-01T00:01:00Z",
        )
    )

    lineage_path = tmp_path / "dataset-snapshot-lineage.json"
    authority_root = tmp_path / "lineage-machine-authority"
    lineage = DatasetSnapshotLineageAuthority.initialize_pristine(
        lineage_path,
        registry,
        authority_root=authority_root,
    )

    try:
        lineage.register(
            snapshot_id="caller-minted-snapshot",
            member_sha256=members,
        )
    except DatasetSnapshotUnprovenError:
        return
    except TypeError as exc:
        # A compatible repair may require an explicit product-owned provenance
        # resolver/issuer argument. Do not let an unrelated TypeError make the
        # falsifier green.
        message = str(exc).lower()
        assert any(
            token in message
            for token in (
                "provenance",
                "issuer",
                "source",
                "member",
                "resolver",
                "authority",
            )
        ), message
        return

    restarted = DatasetSnapshotLineageAuthority(
        lineage_path,
        registry,
        authority_root=authority_root,
    )
    minted = restarted.record("caller-minted-snapshot")
    assert minted is not None
    assert minted.member_sha256 == members
    assert minted.manifest_sha256 == manifest_sha256

    pytest.fail(
        "caller-computable member hashes plus a caller-appended DatasetSnapshot "
        "minted durable, restart-resolvable dataset lineage authority without "
        "independently product-issued member/source provenance"
    )
