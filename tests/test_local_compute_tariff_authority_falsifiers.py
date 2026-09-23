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


def _goal(workspace: Path) -> EconomicGoalStore:
    values = dict(INITIAL_OWNER_FORM_DEFAULTS)
    values["currency"] = "USD"
    store = EconomicGoalStore(workspace)
    store.initialize_owner(
        build_initial_owner_contract(values, emergency_stop=False)
    )
    return store


def _store(tmp_path: Path):
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "authority"
    goal_store = _goal(workspace)
    tariff_store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority_root
    )
    return workspace, authority_root, goal_store, tariff_store


def _prepare_basis(
    store: subject.LocalComputeTariffAuthorityStore,
    *,
    tariff_id: str,
    amount: str,
):
    basis_id = f"basis-{tariff_id}"
    target_amount = Decimal(amount)
    denominator = 100
    basis_store = store._basis_authority()
    review = basis_store.prepare_owner_review(
        basis_id=basis_id,
        backend_id="local-backend",
        model_id="local-model",
        config_sha256="c" * 64,
        allocation_policy_id="owner-full-cost-v1",
        measurement_source_id=f"owner-reviewed-{tariff_id}",
        measurement_period_start="1999-01-01T00:00:00Z",
        measurement_period_end="1999-12-31T00:00:00Z",
        total_allocable_cost=target_amount * Decimal(denominator),
        request_denominator=denominator,
        measurement_document=(
            f'{{"tariff_id":"{tariff_id}","amount":"{amount}",'
            f'"requests":{denominator}}}'
        ).encode("utf-8"),
    )
    basis_store.publish_owner_basis(review, confirmed=True)
    return basis_id


def _publish(
    store: subject.LocalComputeTariffAuthorityStore,
    *,
    tariff_id: str,
    amount: str,
    effective_from: str = "2000-01-01T00:00:00Z",
    effective_until: str | None = "2100-01-01T00:00:00Z",
):
    basis_id = _prepare_basis(store, tariff_id=tariff_id, amount=amount)
    return store.publish_owner_tariff(
        tariff_id=tariff_id,
        backend_id="local-backend",
        model_id="local-model",
        config_sha256="c" * 64,
        effective_from=effective_from,
        effective_until=effective_until,
        allocation_policy_id="owner-full-cost-v1",
        allocation_basis_id=basis_id,
    )


def _resolve_current(store: subject.LocalComputeTariffAuthorityStore):
    return store.resolve_current(
        backend_id="local-backend",
        model_id="local-model",
        config_sha256="c" * 64,
        bankroll_id="paper-bankroll",
        currency="USD",
    )


def test_owner_goal_revision_change_invalidates_old_tariff(tmp_path):
    _workspace, _authority, goal_store, tariff_store = _store(tmp_path)
    _publish(tariff_store, tariff_id="tariff-r1", amount="0.10")
    assert _resolve_current(tariff_store) is not None

    current = goal_store.load()
    goal_store.persist_automatic_successor(
        replace(current, revision=current.revision + 1)
    )

    assert _resolve_current(tariff_store) is None


def test_module_clock_rebind_cannot_backdate_tariff_publication(
    tmp_path, monkeypatch
):
    _workspace, _authority, _goal_store, tariff_store = _store(tmp_path)
    basis_id = _prepare_basis(
        tariff_store,
        tariff_id="clock-rebind",
        amount="0.10",
    )
    monkeypatch.setattr(
        subject,
        "_authoritative_utc_now",
        lambda: "2000-01-01T00:00:00Z",
    )

    with pytest.raises(
        subject.LocalComputeTariffError,
        match="product clock authority changed",
    ):
        tariff_store.publish_owner_tariff(
            tariff_id="clock-rebind",
            backend_id="local-backend",
            model_id="local-model",
            config_sha256="c" * 64,
            effective_from="2000-01-01T00:00:00Z",
            effective_until="2100-01-01T00:00:00Z",
            allocation_policy_id="owner-full-cost-v1",
            allocation_basis_id=basis_id,
        )


def test_in_place_clock_code_mutation_fails_closed(tmp_path):
    _workspace, _authority, _goal_store, tariff_store = _store(tmp_path)
    original_code = subject._CANONICAL_AUTHORITY_NOW.__code__

    def forged_now():
        return "2000-01-01T00:00:00Z"

    subject._CANONICAL_AUTHORITY_NOW.__code__ = forged_now.__code__
    try:
        with pytest.raises(
            subject.LocalComputeTariffError,
            match="product clock authority changed",
        ):
            _resolve_current(tariff_store)
    finally:
        subject._CANONICAL_AUTHORITY_NOW.__code__ = original_code


def test_historical_timestamp_cannot_resurrect_old_tariff(tmp_path):
    _workspace, _authority, _goal_store, tariff_store = _store(tmp_path)
    _publish(tariff_store, tariff_id="historical", amount="0.10")

    with pytest.raises(
        subject.LocalComputeTariffError,
        match="historical.*durable causal decision authority",
    ):
        tariff_store.resolve(
            backend_id="local-backend",
            model_id="local-model",
            config_sha256="c" * 64,
            decision_at="2000-01-01T00:00:00Z",
            bankroll_id="paper-bankroll",
            currency="USD",
        )


def test_tariff_amount_cannot_diverge_from_resolved_basis(tmp_path):
    _workspace, authority, _goal_store, tariff_store = _store(tmp_path)
    record = _publish(
        tariff_store,
        tariff_id="tariff-derived",
        amount="0.37",
    )

    assert record.amount_per_request == Decimal("0.37")
    assert record.allocation_basis_id == "basis-tariff-derived"

    raw = tariff_store.path.read_text(encoding="utf-8")
    tariff_store.path.write_text(
        raw.replace(
            '"amount_per_request": "0.37"',
            '"amount_per_request": "0.38"',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(MonotonicWorkspaceAuthorityError):
        subject.LocalComputeTariffAuthorityStore(
            tariff_store.workspace,
            authority_root=authority,
        )


def test_deleting_tariff_state_cannot_reset_authority(tmp_path):
    _workspace, authority, _goal_store, tariff_store = _store(tmp_path)
    _publish(tariff_store, tariff_id="tariff-delete", amount="0.10")
    tariff_store.path.unlink()

    with pytest.raises(MonotonicWorkspaceAuthorityError):
        subject.LocalComputeTariffAuthorityStore(
            tariff_store.workspace,
            authority_root=authority,
        )
