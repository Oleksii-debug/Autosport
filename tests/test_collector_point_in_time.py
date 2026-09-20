from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    GapState,
    SyncState,
)
from autosport.collector_point_in_time import (
    COLLECTOR_APPLICATION_WITNESS_KIND,
    CollectorPointInTimeSourceRevisionAuthorityStore,
)
from autosport.point_in_time_authority import (
    AvailabilityWitnessAuthority,
    RevisionPolicyAuthority,
    SourceRevisionAuthorityError,
)


BASE = datetime(2026, 1, 1, tzinfo=UTC)
EVENT_SHA = hashlib.sha256(b"canonical-event").hexdigest()
SOURCE_SHA = hashlib.sha256(b"raw-source-event").hexdigest()


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _delta(
    *,
    delta_id: str = "delta-1",
    revision_of: str | None = None,
    revision_number: int = 0,
    canonical_event_digest: str = EVENT_SHA,
    source_payload_digest: str = SOURCE_SHA,
    desktop_available_at: datetime = BASE + timedelta(seconds=4),
) -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="collector-source-x",
        lawful_terms_ref="terms:collector-source-x:v1",
        retention_ref="retention:collector-source-x:v1",
        stream_epoch="epoch-1",
        source_cursor="1",
        cursor_position=1,
        event_dedupe_key="collector-source-x:event-1:winner:player-a",
        event_id="event-1",
        source_payload_digest=source_payload_digest,
        canonical_event_digest=canonical_event_digest,
        source_observed_at=_iso(BASE + timedelta(seconds=1)),
        collector_received_at=_iso(BASE + timedelta(seconds=2)),
        collector_committed_at=_iso(BASE + timedelta(seconds=3)),
        desktop_available_at=_iso(desktop_available_at),
        revision_of=revision_of,
        revision_number=revision_number,
        quality_flags=(),
        gap_state=GapState.NONE,
        sync_state=SyncState.READY,
    )


def _receipt(
    delta: CollectorDelta,
    *,
    applied_at: datetime = BASE + timedelta(seconds=5),
) -> DesktopApplicationReceipt:
    return DesktopApplicationReceipt(
        delta_id=delta.delta_id,
        canonical_event_digest=delta.canonical_event_digest,
        receipt_id=f"canonical-desktop:{delta.delta_id}",
        applied_at=_iso(applied_at),
    )


def _stores(tmp_path):
    collector = CollectorDeltaStore(tmp_path / "collector.json")
    checkpoint = DesktopDeltaCheckpointStore(tmp_path / "desktop-checkpoint.json")
    authority = CollectorPointInTimeSourceRevisionAuthorityStore.initialize_pristine(
        tmp_path / "pit",
        collector_store=collector,
        checkpoint_store=checkpoint,
    )
    authority.register_collector_policy(
        source_identity="collector-source-x",
        revision_policy_id="collector-desktop-application-policy:v1",
        frozen_at=BASE,
    )
    return collector, checkpoint, authority


def test_collector_revision_requires_completed_durable_desktop_application(tmp_path) -> None:
    collector, _, authority = _stores(tmp_path)
    delta = _delta()
    collector.append(delta)

    with pytest.raises(
        SourceRevisionAuthorityError,
        match="no completed durable desktop application receipt",
    ):
        authority.register_collector_revision(
            delta_id=delta.delta_id,
            revision_policy_id="collector-desktop-application-policy:v1",
            recorded_at=BASE + timedelta(seconds=8),
        )


def test_collector_revision_round_trip_binds_exact_delta_receipt_and_times(tmp_path) -> None:
    collector, checkpoint, authority = _stores(tmp_path)
    delta = _delta()
    collector.append(delta)
    receipt = _receipt(delta)
    checkpoint.ack(
        delta,
        application_receipt=receipt,
        acknowledged_at=_iso(BASE + timedelta(seconds=6)),
    )

    revision = authority.register_collector_revision(
        delta_id=delta.delta_id,
        revision_policy_id="collector-desktop-application-policy:v1",
        recorded_at=BASE + timedelta(seconds=7),
    )

    assert revision.source_revision == f"collector-delta:{delta.delta_id}"
    assert revision.source_as_of == BASE + timedelta(seconds=1)
    assert revision.available_at == BASE + timedelta(seconds=5)
    assert revision.witness_kind == COLLECTOR_APPLICATION_WITNESS_KIND
    assert revision.source_revision_sha256 != delta.canonical_event_digest

    reopened = CollectorPointInTimeSourceRevisionAuthorityStore(
        tmp_path / "pit",
        collector_store=CollectorDeltaStore(tmp_path / "collector.json"),
        checkpoint_store=DesktopDeltaCheckpointStore(
            tmp_path / "desktop-checkpoint.json"
        ),
    )
    resolved = reopened.resolve_revision(
        revision.source_revision_authority_id,
        expected_sha256=revision.authority_sha256,
    )
    assert resolved == revision


def test_checkpoint_tamper_is_detected_on_restart_resolution(tmp_path) -> None:
    collector, checkpoint, authority = _stores(tmp_path)
    delta = _delta()
    collector.append(delta)
    checkpoint.ack(
        delta,
        application_receipt=_receipt(delta),
        acknowledged_at=_iso(BASE + timedelta(seconds=9)),
    )
    revision = authority.register_collector_revision(
        delta_id=delta.delta_id,
        revision_policy_id="collector-desktop-application-policy:v1",
        recorded_at=BASE + timedelta(seconds=8),
    )

    path = tmp_path / "desktop-checkpoint.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["acks"][0]["applied_at"] = _iso(BASE + timedelta(seconds=8))
    path.write_text(
        json.dumps(raw, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        SourceRevisionAuthorityError,
        match="does not match durable application evidence",
    ):
        CollectorPointInTimeSourceRevisionAuthorityStore(
            tmp_path / "pit",
            collector_store=CollectorDeltaStore(tmp_path / "collector.json"),
            checkpoint_store=DesktopDeltaCheckpointStore(path),
        )

    assert revision.source_revision_authority_id


def test_later_correction_gets_distinct_exact_point_in_time_authority(tmp_path) -> None:
    collector, checkpoint, authority = _stores(tmp_path)
    first = _delta()
    collector.append(first)
    checkpoint.ack(
        first,
        application_receipt=_receipt(first),
        acknowledged_at=_iso(BASE + timedelta(seconds=6)),
    )
    first_revision = authority.register_collector_revision(
        delta_id=first.delta_id,
        revision_policy_id="collector-desktop-application-policy:v1",
        recorded_at=BASE + timedelta(seconds=7),
    )

    corrected_event_sha = hashlib.sha256(b"corrected-canonical-event").hexdigest()
    corrected_source_sha = hashlib.sha256(b"corrected-source-event").hexdigest()
    correction = _delta(
        delta_id="delta-1-correction-1",
        revision_of=first.delta_id,
        revision_number=1,
        canonical_event_digest=corrected_event_sha,
        source_payload_digest=corrected_source_sha,
        desktop_available_at=BASE + timedelta(seconds=10),
    )
    collector.append(correction)
    checkpoint.ack(
        correction,
        application_receipt=_receipt(
            correction,
            applied_at=BASE + timedelta(seconds=11),
        ),
        acknowledged_at=_iso(BASE + timedelta(seconds=12)),
    )
    corrected_revision = authority.register_collector_revision(
        delta_id=correction.delta_id,
        revision_policy_id="collector-desktop-application-policy:v1",
        recorded_at=BASE + timedelta(seconds=13),
    )

    assert corrected_revision.source_revision_authority_id != (
        first_revision.source_revision_authority_id
    )
    assert corrected_revision.source_revision_sha256 != (
        first_revision.source_revision_sha256
    )
    assert corrected_revision.available_at == BASE + timedelta(seconds=11)
    assert authority.resolve_revision(
        first_revision.source_revision_authority_id
    ) == first_revision


def test_generic_caller_cannot_mint_collector_policy_or_witness(tmp_path) -> None:
    _, _, authority = _stores(tmp_path)
    policy_payload = {
        "schema": "autosport.revision_availability_policy",
        "schema_version": 1,
        "source_identity": "collector-source-x",
        "policy_version": "1",
        "witness_kind": COLLECTOR_APPLICATION_WITNESS_KIND,
        "availability_semantics": "source_as_of<=available_at",
    }
    policy_json = json.dumps(
        policy_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    forged_policy = RevisionPolicyAuthority.create(
        revision_policy_id="forged-policy",
        policy_content_json=policy_json,
        frozen_at=BASE,
    )
    with pytest.raises(
        SourceRevisionAuthorityError,
        match="must be registered from canonical collector authority",
    ):
        authority.register_policy(forged_policy)

    witness_payload = {
        "schema": "autosport.source_availability_witness",
        "schema_version": 1,
        "source_identity": "collector-source-x",
        "source_revision": "collector-delta:forged",
        "source_revision_sha256": hashlib.sha256(b"forged").hexdigest(),
        "witness_kind": COLLECTOR_APPLICATION_WITNESS_KIND,
        "source_as_of": _iso(BASE + timedelta(seconds=1)),
        "available_at": _iso(BASE + timedelta(seconds=2)),
    }
    witness_json = json.dumps(
        witness_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    forged_witness = AvailabilityWitnessAuthority.create(
        availability_witness_id="collector-application:forged",
        witness_content_json=witness_json,
        recorded_at=BASE + timedelta(seconds=3),
    )
    with pytest.raises(
        SourceRevisionAuthorityError,
        match="requires canonical collector/checkpoint evidence",
    ):
        authority.register_witness(forged_witness)
