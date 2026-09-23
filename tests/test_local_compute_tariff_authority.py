from __future__ import annotations

from decimal import Decimal, localcontext
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


def _component(
    amount: Decimal = Decimal("10"),
    *,
    component_id: str = "measured-local-cost",
    currency: str = "USD",
) -> subject.LocalComputeCostComponent:
    return subject.LocalComputeCostComponent(
        component_id=component_id,
        kind=subject.LocalComputeCostComponentKind.OTHER_ALLOCABLE,
        amount=amount,
        currency=currency,
    )


def _publish(store: subject.LocalComputeTariffAuthorityStore, **overrides):
    basis_total = overrides.pop("basis_total", Decimal("10"))
    basis_denominator = overrides.pop("basis_denominator", 80)
    values = {
        "tariff_id": "local-model-a-2026-09",
        "backend_id": "local-backend",
        "model_id": "model-a",
        "config_sha256": "c" * 64,
        "effective_from": "2026-09-23T10:00:00Z",
        "effective_until": "2026-10-01T00:00:00Z",
        "allocation_policy_id": "owner-full-cost-per-request-v1",
        "allocation_basis_id": None,
    }
    values.update(overrides)
    if values["allocation_basis_id"] is None:
        values["allocation_basis_id"] = f"basis-{values['tariff_id']}"
    store.publish_allocation_basis(
        basis_id=values["allocation_basis_id"],
        backend_id=values["backend_id"],
        model_id=values["model_id"],
        config_sha256=values["config_sha256"],
        allocation_policy_id=values["allocation_policy_id"],
        components=(_component(basis_total),),
        denominator_request_count=basis_denominator,
    )
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
    assert len(resolved.allocation_basis_sha256) == 64
    assert resolved.allocation_basis_id == "basis-local-model-a-2026-09"
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


def test_opaque_hash_and_invented_historical_time_cannot_mint_tariff(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )

    with pytest.raises(
        TypeError,
        match="unexpected keyword argument",
    ):
        store.publish_owner_tariff(
            tariff_id="forged-old-api",
            backend_id="local-backend",
            model_id="model-a",
            config_sha256="c" * 64,
            amount_per_request=Decimal("0.125"),
            effective_from="2026-09-23T10:00:00Z",
            effective_until=None,
            allocation_policy_id="owner-full-cost-per-request-v1",
            allocation_basis_sha256="b" * 64,
            basis_available_at="2020-01-01T00:00:00Z",
        )

    with pytest.raises(subject.LocalComputeTariffError, match="product-owned allocation basis"):
        store.publish_owner_tariff(
            tariff_id="forged-new-api",
            backend_id="local-backend",
            model_id="model-a",
            config_sha256="c" * 64,
            effective_from="2026-09-23T10:00:00Z",
            effective_until=None,
            allocation_policy_id="owner-full-cost-per-request-v1",
            allocation_basis_id="opaque-basis-id",
        )


def test_allocation_basis_derives_amount_and_stamps_causal_availability(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )

    basis = store.publish_allocation_basis(
        basis_id="measured-basis",
        backend_id="local-backend",
        model_id="model-a",
        config_sha256="c" * 64,
        allocation_policy_id="owner-full-cost-per-request-v1",
        components=(
            _component(Decimal("6"), component_id="electricity"),
            _component(Decimal("4"), component_id="hardware"),
        ),
        denominator_request_count=80,
    )

    assert basis.total_allocable_cost == Decimal("10")
    assert basis.amount_per_request == Decimal("0.125")
    assert basis.observed_at == "2026-09-23T09:30:00Z"
    assert basis.available_at == "2026-09-23T09:30:00Z"


def test_recurring_decimal_allocation_fails_closed_instead_of_rounding(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )

    with pytest.raises(subject.LocalComputeTariffError, match="exact finite Decimal"):
        store.publish_allocation_basis(
            basis_id="recurring-basis",
            backend_id="local-backend",
            model_id="model-a",
            config_sha256="c" * 64,
            allocation_policy_id="owner-full-cost-per-request-v1",
            components=(_component(Decimal("1")),),
            denominator_request_count=3,
        )


def test_allocation_arithmetic_is_independent_of_decimal_context(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )

    with localcontext() as context:
        context.prec = 6
        basis = store.publish_allocation_basis(
            basis_id="context-independent-basis",
            backend_id="local-backend",
            model_id="model-a",
            config_sha256="c" * 64,
            allocation_policy_id="owner-full-cost-per-request-v1",
            components=(
                _component(Decimal("123456.78"), component_id="hardware"),
                _component(Decimal("0.22"), component_id="electricity"),
            ),
            denominator_request_count=4,
        )

    assert basis.total_allocable_cost == Decimal("123457.00")
    assert basis.amount_per_request == Decimal("30864.25")


def test_float_and_negative_component_money_are_rejected():
    with pytest.raises(subject.LocalComputeTariffError):
        _component(0.1)  # type: ignore[arg-type]
    with pytest.raises(subject.LocalComputeTariffError):
        _component(Decimal("-0.01"))


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
        _publish(
            store,
            allocation_basis_id="basis-conflict",
            basis_total=Decimal("10.08"),
        )


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


def test_allocation_basis_rollback_is_detected_by_independent_authority(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    monkeypatch.setattr(subject, "_authority_now", lambda: "2026-09-23T09:30:00Z")
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )
    first = store.publish_allocation_basis(
        basis_id="basis-first",
        backend_id="local-backend",
        model_id="model-a",
        config_sha256="c" * 64,
        allocation_policy_id="owner-full-cost-per-request-v1",
        components=(_component(Decimal("10")),),
        denominator_request_count=80,
    )
    basis_store = subject.LocalComputeAllocationBasisAuthorityStore(
        workspace, authority_root=authority
    )
    old_bytes = basis_store.path.read_bytes()
    assert first.basis_id == "basis-first"
    store.publish_allocation_basis(
        basis_id="basis-second",
        backend_id="local-backend",
        model_id="model-b",
        config_sha256="d" * 64,
        allocation_policy_id="owner-full-cost-per-request-v1",
        components=(_component(Decimal("20")),),
        denominator_request_count=80,
    )
    basis_store.path.write_bytes(old_bytes)

    with pytest.raises(MonotonicWorkspaceAuthorityError):
        subject.LocalComputeAllocationBasisAuthorityStore(
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
