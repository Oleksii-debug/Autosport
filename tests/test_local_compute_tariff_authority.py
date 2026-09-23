from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import autosport.local_compute_tariff_authority as subject
from autosport.economic_goal_store import EconomicGoalStore
from autosport.owner_economic_authority import (
    INITIAL_OWNER_FORM_DEFAULTS,
    build_initial_owner_contract,
)
from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthorityError


def _owner_goal(workspace: Path, *, currency: str = "USD") -> None:
    values = dict(INITIAL_OWNER_FORM_DEFAULTS)
    values["currency"] = currency
    contract = build_initial_owner_contract(values, emergency_stop=False)
    EconomicGoalStore(workspace).initialize_owner(contract)


def _publish(store: subject.LocalComputeTariffAuthorityStore, **overrides):
    values = {
        "tariff_id": "local-model-a-2026-09",
        "backend_id": "local-backend",
        "model_id": "model-a",
        "config_sha256": "c" * 64,
        "amount_per_request": Decimal("0.125"),
        "effective_from": "2026-09-23T10:00:00Z",
        "effective_until": "2026-10-01T00:00:00Z",
        "allocation_policy_id": "owner-full-cost-per-request-v1",
        "allocation_basis_sha256": "b" * 64,
        "basis_available_at": "2026-09-23T09:00:00Z",
    }
    values.update(overrides)
    return store.publish_owner_tariff(**values)


def test_owner_tariff_resolves_exact_amount_and_identity(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )

    record = _publish(store)

    resolved = store.resolve(
        backend_id="local-backend",
        model_id="model-a",
        config_sha256="c" * 64,
        decision_at="2026-09-23T12:00:00Z",
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    assert resolved == record
    assert resolved.amount_per_request == Decimal("0.125")
    assert resolved.currency == "USD"
    assert len(resolved.tariff_sha256) == 64
    assert (
        resolved.allocation_treatment
        is subject.LocalComputeCostTreatment.FULLY_ALLOCATED_PER_REQUEST
    )


def test_tariff_published_after_decision_cannot_backdate_money(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T13:00:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )
    _publish(store)

    assert store.resolve(
        backend_id="local-backend",
        model_id="model-a",
        config_sha256="c" * 64,
        decision_at="2026-09-23T12:00:00Z",
        bankroll_id="paper-bankroll",
        currency="USD",
    ) is None


def test_future_allocation_basis_cannot_be_owner_published(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )

    with pytest.raises(subject.LocalComputeTariffError, match="allocation basis"):
        _publish(store, basis_available_at="2026-09-23T09:31:00Z")


def test_float_and_negative_money_are_rejected(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )

    with pytest.raises(subject.LocalComputeTariffError):
        _publish(store, amount_per_request=0.1)
    with pytest.raises(subject.LocalComputeTariffError):
        _publish(store, amount_per_request=Decimal("-0.01"))


def test_same_tariff_id_is_idempotent_but_conflict_rejects(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )
    first = _publish(store)
    assert _publish(store) == first

    with pytest.raises(subject.LocalComputeTariffError, match="immutable"):
        _publish(store, amount_per_request=Decimal("0.126"))


def test_overlapping_same_compute_tariffs_reject(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )
    _publish(store)

    with pytest.raises(subject.LocalComputeTariffError, match="overlapping"):
        _publish(
            store,
            tariff_id="local-model-a-overlap",
            effective_from="2026-09-30T00:00:00Z",
            effective_until="2026-10-03T00:00:00Z",
        )


def test_exact_compute_identity_and_owner_currency_are_required(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )
    _publish(store)

    assert store.resolve(
        backend_id="other-backend",
        model_id="model-a",
        config_sha256="c" * 64,
        decision_at="2026-09-23T12:00:00Z",
        bankroll_id="paper-bankroll",
        currency="USD",
    ) is None
    with pytest.raises(subject.LocalComputeTariffError, match="bankroll/currency"):
        store.resolve(
            backend_id="local-backend",
            model_id="model-a",
            config_sha256="c" * 64,
            decision_at="2026-09-23T12:00:00Z",
            bankroll_id="paper-bankroll",
            currency="EUR",
        )


def test_workspace_state_rollback_is_detected_by_independent_authority(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )
    _publish(store)
    old_bytes = store.path.read_bytes()
    _publish(
        store,
        tariff_id="local-model-b",
        model_id="model-b",
        config_sha256="d" * 64,
        effective_from="2026-09-23T10:00:00Z",
        effective_until="2026-10-01T00:00:00Z",
    )

    store.path.write_bytes(old_bytes)
    with pytest.raises(MonotonicWorkspaceAuthorityError):
        subject.LocalComputeTariffAuthorityStore(
            workspace, authority_root=authority
        )


def test_duplicate_json_keys_fail_closed_on_restart(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )
    _publish(store)

    raw = store.path.read_text(encoding="utf-8")
    store.path.write_text(
        raw.replace('"schema":', '"schema":"wrong","schema":', 1),
        encoding="utf-8",
    )
    with pytest.raises(MonotonicWorkspaceAuthorityError):
        subject.LocalComputeTariffAuthorityStore(
            workspace, authority_root=authority
        )
