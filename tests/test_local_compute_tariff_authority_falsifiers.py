from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.local_compute_allocation_basis as basis_subject
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


def _store(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "authority"
    goal_store = _goal(workspace)
    monkeypatch.setattr(
        subject, "_authority_now", lambda: "2026-09-23T09:30:00Z"
    )
    monkeypatch.setattr(
        basis_subject, "_authority_now", lambda: "2026-09-23T09:30:00Z"
    )
    tariff_store = subject.LocalComputeTariffAuthorityStore(
        workspace, authority_root=authority_root
    )
    return workspace, authority_root, goal_store, tariff_store


def _publish(
    store: subject.LocalComputeTariffAuthorityStore,
    *,
    tariff_id: str,
    amount: str,
    effective_from: str,
    effective_until: str | None,
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
        measurement_period_start="2026-09-01T00:00:00Z",
        measurement_period_end="2026-09-20T00:00:00Z",
        total_allocable_cost=target_amount * Decimal(denominator),
        request_denominator=denominator,
        measurement_document=(
            f'{{"tariff_id":"{tariff_id}","amount":"{amount}",'
            f'"requests":{denominator}}}'
        ).encode("utf-8"),
    )
    basis_store.publish_owner_basis(review, confirmed=True)
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


def _resolve(
    store: subject.LocalComputeTariffAuthorityStore, decision_at: str
):
    return store.resolve(
        backend_id="local-backend",
        model_id="local-model",
        config_sha256="c" * 64,
        decision_at=decision_at,
        bankroll_id="paper-bankroll",
        currency="USD",
    )


def test_owner_goal_revision_change_invalidates_old_tariff(tmp_path, monkeypatch):
    _workspace, _authority, goal_store, tariff_store = _store(
        tmp_path, monkeypatch
    )
    _publish(
        tariff_store,
        tariff_id="tariff-r1",
        amount="0.10",
        effective_from="2026-09-23T10:00:00Z",
        effective_until=None,
    )
    assert _resolve(tariff_store, "2026-09-23T12:00:00Z") is not None

    current = goal_store.load()
    goal_store.persist_automatic_successor(
        replace(current, revision=current.revision + 1)
    )

    assert _resolve(tariff_store, "2026-09-23T12:00:01Z") is None


def test_deleting_tariff_state_cannot_reset_authority(tmp_path, monkeypatch):
    _workspace, authority, _goal_store, tariff_store = _store(
        tmp_path, monkeypatch
    )
    _publish(
        tariff_store,
        tariff_id="tariff-r1",
        amount="0.10",
        effective_from="2026-09-23T10:00:00Z",
        effective_until=None,
    )
    tariff_store.path.unlink()

    with pytest.raises(MonotonicWorkspaceAuthorityError):
        subject.LocalComputeTariffAuthorityStore(
            tariff_store.workspace, authority_root=authority
        )


def test_effective_until_boundary_is_exclusive(tmp_path, monkeypatch):
    _workspace, _authority, _goal_store, tariff_store = _store(
        tmp_path, monkeypatch
    )
    _publish(
        tariff_store,
        tariff_id="tariff-window",
        amount="0.10",
        effective_from="2026-09-23T10:00:00Z",
        effective_until="2026-09-23T12:00:00Z",
    )

    assert _resolve(tariff_store, "2026-09-23T11:59:59Z") is not None
    assert _resolve(tariff_store, "2026-09-23T12:00:00Z") is None


def test_nonoverlapping_tariff_rollover_selects_exact_interval(
    tmp_path, monkeypatch
):
    _workspace, _authority, _goal_store, tariff_store = _store(
        tmp_path, monkeypatch
    )
    first = _publish(
        tariff_store,
        tariff_id="tariff-first",
        amount="0.10",
        effective_from="2026-09-23T10:00:00Z",
        effective_until="2026-09-24T00:00:00Z",
    )
    second = _publish(
        tariff_store,
        tariff_id="tariff-second",
        amount="0.15",
        effective_from="2026-09-24T00:00:00Z",
        effective_until=None,
    )

    assert _resolve(tariff_store, "2026-09-23T23:59:59Z") == first
    assert _resolve(tariff_store, "2026-09-24T00:00:00Z") == second


def test_tariff_amount_cannot_diverge_from_resolved_basis(tmp_path, monkeypatch):
    _workspace, _authority, _goal_store, tariff_store = _store(
        tmp_path, monkeypatch
    )
    record = _publish(
        tariff_store,
        tariff_id="tariff-derived",
        amount="0.37",
        effective_from="2026-09-23T10:00:00Z",
        effective_until=None,
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
            authority_root=_authority,
        )
