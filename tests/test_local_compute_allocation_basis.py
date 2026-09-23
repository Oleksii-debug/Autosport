from __future__ import annotations

import base64
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.local_compute_allocation_basis as subject
from autosport.economic_goal_store import EconomicGoalStore
from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthorityError
from autosport.owner_economic_authority import (
    INITIAL_OWNER_FORM_DEFAULTS,
    build_initial_owner_contract,
)


def _goal(workspace: Path, *, currency: str = "USD") -> EconomicGoalStore:
    values = dict(INITIAL_OWNER_FORM_DEFAULTS)
    values["currency"] = currency
    store = EconomicGoalStore(workspace)
    store.initialize_owner(
        build_initial_owner_contract(values, emergency_stop=False)
    )
    return store


def _review(**overrides) -> subject.LocalComputeAllocationBasisReview:
    values = {
        "basis_id": "basis-local-a-2026-09",
        "backend_id": "local-backend",
        "model_id": "model-a",
        "config_sha256": "c" * 64,
        "allocation_policy_id": "full-cost-per-request-v1",
        "measurement_source_id": "owner-reviewed-local-cost-ledger-2026-09",
        "measurement_period_start": "2026-09-01T00:00:00Z",
        "measurement_period_end": "2026-09-20T00:00:00Z",
        "total_allocable_cost": Decimal("12.50"),
        "request_denominator": 100,
        "measurement_document": (
            b'{"source":"local-cost-ledger","period":"2026-09","total":"12.50","requests":100}'
        ),
    }
    values.update(overrides)
    return subject.prepare_owner_review(**values)


def _store(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "authority"
    goal_store = _goal(workspace)
    monkeypatch.setattr(
        subject,
        "_authority_now",
        lambda: "2026-09-23T10:00:00Z",
    )
    store = subject.LocalComputeAllocationBasisAuthorityStore(
        workspace,
        authority_root=authority_root,
    )
    return workspace, authority_root, goal_store, store


def _resolve(
    store: subject.LocalComputeAllocationBasisAuthorityStore,
    *,
    decision_at: str = "2026-09-23T12:00:00Z",
):
    return store.resolve(
        basis_id="basis-local-a-2026-09",
        backend_id="local-backend",
        model_id="model-a",
        config_sha256="c" * 64,
        decision_at=decision_at,
        bankroll_id="paper-bankroll",
        currency="USD",
    )


def test_owner_review_persists_exact_document_and_derives_amount(tmp_path, monkeypatch):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    review = _review()

    record = store.publish_owner_basis(review, confirmed=True)

    assert record.amount_per_request == Decimal("0.125")
    assert record.currency == "USD"
    assert record.available_at == "2026-09-23T10:00:00Z"
    assert record.owner_review_sha256 == review.review_sha256
    assert (
        base64.b64decode(record.measurement_document_b64)
        == base64.b64decode(review.measurement_document_b64)
    )
    assert len(record.measurement_document_sha256) == 64
    assert len(record.basis_sha256) == 64
    assert _resolve(store) == record


def test_bare_digest_or_historical_timestamp_is_not_publish_authority(
    tmp_path, monkeypatch
):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    review = _review()

    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="confirmation",
    ):
        store.publish_owner_basis(review, confirmed=False)

    record = store.publish_owner_basis(review, confirmed=True)

    # Availability is the product publication clock, never a caller-selected
    # historical timestamp.  The exact same record cannot authorize an earlier
    # decision even though its measurement period is historical.
    assert record.available_at == "2026-09-23T10:00:00Z"
    assert _resolve(
        store,
        decision_at="2026-09-23T09:59:59Z",
    ) is None


def test_measurement_period_cannot_end_after_owner_confirmation(tmp_path, monkeypatch):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    review = _review(
        measurement_period_end="2026-09-24T00:00:00Z",
    )

    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="has not completed",
    ):
        store.publish_owner_basis(review, confirmed=True)


def test_per_request_amount_must_have_exact_finite_decimal_representation():
    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="no exact finite Decimal",
    ):
        _review(
            total_allocable_cost=Decimal("10"),
            request_denominator=3,
        )


def test_float_money_and_empty_measurement_document_fail_closed():
    with pytest.raises(subject.LocalComputeAllocationBasisError):
        _review(total_allocable_cost=12.5)

    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="measurement_document size",
    ):
        _review(measurement_document=b"")


def test_basis_id_is_idempotent_for_same_review_but_immutable_on_change(
    tmp_path, monkeypatch
):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    first_review = _review()
    first = store.publish_owner_basis(first_review, confirmed=True)

    monkeypatch.setattr(
        subject,
        "_authority_now",
        lambda: "2026-09-23T11:00:00Z",
    )
    assert store.publish_owner_basis(first_review, confirmed=True) == first

    changed = _review(total_allocable_cost=Decimal("15"))
    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="immutable",
    ):
        store.publish_owner_basis(changed, confirmed=True)


def test_owner_goal_revision_invalidates_old_basis(tmp_path, monkeypatch):
    _workspace, _authority, goal_store, store = _store(tmp_path, monkeypatch)
    store.publish_owner_basis(_review(), confirmed=True)
    assert _resolve(store) is not None

    current = goal_store.load()
    goal_store.persist_automatic_successor(
        replace(current, revision=current.revision + 1)
    )

    assert _resolve(store) is None


def test_exact_compute_identity_and_owner_currency_are_required(tmp_path, monkeypatch):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    store.publish_owner_basis(_review(), confirmed=True)

    assert store.resolve(
        basis_id="basis-local-a-2026-09",
        backend_id="other-backend",
        model_id="model-a",
        config_sha256="c" * 64,
        decision_at="2026-09-23T12:00:00Z",
        bankroll_id="paper-bankroll",
        currency="USD",
    ) is None

    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="bankroll/currency",
    ):
        store.resolve(
            basis_id="basis-local-a-2026-09",
            backend_id="local-backend",
            model_id="model-a",
            config_sha256="c" * 64,
            decision_at="2026-09-23T12:00:00Z",
            bankroll_id="paper-bankroll",
            currency="EUR",
        )


def test_deleted_basis_state_cannot_reset_monotonic_authority(tmp_path, monkeypatch):
    _workspace, authority_root, _goal_store, store = _store(tmp_path, monkeypatch)
    store.publish_owner_basis(_review(), confirmed=True)
    store.path.unlink()

    with pytest.raises(MonotonicWorkspaceAuthorityError):
        subject.LocalComputeAllocationBasisAuthorityStore(
            store.workspace,
            authority_root=authority_root,
        )


def test_tampered_document_bytes_fail_before_resolution(tmp_path, monkeypatch):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    record = store.publish_owner_basis(_review(), confirmed=True)

    raw = store.path.read_text(encoding="utf-8")
    original = record.measurement_document_b64
    decoded = base64.b64decode(original)
    altered = base64.b64encode(decoded + b" ").decode("ascii")
    store.path.write_text(
        raw.replace(original, altered, 1),
        encoding="utf-8",
    )

    with pytest.raises(MonotonicWorkspaceAuthorityError):
        subject.LocalComputeAllocationBasisAuthorityStore(
            store.workspace,
            authority_root=_authority,
        )
