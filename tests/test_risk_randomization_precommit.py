from __future__ import annotations

from dataclasses import replace
import inspect
import json

import pytest

from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority
from autosport.risk_membership_publication import RiskMembershipPublicationReceipt
import autosport.risk_randomization_precommit as precommit


def _membership_receipt(*, suffix: str = "a") -> RiskMembershipPublicationReceipt:
    return RiskMembershipPublicationReceipt(
        workspace_instance_id="workspace-test",
        membership_sha256=suffix * 64,
        authority_generation=1,
        authority_record_sha256="b" * 64,
        state_sha256="c" * 64,
        research_protocol_id="protocol-001",
        protocol_sha256="d" * 64,
        protocol_record_sha256="e" * 64,
        dataset_snapshot_id="dataset-001",
        dataset_manifest_sha256="f" * 64,
        dataset_record_sha256="1" * 64,
        causal_cutoff="2026-09-23T00:00:00+00:00",
        outcome_reveal_after="2026-09-24T00:00:00+00:00",
        planned_run_ids=("run-001", "run-002"),
        sampling_frame_sha256="2" * 64,
        design_sha256="3" * 64,
        receipt_sha256="4" * 64 if suffix == "a" else "5" * 64,
    )


def _paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = workspace / "scientific-registry.json"
    registry.write_text("{}\n", encoding="utf-8")
    authority_root = tmp_path / "machine-authority"
    return workspace, registry, authority_root


def _install_membership(monkeypatch, receipt: RiskMembershipPublicationReceipt) -> None:
    monkeypatch.setattr(
        precommit,
        "resolve_fixed_n_membership_publication",
        lambda *_args, **_kwargs: receipt,
    )


def _issue(tmp_path, monkeypatch, *, receipt=None, experiment_id="experiment-001"):
    workspace, registry, authority_root = _paths(tmp_path)
    value = receipt or _membership_receipt()
    _install_membership(monkeypatch, value)
    issued = precommit.issue_risk_randomization_precommit(
        registry,
        workspace=workspace,
        research_protocol_id=value.research_protocol_id,
        dataset_snapshot_id=value.dataset_snapshot_id,
        experiment_id=experiment_id,
        authority_root=authority_root,
    )
    return issued, workspace, registry, authority_root, value


def test_issuer_surface_accepts_no_caller_seed_or_root() -> None:
    parameters = inspect.signature(
        precommit.issue_risk_randomization_precommit
    ).parameters
    assert "seed" not in parameters
    assert "randomization_root" not in parameters
    assert "randomization_root_sha256" not in parameters
    assert "entropy" not in parameters
    assert "_product_token_bytes" not in parameters


def test_simultaneous_entropy_dispatch_rebinding_fails_closed(
    tmp_path, monkeypatch
) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    membership = _membership_receipt()
    _install_membership(monkeypatch, membership)
    attacker_called = False

    def forged_token_bytes(size: int) -> bytes:
        nonlocal attacker_called
        attacker_called = True
        return b"\x00" * size

    monkeypatch.setattr(precommit.secrets, "token_bytes", forged_token_bytes)
    # Recreate the predecessor's private module token too.  The public issuer's
    # entropy source is closure-sealed and must not late-read either surface.
    monkeypatch.setattr(
        precommit,
        "_PRODUCT_TOKEN_BYTES",
        forged_token_bytes,
        raising=False,
    )

    with pytest.raises(
        precommit.RiskRandomizationPrecommitError,
        match="entropy source was rebound",
    ):
        precommit.issue_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
            experiment_id="experiment-001",
            authority_root=authority_root,
        )

    assert attacker_called is False


def test_issue_retry_restart_resolves_exact_same_product_root(
    tmp_path, monkeypatch
) -> None:
    first, workspace, registry, authority_root, membership = _issue(
        tmp_path, monkeypatch
    )
    second = precommit.issue_risk_randomization_precommit(
        registry,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        experiment_id=first.experiment_id,
        authority_root=authority_root,
    )
    resolved = precommit.resolve_risk_randomization_precommit(
        registry,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        experiment_id=first.experiment_id,
        authority_root=authority_root,
    )

    assert second == first
    assert resolved == first
    assert len(first.randomization_root_sha256) == 64
    assert first.product_randomization_root_issued is True
    assert first.occurrence_ancestry_proven is False
    assert first.iid_qualified is False
    assert first.grants_real_money_authority is False


def test_issue_retry_rejects_commit_with_wrong_semantic_binding(
    tmp_path, monkeypatch
) -> None:
    first, workspace, registry, authority_root, membership = _issue(
        tmp_path, monkeypatch
    )
    original_recover = MonotonicWorkspaceAuthority.recover

    def recover_with_wrong_binding(self, **kwargs):
        recovery = original_recover(self, **kwargs)
        if recovery.record is None:
            return recovery
        return replace(
            recovery,
            record=replace(
                recovery.record,
                semantic_binding_sha256="0" * 64,
            ),
        )

    monkeypatch.setattr(
        MonotonicWorkspaceAuthority,
        "recover",
        recover_with_wrong_binding,
    )

    with pytest.raises(
        precommit.RiskRandomizationPrecommitError,
        match="semantic binding",
    ):
        precommit.issue_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
            experiment_id=first.experiment_id,
            authority_root=authority_root,
        )


def test_distinct_experiments_get_distinct_roots(tmp_path, monkeypatch) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    membership = _membership_receipt()
    _install_membership(monkeypatch, membership)

    first = precommit.issue_risk_randomization_precommit(
        registry,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        experiment_id="experiment-001",
        authority_root=authority_root,
    )
    second = precommit.issue_risk_randomization_precommit(
        registry,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        experiment_id="experiment-002",
        authority_root=authority_root,
    )

    assert first.randomization_root_sha256 != second.randomization_root_sha256
    assert first.receipt_sha256 != second.receipt_sha256


def test_same_experiment_cannot_be_rebound_to_changed_membership(
    tmp_path, monkeypatch
) -> None:
    first, workspace, registry, authority_root, _ = _issue(tmp_path, monkeypatch)
    changed = _membership_receipt(suffix="9")
    _install_membership(monkeypatch, changed)

    with pytest.raises(
        precommit.RiskRandomizationPrecommitError,
        match="conflicts with exact experiment membership",
    ):
        precommit.issue_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=changed.research_protocol_id,
            dataset_snapshot_id=changed.dataset_snapshot_id,
            experiment_id=first.experiment_id,
            authority_root=authority_root,
        )


def test_state_deletion_after_commit_fails_closed(tmp_path, monkeypatch) -> None:
    issued, workspace, registry, authority_root, membership = _issue(
        tmp_path, monkeypatch
    )
    key = precommit._experiment_key(issued.experiment_id)
    precommit._state_path(workspace, key).unlink()

    with pytest.raises(
        precommit.RiskRandomizationPrecommitError,
        match="failed closed|missing",
    ):
        precommit.issue_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
            experiment_id=issued.experiment_id,
            authority_root=authority_root,
        )


def test_self_consistent_state_tamper_cannot_replace_committed_root(
    tmp_path, monkeypatch
) -> None:
    issued, workspace, registry, authority_root, membership = _issue(
        tmp_path, monkeypatch
    )
    path = precommit._state_path(
        workspace, precommit._experiment_key(issued.experiment_id)
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["randomization_root_sha256"] = "0" * 64
    path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(precommit.RiskRandomizationPrecommitError):
        precommit.resolve_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
            experiment_id=issued.experiment_id,
            authority_root=authority_root,
        )


def test_crash_after_publish_before_commit_recovers_exact_root(
    tmp_path, monkeypatch
) -> None:
    workspace, registry, authority_root = _paths(tmp_path)
    membership = _membership_receipt()
    _install_membership(monkeypatch, membership)
    original_commit = MonotonicWorkspaceAuthority.commit

    def crash_before_commit(self, **_kwargs):
        raise OSError("simulated crash after publication")

    monkeypatch.setattr(MonotonicWorkspaceAuthority, "commit", crash_before_commit)
    with pytest.raises(precommit.RiskRandomizationPrecommitError):
        precommit.issue_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
            experiment_id="experiment-001",
            authority_root=authority_root,
        )

    key = precommit._experiment_key("experiment-001")
    path = precommit._state_path(workspace, key)
    published_root = json.loads(path.read_text(encoding="utf-8"))[
        "randomization_root_sha256"
    ]

    with pytest.raises(
        precommit.RiskRandomizationPrecommitError,
        match="pending PREPARE",
    ):
        precommit.resolve_risk_randomization_precommit(
            registry,
            workspace=workspace,
            research_protocol_id=membership.research_protocol_id,
            dataset_snapshot_id=membership.dataset_snapshot_id,
            experiment_id="experiment-001",
            authority_root=authority_root,
        )

    monkeypatch.setattr(MonotonicWorkspaceAuthority, "commit", original_commit)
    recovered = precommit.issue_risk_randomization_precommit(
        registry,
        workspace=workspace,
        research_protocol_id=membership.research_protocol_id,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        experiment_id="experiment-001",
        authority_root=authority_root,
    )

    assert recovered.randomization_root_sha256 == published_root
    assert recovered.authority_generation == 1
