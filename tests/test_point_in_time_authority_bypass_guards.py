from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import autosport._historical_capture_authority_guard as historical_guard
from autosport.causal_collector import CollectorDeltaStore, DesktopDeltaCheckpointStore
from autosport.collector_point_in_time import (
    CollectorPointInTimeSourceRevisionAuthorityStore,
)
from autosport.historical_snapshot import (
    assert_historical_snapshot_capture_authoritative,
    capture_historical_snapshot,
)
from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
)
from autosport.point_in_time_authority import (
    AvailabilityWitnessAuthority,
    RevisionPolicyAuthority,
    SourceRevisionAuthority,
    SourceRevisionAuthorityError,
    SourceRevisionAuthorityStore,
)


BASE = datetime(2026, 1, 1, tzinfo=UTC)
SOURCE_ID = "collector-source-x"
GENERIC_KIND = "caller-generic-availability-v1"


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _injected_provider() -> ParlayApiTableTennisProvider:
    payload = {
        "timestamp": "2026-01-04T10:00:00Z",
        "previous_timestamp": None,
        "next_timestamp": None,
        "data": [],
    }

    def transport(url: str, headers: object, timeout: float) -> HttpJsonResponse:
        del url, headers, timeout
        return HttpJsonResponse(payload=payload, status_code=200, headers={})

    return ParlayApiTableTennisProvider(
        "test-key",
        transport=transport,
        clock=lambda: "2026-01-04T10:06:00Z",
        sleeper=lambda _: None,
    )


def _generic_policy(policy_id: str = "generic-policy:v1") -> RevisionPolicyAuthority:
    payload = {
        "schema": "autosport.revision_availability_policy",
        "schema_version": 1,
        "source_identity": SOURCE_ID,
        "policy_version": "1",
        "witness_kind": GENERIC_KIND,
        "availability_semantics": "source_as_of<=available_at",
    }
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return RevisionPolicyAuthority.create(
        revision_policy_id=policy_id,
        policy_content_json=text,
        frozen_at=BASE,
    )


def _generic_witness(
    witness_id: str = "generic-witness:v1",
) -> AvailabilityWitnessAuthority:
    revision_sha = hashlib.sha256(b"caller-revision").hexdigest()
    payload = {
        "schema": "autosport.source_availability_witness",
        "schema_version": 1,
        "source_identity": SOURCE_ID,
        "source_revision": "caller-revision:1",
        "source_revision_sha256": revision_sha,
        "witness_kind": GENERIC_KIND,
        "source_as_of": _iso(BASE + timedelta(seconds=1)),
        "available_at": _iso(BASE + timedelta(seconds=2)),
    }
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return AvailabilityWitnessAuthority.create(
        availability_witness_id=witness_id,
        witness_content_json=text,
        recorded_at=BASE + timedelta(seconds=3),
    )


def _generic_revision(
    policy: RevisionPolicyAuthority,
    witness: AvailabilityWitnessAuthority,
) -> SourceRevisionAuthority:
    return SourceRevisionAuthority(
        source_revision_authority_id="generic-source-revision:v1",
        source_identity=SOURCE_ID,
        source_revision=witness.source_revision,
        source_revision_sha256=witness.source_revision_sha256,
        revision_policy_id=policy.revision_policy_id,
        revision_policy_record_sha256=policy.authority_sha256,
        availability_witness_id=witness.availability_witness_id,
        availability_witness_sha256=witness.witness_content_sha256,
        availability_witness_record_sha256=witness.authority_sha256,
        witness_kind=GENERIC_KIND,
        source_as_of=witness.source_as_of,
        available_at=witness.available_at,
        recorded_at=BASE + timedelta(seconds=4),
    )


def _collector_store(tmp_path: Path) -> CollectorPointInTimeSourceRevisionAuthorityStore:
    return CollectorPointInTimeSourceRevisionAuthorityStore.initialize_pristine(
        tmp_path / "pit",
        collector_store=CollectorDeltaStore(tmp_path / "collector.json"),
        checkpoint_store=DesktopDeltaCheckpointStore(
            tmp_path / "desktop-checkpoint.json"
        ),
    )


def _reserve_collector_source(
    store: CollectorPointInTimeSourceRevisionAuthorityStore,
) -> None:
    store.register_collector_policy(
        source_identity=SOURCE_ID,
        revision_policy_id="collector-desktop-application-policy:v1",
        frozen_at=BASE,
    )


def test_historical_artifact_cannot_be_promoted_through_importable_guard_state(
    tmp_path: Path,
) -> None:
    capture = capture_historical_snapshot(
        _injected_provider(),
        requested_at="2026-01-04T10:00:00Z",
        output_path=tmp_path / "market.jsonl",
        evidence_path=tmp_path / "evidence.json",
    )

    # Authority mutation is lexical only; there is no package-importable issuer
    # hook or mutable registry that a consumer can call/populate.
    assert "_remember" not in vars(historical_guard)
    assert "_issued" not in vars(historical_guard)
    assert "_build_authority_boundary" not in vars(historical_guard)

    with pytest.raises(ProviderPayloadError, match="canonical production capture path"):
        assert_historical_snapshot_capture_authoritative(capture)


def test_reserved_collector_source_rejects_alternate_generic_authority_after_restart(
    tmp_path: Path,
) -> None:
    store = _collector_store(tmp_path)
    _reserve_collector_source(store)
    policy = _generic_policy()
    witness = _generic_witness()
    revision = _generic_revision(policy, witness)

    with pytest.raises(SourceRevisionAuthorityError, match="reserved collector source"):
        store.register_policy(policy)
    with pytest.raises(SourceRevisionAuthorityError, match="reserved collector source"):
        store.register_witness(witness)
    with pytest.raises(SourceRevisionAuthorityError, match="reserved collector source"):
        store.register_revision(revision)

    reopened = CollectorPointInTimeSourceRevisionAuthorityStore(
        tmp_path / "pit",
        collector_store=CollectorDeltaStore(tmp_path / "collector.json"),
        checkpoint_store=DesktopDeltaCheckpointStore(
            tmp_path / "desktop-checkpoint.json"
        ),
    )
    with pytest.raises(SourceRevisionAuthorityError, match="reserved collector source"):
        reopened.register_policy(policy)


def test_preexisting_generic_positive_authority_blocks_collector_source_reservation(
    tmp_path: Path,
) -> None:
    store = _collector_store(tmp_path)
    policy = _generic_policy()
    witness = _generic_witness()
    revision = _generic_revision(policy, witness)
    store.register_policy(policy)
    store.register_witness(witness)
    store.register_revision(revision)

    with pytest.raises(
        SourceRevisionAuthorityError,
        match="already has non-canonical generic availability authority",
    ):
        _reserve_collector_source(store)

    reopened = CollectorPointInTimeSourceRevisionAuthorityStore(
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
        _reserve_collector_source(reopened)


def test_restart_fails_closed_if_parent_store_injects_generic_path_for_reserved_source(
    tmp_path: Path,
) -> None:
    store = _collector_store(tmp_path)
    _reserve_collector_source(store)

    # Simulate a caller bypassing the collector subclass and mutating the shared
    # generic authority store directly after the source family was reserved.
    parent = SourceRevisionAuthorityStore(tmp_path / "pit")
    policy = _generic_policy()
    witness = _generic_witness()
    revision = _generic_revision(policy, witness)
    parent.register_policy(policy)
    parent.register_witness(witness)
    parent.register_revision(revision)

    with pytest.raises(
        SourceRevisionAuthorityError,
        match="reserved collector source has alternate generic",
    ):
        CollectorPointInTimeSourceRevisionAuthorityStore(
            tmp_path / "pit",
            collector_store=CollectorDeltaStore(tmp_path / "collector.json"),
            checkpoint_store=DesktopDeltaCheckpointStore(
                tmp_path / "desktop-checkpoint.json"
            ),
        )
