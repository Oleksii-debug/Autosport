from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import autosport.local_compute_tariff_authority as subject
from autosport.economic_goal_store import EconomicGoalStore
from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority
from autosport.owner_economic_authority import (
    INITIAL_OWNER_FORM_DEFAULTS,
    build_initial_owner_contract,
)


def _store(tmp_path: Path) -> subject.LocalComputeTariffAuthorityStore:
    workspace = tmp_path / "workspace"
    values = dict(INITIAL_OWNER_FORM_DEFAULTS)
    values["currency"] = "USD"
    EconomicGoalStore(workspace).initialize_owner(
        build_initial_owner_contract(values, emergency_stop=False)
    )
    return subject.LocalComputeTariffAuthorityStore(workspace)


def _basis(store: subject.LocalComputeTariffAuthorityStore, basis_id: str = "basis-a") -> str:
    basis_store = store._basis_authority()
    review = basis_store.prepare_owner_review(
        basis_id=basis_id,
        backend_id="local-backend",
        model_id="local-model",
        config_sha256="c" * 64,
        allocation_policy_id="owner-full-cost-v1",
        measurement_source_id="owner-ledger",
        measurement_period_start="1999-01-01T00:00:00Z",
        measurement_period_end="1999-12-31T00:00:00Z",
        total_allocable_cost=Decimal("10.00"),
        request_denominator=100,
        measurement_document=b'{"total":"10.00","requests":100}',
    )
    basis_store.publish_owner_basis(review, confirmed=True)
    return basis_id


def _publish(store: subject.LocalComputeTariffAuthorityStore, basis_id: str):
    return store.publish_owner_tariff(
        tariff_id="tariff-a",
        backend_id="local-backend",
        model_id="local-model",
        config_sha256="c" * 64,
        effective_from="2000-01-01T00:00:00Z",
        effective_until="2100-01-01T00:00:00Z",
        allocation_policy_id="owner-full-cost-v1",
        allocation_basis_id=basis_id,
    )


def test_publish_does_not_redispatch_through_rebound_recover(tmp_path, monkeypatch):
    store = _store(tmp_path)
    basis_id = _basis(store)
    calls: list[str] = []

    def forged_recover(_self):
        calls.append("recover")

    monkeypatch.setattr(subject.LocalComputeTariffAuthorityStore, "_recover", forged_recover)

    record = _publish(store, basis_id)
    assert record.amount_per_request == Decimal("0.1")
    assert calls == []


def test_exact_alternate_tariff_authority_object_is_rejected(tmp_path):
    store = _store(tmp_path)
    basis_id = _basis(store)
    canonical = store._authority
    alternate = MonotonicWorkspaceAuthority(
        workspace=store.workspace,
        domain=canonical.domain,
        key=canonical.key,
        authority_root=tmp_path / "alternate-machine-root",
    )

    store._authority = alternate
    with pytest.raises(subject.LocalComputeTariffError, match="runtime authority changed"):
        _publish(store, basis_id)


def test_tariff_authority_instance_method_shadow_is_rejected(tmp_path):
    store = _store(tmp_path)
    basis_id = _basis(store)
    calls: list[str] = []

    def forged_prepare(**_kwargs):
        calls.append("prepare")

    store._authority.prepare = forged_prepare
    try:
        with pytest.raises(subject.LocalComputeTariffError, match="dispatch changed"):
            _publish(store, basis_id)
    finally:
        del store._authority.prepare
    assert calls == []


def test_foreign_exact_basis_store_is_rejected_before_resolution(tmp_path):
    canonical = _store(tmp_path / "canonical")
    foreign = _store(tmp_path / "foreign")
    canonical._basis_store = foreign._basis_store

    with pytest.raises(subject.LocalComputeTariffError, match="runtime authority changed"):
        canonical._basis_authority()


def test_tariff_current_goal_reuses_sealed_basis_composition(tmp_path, monkeypatch):
    store = _store(tmp_path)
    basis_id = _basis(store)
    calls: list[str] = []

    class ForgedGoalStore:
        def __init__(self, *_args, **_kwargs):
            calls.append("init")
            raise AssertionError("tariff must not construct an independent goal store")

    monkeypatch.setattr(subject, "_CANONICAL_ECONOMIC_GOAL_STORE_CLASS", ForgedGoalStore)
    monkeypatch.setattr(
        subject,
        "_CANONICAL_ECONOMIC_GOAL_STORE_LOAD",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("tariff must not independently load EconomicGoal")
        ),
    )

    record = _publish(store, basis_id)
    assert record.owner_goal_revision == 1
    assert calls == []


def test_basis_resolve_dispatch_rebind_fails_closed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    basis_type = type(store._basis_store)
    calls: list[str] = []

    def forged_resolve(*_args, **_kwargs):
        calls.append("resolve")
        return None

    monkeypatch.setattr(basis_type, "resolve_current", forged_resolve)
    with pytest.raises(subject.LocalComputeTariffError, match="basis authority dispatch changed"):
        store._basis_authority()
    assert calls == []
