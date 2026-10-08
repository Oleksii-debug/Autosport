from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import autosport.provider_owner_approval as approval_module
from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
from autosport.provider_owner_approval import (
    OwnerApprovalResolution,
    OwnerApprovalResolutionReason,
    OwnerApprovalStore,
)

AUTHORITY_ID = "a" * 64
APPROVAL_SHA = "b" * 64
REFERENCE = "urn:autosport:owner-approval:provider-governance-1"
PROVIDER = "betfair"
SERVICE = "exchange-api"
APPROVED_AT = "2026-09-21T12:00:00Z"
REVOKED_AT = "2026-09-21T13:00:00Z"


@pytest.fixture(autouse=True)
def product_clock(monkeypatch: pytest.MonkeyPatch):
    value = [datetime.fromisoformat(APPROVED_AT.replace("Z", "+00:00"))]
    monkeypatch.setattr(approval_module, "_utc_now", lambda: value[0])

    def set_clock(text: str) -> None:
        value[0] = datetime.fromisoformat(text.replace("Z", "+00:00"))

    return set_clock


def _store(tmp_path: Path) -> OwnerApprovalStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return OwnerApprovalStore(
        (workspace / "provider-owner-approvals.json").resolve(),
        authority_root=(tmp_path / "machine-authority").resolve(),
    )


def _approve(store: OwnerApprovalStore):
    return store.approve(
        governance_authority_id=AUTHORITY_ID,
        provider_id=PROVIDER,
        service_id=SERVICE,
        owner_approval_reference=REFERENCE,
        owner_approval_sha256=APPROVAL_SHA,
    )


def _resolve(store: OwnerApprovalStore, as_of: str = APPROVED_AT, **overrides):
    values = {
        "governance_authority_id": AUTHORITY_ID,
        "provider_id": PROVIDER,
        "service_id": SERVICE,
        "owner_approval_reference": REFERENCE,
        "owner_approval_sha256": APPROVAL_SHA,
        "as_of": as_of,
    }
    values.update(overrides)
    return store.resolve(**values)


def test_positive_requires_durable_record_and_survives_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert _resolve(store, "2026-09-21T11:59:59Z").reason is OwnerApprovalResolutionReason.UNRESOLVED
    issued = _approve(store)
    assert issued.approved is True
    restarted = _store(tmp_path)
    resolved = _resolve(restarted, "2026-09-21T12:00:01Z")
    assert resolved.approved is True
    assert resolved.approval_event_sha256 == issued.approval_event_sha256


def test_caller_cannot_backdate_approval_or_revocation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(TypeError):
        store.approve(
            governance_authority_id=AUTHORITY_ID,
            provider_id=PROVIDER,
            service_id=SERVICE,
            owner_approval_reference=REFERENCE,
            owner_approval_sha256=APPROVAL_SHA,
            approved_at="2000-01-01T00:00:00Z",
        )
    _approve(store)
    with pytest.raises(TypeError):
        store.revoke(governance_authority_id=AUTHORITY_ID, revoked_at="2000-01-01T00:00:00Z")


def test_before_approval_and_identity_mismatch_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _approve(store)
    before = _resolve(store, "2026-09-21T11:59:59.999999Z")
    assert before.reason is OwnerApprovalResolutionReason.NOT_YET_APPROVED
    for mismatch in (
        {"provider_id": "other-provider"},
        {"service_id": "other-service"},
        {"owner_approval_reference": "urn:autosport:owner-approval:other"},
        {"owner_approval_sha256": "c" * 64},
    ):
        result = _resolve(store, "2026-09-21T12:00:01Z", **mismatch)
        assert result.reason is OwnerApprovalResolutionReason.IDENTITY_MISMATCH


def test_revocation_boundary_and_restart(tmp_path: Path, product_clock) -> None:
    store = _store(tmp_path)
    approved = _approve(store)
    product_clock(REVOKED_AT)
    revoked = store.revoke(governance_authority_id=AUTHORITY_ID)
    assert revoked.reason is OwnerApprovalResolutionReason.REVOKED
    assert revoked.approval_event_sha256 == approved.approval_event_sha256
    assert _resolve(store, "2026-09-21T12:59:59.999999Z").approved is True
    assert _resolve(store, REVOKED_AT).reason is OwnerApprovalResolutionReason.REVOKED
    assert _resolve(_store(tmp_path), "2026-09-21T13:00:01Z").reason is OwnerApprovalResolutionReason.REVOKED


def test_rebind_reapproval_and_noncausal_revocation_are_rejected(tmp_path: Path, product_clock) -> None:
    store = _store(tmp_path)
    first = _approve(store)
    product_clock("2026-09-21T12:10:00Z")
    retry = _approve(store)
    assert retry.approval_event_sha256 == first.approval_event_sha256
    with pytest.raises(ValueError, match="cannot be rebound"):
        store.approve(
            governance_authority_id=AUTHORITY_ID,
            provider_id="other-provider",
            service_id=SERVICE,
            owner_approval_reference=REFERENCE,
            owner_approval_sha256=APPROVAL_SHA,
        )
    product_clock(APPROVED_AT)
    with pytest.raises(ValueError, match="later than approved_at"):
        store.revoke(governance_authority_id=AUTHORITY_ID)
    product_clock(REVOKED_AT)
    store.revoke(governance_authority_id=AUTHORITY_ID)
    with pytest.raises(ValueError, match="cannot be rebound"):
        _approve(store)


def test_product_clock_rollback_behind_durable_history_fails_closed(tmp_path: Path, product_clock) -> None:
    store = _store(tmp_path)
    _approve(store)
    product_clock("2026-09-21T11:59:59Z")
    with pytest.raises(RuntimeError, match="product clock moved backwards"):
        store.approve(
            governance_authority_id="d" * 64,
            provider_id=PROVIDER,
            service_id=SERVICE,
            owner_approval_reference="urn:autosport:owner-approval:provider-governance-2",
            owner_approval_sha256="e" * 64,
        )
    with pytest.raises(RuntimeError, match="product clock moved backwards"):
        _approve(store)


def test_resolution_object_is_not_caller_constructible() -> None:
    with pytest.raises(TypeError, match="product-issued"):
        OwnerApprovalResolution()


def test_stale_image_and_deletion_fail_rollback_authority(tmp_path: Path, product_clock) -> None:
    store = _store(tmp_path)
    _approve(store)
    old_bytes = store.path.read_bytes()
    product_clock(REVOKED_AT)
    store.revoke(governance_authority_id=AUTHORITY_ID)
    store.path.write_bytes(old_bytes)
    with pytest.raises(MonotonicAuthorityRollbackError):
        _resolve(_store(tmp_path), "2026-09-21T12:30:00Z")

    product_clock(APPROVED_AT)
    other = _store(tmp_path / "delete")
    _approve(other)
    other.path.unlink()
    with pytest.raises(MonotonicAuthorityRollbackError):
        _resolve(_store(tmp_path / "delete"), "2026-09-21T12:30:00Z")


def test_crash_after_publish_recovers_exact_prepared_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    real_write = approval_module.atomic_write_json

    def publish_then_crash(path, payload):
        real_write(path, payload)
        raise RuntimeError("simulated crash after publish")

    monkeypatch.setattr(approval_module, "atomic_write_json", publish_then_crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _approve(store)
    monkeypatch.setattr(approval_module, "atomic_write_json", real_write)
    assert _resolve(_store(tmp_path), "2026-09-21T12:00:01Z").approved is True


def test_crash_before_publish_aborts_prepare_and_allows_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    real_write = approval_module.atomic_write_json

    def crash_before_publish(path, payload):
        raise RuntimeError("simulated crash before publish")

    monkeypatch.setattr(approval_module, "atomic_write_json", crash_before_publish)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _approve(store)
    monkeypatch.setattr(approval_module, "atomic_write_json", real_write)
    restarted = _store(tmp_path)
    assert _resolve(restarted, "2026-09-21T12:00:01Z").reason is OwnerApprovalResolutionReason.UNRESOLVED
    assert _approve(restarted).approved is True


def test_store_rejects_noncanonical_and_duplicate_json(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _approve(store)
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    store.path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="not canonical"):
        _resolve(store, "2026-09-21T12:00:01Z")

    other = _store(tmp_path / "duplicate")
    other.path.write_text(
        '{"schema_version":1,"schema_version":1,"generation":1,"events":[],"state_sha256":"'
        + ("0" * 64) + '"}\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="strict JSON"):
        _resolve(other, "2026-09-21T12:00:01Z")


def test_reference_validation_rejects_unsafe_forms(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for reference in (
        "approval.json",
        "file:///approval.json",
        "https://user:pass@example.invalid/approval",
        "https://example.invalid/approval?token=secret",
        "urn:autosport:approval#fragment",
    ):
        with pytest.raises(ValueError):
            store.approve(
                governance_authority_id=AUTHORITY_ID,
                provider_id=PROVIDER,
                service_id=SERVICE,
                owner_approval_reference=reference,
                owner_approval_sha256=APPROVAL_SHA,
            )
