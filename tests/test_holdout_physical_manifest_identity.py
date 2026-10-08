from __future__ import annotations

import pytest

from autosport import (
    _dataset_snapshot_lineage_publication_trust_root as lineage_trust_root,
)
from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    membership_manifest_sha256,
)
from autosport.point_in_time_evidence import (
    HoldoutAlreadyConsumedError,
    HoldoutConsumptionLedger,
)
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


_MEMBER_SHA = "a" * 64
_MANIFEST_SHA = membership_manifest_sha256((_MEMBER_SHA,))


def _authority_root(tmp_path):
    return tmp_path.parent / f"{tmp_path.name}-machine-authority"


@pytest.fixture(autouse=True)
def _use_isolated_product_authority_root(tmp_path, monkeypatch):
    product_root = _authority_root(tmp_path).resolve(strict=False)
    monkeypatch.setattr(
        lineage_trust_root,
        "_machine_account_authority_root",
        lambda: product_root,
    )


def _snapshot(
    snapshot_id: str,
    *,
    source_identity: str,
    license_identity: str,
) -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset_snapshot_id=snapshot_id,
        manifest_sha256=_MANIFEST_SHA,
        source_identity=source_identity,
        license_identity=license_identity,
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:01:00Z",
    )


def _lineage(
    tmp_path,
    original: DatasetSnapshot,
    alias: DatasetSnapshot,
) -> DatasetSnapshotLineageAuthority:
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    registry.append(original)
    registry.append(alias)
    lineage = DatasetSnapshotLineageAuthority.initialize_pristine(
        tmp_path / "dataset-snapshot-lineage.json",
        registry,
        authority_root=_authority_root(tmp_path),
    )
    lineage.register(
        snapshot_id=original.dataset_snapshot_id,
        member_sha256=(_MEMBER_SHA,),
    )
    lineage.register(
        snapshot_id=alias.dataset_snapshot_id,
        member_sha256=(_MEMBER_SHA,),
        parent_snapshot_id=original.dataset_snapshot_id,
    )
    return lineage


@pytest.mark.parametrize(
    ("alias_source", "alias_license"),
    [
        ("provider:renamed-feed", "terms:v1"),
        ("provider:canonical-feed", "terms:renamed"),
        ("provider:renamed-feed", "terms:renamed"),
    ],
)
def test_same_manifest_provenance_alias_cannot_launder_consumed_holdout_after_restart(
    tmp_path,
    alias_source: str,
    alias_license: str,
) -> None:
    original = _snapshot(
        "confirmation-original",
        source_identity="provider:canonical-feed",
        license_identity="terms:v1",
    )
    alias = _snapshot(
        "confirmation-alias",
        source_identity=alias_source,
        license_identity=alias_license,
    )
    lineage = _lineage(tmp_path, original, alias)
    path = tmp_path / "holdout-consumption.json"

    first = HoldoutConsumptionLedger(path, lineage_authority=lineage).consume(
        dataset_snapshot=original,
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:challenger-a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )

    restarted = HoldoutConsumptionLedger(path, lineage_authority=lineage)
    assert restarted.freshness_id(
        dataset_snapshot=alias,
        confirmation_trial_family_id="family-renamed",
    ) != first.holdout_freshness_id

    with pytest.raises(HoldoutAlreadyConsumedError, match="already consumed"):
        restarted.assert_unused(
            dataset_snapshot=alias,
            research_protocol_id="protocol-renamed",
            confirmation_trial_family_id="family-renamed",
        )
    with pytest.raises(HoldoutAlreadyConsumedError, match="physical holdout"):
        restarted.consume(
            dataset_snapshot=alias,
            research_protocol_id="protocol-renamed",
            confirmation_trial_family_id="family-renamed",
            consumer_identity="experiment:challenger-b",
            purpose="renamed-provenance-confirmation",
            consumed_at_utc="2026-09-20T10:06:00Z",
        )

    assert restarted.records() == (first,)
