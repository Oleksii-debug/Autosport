from __future__ import annotations

import base64
import io
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.economic_goal_store as economic_goal_store_module
import autosport.local_compute_allocation_basis as subject
from autosport.economic_goal_store import EconomicGoalStore, economic_goal_to_payload
from autosport.monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
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


@pytest.mark.parametrize("mutation", ("file-name", "init"))
def test_goal_store_construction_cannot_retarget_current_goal_authority(
    tmp_path, monkeypatch, mutation
):
    workspace, _authority, goal_store, store = _store(tmp_path, monkeypatch)
    durable_goal = goal_store.load()
    expected_goal_sha256 = subject._digest(
        economic_goal_to_payload(durable_goal)
    )
    forged_goal = replace(
        durable_goal,
        revision=durable_goal.revision + 37,
    )
    forged_store = EconomicGoalStore(workspace)
    forged_store.path = workspace / "forged-economic-goal.json"
    forged_store.initialize_owner(forged_goal)
    forged_init_calls: list[str] = []

    def forged_init(self, _workspace) -> None:
        forged_init_calls.append("init")
        self.workspace = workspace
        self.path = forged_store.path

    if mutation == "file-name":
        monkeypatch.setattr(
            EconomicGoalStore,
            "FILE_NAME",
            forged_store.path.name,
        )
    else:
        monkeypatch.setattr(EconomicGoalStore, "__init__", forged_init)

    review = _review(store)

    assert forged_init_calls == []
    assert review.owner_goal_id == durable_goal.goal_id
    assert review.owner_goal_revision == durable_goal.revision
    assert review.owner_bankroll_id == durable_goal.bankroll_id
    assert review.currency == durable_goal.currency
    assert review.owner_goal_sha256 == expected_goal_sha256

    record = store.publish_owner_basis(review, confirmed=True)

    assert forged_init_calls == []
    assert record.owner_goal_revision == durable_goal.revision
    assert record.owner_goal_sha256 == expected_goal_sha256
    assert _resolve(store) == record


def test_dependency_goal_parser_rebind_cannot_mint_basis_authority(
    tmp_path, monkeypatch
):
    _workspace, _authority, goal_store, store = _store(tmp_path, monkeypatch)
    durable_goal = goal_store.load()
    forged_goal = replace(
        durable_goal,
        revision=durable_goal.revision + 41,
    )
    parser_calls: list[str] = []

    def forged_parser(_text):
        parser_calls.append("economic_goal_from_json")
        return forged_goal

    with monkeypatch.context() as patch:
        patch.setattr(
            economic_goal_store_module,
            "economic_goal_from_json",
            forged_parser,
        )
        with pytest.raises(
            subject.LocalComputeAllocationBasisError,
            match="parser authority changed",
        ):
            _review(store)

    assert parser_calls == []
    assert not store.path.exists()

    review = _review(store)
    assert review.owner_goal_id == durable_goal.goal_id
    assert review.owner_goal_revision == durable_goal.revision
    record = store.publish_owner_basis(review, confirmed=True)
    assert _resolve(store) == record



@pytest.mark.parametrize(
    "dependency_name",
    ("economic_goal_from_payload", "strict_json_loads"),
)
def test_transitive_goal_parser_dependency_rebind_fails_closed(
    tmp_path, monkeypatch, dependency_name
):
    _workspace, _authority, goal_store, store = _store(tmp_path, monkeypatch)
    durable_goal = goal_store.load()
    forged_goal = replace(
        durable_goal,
        revision=durable_goal.revision + 43,
    )
    dependency_calls: list[str] = []
    canonical_dependency = getattr(
        economic_goal_store_module,
        dependency_name,
    )

    def forged_dependency(value):
        dependency_calls.append(dependency_name)
        if dependency_name == "economic_goal_from_payload":
            return forged_goal
        return canonical_dependency(value)

    with monkeypatch.context() as patch:
        patch.setattr(
            economic_goal_store_module,
            dependency_name,
            forged_dependency,
        )
        with pytest.raises(
            subject.LocalComputeAllocationBasisError,
            match="transitive parser authority changed",
        ):
            _review(store)

    assert dependency_calls == []
    assert not store.path.exists()

    review = _review(store)
    assert review.owner_goal_id == durable_goal.goal_id
    assert review.owner_goal_revision == durable_goal.revision
    assert review.owner_bankroll_id == durable_goal.bankroll_id
    assert review.currency == durable_goal.currency
    record = store.publish_owner_basis(review, confirmed=True)
    assert _resolve(store) == record



@pytest.mark.parametrize(
    "member_name",
    ("loads", "dumps", "JSONEncoder", "JSONDecoder"),
)
def test_stdlib_json_member_rebind_fails_before_goal_authority_dispatch(
    tmp_path, monkeypatch, member_name
):
    _workspace, _authority, _goal_store, store = _store(tmp_path, monkeypatch)
    canonical = getattr(json, member_name)
    forged_calls: list[str] = []

    if member_name in {"loads", "dumps"}:
        def forged_member(*args, **kwargs):
            forged_calls.append(member_name)
            return canonical(*args, **kwargs)

        replacement = forged_member
    else:
        class ForgedJsonMember(canonical):
            def __init__(self, *args, **kwargs):
                forged_calls.append(member_name)
                super().__init__(*args, **kwargs)

        replacement = ForgedJsonMember

    with monkeypatch.context() as patch:
        patch.setattr(json, member_name, replacement)
        with pytest.raises(
            subject.LocalComputeAllocationBasisError,
            match="stdlib JSON authority changed",
        ):
            _review(store)

    assert forged_calls == []
    assert not store.path.exists()

    review = _review(store)
    record = store.publish_owner_basis(review, confirmed=True)
    assert _resolve(store) == record


def test_path_open_rebind_cannot_replace_durable_goal_bytes(
    tmp_path, monkeypatch
):
    _workspace, _authority, goal_store, store = _store(tmp_path, monkeypatch)
    durable_goal = goal_store.load()
    forged_goal = replace(
        durable_goal,
        revision=durable_goal.revision + 47,
    )
    forged_bytes = (
        json.dumps(
            economic_goal_to_payload(forged_goal),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    canonical_open = Path.open
    forged_open_calls: list[str] = []

    def forged_open(
        self,
        mode="r",
        buffering=-1,
        encoding=None,
        errors=None,
        newline=None,
    ):
        if self.name == EconomicGoalStore.FILE_NAME:
            forged_open_calls.append(mode)
            if "b" in mode:
                return io.BytesIO(forged_bytes)
            return io.StringIO(forged_bytes.decode("utf-8"))
        return canonical_open(
            self,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", forged_open)
        review = _review(store)

    assert forged_open_calls == []
    assert review.owner_goal_id == durable_goal.goal_id
    assert review.owner_goal_revision == durable_goal.revision
    assert review.owner_bankroll_id == durable_goal.bankroll_id
    assert review.currency == durable_goal.currency

    record = store.publish_owner_basis(review, confirmed=True)
    assert _resolve(store) == record


def test_goal_path_construction_cannot_retarget_durable_goal(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    goal_store = _goal(workspace)
    durable_goal = goal_store.load()
    expected_goal_sha256 = subject._digest(
        economic_goal_to_payload(durable_goal)
    )

    forged_goal = replace(
        durable_goal,
        revision=durable_goal.revision + 53,
    )
    forged_store = EconomicGoalStore(workspace)
    forged_store.path = workspace / "forged-economic-goal.json"
    forged_store.initialize_owner(forged_goal)

    path_type = type(workspace)
    canonical_divide = path_type.__truediv__
    retarget_calls: list[str] = []

    def forged_divide(self, other):
        if other == EconomicGoalStore.FILE_NAME:
            retarget_calls.append(str(self))
            return forged_store.path
        return canonical_divide(self, other)

    with monkeypatch.context() as patch:
        patch.setattr(path_type, "__truediv__", forged_divide)
        store = subject.LocalComputeAllocationBasisAuthorityStore(workspace)
        review = _review(store)

    assert retarget_calls == []
    assert review.owner_goal_id == durable_goal.goal_id
    assert review.owner_goal_revision == durable_goal.revision
    assert review.owner_bankroll_id == durable_goal.bankroll_id
    assert review.currency == durable_goal.currency
    assert review.owner_goal_sha256 == expected_goal_sha256

    record = store.publish_owner_basis(review, confirmed=True)
    assert _resolve(store) == record


def test_current_goal_dispatch_and_alias_rebind_cannot_mint_basis_authority(
    tmp_path, monkeypatch
):
    _workspace, _authority, goal_store, store = _store(tmp_path, monkeypatch)
    durable_goal = goal_store.load()
    expected_goal_sha256 = subject._digest(
        economic_goal_to_payload(durable_goal)
    )
    forged_goal = replace(
        durable_goal,
        revision=durable_goal.revision + 29,
    )
    forged_method_calls: list[str] = []
    forged_alias_calls: list[str] = []

    def forged_current_goal(_store):
        forged_method_calls.append("current_goal")
        return forged_goal, "f" * 64

    class ReboundEconomicGoalStore:
        def __init__(self, _workspace) -> None:
            forged_alias_calls.append("store_init")

    def forged_load(_store):
        forged_alias_calls.append("store_load")
        return forged_goal

    def forged_payload(_goal):
        forged_alias_calls.append("goal_payload")
        return {"forged": True}

    monkeypatch.setattr(
        subject.LocalComputeAllocationBasisAuthorityStore,
        "_current_goal",
        forged_current_goal,
    )
    monkeypatch.setattr(
        subject,
        "_CANONICAL_ECONOMIC_GOAL_STORE_CLASS",
        ReboundEconomicGoalStore,
    )
    monkeypatch.setattr(
        subject,
        "_CANONICAL_ECONOMIC_GOAL_STORE_LOAD",
        forged_load,
    )
    monkeypatch.setattr(
        subject,
        "_CANONICAL_ECONOMIC_GOAL_TO_PAYLOAD",
        forged_payload,
    )

    review = _review(store)

    assert forged_method_calls == []
    assert forged_alias_calls == []
    assert review.owner_goal_id == durable_goal.goal_id
    assert review.owner_goal_revision == durable_goal.revision
    assert review.owner_bankroll_id == durable_goal.bankroll_id
    assert review.currency == durable_goal.currency
    assert review.owner_goal_sha256 == expected_goal_sha256

    record = store.publish_owner_basis(review, confirmed=True)

    assert forged_method_calls == []
    assert forged_alias_calls == []
    assert record.owner_goal_revision == durable_goal.revision
    assert record.owner_goal_sha256 == expected_goal_sha256
    assert _resolve(store) == record


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


def test_exact_alternate_authority_object_cannot_retarget_store(
    tmp_path, monkeypatch
):
    workspace, canonical_root, _goal_store, store = _store(
        tmp_path,
        monkeypatch,
    )
    review = _review(store)
    canonical_authority = store._authority
    alternate_root = tmp_path / "alternate-machine-authority"
    alternate_authority = MonotonicWorkspaceAuthority(
        workspace=workspace,
        domain=canonical_authority.domain,
        key=canonical_authority.key,
        authority_root=alternate_root,
    )

    object.__setattr__(store, "_authority", alternate_authority)
    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="authority state changed",
    ):
        store.publish_owner_basis(review, confirmed=True)

    assert not store.path.exists()
    assert not alternate_root.exists()

    object.__setattr__(store, "_authority", canonical_authority)
    record = store.publish_owner_basis(review, confirmed=True)
    assert canonical_authority.authority_root == canonical_root
    assert _resolve(store) == record


def test_forged_workspace_binding_object_cannot_retarget_authority(
    tmp_path, monkeypatch
):
    _workspace, _canonical_root, _goal_store, store = _store(
        tmp_path,
        monkeypatch,
    )
    review = _review(store)
    authority = store._authority
    canonical_binding = authority.workspace_binding

    class ForgedBinding:
        workspace = canonical_binding.workspace
        authority_root = canonical_binding.authority_root
        workspace_instance_id = canonical_binding.workspace_instance_id
        workspace_marker_path = canonical_binding.workspace_marker_path
        path_binding_path = canonical_binding.path_binding_path
        workspace_locator = canonical_binding.workspace_locator
        workspace_locator_sha256 = canonical_binding.workspace_locator_sha256

        def validate_existing(self, **_kwargs):
            return True, True

        def ensure_bound(self):
            return None

    object.__setattr__(authority, "workspace_binding", ForgedBinding())
    try:
        with pytest.raises(
            subject.LocalComputeAllocationBasisError,
            match="workspace binding identity changed",
        ):
            store.publish_owner_basis(review, confirmed=True)
    finally:
        object.__setattr__(authority, "workspace_binding", canonical_binding)

    assert not store.path.exists()
    record = store.publish_owner_basis(review, confirmed=True)
    assert _resolve(store) == record


def test_constructor_workspace_binding_coordinates_cannot_be_retargeted(
    tmp_path, monkeypatch
):
    _workspace, canonical_root, _goal_store, store = _store(
        tmp_path,
        monkeypatch,
    )
    review = _review(store)
    binding = store._authority.workspace_binding
    original_digest = binding.workspace_locator_sha256
    original_path = binding.path_binding_path
    forged_digest = "f" * 64
    forged_path = (
        canonical_root
        / "workspace-bindings"
        / forged_digest[:2]
        / f"{forged_digest}.json"
    )

    object.__setattr__(binding, "workspace_locator_sha256", forged_digest)
    object.__setattr__(binding, "path_binding_path", forged_path)
    try:
        with pytest.raises(
            subject.LocalComputeAllocationBasisError,
            match="workspace binding state changed",
        ):
            store.publish_owner_basis(review, confirmed=True)
    finally:
        object.__setattr__(
            binding,
            "workspace_locator_sha256",
            original_digest,
        )
        object.__setattr__(binding, "path_binding_path", original_path)

    assert not store.path.exists()
    record = store.publish_owner_basis(review, confirmed=True)
    assert _resolve(store) == record


@pytest.mark.parametrize(
    "binding_method",
    ("validate_existing", "ensure_bound", "_read_workspace_marker_id"),
)
def test_workspace_binding_class_dispatch_rebind_fails_closed(
    tmp_path, monkeypatch, binding_method
):
    _workspace, _canonical_root, _goal_store, store = _store(
        tmp_path,
        monkeypatch,
    )
    review = _review(store)
    binding_type = type(store._authority.workspace_binding)
    calls: list[str] = []

    def forged(*_args, **_kwargs):
        calls.append(binding_method)
        if binding_method == "validate_existing":
            return True, True
        return None

    replacement = (
        staticmethod(forged)
        if binding_method == "_read_workspace_marker_id"
        else forged
    )
    with monkeypatch.context() as patch:
        patch.setattr(binding_type, binding_method, replacement)
        with pytest.raises(
            subject.LocalComputeAllocationBasisError,
            match="workspace binding dispatch changed",
        ):
            store.publish_owner_basis(review, confirmed=True)

    assert calls == []
    assert not store.path.exists()
    record = store.publish_owner_basis(review, confirmed=True)
    assert _resolve(store) == record


def test_recover_class_rebind_cannot_bypass_exact_authority_object_seal(
    tmp_path, monkeypatch
):
    workspace, canonical_root, _goal_store, store = _store(
        tmp_path,
        monkeypatch,
    )
    review = _review(store)
    canonical_authority = store._authority
    alternate_root = tmp_path / "alternate-recover-bypass-root"
    alternate_authority = MonotonicWorkspaceAuthority(
        workspace=workspace,
        domain=canonical_authority.domain,
        key=canonical_authority.key,
        authority_root=alternate_root,
    )
    forged_calls: list[str] = []

    def forged_recover(_store):
        forged_calls.append("recover")

    monkeypatch.setattr(
        subject.LocalComputeAllocationBasisAuthorityStore,
        "_recover",
        forged_recover,
    )
    object.__setattr__(store, "_authority", alternate_authority)

    with pytest.raises(
        subject.LocalComputeAllocationBasisError,
        match="authority state changed",
    ):
        store.publish_owner_basis(review, confirmed=True)

    assert forged_calls == []
    assert not store.path.exists()
    assert not alternate_root.exists()

    object.__setattr__(store, "_authority", canonical_authority)
    record = store.publish_owner_basis(review, confirmed=True)
    assert canonical_authority.authority_root == canonical_root
    assert _resolve(store) == record


def test_authority_method_shadow_cannot_bypass_machine_history(
    tmp_path, monkeypatch
):
    _workspace, _canonical_root, _goal_store, store = _store(
        tmp_path,
        monkeypatch,
    )
    review = _review(store)
    authority = store._authority
    calls: list[str] = []

    def forged_prepare(**_kwargs):
        calls.append("prepare")
        return None

    authority.prepare = forged_prepare
    try:
        with pytest.raises(
            subject.LocalComputeAllocationBasisError,
            match="authority dispatch changed",
        ):
            store.publish_owner_basis(review, confirmed=True)
    finally:
        del authority.prepare

    assert calls == []
    assert not store.path.exists()
    record = store.publish_owner_basis(review, confirmed=True)
    assert _resolve(store) == record


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


@pytest.mark.parametrize(
    "helper_name",
    (
        "_load_bound_history",
        "_load_history",
        "_validate_workspace_binding",
        "_new_terminal_record",
        "_append_record",
    ),
)
def test_lower_authority_class_dispatch_rebind_fails_closed(
    tmp_path, monkeypatch, helper_name
):
    _workspace, _canonical_root, _goal_store, store = _store(
        tmp_path,
        monkeypatch,
    )
    review = _review(store)
    calls: list[str] = []

    def forged_helper(*_args, **_kwargs):
        calls.append(helper_name)
        return None

    with monkeypatch.context() as patch:
        patch.setattr(
            MonotonicWorkspaceAuthority,
            helper_name,
            forged_helper,
        )
        with pytest.raises(
            subject.LocalComputeAllocationBasisError,
            match="authority dispatch changed",
        ):
            store.publish_owner_basis(review, confirmed=True)

    assert calls == []
    assert not store.path.exists()
    record = store.publish_owner_basis(review, confirmed=True)
    assert _resolve(store) == record


@pytest.mark.parametrize(
    "helper_name",
    (
        "_load_bound_history",
        "_append_record",
    ),
)
def test_lower_authority_instance_shadow_fails_closed(
    tmp_path, monkeypatch, helper_name
):
    _workspace, _canonical_root, _goal_store, store = _store(
        tmp_path,
        monkeypatch,
    )
    review = _review(store)
    authority = store._authority
    calls: list[str] = []

    def forged_helper(*_args, **_kwargs):
        calls.append(helper_name)
        return None

    setattr(authority, helper_name, forged_helper)
    try:
        with pytest.raises(
            subject.LocalComputeAllocationBasisError,
            match="authority dispatch changed",
        ):
            store.publish_owner_basis(review, confirmed=True)
    finally:
        delattr(authority, helper_name)

    assert calls == []
    assert not store.path.exists()
    record = store.publish_owner_basis(review, confirmed=True)
    assert _resolve(store) == record
