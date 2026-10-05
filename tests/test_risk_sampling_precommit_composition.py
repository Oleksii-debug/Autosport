from __future__ import annotations

import json

import pytest

import autosport.risk_membership_publication as publication
import autosport.risk_randomization_precommit as randomization
import autosport.risk_sampling_dependence as dependence
from autosport.risk_sampling_membership import ResolvedFixedNRiskMembership
from autosport.run_registry import RunRegistry


def _membership() -> ResolvedFixedNRiskMembership:
    return ResolvedFixedNRiskMembership(
        research_protocol_id="risk-fixed-n-protocol",
        protocol_sha256="2" * 64,
        protocol_record_sha256="9" * 64,
        dataset_snapshot_id="risk-fixed-n-dataset",
        dataset_manifest_sha256="3" * 64,
        dataset_record_sha256="a" * 64,
        causal_cutoff="2026-09-01T00:00:00+00:00",
        outcome_reveal_after="2026-09-10T00:00:00+00:00",
        precommitted_at="2026-09-02T12:05:00+00:00",
        planned_run_ids=("run-001", "run-002", "run-003"),
        sampling_frame_sha256="4" * 64,
        design_sha256="1" * 64,
    )


def _manifest(*, randomization_root_sha256: str) -> str:
    payload = {
        "kind": "autosport-risk-iid-resample-with-replacement-v1",
        "experiment_id": "iid-risk-exp-001",
        "membership_design_sha256": "1" * 64,
        "membership_protocol_record_sha256": "9" * 64,
        "membership_dataset_record_sha256": "a" * 64,
        "membership_causal_cutoff": "2026-09-01T00:00:00+00:00",
        "membership_precommitted_at": "2026-09-02T12:05:00+00:00",
        "membership_outcome_reveal_after": "2026-09-10T00:00:00+00:00",
        "research_protocol_id": "risk-fixed-n-protocol",
        "protocol_sha256": "2" * 64,
        "dataset_snapshot_id": "risk-fixed-n-dataset",
        "dataset_manifest_sha256": "3" * 64,
        "sampling_frame_sha256": "4" * 64,
        "initial_capital_state_sha256": "5" * 64,
        "stake_policy_sha256": "6" * 64,
        "horizon_sha256": "7" * 64,
        "sampler_kind": "IID_RESAMPLE_WITH_REPLACEMENT_V1",
        "with_replacement": True,
        "rng_algorithm": "PCG64",
        "rng_version": "numpy-compatible-contract-v1",
        "randomization_root_sha256": randomization_root_sha256,
        "planned_n": 3,
        "planned_member_ids": ["run-001", "run-002", "run-003"],
        "stopping_rule": "FIXED_N_NO_EARLY_STOP",
        "risk_scope": "SIMULATOR_DISTRIBUTION_ONLY",
        "randomization_authority": "PRODUCT_PRECOMMIT_REQUIRED",
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _product_state(tmp_path, monkeypatch):
    membership = _membership()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = workspace / "scientific-registry.json"
    registry.write_text("{}\n", encoding="utf-8")
    RunRegistry.initialize_pristine(workspace / "run_registry.json")
    authority_root = tmp_path / "authority"

    monkeypatch.setattr(
        publication,
        "inspect_fixed_n_risk_membership_structure",
        lambda *_args, **_kwargs: membership,
    )
    published = publication.publish_fixed_n_membership_structure(
        registry,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        authority_root=authority_root,
    )
    issued = randomization.issue_risk_randomization_precommit(
        registry,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        experiment_id="iid-risk-exp-001",
        authority_root=authority_root,
    )
    return membership, workspace, registry, authority_root, published, issued


def _occurrences(structure):
    return tuple(
        dependence.IidSamplingOccurrence(
            member_id=member_id,
            member_index=index,
            stream_sha256=structure.member_stream_sha256[index],
            draw_transcript_sha256=f"{index + 11:064x}",
            initial_capital_state_sha256="5" * 64,
            sampling_frame_sha256="4" * 64,
            protocol_sha256="2" * 64,
            stake_policy_sha256="6" * 64,
            horizon_sha256="7" * 64,
            complete=True,
        )
        for index, member_id in enumerate(structure.planned_member_ids)
    )


def test_re_resolves_exact_product_precommit_without_claiming_iid(
    tmp_path, monkeypatch
) -> None:
    (
        membership,
        workspace,
        registry,
        authority_root,
        published,
        issued,
    ) = _product_state(tmp_path, monkeypatch)
    manifest = _manifest(
        randomization_root_sha256=issued.randomization_root_sha256
    )

    resolved = dependence.resolve_fixed_n_iid_precommit_authority(
        membership,
        registry_path=registry,
        workspace=workspace,
        sampling_manifest_json=manifest,
        authority_root=authority_root,
    )

    assert type(resolved) is dependence.ResolvedFixedNIidPrecommitAuthority
    assert resolved.membership_sha256 == published.membership_sha256
    assert resolved.membership_receipt_sha256 == published.receipt_sha256
    assert (
        resolved.randomization_precommit_receipt_sha256
        == issued.receipt_sha256
    )
    assert resolved.randomization_root_sha256 == issued.randomization_root_sha256
    assert resolved.planned_member_ids == membership.planned_run_ids
    assert resolved.product_membership_preoutcome_chronology_proven is True
    assert resolved.product_randomization_root_issued is True
    assert resolved.occurrence_ancestry_proven is False
    assert resolved.iid_qualified is False
    assert resolved.grants_real_money_authority is False


def test_forged_manifest_root_cannot_rebind_product_precommit(
    tmp_path, monkeypatch
) -> None:
    membership, workspace, registry, authority_root, _published, issued = (
        _product_state(tmp_path, monkeypatch)
    )
    forged_root = "f" * 64
    assert forged_root != issued.randomization_root_sha256

    with pytest.raises(
        dependence.RiskSamplingDependenceError,
        match="does not bind the exact IID design",
    ):
        dependence.resolve_fixed_n_iid_precommit_authority(
            membership,
            registry_path=registry,
            workspace=workspace,
            sampling_manifest_json=_manifest(
                randomization_root_sha256=forged_root
            ),
            authority_root=authority_root,
        )


def test_full_iid_authority_still_rejects_structural_occurrence_rows(
    tmp_path, monkeypatch
) -> None:
    membership, workspace, registry, authority_root, _published, issued = (
        _product_state(tmp_path, monkeypatch)
    )
    manifest = _manifest(
        randomization_root_sha256=issued.randomization_root_sha256
    )
    structure = dependence.inspect_fixed_n_iid_sampling_structure(
        membership,
        sampling_manifest_json=manifest,
    )

    with pytest.raises(
        dependence.RiskSamplingDependenceError,
        match="occurrence ancestry lacks product-owned run/path evidence",
    ):
        dependence.resolve_fixed_n_iid_sampling_authority(
            membership,
            registry_path=registry,
            workspace=workspace,
            sampling_manifest_json=manifest,
            occurrences=_occurrences(structure),
            authority_root=authority_root,
        )


def test_randomization_resolver_rebinding_fails_before_attacker_executes(
    tmp_path, monkeypatch
) -> None:
    membership, workspace, registry, authority_root, _published, issued = (
        _product_state(tmp_path, monkeypatch)
    )
    attacker_called = False

    def forged_resolver(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        return issued

    monkeypatch.setattr(
        randomization,
        "resolve_risk_randomization_precommit",
        forged_resolver,
    )

    with pytest.raises(
        dependence.RiskSamplingDependenceError,
        match="authority dispatch changed",
    ):
        dependence.resolve_fixed_n_iid_precommit_authority(
            membership,
            registry_path=registry,
            workspace=workspace,
            sampling_manifest_json=_manifest(
                randomization_root_sha256=issued.randomization_root_sha256
            ),
            authority_root=authority_root,
        )

    assert attacker_called is False
