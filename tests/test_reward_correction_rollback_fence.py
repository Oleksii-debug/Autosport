from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthorityError
from autosport.reward_correction import (
    DependencyArtifact,
    EvidenceRef,
    RewardCorrectionAssertion,
    RewardCorrectionError,
    RewardCorrectionLedger,
)


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _ref(family: str, seed: str) -> EvidenceRef:
    return EvidenceRef(family, seed, _sha(f"sha:{family}:{seed}"))


def _correction(reward0: EvidenceRef, reward1: EvidenceRef) -> RewardCorrectionAssertion:
    return RewardCorrectionAssertion(
        action_id=_sha("action"),
        transition_id=_sha("transition"),
        superseded_reward=reward0,
        corrected_reward=reward1,
        corrected_reward_value=Decimal("-1.25"),
        corrected_available_at="2026-09-23T00:00:00Z",
        correction_source=_ref("settlement.authority", "settlement-1"),
        generation=1,
    )


def _paths(tmp_path):
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "machine-authority"
    workspace.mkdir()
    authority_root.mkdir()
    return workspace / "corrections.sqlite3", authority_root


def test_complete_older_valid_snapshot_cannot_erase_committed_correction(tmp_path):
    path, authority_root = _paths(tmp_path)
    reward0 = _ref("learning.reward", "reward-0")
    reward1 = _ref("learning.reward", "reward-1")
    update = DependencyArtifact(
        _ref("learning.policy-update", "update-1"),
        (reward0,),
    )

    with RewardCorrectionLedger.create(
        path, monotonic_authority_root=authority_root
    ) as ledger:
        ledger.append_artifact(update)
        old_valid_snapshot = path.read_bytes()
        receipt = ledger.append_correction(_correction(reward0, reward1))
        assert receipt.invalidated_artifacts == (update.artifact,)

    # Restore an exact older SQLite image. Its internal schema, triggers, indexes,
    # dependency graph and state-chain prefix are all individually valid; only the
    # independent machine authority can prove it is stale.
    path.write_bytes(old_valid_snapshot)

    with pytest.raises(
        RewardCorrectionError,
        match="rolled back or diverged from monotonic authority",
    ):
        RewardCorrectionLedger.open(
            path, monotonic_authority_root=authority_root
        )


def test_post_sqlite_pre_authority_commit_crash_recovers_exact_prepared_state(
    tmp_path, monkeypatch
):
    path, authority_root = _paths(tmp_path)
    reward0 = _ref("learning.reward", "reward-0")
    reward1 = _ref("learning.reward", "reward-1")
    expected = _correction(reward0, reward1)

    with RewardCorrectionLedger.create(
        path, monotonic_authority_root=authority_root
    ) as ledger:
        def fail_commit(**_kwargs):
            raise MonotonicWorkspaceAuthorityError("simulated authority commit crash")

        monkeypatch.setattr(ledger._authority, "commit", fail_commit)
        with pytest.raises(
            RewardCorrectionError,
            match="committed locally but monotonic authority recovery is required",
        ):
            ledger.append_correction(expected)

    # The SQLite transaction is durable and the authority has PREPARE. Reopen
    # recomputes the exact local chain tip and may therefore finish that PREPARE.
    with RewardCorrectionLedger.open(
        path, monotonic_authority_root=authority_root
    ) as reopened:
        assert reopened.latest_correction(
            action_id=_sha("action"), transition_id=_sha("transition")
        ) == expected
        reopened.verify_integrity()


def test_post_prepare_pre_sqlite_commit_crash_aborts_prepare_and_keeps_old_state(
    tmp_path, monkeypatch
):
    path, authority_root = _paths(tmp_path)
    reward0 = _ref("learning.reward", "reward-0")
    reward1 = _ref("learning.reward", "reward-1")
    expected = _correction(reward0, reward1)

    with RewardCorrectionLedger.create(
        path, monotonic_authority_root=authority_root
    ) as ledger:
        real_prepare = ledger._authority.prepare

        def prepare_then_fail(**kwargs):
            real_prepare(**kwargs)
            raise MonotonicWorkspaceAuthorityError("simulated crash after prepare")

        monkeypatch.setattr(ledger._authority, "prepare", prepare_then_fail)
        with pytest.raises(
            RewardCorrectionError,
            match="monotonic authority rejected mutation",
        ):
            ledger.append_correction(expected)
        assert ledger.latest_correction(
            action_id=_sha("action"), transition_id=_sha("transition")
        ) is None

    # Reopen sees the previous committed local tip and deterministically ABORTs the
    # stranded PREPARE. A normal retry can then advance exactly once.
    with RewardCorrectionLedger.open(
        path, monotonic_authority_root=authority_root
    ) as reopened:
        assert reopened.latest_correction(
            action_id=_sha("action"), transition_id=_sha("transition")
        ) is None
        reopened.append_correction(expected)
        assert reopened.latest_correction(
            action_id=_sha("action"), transition_id=_sha("transition")
        ) == expected
