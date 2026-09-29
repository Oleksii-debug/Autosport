from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import autosport.campaign_inception as inception_module
from autosport.campaign_inception import (
    AUTHORITY_DOMAIN,
    CampaignInceptionConflictError,
    CampaignInceptionIntegrityError,
    CampaignInceptionReceipt,
    CampaignInceptionSourceSpec,
    establish_campaign_inception,
)
from autosport.campaign_precommit_manifest import (
    CampaignPrecommitManifest,
    publish_campaign_precommit_manifest,
)
from autosport.causal_collector import CollectorDeltaStore
from autosport.forward_universe_precommit_authority import (
    ForwardUniversePrecommitLocator,
)
from autosport.monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)


A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64
ZERO = "0" * 64
ONE = "1" * 64


def _manifest(**overrides: object) -> CampaignPrecommitManifest:
    values: dict[str, object] = {
        "campaign_id": "campaign-inception-test",
        "source_id": "betfair:exchange",
        "source_snapshot_sha256": A,
        "committed_at": "2099-12-31T19:00:00Z",
        "observation_not_before": "2100-01-01T06:00:00Z",
        "observation_not_after": "2100-01-08T06:00:00Z",
        "evaluation_universe_sha256": B,
        "strategy_version_id": "strategy-v17",
        "champion_version_id": "model-v42",
        "baseline_version_id": "market-baseline-v3",
        "cost_contract_sha256": C,
        "multiplicity_policy_sha256": D,
        "stopping_policy_sha256": E,
        "restart_policy_sha256": F,
        "causal_evidence_policy_sha256": ZERO,
        "config_sha256": ONE,
    }
    values.update(overrides)
    return CampaignPrecommitManifest(**values)


def _setup(
    tmp_path: Path,
    *,
    manifest: CampaignPrecommitManifest | None = None,
) -> tuple[
    CampaignPrecommitManifest,
    ForwardUniversePrecommitLocator,
    CollectorDeltaStore,
    CampaignInceptionSourceSpec,
    Path,
]:
    selected = _manifest() if manifest is None else manifest
    workspace = tmp_path / "workspace"
    evidence = workspace / "evidence"
    evidence.mkdir(parents=True)
    manifest_path = evidence / "precommit.json"
    authority_root = tmp_path / "machine-authority"
    publish_campaign_precommit_manifest(
        manifest_path,
        selected,
        workspace=workspace,
        authority_root=authority_root,
    )
    locator = ForwardUniversePrecommitLocator(
        manifest_path=manifest_path,
        workspace=workspace,
        authority_root=authority_root,
    )
    store_path = tmp_path / "collector.db"
    store = CollectorDeltaStore(store_path)
    spec = CampaignInceptionSourceSpec(
        expected_store_path=store_path,
        source_id=selected.source_id,
        run_id="run-1",
        stream_epoch="epoch-1",
        anchor_at="2100-01-01T06:00:00+00:00",
        interval_seconds=10,
        max_items=250,
        evaluation_start_slot_ordinal=0,
        evaluation_end_slot_ordinal=1,
    )
    return selected, locator, store, spec, authority_root


def _inception_authority(
    *,
    locator: ForwardUniversePrecommitLocator,
    receipt: CampaignInceptionReceipt,
    authority_root: Path,
) -> MonotonicWorkspaceAuthority:
    return MonotonicWorkspaceAuthority(
        workspace=locator.workspace,
        workspace_instance_id=receipt.workspace_instance_id,
        domain=AUTHORITY_DOMAIN,
        key=receipt.campaign_id,
        authority_root=authority_root,
    )


def _state_file(locator: ForwardUniversePrecommitLocator) -> Path:
    paths = tuple((locator.workspace / "campaign-inception-v1").glob("*.json"))
    assert len(paths) == 1
    return paths[0]


def test_receipt_commit_precedes_exact_gate_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, locator, store, spec, authority_root = _setup(tmp_path)
    original_authorize = inception_module._CANONICAL_GATE_AUTHORIZE
    observed_commit_tips: list[str] = []

    def checked_authorize(
        target: CollectorDeltaStore,
        **kwargs: object,
    ) -> dict[str, object]:
        authority = MonotonicWorkspaceAuthority(
            workspace=locator.workspace,
            domain=AUTHORITY_DOMAIN,
            key=manifest.campaign_id,
            authority_root=authority_root,
        )
        history = authority.read_history()
        assert history
        assert history[-1].phase is AuthorityPhase.COMMIT
        observed_commit_tips.append(history[-1].record_sha256)
        return original_authorize(target, **kwargs)

    monkeypatch.setattr(
        inception_module,
        "_CANONICAL_GATE_AUTHORIZE",
        checked_authorize,
    )
    receipt = establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )

    assert observed_commit_tips == [receipt.authority_record_sha256]
    assert receipt.evaluation_universe_sha256 == manifest.evaluation_universe_sha256
    status = store._collector_schedule_start_gate_status(
        source_id=spec.source_id,
        run_id=spec.run_id,
    )
    assert status is not None
    assert status["authorization_sha256"] == receipt.authority_record_sha256

    authority = _inception_authority(
        locator=locator,
        receipt=receipt,
        authority_root=authority_root,
    )
    history = authority.read_history()
    assert history[-1].phase is AuthorityPhase.COMMIT
    assert history[-1].record_sha256 == receipt.authority_record_sha256
    assert history[-1].intended_state_sha256 == receipt.receipt_sha256


def test_receipt_is_resolver_issued_not_caller_constructible() -> None:
    with pytest.raises(TypeError, match="resolver-issued"):
        CampaignInceptionReceipt()


def test_exact_retry_after_first_start_reopens_same_receipt(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    receipt = establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    slot = store._next_collector_schedule_slot(
        source_id=spec.source_id,
        run_id=spec.run_id,
    )
    assert slot["slot_ordinal"] == 0
    assert (
        store._begin_scheduled_collector_cycle(
            source_id=spec.source_id,
            run_id=spec.run_id,
            stream_epoch=spec.stream_epoch,
            max_items=spec.max_items,
            slot_ordinal=slot["slot_ordinal"],
            due_at=slot["due_at"],
            attempted_at=slot["due_at"],
        )
        == 1
    )

    reopened = establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    assert reopened == receipt
    status = store._collector_schedule_start_gate_status(
        source_id=spec.source_id,
        run_id=spec.run_id,
    )
    assert status is not None
    assert status["authorization_sha256"] == receipt.authority_record_sha256


def test_crash_after_prepare_before_local_receipt_keeps_start_blocked_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    original_write = inception_module._write_state
    writes = 0

    def fail_first_write(path: Path, payload: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("forced state publication crash")
        original_write(path, payload)

    monkeypatch.setattr(inception_module, "_write_state", fail_first_write)
    with pytest.raises(OSError, match="forced state publication crash"):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
        )

    status = store._collector_schedule_start_gate_status(
        source_id=spec.source_id,
        run_id=spec.run_id,
    )
    assert status is not None
    assert status["authorization_sha256"] is None
    slot = store._next_collector_schedule_slot(
        source_id=spec.source_id,
        run_id=spec.run_id,
    )
    with pytest.raises(ValueError, match="not durably authorized"):
        store._begin_scheduled_collector_cycle(
            source_id=spec.source_id,
            run_id=spec.run_id,
            stream_epoch=spec.stream_epoch,
            max_items=spec.max_items,
            slot_ordinal=slot["slot_ordinal"],
            due_at=slot["due_at"],
            attempted_at=slot["due_at"],
        )

    receipt = establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    assert len(receipt.authority_record_sha256) == 64
    assert (
        store._collector_schedule_start_gate_status(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )["authorization_sha256"]
        == receipt.authority_record_sha256
    )


def test_crash_after_local_receipt_before_commit_recovers_exact_pending_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    original_commit = MonotonicWorkspaceAuthority.commit
    calls = 0

    def fail_first_commit(self: MonotonicWorkspaceAuthority, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MonotonicWorkspaceAuthorityError("forced commit crash")
        return original_commit(self, **kwargs)

    monkeypatch.setattr(MonotonicWorkspaceAuthority, "commit", fail_first_commit)
    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="cannot durably commit campaign inception authority",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
        )

    assert _state_file(locator).is_file()
    status = store._collector_schedule_start_gate_status(
        source_id=spec.source_id,
        run_id=spec.run_id,
    )
    assert status is not None
    assert status["authorization_sha256"] is None

    receipt = establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    assert (
        store._collector_schedule_start_gate_status(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )["authorization_sha256"]
        == receipt.authority_record_sha256
    )


def test_deleted_local_receipt_is_reconstructed_from_exact_committed_authority(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    receipt = establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    path = _state_file(locator)
    path.unlink()

    reconstructed = establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )

    assert reconstructed == receipt
    assert path.is_file()


def test_deleted_local_receipt_after_started_slot_reconstructs_without_recapture(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    receipt = establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    slot = store._next_collector_schedule_slot(
        source_id=spec.source_id,
        run_id=spec.run_id,
    )
    store._begin_scheduled_collector_cycle(
        source_id=spec.source_id,
        run_id=spec.run_id,
        stream_epoch=spec.stream_epoch,
        max_items=spec.max_items,
        slot_ordinal=slot["slot_ordinal"],
        due_at=slot["due_at"],
        attempted_at=slot["due_at"],
    )
    _state_file(locator).unlink()

    reconstructed = establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )

    assert reconstructed == receipt
    assert (
        store._next_collector_schedule_slot(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )["slot_ordinal"]
        == 1
    )


def test_deleted_store_and_local_receipt_cannot_rebootstrap_old_campaign(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    _state_file(locator).unlink()
    store.path.unlink()
    replacement = CollectorDeltaStore(store.path)

    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="committed campaign schedule/gate cannot be re-resolved",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=replacement,
            source_spec=spec,
        )

    assert (
        replacement._collector_schedule_start_gate_status(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
        is None
    )


def test_changed_run_under_same_campaign_conflicts_before_new_gate(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    changed = replace(spec, run_id="run-2")

    with pytest.raises(
        CampaignInceptionConflictError,
        match="different collector source/run",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=changed,
        )

    assert (
        store._collector_schedule_start_gate_status(
            source_id=spec.source_id,
            run_id="run-2",
        )
        is None
    )


def test_schedule_outside_precommitted_window_is_rejected_before_gate(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    outside = replace(spec, anchor_at="2099-12-31T23:59:59+00:00")

    with pytest.raises(
        CampaignInceptionConflictError,
        match="outside precommitted observation window",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=outside,
        )

    assert (
        store._collector_schedule_start_gate_status(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
        is None
    )


def test_source_mismatch_is_rejected_before_schedule_mutation(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    mismatch = replace(spec, source_id="another:source")

    with pytest.raises(
        CampaignInceptionConflictError,
        match="does not match prospective precommit source",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=mismatch,
        )

    assert (
        store._collector_schedule_start_gate_status(
            source_id="another:source",
            run_id=spec.run_id,
        )
        is None
    )
