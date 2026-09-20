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


_MEMBER_SHA = "c" * 64
_MANIFEST_SHA = membership_manifest_sha256((_MEMBER_SHA,))


def _snapshot(snapshot_id: str) -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset_snapshot_id=snapshot_id,
        manifest_sha256=_MANIFEST_SHA,
        source_identity="provider:canonical-feed",
        license_identity="terms:v1",
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:01:00Z",
    )


@pytest.fixture(autouse=True)
def _use_isolated_product_authority_root(tmp_path, monkeypatch):
    product_root = (
        tmp_path.parent / f"{tmp_path.name}-lineage-authority"
    ).resolve(strict=False)
    monkeypatch.setattr(
        lineage_trust_root,
        "_machine_account_authority_root",
        lambda: product_root,
    )


def _canonical_lineage(tmp_path):
    first = _snapshot("snapshot-a")
    alias = _snapshot("renamed-alias")
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
    registry.append(first)
    registry.append(alias)
    lineage = DatasetSnapshotLineageAuthority.initialize_pristine(
        tmp_path / "dataset-snapshot-lineage.json",
        registry,
        authority_root=tmp_path.parent / f"{tmp_path.name}-lineage-authority",
    )
    lineage.register(snapshot_id=first.dataset_snapshot_id, member_sha256=(_MEMBER_SHA,))
    lineage.register(
        snapshot_id=alias.dataset_snapshot_id,
        member_sha256=(_MEMBER_SHA,),
        parent_snapshot_id=first.dataset_snapshot_id,
    )
    return lineage, first, alias


def test_stale_ledger_instance_reloads_before_consuming(tmp_path) -> None:
    path = tmp_path / "holdout.json"
    lineage, first, alias = _canonical_lineage(tmp_path)
    first_process_view = HoldoutConsumptionLedger(path, lineage_authority=lineage)
    stale_process_view = HoldoutConsumptionLedger(path, lineage_authority=lineage)

    first_process_view.consume(
        dataset_snapshot=first,
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )

    with pytest.raises(HoldoutAlreadyConsumedError, match="already consumed"):
        stale_process_view.consume(
            dataset_snapshot=alias,
            research_protocol_id="protocol-42",
            confirmation_trial_family_id="family-9",
            consumer_identity="experiment:b",
            purpose="fresh-confirmation",
            consumed_at_utc="2026-09-20T10:06:00Z",
        )


def test_stale_ledger_instance_observes_consumption_on_read(tmp_path) -> None:
    path = tmp_path / "holdout.json"
    lineage, first, _ = _canonical_lineage(tmp_path)
    first_process_view = HoldoutConsumptionLedger(path, lineage_authority=lineage)
    stale_process_view = HoldoutConsumptionLedger(path, lineage_authority=lineage)

    first_process_view.consume(
        dataset_snapshot=first,
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )

    records = stale_process_view.records()
    assert len(records) == 1
    assert records[0].consumer_identity == "experiment:a"
