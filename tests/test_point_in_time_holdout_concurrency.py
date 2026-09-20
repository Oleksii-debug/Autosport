from __future__ import annotations

import pytest

from autosport.point_in_time_evidence import (
    HoldoutAlreadyConsumedError,
    HoldoutConsumptionLedger,
)
from autosport.scientific_registry import DatasetSnapshot


_SHA = "c" * 64


def _snapshot(snapshot_id: str) -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset_snapshot_id=snapshot_id,
        manifest_sha256=_SHA,
        source_identity="provider:canonical-feed",
        license_identity="terms:v1",
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:01:00Z",
    )


def test_stale_ledger_instance_reloads_before_consuming(tmp_path) -> None:
    path = tmp_path / "holdout.json"
    first_process_view = HoldoutConsumptionLedger(path)
    stale_process_view = HoldoutConsumptionLedger(path)

    first_process_view.consume(
        dataset_snapshot=_snapshot("snapshot-a"),
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )

    with pytest.raises(HoldoutAlreadyConsumedError, match="already consumed"):
        stale_process_view.consume(
            dataset_snapshot=_snapshot("renamed-alias"),
            research_protocol_id="protocol-42",
            confirmation_trial_family_id="family-9",
            consumer_identity="experiment:b",
            purpose="fresh-confirmation",
            consumed_at_utc="2026-09-20T10:06:00Z",
        )


def test_stale_ledger_instance_observes_consumption_on_read(tmp_path) -> None:
    path = tmp_path / "holdout.json"
    first_process_view = HoldoutConsumptionLedger(path)
    stale_process_view = HoldoutConsumptionLedger(path)

    first_process_view.consume(
        dataset_snapshot=_snapshot("snapshot-a"),
        research_protocol_id="protocol-42",
        confirmation_trial_family_id="family-9",
        consumer_identity="experiment:a",
        purpose="final-confirmation",
        consumed_at_utc="2026-09-20T10:05:00Z",
    )

    records = stale_process_view.records()
    assert len(records) == 1
    assert records[0].consumer_identity == "experiment:a"
