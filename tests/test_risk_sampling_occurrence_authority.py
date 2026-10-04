from __future__ import annotations

import hashlib
import json

import pytest

import autosport.risk_membership_publication as publication
import autosport.risk_randomization_precommit as randomization
import autosport.risk_sampling_occurrence_authority as draw_authority
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.paper import PaperBook
from autosport.replay import ReplayEngine, market_event_payload_sha256
from autosport.risk_sampling_membership import ResolvedFixedNRiskMembership
from autosport.risk_sampling_occurrence_authority import (
    ProductIidDrawPlanError,
    ProductIidExpectedDrawPlan,
    ProductIidExpectedMemberDraw,
    ProductIidRunAdmissionReceipt,
    ProductIidRunExecutionReceipt,
    issue_product_iid_run_admission,
    resolve_product_iid_expected_draw_plan,
    resolve_product_iid_run_execution,
    verify_product_iid_expected_draw_plan,
    verify_product_iid_run_admission,
    verify_product_iid_run_execution,
)
from autosport.run_registry import RunRegistry
from autosport.run_transaction import RunTransaction


RUN_ID = "run-001"


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _frame(
    *,
    duplicate: bool = False,
    duplicate_payload: bool = False,
) -> str:
    second_id = "unit-a" if duplicate else "unit-b"
    second_payload = "a" * 64 if duplicate_payload else "b" * 64
    return _canonical(
        {
            "schema": "AUTOSPORT_RISK_IID_SAMPLING_FRAME_V1",
            "units": [
                {"payload_sha256": "a" * 64, "unit_id": "unit-a"},
                {"payload_sha256": second_payload, "unit_id": second_id},
            ],
        }
    )


def _single_unit_frame() -> str:
    return _canonical(
        {
            "schema": "AUTOSPORT_RISK_IID_SAMPLING_FRAME_V1",
            "units": [
                {"payload_sha256": "a" * 64, "unit_id": "unit-a"},
            ],
        }
    )


def _horizon(draw_count: int = 4) -> str:
    return _canonical(
        {
            "draw_count": draw_count,
            "schema": "AUTOSPORT_RISK_IID_FIXED_DRAW_HORIZON_V1",
        }
    )


def _membership(*, frame_json: str, horizon_json: str) -> ResolvedFixedNRiskMembership:
    return ResolvedFixedNRiskMembership(
        research_protocol_id="risk-fixed-n-protocol",
        protocol_sha256="2" * 64,
        protocol_record_sha256="9" * 64,
        dataset_snapshot_id="risk-fixed-n-dataset",
        dataset_manifest_sha256="3" * 64,
        dataset_record_sha256="c" * 64,
        causal_cutoff="2026-09-01T00:00:00+00:00",
        outcome_reveal_after="2026-09-10T00:00:00+00:00",
        precommitted_at="2026-09-02T12:05:00+00:00",
        planned_run_ids=(RUN_ID,),
        sampling_frame_sha256=_sha_text(frame_json),
        design_sha256="1" * 64,
    )


def _manifest(
    membership: ResolvedFixedNRiskMembership,
    *,
    randomization_root_sha256: str,
    horizon_json: str,
    rng_algorithm: str = "AUTOSPORT_SHA256_REJECTION_V1",
    rng_version: str = "1",
) -> str:
    return _canonical(
        {
            "dataset_manifest_sha256": membership.dataset_manifest_sha256,
            "dataset_snapshot_id": membership.dataset_snapshot_id,
            "experiment_id": "iid-risk-exp-001",
            "horizon_sha256": _sha_text(horizon_json),
            "initial_capital_state_sha256": "5" * 64,
            "kind": "autosport-risk-iid-resample-with-replacement-v1",
            "membership_causal_cutoff": membership.causal_cutoff,
            "membership_dataset_record_sha256": membership.dataset_record_sha256,
            "membership_design_sha256": membership.design_sha256,
            "membership_outcome_reveal_after": membership.outcome_reveal_after,
            "membership_precommitted_at": membership.precommitted_at,
            "membership_protocol_record_sha256": membership.protocol_record_sha256,
            "planned_member_ids": list(membership.planned_run_ids),
            "planned_n": membership.planned_n,
            "protocol_sha256": membership.protocol_sha256,
            "randomization_authority": "PRODUCT_PRECOMMIT_REQUIRED",
            "randomization_root_sha256": randomization_root_sha256,
            "research_protocol_id": membership.research_protocol_id,
            "risk_scope": "SIMULATOR_DISTRIBUTION_ONLY",
            "rng_algorithm": rng_algorithm,
            "rng_version": rng_version,
            "sampler_kind": "IID_RESAMPLE_WITH_REPLACEMENT_V1",
            "sampling_frame_sha256": membership.sampling_frame_sha256,
            "stake_policy_sha256": "6" * 64,
            "stopping_rule": "FIXED_N_NO_EARLY_STOP",
            "with_replacement": True,
        }
    )


def _product_precommit(
    tmp_path,
    monkeypatch,
    *,
    frame_json: str | None = None,
    horizon_json: str | None = None,
    rng_algorithm: str = "AUTOSPORT_SHA256_REJECTION_V1",
    rng_version: str = "1",
):
    frame_json = frame_json or _frame()
    horizon_json = horizon_json or _horizon()
    membership = _membership(frame_json=frame_json, horizon_json=horizon_json)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry_path = workspace / "scientific-registry.json"
    registry_path.write_text("{}\n", encoding="utf-8")
    RunRegistry.initialize_pristine(workspace / "run_registry.json")
    authority_root = tmp_path / "authority"

    monkeypatch.setattr(
        publication,
        "inspect_fixed_n_risk_membership_structure",
        lambda *_args, **_kwargs: membership,
    )
    publication.publish_fixed_n_membership_structure(
        registry_path,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        authority_root=authority_root,
    )
    issued = randomization.issue_risk_randomization_precommit(
        registry_path,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        experiment_id="iid-risk-exp-001",
        authority_root=authority_root,
    )
    manifest = _manifest(
        membership,
        randomization_root_sha256=issued.randomization_root_sha256,
        horizon_json=horizon_json,
        rng_algorithm=rng_algorithm,
        rng_version=rng_version,
    )
    return (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    )


def _resolve(values):
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values
    return resolve_product_iid_expected_draw_plan(
        membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=manifest,
        sampling_frame_json=frame_json,
        horizon_json=horizon_json,
        authority_root=authority_root,
    )


def _issue_admission(values):
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values
    return issue_product_iid_run_admission(
        membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=manifest,
        sampling_frame_json=frame_json,
        horizon_json=horizon_json,
        member_index=0,
        authority_root=authority_root,
    )


def test_expected_draw_plan_materializes_precommitted_frame_and_draws(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    plan = _resolve(values)

    assert plan.product_precommit_bound is True
    assert plan.sampling_frame_materialized is True
    assert plan.expected_draws_product_derived is True
    assert plan.occurrence_ancestry_proven is False
    assert plan.iid_qualified is False
    assert plan.grants_real_money_authority is False
    assert len(plan.frame_units) == 2
    assert len(plan.member_draws) == 1
    draw = plan.member_draws[0]
    assert draw.member_id == RUN_ID
    assert draw.member_index == 0
    assert draw.draw_count == 4
    assert len(draw.draw_indices) == 4
    assert len(draw.draw_unit_ids) == 4
    assert len(draw.draw_payload_sha256) == 4
    assert len(draw.draw_transcript_sha256) == 64
    assert draw.execution_consumption_proven is False


def test_expected_draw_plan_is_deterministic_for_same_frozen_roots(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    first = _resolve(values)
    second = _resolve(values)

    assert first == second
    assert first.plan_sha256 == second.plan_sha256


def test_with_replacement_semantics_are_explicit_on_single_unit_frame(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(
        tmp_path,
        monkeypatch,
        frame_json=_single_unit_frame(),
        horizon_json=_horizon(5),
    )
    draw = _resolve(values).member_draws[0]

    assert draw.draw_indices == (0, 0, 0, 0, 0)
    assert draw.draw_unit_ids == ("unit-a",) * 5


def test_materialized_frame_must_match_precommitted_digest(
    tmp_path,
    monkeypatch,
) -> None:
    values = list(_product_precommit(tmp_path, monkeypatch))
    values[5] = _canonical(
        {
            "schema": "AUTOSPORT_RISK_IID_SAMPLING_FRAME_V1",
            "units": [
                {"payload_sha256": "d" * 64, "unit_id": "unit-a"},
                {"payload_sha256": "b" * 64, "unit_id": "unit-b"},
            ],
        }
    )

    with pytest.raises(
        ProductIidDrawPlanError,
        match="differs from the frozen frame digest",
    ):
        _resolve(tuple(values))


def test_materialized_horizon_must_match_precommitted_digest(
    tmp_path,
    monkeypatch,
) -> None:
    values = list(_product_precommit(tmp_path, monkeypatch))
    values[6] = _horizon(5)

    with pytest.raises(
        ProductIidDrawPlanError,
        match="differs from the frozen horizon digest",
    ):
        _resolve(tuple(values))


def test_noncanonical_sampling_frame_json_is_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    values = list(_product_precommit(tmp_path, monkeypatch))
    values[5] = json.dumps(json.loads(values[5]), indent=2)

    with pytest.raises(ProductIidDrawPlanError, match="exact canonical JSON"):
        _resolve(tuple(values))


def test_noncanonical_horizon_json_is_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    values = list(_product_precommit(tmp_path, monkeypatch))
    values[6] = json.dumps(json.loads(values[6]), indent=2)

    with pytest.raises(ProductIidDrawPlanError, match="exact canonical JSON"):
        _resolve(tuple(values))


def test_duplicate_frame_unit_ids_are_rejected_even_when_precommitted(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(
        tmp_path,
        monkeypatch,
        frame_json=_frame(duplicate=True),
    )

    with pytest.raises(ProductIidDrawPlanError, match="unit ids must be unique"):
        _resolve(values)


@pytest.mark.parametrize("draw_count", [0, -1, True])
def test_invalid_fixed_draw_horizon_is_rejected(
    tmp_path,
    monkeypatch,
    draw_count,
) -> None:
    horizon = _horizon(draw_count)
    values = _product_precommit(
        tmp_path,
        monkeypatch,
        horizon_json=horizon,
    )

    with pytest.raises(ProductIidDrawPlanError, match="positive exact integer"):
        _resolve(values)


def test_unsupported_rng_contract_fails_closed(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(
        tmp_path,
        monkeypatch,
        rng_algorithm="PCG64",
        rng_version="numpy-compatible-contract-v1",
    )

    with pytest.raises(ProductIidDrawPlanError, match="not product-supported"):
        _resolve(values)


def test_forged_plan_cannot_pass_canonical_verifier(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    plan = _resolve(values)
    forged = object.__new__(ProductIidExpectedDrawPlan)
    for field_name in (
        "experiment_id",
        "sampling_manifest_sha256",
        "sampling_frame_sha256",
        "horizon_sha256",
        "frame_units",
        "member_draws",
        "plan_sha256",
    ):
        object.__setattr__(forged, field_name, getattr(plan, field_name))
    object.__setattr__(forged, "plan_sha256", "0" * 64)
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values

    with pytest.raises(ProductIidDrawPlanError, match="differs from canonical"):
        verify_product_iid_expected_draw_plan(
            forged,
            membership=membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=manifest,
            sampling_frame_json=frame_json,
            horizon_json=horizon_json,
            authority_root=authority_root,
        )


def test_duplicate_frame_payloads_are_rejected_even_with_distinct_ids(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(
        tmp_path,
        monkeypatch,
        frame_json=_frame(duplicate_payload=True),
    )

    with pytest.raises(ProductIidDrawPlanError, match="payloads must be unique"):
        _resolve(values)


def test_positive_draw_truth_cannot_be_caller_constructed() -> None:
    with pytest.raises(TypeError, match="product-issued"):
        ProductIidExpectedDrawPlan(
            experiment_id="forged",
            sampling_manifest_sha256="1" * 64,
            sampling_frame_sha256="2" * 64,
            horizon_sha256="3" * 64,
            frame_units=(),
            member_draws=(),
            plan_sha256="4" * 64,
        )

    with pytest.raises(TypeError, match="product-issued"):
        ProductIidExpectedMemberDraw(
            member_id="forged",
            member_index=0,
            stream_sha256="1" * 64,
            draw_count=1,
            draw_indices=(0,),
            draw_unit_ids=("unit",),
            draw_payload_sha256=("2" * 64,),
            draw_transcript_sha256="3" * 64,
        )


def test_horizon_serialized_resource_bound_fails_before_json_work(
    tmp_path,
    monkeypatch,
) -> None:
    values = list(_product_precommit(tmp_path, monkeypatch))
    values[6] = "{" + (" " * 5000) + "}"

    with pytest.raises(ProductIidDrawPlanError, match="serialized size"):
        _resolve(tuple(values))


def test_precommit_resolver_rebinding_fails_before_attacker_executes(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    attacker_called = False

    def forged_precommit(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("attacker precommit executed")

    monkeypatch.setattr(
        draw_authority,
        "resolve_fixed_n_iid_precommit_authority",
        forged_precommit,
    )

    with pytest.raises(ProductIidDrawPlanError, match="authority dispatch changed"):
        _resolve(values)
    assert attacker_called is False


def test_structure_resolver_rebinding_fails_before_attacker_executes(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    attacker_called = False

    def forged_structure(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("attacker structure executed")

    monkeypatch.setattr(
        draw_authority,
        "inspect_fixed_n_iid_sampling_structure",
        forged_structure,
    )

    with pytest.raises(ProductIidDrawPlanError, match="authority dispatch changed"):
        _resolve(values)
    assert attacker_called is False


def test_verifier_rejects_public_resolver_rebinding_before_attacker_executes(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    candidate = _resolve(values)
    attacker_called = False

    def forged_resolver(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        return candidate

    monkeypatch.setattr(
        draw_authority,
        "resolve_product_iid_expected_draw_plan",
        forged_resolver,
    )
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values

    with pytest.raises(
        ProductIidDrawPlanError,
        match="verifier authority dispatch changed",
    ):
        verify_product_iid_expected_draw_plan(
            candidate,
            membership=membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=manifest,
            sampling_frame_json=frame_json,
            horizon_json=horizon_json,
            authority_root=authority_root,
        )
    assert attacker_called is False


def test_result_type_rebinding_fails_before_attacker_executes(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    attacker_called = False

    class ForgedPlan:
        def __new__(cls, *_args, **_kwargs):
            nonlocal attacker_called
            attacker_called = True
            return super().__new__(cls)

    monkeypatch.setattr(
        draw_authority,
        "ProductIidExpectedDrawPlan",
        ForgedPlan,
    )
    with pytest.raises(ProductIidDrawPlanError, match="authority dispatch changed"):
        _resolve(values)
    assert attacker_called is False


def test_draw_truth_types_cannot_be_subclassed() -> None:
    with pytest.raises(TypeError, match="must not be subclassed"):
        class ForgedPlan(ProductIidExpectedDrawPlan):
            pass

    with pytest.raises(TypeError, match="must not be subclassed"):
        class ForgedDraw(ProductIidExpectedMemberDraw):
            pass



def test_run_admission_is_product_issued_before_run_start(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    receipt = _issue_admission(values)

    assert receipt.member_id == RUN_ID
    assert receipt.member_index == 0
    assert len(receipt.expected_draw_plan_sha256) == 64
    assert len(receipt.expected_draw_transcript_sha256) == 64
    assert receipt.workspace_instance_id
    assert receipt.authority_generation > 0
    assert len(receipt.authority_record_sha256) == 64
    assert receipt.product_precommit_bound is True
    assert receipt.run_admission_bound is False
    assert receipt.execution_consumption_proven is False
    assert receipt.occurrence_ancestry_proven is False
    assert receipt.iid_qualified is False
    assert receipt.grants_real_money_authority is False


def test_manual_admission_sidecar_without_monotonic_authority_is_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    plan = _resolve(values)
    workspace = values[1]
    authority_root = values[3]
    authority_state = draw_authority._run_admission_authority(
        workspace,
        experiment_id=plan.experiment_id,
        member_index=0,
        authority_root=authority_root,
    )
    state = draw_authority._run_admission_core(
        plan,
        member_index=0,
    )
    state["workspace_instance_id"] = authority_state.workspace_instance_id
    path = draw_authority._run_admission_state_path(
        workspace,
        experiment_id=plan.experiment_id,
        member_index=0,
    )
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ProductIidDrawPlanError,
        match="authority|history|rollback|re-resolved",
    ):
        _issue_admission(values)


def test_run_registry_persists_exact_draw_admission_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    receipt = _issue_admission(values)
    workspace = values[1]
    registry = RunRegistry(workspace / "run_registry.json")

    key = registry.begin(
        "a" * 64,
        "b" * 64,
        "strategy",
        RUN_ID,
        sampling_draw_admission_receipt_sha256=receipt.receipt_sha256,
    )

    assert (
        registry.get(key)["sampling_draw_admission_receipt_sha256"]
        == receipt.receipt_sha256
    )
    recovered = _issue_admission(values)
    assert recovered.receipt_sha256 == receipt.receipt_sha256
    assert recovered.run_admission_bound is False


def test_run_admission_cannot_be_backfilled_after_registry_begin(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    workspace = values[1]
    registry = RunRegistry(workspace / "run_registry.json")
    registry.begin("a" * 64, "b" * 64, "strategy", RUN_ID)

    with pytest.raises(
        ProductIidDrawPlanError,
        match="before RunRegistry.begin",
    ):
        _issue_admission(values)


def test_tampered_run_admission_state_fails_closed(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    receipt = _issue_admission(values)
    workspace = values[1]
    paths = tuple(workspace.glob(".risk-iid-run-admission-*.json"))
    assert len(paths) == 1
    raw = json.loads(paths[0].read_text(encoding="utf-8"))
    raw["expected_draw_transcript_sha256"] = "0" * 64
    paths[0].write_text(json.dumps(raw) + "\n", encoding="utf-8")

    with pytest.raises(
        ProductIidDrawPlanError,
        match="differs from frozen expected draws",
    ):
        _issue_admission(values)
    assert len(receipt.receipt_sha256) == 64


def test_run_admission_rejects_hardlinked_state_alias(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    _issue_admission(values)
    workspace = values[1]
    paths = tuple(workspace.glob(".risk-iid-run-admission-*.json"))
    assert len(paths) == 1
    alias = workspace / "admission-hardlink-alias.json"
    try:
        alias.hardlink_to(paths[0])
    except OSError:
        pytest.skip("hard links are unavailable on this filesystem")

    with pytest.raises(
        ProductIidDrawPlanError,
        match="re-resolved|regular non-aliased",
    ):
        _issue_admission(values)


def test_run_admission_rejects_symlink_path_replacement(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    _issue_admission(values)
    workspace = values[1]
    paths = tuple(workspace.glob(".risk-iid-run-admission-*.json"))
    assert len(paths) == 1
    path = paths[0]
    real = workspace / "admission-real.json"
    path.replace(real)
    try:
        path.symlink_to(real)
    except OSError:
        pytest.skip("symbolic links are unavailable on this platform")

    with pytest.raises(
        ProductIidDrawPlanError,
        match="re-resolved|regular non-aliased",
    ):
        _issue_admission(values)


def test_deleted_admission_state_cannot_be_silently_reissued(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    _issue_admission(values)
    workspace = values[1]
    paths = tuple(workspace.glob(".risk-iid-run-admission-*.json"))
    assert len(paths) == 1
    paths[0].unlink()

    with pytest.raises(
        ProductIidDrawPlanError,
        match="authority|rollback|missing",
    ):
        _issue_admission(values)


def test_run_admission_stable_reader_rebinding_fails_before_attacker_executes(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    _issue_admission(values)
    attacker_called = False

    def forged_reader(_path):
        nonlocal attacker_called
        attacker_called = True
        return {}

    monkeypatch.setattr(
        draw_authority,
        "_read_stable_run_admission_state",
        forged_reader,
    )

    with pytest.raises(ProductIidDrawPlanError, match="authority dispatch changed"):
        _issue_admission(values)
    assert attacker_called is False


def test_run_admission_descriptor_rebinding_fails_before_attacker_executes(
    tmp_path,
    monkeypatch,
) -> None:
    values = _product_precommit(tmp_path, monkeypatch)
    _issue_admission(values)
    attacker_called = False

    def forged_open(_path):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("attacker descriptor opener executed")

    monkeypatch.setattr(
        draw_authority,
        "_open_read_only_descriptor",
        forged_open,
    )

    with pytest.raises(ProductIidDrawPlanError, match="authority dispatch changed"):
        _issue_admission(values)
    assert attacker_called is False



def _market_event(
    *,
    event_id: str = "iid-event",
    sequence: int = 1,
    decimal_odds: str = "2.0",
) -> MarketEvent:
    return MarketEvent.from_dict(
        {
            "event_id": event_id,
            "market_id": "winner",
            "selection_id": "home",
            "decimal_odds": decimal_odds,
            "observed_ts": f"2026-01-01T00:00:0{sequence}+00:00",
            "source_id": "iid-fixture",
            "sequence": sequence,
        }
    )


def _frame_for_payload(payload_sha256: str) -> str:
    return _canonical(
        {
            "schema": "AUTOSPORT_RISK_IID_SAMPLING_FRAME_V1",
            "units": [
                {
                    "payload_sha256": payload_sha256,
                    "unit_id": "unit-a",
                }
            ],
        }
    )


def _completed_iid_execution(
    tmp_path,
    monkeypatch,
    *,
    draw_count: int = 1,
    replay_events: tuple[MarketEvent, ...] | None = None,
):
    frame_event = _market_event()
    frame_json = _frame_for_payload(
        market_event_payload_sha256(frame_event)
    )
    horizon_json = _horizon(draw_count)
    values = _product_precommit(
        tmp_path,
        monkeypatch,
        frame_json=frame_json,
        horizon_json=horizon_json,
    )
    admission = _issue_admission(values)
    (
        _membership_value,
        workspace,
        _registry_path,
        _authority_root,
        _manifest,
        _frame_json,
        _horizon_json,
    ) = values

    book_path = workspace / "paper_book.json"
    PaperBook("100").save(book_path)
    ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
    ledger.path.write_bytes(b"")
    base_book_sha256 = hashlib.sha256(book_path.read_bytes()).hexdigest()
    base_ledger_sha256 = hashlib.sha256(b"").hexdigest()

    registry = RunRegistry(workspace / "run_registry.json")
    experiment_key = registry.begin(
        "a" * 64,
        "b" * 64,
        "iid-execution-strategy",
        RUN_ID,
        base_paper_book_sha256=base_book_sha256,
        base_decision_ledger_sha256=base_ledger_sha256,
        sampling_draw_admission_receipt_sha256=admission.receipt_sha256,
    )
    tx = RunTransaction.start(
        workspace,
        run_id=RUN_ID,
        experiment_key=experiment_key,
        market_sha256="a" * 64,
        results_sha256="b" * 64,
        strategy_id="iid-execution-strategy",
        base_paper_book_sha256=base_book_sha256,
        base_decision_ledger_sha256=base_ledger_sha256,
        sampling_draw_admission_receipt_sha256=admission.receipt_sha256,
    )
    tx.run_ledger_path.write_bytes(b"")
    new_book_sha256, new_ledger_sha256 = tx.stage_outputs(
        PaperBook.load(book_path),
        ledger.path,
    )
    events = (
        replay_events
        if replay_events is not None
        else tuple(frame_event for _ in range(draw_count))
    )
    replay = ReplayEngine(events).run(
        lambda _event: None,
        run_id=RUN_ID,
    )
    summary = tx.precommit(
        {
            "schema_version": 2,
            "run_id": RUN_ID,
            "experiment_key": experiment_key,
            "market_sha256": "a" * 64,
            "sealed_results_sha256": "b" * 64,
            "strategy_id": "iid-execution-strategy",
            "real_money_execution": False,
        },
        replay_execution_receipt=replay.execution_receipt,
    )
    summary_path = tx.commit()
    registry.complete(
        experiment_key,
        str(summary_path),
        paper_book_sha256=new_book_sha256,
        decision_ledger_sha256=new_ledger_sha256,
    )
    tx.mark_registry_completed()
    return values, replay, summary


def test_iid_run_execution_proves_exact_draw_reached_strategy_replay(
    tmp_path,
    monkeypatch,
) -> None:
    values, replay, summary = _completed_iid_execution(
        tmp_path,
        monkeypatch,
    )
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values

    receipt = resolve_product_iid_run_execution(
        membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=manifest,
        sampling_frame_json=frame_json,
        horizon_json=horizon_json,
        member_index=0,
        authority_root=authority_root,
    )

    assert type(receipt) is ProductIidRunExecutionReceipt
    assert receipt.member_id == RUN_ID
    assert receipt.event_count == 1
    assert receipt.replay_dataset_hash == replay.dataset_hash
    assert (
        receipt.replay_execution_receipt_sha256
        == summary["replay_execution_receipt_sha256"]
    )
    assert receipt.product_precommit_bound is True
    assert receipt.run_admission_bound is True
    assert receipt.execution_consumption_proven is True
    assert receipt.occurrence_ancestry_proven is True
    assert receipt.iid_qualified is False
    assert receipt.grants_real_money_authority is False


def test_iid_run_execution_verifier_reresolves_canonical_completed_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    values, _replay, _summary = _completed_iid_execution(
        tmp_path,
        monkeypatch,
    )
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values
    receipt = resolve_product_iid_run_execution(
        membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=manifest,
        sampling_frame_json=frame_json,
        horizon_json=horizon_json,
        member_index=0,
        authority_root=authority_root,
    )

    verified = verify_product_iid_run_execution(
        receipt,
        membership=membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=manifest,
        sampling_frame_json=frame_json,
        horizon_json=horizon_json,
        member_index=0,
        authority_root=authority_root,
    )

    assert type(verified) is ProductIidRunExecutionReceipt
    assert verified.receipt_sha256 == receipt.receipt_sha256
    assert verified.completed_summary_sha256 == receipt.completed_summary_sha256


def test_iid_run_execution_verifier_rejects_object_new_forgery(
    tmp_path,
    monkeypatch,
) -> None:
    values, _replay, _summary = _completed_iid_execution(
        tmp_path,
        monkeypatch,
    )
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values
    canonical = resolve_product_iid_run_execution(
        membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=manifest,
        sampling_frame_json=frame_json,
        horizon_json=horizon_json,
        member_index=0,
        authority_root=authority_root,
    )
    forged = object.__new__(ProductIidRunExecutionReceipt)
    field_names = (
        "experiment_id",
        "member_id",
        "member_index",
        "expected_draw_plan_sha256",
        "expected_draw_transcript_sha256",
        "run_admission_receipt_sha256",
        "replay_execution_receipt_sha256",
        "replay_dataset_hash",
        "event_count",
        "expected_event_payload_sequence_sha256",
        "expected_event_payload_multiset_sha256",
        "completed_summary_sha256",
        "receipt_sha256",
    )
    for field_name in field_names:
        object.__setattr__(
            forged,
            field_name,
            object.__getattribute__(canonical, field_name),
        )
    object.__setattr__(forged, "replay_dataset_hash", "0" * 64)

    with pytest.raises(
        ProductIidDrawPlanError,
        match="differs from canonical completed evidence",
    ):
        verify_product_iid_run_execution(
            forged,
            membership=membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=manifest,
            sampling_frame_json=frame_json,
            horizon_json=horizon_json,
            member_index=0,
            authority_root=authority_root,
        )


def test_iid_run_execution_verifier_rejects_resolver_rebinding_before_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    values, _replay, _summary = _completed_iid_execution(
        tmp_path,
        monkeypatch,
    )
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values
    receipt = resolve_product_iid_run_execution(
        membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=manifest,
        sampling_frame_json=frame_json,
        horizon_json=horizon_json,
        member_index=0,
        authority_root=authority_root,
    )
    forged_calls: list[str] = []

    def forged_resolver(*_args, **_kwargs):
        forged_calls.append("resolver")
        return receipt

    monkeypatch.setattr(
        draw_authority,
        "resolve_product_iid_run_execution",
        forged_resolver,
    )

    with pytest.raises(
        ProductIidDrawPlanError,
        match="verifier authority dispatch changed",
    ):
        verify_product_iid_run_execution(
            receipt,
            membership=membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=manifest,
            sampling_frame_json=frame_json,
            horizon_json=horizon_json,
            member_index=0,
            authority_root=authority_root,
        )
    assert forged_calls == []


def test_iid_run_execution_rejects_different_replay_payload(
    tmp_path,
    monkeypatch,
) -> None:
    different = _market_event(
        event_id="different-event",
        decimal_odds="3.0",
    )
    values, _replay, _summary = _completed_iid_execution(
        tmp_path,
        monkeypatch,
        replay_events=(different,),
    )
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values

    with pytest.raises(
        ProductIidDrawPlanError,
        match="did not consume and apply the exact IID draw transcript",
    ):
        resolve_product_iid_run_execution(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=manifest,
            sampling_frame_json=frame_json,
            horizon_json=horizon_json,
            member_index=0,
            authority_root=authority_root,
        )


def test_iid_run_execution_rejects_strategy_suppressed_duplicate_draw(
    tmp_path,
    monkeypatch,
) -> None:
    values, _replay, _summary = _completed_iid_execution(
        tmp_path,
        monkeypatch,
        draw_count=2,
    )
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values

    with pytest.raises(
        ProductIidDrawPlanError,
        match="did not consume and apply the exact IID draw transcript",
    ):
        resolve_product_iid_run_execution(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=manifest,
            sampling_frame_json=frame_json,
            horizon_json=horizon_json,
            member_index=0,
            authority_root=authority_root,
        )


def test_iid_run_execution_truth_cannot_be_caller_constructed_or_subclassed() -> None:
    with pytest.raises(TypeError, match="product-resolved"):
        ProductIidRunExecutionReceipt(
            experiment_id="forged",
            member_id=RUN_ID,
            member_index=0,
            expected_draw_plan_sha256="1" * 64,
            expected_draw_transcript_sha256="2" * 64,
            run_admission_receipt_sha256="3" * 64,
            replay_execution_receipt_sha256="4" * 64,
            replay_dataset_hash="5" * 64,
            event_count=1,
            expected_event_payload_sequence_sha256="6" * 64,
            expected_event_payload_multiset_sha256="7" * 64,
            completed_summary_sha256="8" * 64,
            receipt_sha256="9" * 64,
        )

    with pytest.raises(TypeError, match="must not be subclassed"):
        class ForgedExecution(ProductIidRunExecutionReceipt):
            pass


def test_iid_run_admission_verifier_reresolves_completed_binding(
    tmp_path,
    monkeypatch,
) -> None:
    values, _replay, _summary = _completed_iid_execution(
        tmp_path,
        monkeypatch,
    )
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values
    receipt = draw_authority.resolve_product_iid_run_admission(
        membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=manifest,
        sampling_frame_json=frame_json,
        horizon_json=horizon_json,
        member_index=0,
        authority_root=authority_root,
    )

    verified = verify_product_iid_run_admission(
        receipt,
        membership=membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=manifest,
        sampling_frame_json=frame_json,
        horizon_json=horizon_json,
        member_index=0,
        authority_root=authority_root,
    )

    assert type(verified) is ProductIidRunAdmissionReceipt
    assert verified.receipt_sha256 == receipt.receipt_sha256
    assert verified.run_admission_bound is True


def test_iid_run_admission_verifier_rejects_object_new_forgery(
    tmp_path,
    monkeypatch,
) -> None:
    values, _replay, _summary = _completed_iid_execution(
        tmp_path,
        monkeypatch,
    )
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values
    canonical = draw_authority.resolve_product_iid_run_admission(
        membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=manifest,
        sampling_frame_json=frame_json,
        horizon_json=horizon_json,
        member_index=0,
        authority_root=authority_root,
    )
    forged = object.__new__(ProductIidRunAdmissionReceipt)
    field_names = (
        "experiment_id",
        "member_id",
        "member_index",
        "stream_sha256",
        "expected_draw_plan_sha256",
        "expected_draw_transcript_sha256",
        "sampling_manifest_sha256",
        "sampling_frame_sha256",
        "horizon_sha256",
        "workspace_instance_id",
        "state_sha256",
        "authority_generation",
        "authority_record_sha256",
        "receipt_sha256",
        "run_admission_bound",
    )
    for field_name in field_names:
        object.__setattr__(
            forged,
            field_name,
            object.__getattribute__(canonical, field_name),
        )
    object.__setattr__(forged, "receipt_sha256", "0" * 64)

    with pytest.raises(
        ProductIidDrawPlanError,
        match="differs from canonical completed evidence",
    ):
        verify_product_iid_run_admission(
            forged,
            membership=membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=manifest,
            sampling_frame_json=frame_json,
            horizon_json=horizon_json,
            member_index=0,
            authority_root=authority_root,
        )


def test_iid_run_admission_verifier_rejects_resolver_rebinding_before_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    values, _replay, _summary = _completed_iid_execution(
        tmp_path,
        monkeypatch,
    )
    (
        membership,
        workspace,
        registry_path,
        authority_root,
        manifest,
        frame_json,
        horizon_json,
    ) = values
    receipt = draw_authority.resolve_product_iid_run_admission(
        membership,
        registry_path=registry_path,
        workspace=workspace,
        sampling_manifest_json=manifest,
        sampling_frame_json=frame_json,
        horizon_json=horizon_json,
        member_index=0,
        authority_root=authority_root,
    )
    forged_calls: list[str] = []

    def forged_resolver(*_args, **_kwargs):
        forged_calls.append("resolver")
        return receipt

    monkeypatch.setattr(
        draw_authority,
        "resolve_product_iid_run_admission",
        forged_resolver,
    )

    with pytest.raises(
        ProductIidDrawPlanError,
        match="verifier authority dispatch changed",
    ):
        verify_product_iid_run_admission(
            receipt,
            membership=membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=manifest,
            sampling_frame_json=frame_json,
            horizon_json=horizon_json,
            member_index=0,
            authority_root=authority_root,
        )
    assert forged_calls == []


def test_run_admission_truth_cannot_be_caller_constructed_or_subclassed() -> None:
    with pytest.raises(TypeError, match="product-issued"):
        ProductIidRunAdmissionReceipt(
            experiment_id="forged",
            member_id=RUN_ID,
            member_index=0,
            stream_sha256="1" * 64,
            expected_draw_plan_sha256="2" * 64,
            expected_draw_transcript_sha256="3" * 64,
            sampling_manifest_sha256="4" * 64,
            sampling_frame_sha256="5" * 64,
            horizon_sha256="6" * 64,
            state_sha256="7" * 64,
            receipt_sha256="8" * 64,
            run_admission_bound=True,
        )

    with pytest.raises(TypeError, match="must not be subclassed"):
        class ForgedAdmission(ProductIidRunAdmissionReceipt):
            pass
