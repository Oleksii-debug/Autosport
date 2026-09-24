from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.local_compute_allocation_basis as subject
from autosport.economic_goal_store import EconomicGoalStore, economic_goal_to_payload
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


def _store(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    goal_store = _goal(workspace)
    store = subject.LocalComputeAllocationBasisAuthorityStore(workspace)
    authority_root = store._authority.authority_root
    return workspace, authority_root, goal_store, store


def _review(
    store: subject.LocalComputeAllocationBasisAuthorityStore,
    **overrides,
) -> subject.LocalComputeAllocationBasisReview:
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
    return store.prepare_owner_review(**values)


def _resolve(
    store: subject.LocalComputeAllocationBasisAuthorityStore,
    *,
    allocation_policy_id: str = "full-cost-per-request-v1",
):
    return store.resolve_current(
        basis_id="basis-local-a-2026-09",
        backend_id="local-backend",
        model_id="model-a",
        config_sha256="c" * 64,
        allocation_policy_id=allocation_policy_id,
        bankroll_id="paper-bankroll",
        currency="USD",
    )


def test_owner_review_persists_exact_document_goal_and_derived_amount(
    tmp_path, monkeypatch
):
    _workspace, _authority, goal_store, store = _store(tmp_path, monkeypatch)
    review = _review(store)
    goal = goal_store.load()

    assert review.currency == "USD"
    assert review.owner_goal_id == goal.goal_id
    assert review.owner_goal_revision == goal.revision
    assert review.owner_bankroll_id == goal.bankroll_id
    assert len(review.owner_goal_sha256) == 64

    record = store.publish_owner_basis(review, confirmed=True)

    assert record.amount_per_request == Decimal("0.125")
    assert record.currency == "USD"
    published_at = datetime.fromisoformat(
        record.available_at.replace("Z", "+00:00")
    )
    assert published_at <= datetime.now(published_at.tzinfo)
    assert record.owner_review_sha256 == review.review_sha256
    assert (
        base64.b64decode(record.measurement_document_b64)
        == base64.b64decode(review.measurement_document_b64)
    )
    assert len(record.measurement_document_sha256) == 64
    assert len(record.basis_sha256) == 64
    assert _resolve(store) == record


def test_goal_change_between_review_and_confirmation_fails_closed(
    tmp_path, monkeypatch
):
    _workspace, _authority, goal_store, store = _store(tmp_path, monkeypatch)
    review = _review(store)
    old_review_sha256 = review.review_sha256

    current = goal_store.load()
    goal_store.persist_automatic_successor(
        replace(current, revision=current.revision + 1)
    )

    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="EconomicGoal changed",
    ):
        store.publish_owner_basis(review, confirmed=True)

    fresh = _review(store)
    assert fresh.owner_goal_revision == current.revision + 1
    assert fresh.review_sha256 != old_review_sha256


def test_goal_store_module_rebind_cannot_redefine_durable_owner_goal(
    tmp_path, monkeypatch
):
    _workspace, _authority, goal_store, store = _store(tmp_path, monkeypatch)
    durable_goal = goal_store.load()
    forged_goal = replace(
        durable_goal,
        revision=durable_goal.revision + 17,
    )
    fake_store_calls: list[str] = []

    class ReboundEconomicGoalStore:
        def __init__(self, _workspace) -> None:
            fake_store_calls.append("init")

        def load(self):
            fake_store_calls.append("load")
            return forged_goal

    monkeypatch.setattr(
        subject,
        "EconomicGoalStore",
        ReboundEconomicGoalStore,
    )
    monkeypatch.setattr(
        subject,
        "economic_goal_to_payload",
        lambda _goal: {"forged": "module-global-serializer"},
    )

    review = _review(store)
    expected_goal_sha256 = subject._digest(
        economic_goal_to_payload(durable_goal)
    )

    assert fake_store_calls == []
    assert review.owner_goal_id == durable_goal.goal_id
    assert review.owner_goal_revision == durable_goal.revision
    assert review.owner_bankroll_id == durable_goal.bankroll_id
    assert review.currency == durable_goal.currency
    assert review.owner_goal_sha256 == expected_goal_sha256

    record = store.publish_owner_basis(review, confirmed=True)
    assert fake_store_calls == []
    assert record.owner_goal_revision == durable_goal.revision
    assert record.owner_goal_sha256 == expected_goal_sha256
    assert goal_store.load() == durable_goal


def test_bare_digest_or_historical_timestamp_is_not_publish_authority(
    tmp_path, monkeypatch
):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    review = _review(store)

    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="confirmation",
    ):
        store.publish_owner_basis(review, confirmed=False)

    record = store.publish_owner_basis(review, confirmed=True)
    published_at = datetime.fromisoformat(
        record.available_at.replace("Z", "+00:00")
    )
    before_publication = (
        published_at - timedelta(microseconds=1)
    ).isoformat()

    for decision_at in (
        before_publication,
        record.available_at,
        "2099-01-01T00:00:00Z",
    ):
        with pytest.raises(
            subject.LocalComputeAllocationBasisError,
            match="timestamp-only historical",
        ):
            store.resolve(
                basis_id="basis-local-a-2026-09",
                backend_id="local-backend",
                model_id="model-a",
                config_sha256="c" * 64,
                allocation_policy_id="full-cost-per-request-v1",
                decision_at=decision_at,
                bankroll_id="paper-bankroll",
                currency="USD",
            )

    assert _resolve(store) == record


def test_measurement_period_cannot_end_after_owner_confirmation(
    tmp_path, monkeypatch
):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    review = _review(
        store,
        measurement_period_end="2099-01-01T00:00:00Z",
    )

    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="has not completed",
    ):
        store.publish_owner_basis(review, confirmed=True)


@pytest.mark.parametrize("offset_seconds", (0, -1))
def test_publication_time_validation_requires_strict_advance(
    tmp_path, monkeypatch, offset_seconds
):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    first = store.publish_owner_basis(_review(store), confirmed=True)
    first_at = datetime.fromisoformat(
        first.available_at.replace("Z", "+00:00")
    )
    candidate = (
        first_at + timedelta(seconds=offset_seconds)
    ).isoformat()

    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="product clock did not advance",
    ):
        subject._validate_publication_available_at((first,), candidate)


def test_clock_module_rebind_cannot_backdate_first_owner_basis(
    tmp_path, monkeypatch
):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    review = _review(store)
    fake_clock_calls: list[str] = []

    def forged_clock() -> str:
        fake_clock_calls.append("called")
        return "2026-09-21T10:00:00Z"

    monkeypatch.setattr(
        subject,
        "_authoritative_utc_now",
        forged_clock,
    )

    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="product clock authority changed",
    ):
        store.publish_owner_basis(review, confirmed=True)

    assert fake_clock_calls == []
    assert not store.path.exists()


def test_clock_code_mutation_cannot_backdate_first_owner_basis(
    tmp_path, monkeypatch
):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    review = _review(store)
    clock = subject._CANONICAL_AUTHORITY_NOW
    original_code = clock.__code__

    def forged_clock() -> str:
        return "2026-09-21T10:00:00Z"

    try:
        clock.__code__ = forged_clock.__code__
        with pytest.raises(
            subject.LocalComputeAllocationBasisError,
            match="product clock authority changed",
        ):
            store.publish_owner_basis(review, confirmed=True)
    finally:
        clock.__code__ = original_code

    assert not store.path.exists()


def test_per_request_amount_must_have_exact_finite_decimal_representation(
    tmp_path, monkeypatch
):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="no exact finite Decimal",
    ):
        _review(
            store,
            total_allocable_cost=Decimal("10"),
            request_denominator=3,
        )


def test_float_money_and_empty_measurement_document_fail_closed(
    tmp_path, monkeypatch
):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    with pytest.raises(subject.LocalComputeAllocationBasisError):
        _review(store, total_allocable_cost=12.5)

    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="measurement_document size",
    ):
        _review(store, measurement_document=b"")


def test_basis_id_is_idempotent_for_same_review_but_immutable_on_change(
    tmp_path, monkeypatch
):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    first_review = _review(store)
    first = store.publish_owner_basis(first_review, confirmed=True)

    assert store.publish_owner_basis(first_review, confirmed=True) == first

    changed = _review(store, total_allocable_cost=Decimal("15"))
    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="immutable",
    ):
        store.publish_owner_basis(changed, confirmed=True)


def test_owner_goal_revision_invalidates_old_basis(tmp_path, monkeypatch):
    _workspace, _authority, goal_store, store = _store(tmp_path, monkeypatch)
    store.publish_owner_basis(_review(store), confirmed=True)
    assert _resolve(store) is not None

    current = goal_store.load()
    goal_store.persist_automatic_successor(
        replace(current, revision=current.revision + 1)
    )

    assert _resolve(store) is None


def test_exact_policy_compute_identity_and_owner_currency_are_required(
    tmp_path, monkeypatch
):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    store.publish_owner_basis(_review(store), confirmed=True)

    assert _resolve(
        store,
        allocation_policy_id="different-allocation-policy",
    ) is None

    assert store.resolve_current(
        basis_id="basis-local-a-2026-09",
        backend_id="other-backend",
        model_id="model-a",
        config_sha256="c" * 64,
        allocation_policy_id="full-cost-per-request-v1",
        bankroll_id="paper-bankroll",
        currency="USD",
    ) is None

    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="bankroll/currency",
    ):
        store.resolve_current(
            basis_id="basis-local-a-2026-09",
            backend_id="local-backend",
            model_id="model-a",
            config_sha256="c" * 64,
            allocation_policy_id="full-cost-per-request-v1",
            bankroll_id="paper-bankroll",
            currency="EUR",
        )


def test_deleted_basis_state_cannot_reset_monotonic_authority(
    tmp_path, monkeypatch
):
    _workspace, _authority_root, _goal_store, store = _store(
        tmp_path,
        monkeypatch,
    )
    store.publish_owner_basis(_review(store), confirmed=True)
    store.path.unlink()

    with pytest.raises(MonotonicWorkspaceAuthorityError):
        subject.LocalComputeAllocationBasisAuthorityStore(store.workspace)


def test_machine_authority_root_cannot_be_retargeted_after_local_state_loss(
    tmp_path, monkeypatch
):
    workspace, canonical_root, _goal_store, store = _store(
        tmp_path,
        monkeypatch,
    )
    store.publish_owner_basis(_review(store), confirmed=True)

    canonical_path = store.path
    canonical_domain = store._authority.domain
    canonical_key = store._authority.key
    alternate_root = tmp_path / "alternate-machine-authority"
    alternate_file = "alternate-allocation-bases.json"

    monkeypatch.setattr(
        subject,
        "local_compute_monotonic_authority_root",
        lambda: alternate_root,
    )
    monkeypatch.setattr(subject, "FILE_NAME", alternate_file)
    monkeypatch.setattr(
        subject,
        "AUTHORITY_DOMAIN",
        canonical_domain + ".alternate",
    )
    monkeypatch.setattr(
        subject,
        "AUTHORITY_KEY",
        canonical_key + "-alternate",
    )

    rebound = subject.LocalComputeAllocationBasisAuthorityStore(workspace)
    assert rebound.path == canonical_path
    assert rebound._authority.authority_root == canonical_root
    assert rebound._authority.domain == canonical_domain
    assert rebound._authority.key == canonical_key
    assert not alternate_root.exists()
    assert not (workspace / alternate_file).exists()

    canonical_path.unlink()

    with pytest.raises(TypeError, match="authority_root"):
        subject.LocalComputeAllocationBasisAuthorityStore(
            workspace,
            authority_root=alternate_root,  # type: ignore[call-arg]
        )

    with pytest.raises(MonotonicWorkspaceAuthorityError):
        subject.LocalComputeAllocationBasisAuthorityStore(workspace)

    assert not alternate_root.exists()
    assert not (workspace / alternate_file).exists()


def test_tampered_document_bytes_fail_before_resolution(tmp_path, monkeypatch):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    record = store.publish_owner_basis(_review(store), confirmed=True)

    raw = store.path.read_text(encoding="utf-8")
    original = record.measurement_document_b64
    decoded = base64.b64decode(original)
    altered = base64.b64encode(decoded + b" ").decode("ascii")
    store.path.write_text(
        raw.replace(original, altered, 1),
        encoding="utf-8",
    )

    with pytest.raises(MonotonicWorkspaceAuthorityError):
        subject.LocalComputeAllocationBasisAuthorityStore(store.workspace)
