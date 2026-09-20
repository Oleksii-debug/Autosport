from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    CollectorPointInTimeSourceRevisionAuthorityStore,
)
from autosport.point_in_time_authority import (
    RevisionPolicyAuthority,
    SourceRevisionAuthorityError,
    SourceRevisionAuthorityStore,
)


BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _generic_policy(
    *,
    source_identity: str = "collector-source-x",
    revision_policy_id: str = "caller-generic-policy:v1",
) -> RevisionPolicyAuthority:
    payload = {
        "schema": "autosport.revision_availability_policy",
        "schema_version": 1,
        "source_identity": source_identity,
        "policy_version": "1",
        "witness_kind": "caller-generic-v1",
        "availability_semantics": "source_as_of<=available_at",
    }
    return RevisionPolicyAuthority.create(
        revision_policy_id=revision_policy_id,
        policy_content_json=json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        frozen_at=BASE,
    )


def _collector_store(tmp_path: Path) -> CollectorPointInTimeSourceRevisionAuthorityStore:
    collector = CollectorDeltaStore(tmp_path / "collector.json")
    checkpoint = DesktopDeltaCheckpointStore(tmp_path / "desktop-checkpoint.json")
    return CollectorPointInTimeSourceRevisionAuthorityStore.initialize_pristine(
        tmp_path / "pit",
        collector_store=collector,
        checkpoint_store=checkpoint,
    )


def _reserve_collector_source(
    store: CollectorPointInTimeSourceRevisionAuthorityStore,
) -> None:
    store.register_collector_policy(
        source_identity="collector-source-x",
        revision_policy_id="collector-desktop-application-policy:v1",
        frozen_at=BASE,
    )


def _delta() -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id="delta-1",
        source_id="collector-source-x",
        lawful_terms_ref="terms:collector-source-x:v1",
        retention_ref="retention:collector-source-x:v1",
        stream_epoch="epoch-1",
        source_cursor="1",
        cursor_position=1,
        event_dedupe_key="collector-source-x:event-1:winner:player-a",
        event_id="event-1",
        source_payload_digest=hashlib.sha256(b"raw-source-event").hexdigest(),
        canonical_event_digest=hashlib.sha256(b"canonical-event").hexdigest(),
        source_observed_at=_iso(BASE + timedelta(seconds=1)),
        collector_received_at=_iso(BASE + timedelta(seconds=2)),
        collector_committed_at=_iso(BASE + timedelta(seconds=3)),
        desktop_available_at=_iso(BASE + timedelta(seconds=4)),
        revision_of=None,
        revision_number=0,
        quality_flags=(),
        gap_state=GapState.NONE,
        sync_state=SyncState.READY,
    )


def test_parent_store_cannot_write_reserved_collector_source_but_unrelated_stays_generic(
    tmp_path: Path,
) -> None:
    collector = _collector_store(tmp_path)
    _reserve_collector_source(collector)
    parent = SourceRevisionAuthorityStore(tmp_path / "pit")

    with pytest.raises(
        SourceRevisionAuthorityError,
        match="reserved collector source",
    ):
        parent.register_policy(_generic_policy())

    unrelated = _generic_policy(
        source_identity="unrelated-source",
        revision_policy_id="unrelated-policy:v1",
    )
    assert parent.register_policy(unrelated) == unrelated.authority_sha256


def test_preexisting_generic_authority_blocks_collector_reservation(
    tmp_path: Path,
) -> None:
    parent = SourceRevisionAuthorityStore.initialize_pristine(tmp_path / "pit")
    generic = _generic_policy()
    parent.register_policy(generic)

    collector = CollectorPointInTimeSourceRevisionAuthorityStore(
        tmp_path / "pit",
        collector_store=CollectorDeltaStore(tmp_path / "collector.json"),
        checkpoint_store=DesktopDeltaCheckpointStore(
            tmp_path / "desktop-checkpoint.json"
        ),
    )
    with pytest.raises(
        SourceRevisionAuthorityError,
        match="already has non-canonical generic availability authority",
    ):
        _reserve_collector_source(collector)


def test_parent_resolver_cannot_consume_canonical_collector_revision(
    tmp_path: Path,
) -> None:
    collector_store = CollectorDeltaStore(tmp_path / "collector.json")
    checkpoint_store = DesktopDeltaCheckpointStore(tmp_path / "desktop-checkpoint.json")
    authority = CollectorPointInTimeSourceRevisionAuthorityStore.initialize_pristine(
        tmp_path / "pit",
        collector_store=collector_store,
        checkpoint_store=checkpoint_store,
    )
    _reserve_collector_source(authority)

    delta = _delta()
    collector_store.append(delta)
    checkpoint_store.ack(
        delta,
        application_receipt=DesktopApplicationReceipt(
            delta_id=delta.delta_id,
            canonical_event_digest=delta.canonical_event_digest,
            receipt_id="canonical-desktop:delta-1",
            applied_at=_iso(BASE + timedelta(seconds=5)),
        ),
        acknowledged_at=_iso(BASE + timedelta(seconds=6)),
    )
    revision = authority.register_collector_revision(
        delta_id=delta.delta_id,
        revision_policy_id="collector-desktop-application-policy:v1",
        recorded_at=BASE + timedelta(seconds=7),
    )

    parent = SourceRevisionAuthorityStore(tmp_path / "pit")
    with pytest.raises(
        SourceRevisionAuthorityError,
        match="canonical collector resolver",
    ):
        parent.resolve_revision(revision.source_revision_authority_id)

    assert authority.resolve_revision(
        revision.source_revision_authority_id,
        expected_sha256=revision.authority_sha256,
    ) == revision


def test_collector_reservation_and_generic_write_are_serialized_without_toctou(
    tmp_path: Path,
) -> None:
    collector = _collector_store(tmp_path)
    parent = SourceRevisionAuthorityStore(tmp_path / "pit")
    generic = _generic_policy()
    barrier = threading.Barrier(3)
    outcomes: list[tuple[str, str]] = []

    def register_generic() -> None:
        barrier.wait()
        try:
            parent.register_policy(generic)
        except SourceRevisionAuthorityError:
            outcomes.append(("generic", "rejected"))
        else:
            outcomes.append(("generic", "accepted"))

    def reserve_collector() -> None:
        barrier.wait()
        try:
            _reserve_collector_source(collector)
        except SourceRevisionAuthorityError:
            outcomes.append(("collector", "rejected"))
        else:
            outcomes.append(("collector", "accepted"))

    first = threading.Thread(target=register_generic)
    second = threading.Thread(target=reserve_collector)
    first.start()
    second.start()
    barrier.wait()
    first.join()
    second.join()

    assert len(outcomes) == 2
    assert sum(result == "accepted" for _, result in outcomes) == 1
    assert sum(result == "rejected" for _, result in outcomes) == 1
