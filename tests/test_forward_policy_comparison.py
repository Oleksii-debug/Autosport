from __future__ import annotations

from dataclasses import replace

import pytest

import autosport.forward_policy_comparison as comparison_module
from autosport.forward_policy_comparison import (
    ComparisonArm,
    ComparisonState,
    ForwardComparisonIdentity,
    ForwardComparisonMember,
    ForwardPolicyComparison,
    ForwardPolicyComparisonError,
    ForwardPolicyComparisonIntegrityError,
    ForwardPolicyComparisonStore,
    MemberState,
)
from autosport.monotonic_workspace_authority import (
    MonotonicAuthorityConflictError,
)


H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
H5 = "5" * 64
H6 = "6" * 64
H7 = "7" * 64
H8 = "8" * 64
H9 = "9" * 64
HA = "a" * 64
HB = "b" * 64
HC = "c" * 64
T0 = "2026-09-23T10:00:00Z"
T1 = "2026-09-23T10:01:00Z"
T2 = "2026-09-23T10:02:00Z"
T3 = "2026-09-23T10:03:00Z"
T4 = "2026-09-23T10:04:00Z"


def _identity(**changes) -> ForwardComparisonIdentity:
    values = {
        "trial_family_id": "trial-family-1",
        "trial_family_snapshot_sha256": H1,
        "research_protocol_id": "protocol-1",
        "protocol_sha256": H2,
        "champion_policy_id": H3,
        "challenger_policy_id": H4,
        "campaign_id": "campaign-1",
        "campaign_sha256": H5,
        "holdout_access_id": "holdout-access-1",
        "holdout_access_sha256": H6,
        "evaluator_sha256": H7,
        "metric_definition_sha256": H8,
        "guardrail_definition_sha256": H9,
        "cost_definition_sha256": HA,
        "risk_policy_sha256": HB,
        "causal_boundary_sha256": HC,
        "created_at": T0,
        "first_eligible_at": T1,
    }
    values.update(changes)
    return ForwardComparisonIdentity(**values)


def _member(
    member_id: str,
    *,
    manifest: str = H1,
    observed_at: str = T1,
    denominator_class: str = "ELIGIBLE",
) -> ForwardComparisonMember:
    return ForwardComparisonMember(
        member_id=member_id,
        member_manifest_sha256=manifest,
        observed_at=observed_at,
        denominator_class=denominator_class,
        source_evidence_sha256=H2,
    )


def _store(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    authority_root = tmp_path / "authority"
    identity = _identity()
    store, ledger = ForwardPolicyComparisonStore.initialize(
        workspace,
        identity,
        authority_root=authority_root,
    )
    return workspace, authority_root, store, ledger


def _complete_member(
    ledger: ForwardPolicyComparison, member_id: str
) -> ForwardPolicyComparison:
    ledger = ledger.record_arm_evaluation(
        member_id,
        arm=ComparisonArm.CHAMPION,
        evaluation_sha256=H3,
        reward_available_at=T2,
    )
    ledger = ledger.record_arm_evaluation(
        member_id,
        arm=ComparisonArm.CHALLENGER,
        evaluation_sha256=H4,
        reward_available_at=T2,
    )
    return ledger.resolve_member(
        member_id,
        state=MemberState.COMPLETE,
        resolved_at=T3,
    )


def test_identity_is_content_addressed_and_predates_first_eligible_boundary():
    first = _identity()
    assert len(first.comparison_id) == 64
    assert ForwardComparisonIdentity.from_payload(first.to_payload()) == first
    assert _identity().comparison_id == first.comparison_id

    with pytest.raises(ForwardPolicyComparisonError, match="must exist before"):
        _identity(created_at=T1, first_eligible_at=T1)
    with pytest.raises(
        ForwardPolicyComparisonError,
        match="distinct exact identities",
    ):
        _identity(challenger_policy_id=H3)


def test_exact_member_retry_is_idempotent_but_conflict_fails_closed():
    ledger = ForwardPolicyComparison(_identity())
    member = _member("member-1")
    once = ledger.commit_member(member)
    assert once.commit_member(member) == once
    with pytest.raises(
        ForwardPolicyComparisonError,
        match="conflicting duplicate",
    ):
        once.commit_member(_member("member-1", manifest=H2))


def test_member_before_frozen_forward_boundary_is_rejected():
    ledger = ForwardPolicyComparison(_identity())
    with pytest.raises(ForwardPolicyComparisonError, match="predates frozen"):
        ledger.commit_member(_member("member-1", observed_at=T0))


def test_one_sided_pair_remains_pending_and_cannot_terminalize_complete():
    ledger = ForwardPolicyComparison(_identity()).commit_member(
        _member("member-1")
    )
    ledger = ledger.record_arm_evaluation(
        "member-1",
        arm=ComparisonArm.CHAMPION,
        evaluation_sha256=H3,
        reward_available_at=T2,
    )
    with pytest.raises(
        ForwardPolicyComparisonError,
        match="both arm evaluations",
    ):
        ledger.resolve_member(
            "member-1",
            state=MemberState.COMPLETE,
            resolved_at=T3,
        )
    with pytest.raises(
        ForwardPolicyComparisonError,
        match="pending members",
    ):
        ledger.terminalize(
            state=ComparisonState.COMPLETE,
            terminal_at=T4,
        )

    inconclusive = ledger.resolve_member(
        "member-1",
        state=MemberState.INCONCLUSIVE,
        resolved_at=T3,
    ).terminalize(
        state=ComparisonState.INCONCLUSIVE,
        terminal_at=T4,
    )
    assert inconclusive.members[0].state is MemberState.INCONCLUSIVE
    assert inconclusive.terminal_evidence_sha256 == inconclusive.ledger_sha256


def test_complete_terminal_evidence_binds_both_arms_and_is_immutable():
    ledger = ForwardPolicyComparison(_identity()).commit_member(
        _member("member-1")
    )
    ledger = _complete_member(ledger, "member-1")
    terminal = ledger.terminalize(
        state=ComparisonState.COMPLETE,
        terminal_at=T4,
    )
    assert terminal.state is ComparisonState.COMPLETE
    assert terminal.terminal_evidence_sha256 is not None
    assert (
        terminal.terminalize(
            state=ComparisonState.COMPLETE,
            terminal_at=T4,
        )
        == terminal
    )
    with pytest.raises(
        ForwardPolicyComparisonError,
        match="terminal comparison",
    ):
        terminal.commit_member(_member("member-2", observed_at=T2))


def test_wait_and_negative_denominator_members_survive_restart(tmp_path):
    workspace, authority_root, store, ledger = _store(tmp_path)
    ledger = ledger.commit_member(
        _member("a-wait", denominator_class="WAIT_ZERO")
    )
    ledger = ledger.commit_member(
        _member(
            "b-loss",
            observed_at=T2,
            denominator_class="SETTLED_NEGATIVE",
        )
    )
    ledger = ledger.resolve_member(
        "a-wait",
        state=MemberState.INCONCLUSIVE,
        resolved_at=T2,
    )
    ledger = _complete_member(ledger, "b-loss")
    store.save(ledger)

    restarted = ForwardPolicyComparisonStore(
        workspace,
        comparison_id=ledger.comparison_id,
        authority_root=authority_root,
    ).load()
    assert restarted is not None
    assert tuple(member.member_id for member in restarted.members) == (
        "a-wait",
        "b-loss",
    )
    assert tuple(member.state for member in restarted.members) == (
        MemberState.INCONCLUSIVE,
        MemberState.COMPLETE,
    )


def test_member_order_is_deterministic_independent_of_arrival_order():
    identity = _identity()
    left = (
        ForwardPolicyComparison(identity)
        .commit_member(_member("b", observed_at=T2))
        .commit_member(_member("a"))
    )
    right = (
        ForwardPolicyComparison(identity)
        .commit_member(_member("a"))
        .commit_member(_member("b", observed_at=T2))
    )
    assert left == right
    assert left.ledger_sha256 == right.ledger_sha256


def test_store_rejects_denominator_shrink_and_result_rewrite(tmp_path):
    _, _, store, ledger = _store(tmp_path)
    ledger = ledger.commit_member(_member("a")).commit_member(
        _member("b", observed_at=T2)
    )
    ledger = ledger.record_arm_evaluation(
        "a",
        arm=ComparisonArm.CHAMPION,
        evaluation_sha256=H3,
        reward_available_at=T2,
    )
    store.save(ledger)

    shrunken = replace(ledger, members=(ledger.members[0],))
    with pytest.raises(
        ForwardPolicyComparisonIntegrityError,
        match="cannot shrink",
    ):
        store.save(shrunken)

    rewritten_member = replace(
        ledger.members[0],
        champion_evaluation_sha256=H4,
    )
    rewritten = replace(
        ledger,
        members=(rewritten_member, ledger.members[1]),
    )
    with pytest.raises(
        ForwardPolicyComparisonIntegrityError,
        match="cannot be removed or rewritten",
    ):
        store.save(rewritten)


def test_crash_after_prepare_before_publish_recovers_old_state_and_retry(
    tmp_path,
    monkeypatch,
):
    workspace, authority_root, store, ledger = _store(tmp_path)
    successor = ledger.commit_member(_member("member-1"))
    real_write = comparison_module.atomic_write_json

    def crash_before_publish(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("simulated crash after PREPARE")

    monkeypatch.setattr(
        comparison_module,
        "atomic_write_json",
        crash_before_publish,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.save(successor)

    monkeypatch.setattr(
        comparison_module,
        "atomic_write_json",
        real_write,
    )
    restarted_store = ForwardPolicyComparisonStore(
        workspace,
        comparison_id=ledger.comparison_id,
        authority_root=authority_root,
    )
    recovered = restarted_store.load()
    assert recovered == ledger
    restarted_store.save(successor)
    assert restarted_store.load() == successor


def test_crash_after_publish_before_commit_commits_prepared_state_on_restart(
    tmp_path,
    monkeypatch,
):
    workspace, authority_root, store, ledger = _store(tmp_path)
    successor = ledger.commit_member(_member("member-1"))

    def crash_before_commit(**kwargs):
        del kwargs
        raise MonotonicAuthorityConflictError(
            "simulated crash before COMMIT"
        )

    monkeypatch.setattr(
        store.monotonic_authority,
        "commit",
        crash_before_commit,
    )
    with pytest.raises(
        ForwardPolicyComparisonIntegrityError,
        match="publication failed closed",
    ):
        store.save(successor)

    restarted = ForwardPolicyComparisonStore(
        workspace,
        comparison_id=ledger.comparison_id,
        authority_root=authority_root,
    ).load()
    assert restarted == successor


def test_machine_authority_rejects_local_rollback(tmp_path):
    workspace, authority_root, store, ledger = _store(tmp_path)
    old_bytes = store.path.read_bytes()
    successor = ledger.commit_member(_member("member-1"))
    store.save(successor)
    store.path.write_bytes(old_bytes)

    with pytest.raises(
        ForwardPolicyComparisonIntegrityError,
        match="rolled back",
    ):
        ForwardPolicyComparisonStore(
            workspace,
            comparison_id=ledger.comparison_id,
            authority_root=authority_root,
        ).load()


def test_terminal_comparison_survives_restart_and_cannot_be_reopened(
    tmp_path,
):
    workspace, authority_root, store, ledger = _store(tmp_path)
    ledger = ledger.commit_member(_member("member-1"))
    ledger = _complete_member(ledger, "member-1")
    terminal = ledger.terminalize(
        state=ComparisonState.COMPLETE,
        terminal_at=T4,
    )
    store.save(terminal)

    restarted_store = ForwardPolicyComparisonStore(
        workspace,
        comparison_id=terminal.comparison_id,
        authority_root=authority_root,
    )
    restarted = restarted_store.load()
    assert restarted == terminal
    assert restarted is not None
    reopened = replace(
        restarted,
        state=ComparisonState.RUNNING,
        terminal_at=None,
    )
    with pytest.raises(
        ForwardPolicyComparisonIntegrityError,
        match="terminal comparison",
    ):
        restarted_store.save(reopened)


def test_reward_availability_and_arm_identity_cannot_be_rewritten():
    ledger = ForwardPolicyComparison(_identity()).commit_member(
        _member("member-1")
    )
    ledger = ledger.record_arm_evaluation(
        "member-1",
        arm=ComparisonArm.CHAMPION,
        evaluation_sha256=H3,
        reward_available_at=T2,
    )
    assert (
        ledger.record_arm_evaluation(
            "member-1",
            arm=ComparisonArm.CHAMPION,
            evaluation_sha256=H3,
            reward_available_at=T2,
        )
        == ledger
    )
    with pytest.raises(
        ForwardPolicyComparisonError,
        match="evaluation identity",
    ):
        ledger.record_arm_evaluation(
            "member-1",
            arm=ComparisonArm.CHAMPION,
            evaluation_sha256=H4,
            reward_available_at=T2,
        )
    with pytest.raises(
        ForwardPolicyComparisonError,
        match="reward availability",
    ):
        ledger.record_arm_evaluation(
            "member-1",
            arm=ComparisonArm.CHALLENGER,
            evaluation_sha256=H4,
            reward_available_at=T3,
        )
