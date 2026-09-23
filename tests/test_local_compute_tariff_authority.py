from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.local_compute_tariff_authority as subject
from autosport.economic_goal_store import EconomicGoalStore
from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthorityError
from autosport.owner_economic_authority import (
    INITIAL_OWNER_FORM_DEFAULTS,
    build_initial_owner_contract,
)


def _owner_goal(workspace: Path, *, currency: str = "USD") -> EconomicGoalStore:
    values = dict(INITIAL_OWNER_FORM_DEFAULTS)
    values["currency"] = currency
    store = EconomicGoalStore(workspace)
    store.initialize_owner(
        build_initial_owner_contract(values, emergency_stop=False)
    )
    return store


def _publish(
    store: subject.LocalComputeTariffAuthorityStore,
    **overrides,
):
    basis_total = overrides.pop("basis_total", Decimal("10"))
    basis_denominator = overrides.pop("basis_denominator", 80)
    values = {
        "tariff_id": "local-model-a-current",
        "backend_id": "local-backend",
        "model_id": "model-a",
        "config_sha256": "c" * 64,
        "effective_from": "2000-01-01T00:00:00Z",
        "effective_until": "2100-01-01T00:00:00Z",
        "allocation_policy_id": "owner-full-cost-per-request-v1",
        "allocation_basis_id": None,
    }
    values.update(overrides)
    if values["allocation_basis_id"] is None:
        values["allocation_basis_id"] = f"basis-{values['tariff_id']}"

    basis_store = store._basis_authority()
    measurement_document = (
        f'{{"basis_id":"{values["allocation_basis_id"]}",'
        f'"total":"{basis_total}","requests":{basis_denominator}}}'
    ).encode("utf-8")
    review = basis_store.prepare_owner_review(
        basis_id=values["allocation_basis_id"],
        backend_id=values["backend_id"],
        model_id=values["model_id"],
        config_sha256=values["config_sha256"],
        allocation_policy_id=values["allocation_policy_id"],
        measurement_source_id=f"owner-reviewed-{values['tariff_id']}",
        measurement_period_start="1999-01-01T00:00:00Z",
        measurement_period_end="1999-12-31T00:00:00Z",
        total_allocable_cost=basis_total,
        request_denominator=basis_denominator,
        measurement_document=measurement_document,
    )
    basis_store.publish_owner_basis(review, confirmed=True)
    return store.publish_owner_tariff(**values)


def _resolve_current(
    store: subject.LocalComputeTariffAuthorityStore,
    *,
    backend_id: str = "local-backend",
    model_id: str = "model-a",
    config_sha256: str = "c" * 64,
    bankroll_id: str = "paper-bankroll",
    currency: str = "USD",
):
    return store.resolve_current(
        backend_id=backend_id,
        model_id=model_id,
        config_sha256=config_sha256,
        bankroll_id=bankroll_id,
        currency=currency,
    )


def test_owner_tariff_resolves_current_exact_amount_and_identity(tmp_path):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )

    record = _publish(store)
    resolved = _resolve_current(store)

    assert resolved == record
    assert resolved.amount_per_request == Decimal("0.125")
    assert resolved.currency == "USD"
    assert len(resolved.tariff_sha256) == 64
    assert len(resolved.allocation_basis_sha256) == 64
    assert resolved.allocation_basis_id == "basis-local-model-a-current"

    basis = store._basis_authority().resolve_current(
        basis_id=resolved.allocation_basis_id,
        backend_id=resolved.backend_id,
        model_id=resolved.model_id,
        config_sha256=resolved.config_sha256,
        allocation_policy_id=resolved.allocation_policy_id,
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    assert basis is not None
    assert resolved.allocation_basis_sha256 == basis.basis_sha256
    assert len(basis.measurement_document_sha256) == 64
    assert len(basis.owner_review_sha256) == 64
    assert (
        resolved.allocation_treatment
        is subject.LocalComputeCostTreatment.FULLY_ALLOCATED_PER_REQUEST
    )


def test_timestamp_only_historical_resolution_fails_closed(tmp_path):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )
    _publish(store)

    with pytest.raises(
        subject.LocalComputeTariffError,
        match="historical.*durable causal decision authority",
    ):
        store.resolve(
            backend_id="local-backend",
            model_id="model-a",
            config_sha256="c" * 64,
            decision_at="2026-09-23T12:00:00Z",
            bankroll_id="paper-bankroll",
            currency="USD",
        )


def test_opaque_hash_and_invented_historical_time_cannot_mint_tariff(tmp_path):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        store.publish_owner_tariff(
            tariff_id="forged-old-api",
            backend_id="local-backend",
            model_id="model-a",
            config_sha256="c" * 64,
            amount_per_request=Decimal("0.125"),
            effective_from="2000-01-01T00:00:00Z",
            effective_until=None,
            allocation_policy_id="owner-full-cost-per-request-v1",
            allocation_basis_sha256="b" * 64,
            basis_available_at="1990-01-01T00:00:00Z",
        )

    with pytest.raises(
        subject.LocalComputeTariffError,
        match="product-owned allocation basis",
    ):
        store.publish_owner_tariff(
            tariff_id="forged-new-api",
            backend_id="local-backend",
            model_id="model-a",
            config_sha256="c" * 64,
            effective_from="2000-01-01T00:00:00Z",
            effective_until=None,
            allocation_policy_id="owner-full-cost-per-request-v1",
            allocation_basis_id="opaque-basis-id",
        )


def test_substituted_basis_authority_cannot_mint_tariff(tmp_path):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )

    class ForgedBasisAuthority:
        resolve_called = False

        def resolve_current(self, **_kwargs):
            self.resolve_called = True
            raise AssertionError("forged authority callback must not execute")

    forged = ForgedBasisAuthority()
    store._basis_store = forged  # type: ignore[assignment]

    with pytest.raises(subject.LocalComputeTariffError, match="not canonical"):
        store.publish_owner_tariff(
            tariff_id="forged-store",
            backend_id="local-backend",
            model_id="model-a",
            config_sha256="c" * 64,
            effective_from="2000-01-01T00:00:00Z",
            effective_until=None,
            allocation_policy_id="owner-full-cost-per-request-v1",
            allocation_basis_id="forged-basis",
        )
    assert forged.resolve_called is False


def test_same_tariff_id_is_idempotent_but_conflict_rejects(tmp_path):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
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


def test_overlapping_same_compute_tariffs_reject(tmp_path):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )
    _publish(store)

    with pytest.raises(subject.LocalComputeTariffError, match="overlapping"):
        _publish(
            store,
            tariff_id="local-model-a-overlap",
            effective_from="2050-01-01T00:00:00Z",
            effective_until=None,
        )


def test_exact_compute_identity_and_owner_currency_are_required(tmp_path):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )
    _publish(store)

    assert _resolve_current(store, backend_id="other-backend") is None
    with pytest.raises(subject.LocalComputeTariffError, match="bankroll/currency"):
        _resolve_current(store, currency="EUR")


def test_owner_goal_revision_change_invalidates_old_tariff(tmp_path):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    goal_store = _owner_goal(workspace)
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )
    _publish(store)
    assert _resolve_current(store) is not None

    current = goal_store.load()
    goal_store.persist_automatic_successor(
        replace(current, revision=current.revision + 1)
    )

    assert _resolve_current(store) is None


def test_current_resolution_honors_effective_window(tmp_path):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
    store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority
    )

    _publish(
        store,
        tariff_id="expired",
        effective_from="2000-01-01T00:00:00Z",
        effective_until="2001-01-01T00:00:00Z",
    )
    active = _publish(
        store,
        tariff_id="active",
        effective_from="2001-01-01T00:00:00Z",
        effective_until="2100-01-01T00:00:00Z",
    )

    assert _resolve_current(store) == active


def test_workspace_state_rollback_is_detected_by_independent_authority(tmp_path):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
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
    )

    store.path.write_bytes(old_bytes)
    with pytest.raises(MonotonicWorkspaceAuthorityError):
        subject.LocalComputeTariffAuthorityStore(
            workspace, authority_root=authority
        )


def test_duplicate_json_keys_fail_closed_on_restart(tmp_path):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    _owner_goal(workspace)
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
