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


@pytest.mark.parametrize("kind", ("float", "int"))
def test_source_spec_rejects_virtual_interval_before_numeric_dispatch(
    tmp_path: Path,
    kind: str,
) -> None:
    calls: list[str] = []

    if kind == "float":
        class HostileInterval(float):
            def __float__(self) -> float:
                calls.append("float")
                return 10.0

        interval_seconds: object = HostileInterval(10.0)
    else:
        class HostileInterval(int):
            def __float__(self) -> float:
                calls.append("int")
                return 10.0

        interval_seconds = HostileInterval(10)

    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="exact built-in int or float",
    ):
        CampaignInceptionSourceSpec(
            expected_store_path=tmp_path / "collector.db",
            source_id="betfair:exchange",
            run_id="run-1",
            stream_epoch="epoch-1",
            anchor_at="2100-01-01T06:00:00+00:00",
            interval_seconds=interval_seconds,  # type: ignore[arg-type]
            max_items=250,
            evaluation_start_slot_ordinal=0,
            evaluation_end_slot_ordinal=1,
        )

    assert calls == []


def test_source_spec_rejects_unrepresentable_integer_interval(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="representable as a finite float",
    ):
        CampaignInceptionSourceSpec(
            expected_store_path=tmp_path / "collector.db",
            source_id="betfair:exchange",
            run_id="run-1",
            stream_epoch="epoch-1",
            anchor_at="2100-01-01T06:00:00+00:00",
            interval_seconds=10**10_000,
            max_items=250,
            evaluation_start_slot_ordinal=0,
            evaluation_end_slot_ordinal=1,
        )


def test_source_spec_rejects_path_subclass_before_virtual_dispatch(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    ConcretePath = type(Path())

    class HostilePath(ConcretePath):
        def is_absolute(self) -> bool:
            calls.append("is_absolute")
            return True

        def __fspath__(self) -> str:
            calls.append("__fspath__")
            return super().__fspath__()

    hostile_path = HostilePath(tmp_path / "collector.db")

    with pytest.raises(
        TypeError,
        match="exact platform pathlib path type",
    ):
        CampaignInceptionSourceSpec(
            expected_store_path=hostile_path,
            source_id="betfair:exchange",
            run_id="run-1",
            stream_epoch="epoch-1",
            anchor_at="2100-01-01T06:00:00+00:00",
            interval_seconds=10,
            max_items=250,
            evaluation_start_slot_ordinal=0,
            evaluation_end_slot_ordinal=1,
        )

    assert calls == []


def test_source_spec_does_not_dispatch_path_constructor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_path = tmp_path / "collector.db"
    original = inception_module.Path.__new__
    calls: list[tuple[object, ...]] = []

    def hostile(*args: object, **kwargs: object):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        inception_module.Path,
        "__new__",
        staticmethod(hostile),
    )

    spec = CampaignInceptionSourceSpec(
        expected_store_path=store_path,
        source_id="betfair:exchange",
        run_id="run-1",
        stream_epoch="epoch-1",
        anchor_at="2100-01-01T06:00:00+00:00",
        interval_seconds=10,
        max_items=250,
        evaluation_start_slot_ordinal=0,
        evaluation_end_slot_ordinal=1,
    )

    assert spec.expected_store_path is store_path
    assert calls == []


def test_source_spec_rejects_noncanonical_absolute_store_path(
    tmp_path: Path,
) -> None:
    noncanonical = tmp_path / "nested" / ".." / "collector.db"

    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="canonical absolute path",
    ):
        CampaignInceptionSourceSpec(
            expected_store_path=noncanonical,
            source_id="betfair:exchange",
            run_id="run-1",
            stream_epoch="epoch-1",
            anchor_at="2100-01-01T06:00:00+00:00",
            interval_seconds=10,
            max_items=250,
            evaluation_start_slot_ordinal=0,
            evaluation_end_slot_ordinal=1,
        )


def test_source_spec_rejects_boolean_zero_start_ordinal(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="exact integer slot zero",
    ):
        CampaignInceptionSourceSpec(
            expected_store_path=tmp_path / "collector.db",
            source_id="betfair:exchange",
            run_id="run-1",
            stream_epoch="epoch-1",
            anchor_at="2100-01-01T06:00:00+00:00",
            interval_seconds=10,
            max_items=250,
            evaluation_start_slot_ordinal=False,  # type: ignore[arg-type]
            evaluation_end_slot_ordinal=1,
        )


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


def test_oversized_inception_state_fails_closed_before_json_decode(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    path = _state_file(locator)
    path.write_bytes(b"x" * (inception_module._MAX_STATE_BYTES + 1))

    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="state exceeds supported size",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
        )


def test_inception_state_symlink_is_not_followed(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    path = _state_file(locator)
    external = tmp_path / "external-state.json"
    external.write_bytes(path.read_bytes())
    path.unlink()
    try:
        path.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="state must be one regular file",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
        )


def test_inception_state_hardlink_alias_is_rejected(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    path = _state_file(locator)
    alias = tmp_path / "inception-state-alias.json"
    try:
        alias.hardlink_to(path)
    except OSError as exc:
        pytest.skip(f"hardlink creation unavailable: {exc}")

    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="state must be one regular file",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
        )


def _pristine_inception_state_payload(
    *,
    locator: ForwardUniversePrecommitLocator,
    store: CollectorDeltaStore,
    spec: CampaignInceptionSourceSpec,
) -> tuple[dict[str, object], dict[str, object]]:
    manifest, witness = inception_module._resolve_precommit(locator)
    precommit = inception_module._precommit_payload(manifest, witness)
    gate_binding = inception_module._gate_binding_sha256(
        precommit=precommit,
        spec=spec,
    )
    prepared = inception_module._CANONICAL_PRESTART_PREPARER(
        store,
        expected_store_path=spec.expected_store_path,
        expected_source_id=spec.source_id,
        expected_run_id=spec.run_id,
        expected_stream_epoch=spec.stream_epoch,
        anchor_at=spec.anchor_at,
        interval_seconds=spec.interval_seconds,
        max_items=spec.max_items,
        evaluation_start_slot_ordinal=spec.evaluation_start_slot_ordinal,
        evaluation_end_slot_ordinal=spec.evaluation_end_slot_ordinal,
        gate_binding_sha256=gate_binding,
    )
    payload = inception_module._new_state_payload(
        precommit=precommit,
        spec=spec,
        prepared=prepared,
    )
    return precommit, payload


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    (
        ("schedule_id", "9" * 64),
        ("anchor_at", "2100-01-01T06:00:01+00:00"),
        ("interval_seconds", "11.0"),
        ("max_items", 251),
        ("next_due_at", "2100-01-01T06:00:01+00:00"),
        ("schema_version", True),
        ("next_slot_ordinal", False),
        ("evaluation_start_slot_ordinal", False),
        ("evaluation_end_slot_ordinal", True),
    ),
)
def test_validate_state_rejects_semantically_rehashed_prepared_schedule_tamper(
    tmp_path: Path,
    field_name: str,
    replacement: object,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    precommit, payload = _pristine_inception_state_payload(
        locator=locator,
        store=store,
        spec=spec,
    )
    prepared = dict(payload["prepared_schedule"])
    prepared[field_name] = replacement
    prestart_material = dict(prepared)
    prestart_material.pop("prestart_sha256")
    prepared["prestart_sha256"] = inception_module._digest(prestart_material)
    payload["prepared_schedule"] = prepared
    payload["semantic_binding_sha256"] = inception_module._semantic_binding_sha256(
        precommit=precommit,
        spec=spec,
        prepared=prepared,
    )

    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="prepared schedule does not match exact inception source specification",
    ):
        inception_module._validate_state(
            payload,
            precommit=precommit,
            spec=spec,
        )


def test_validate_state_rejects_rehashed_wrong_prestart_digest(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    precommit, payload = _pristine_inception_state_payload(
        locator=locator,
        store=store,
        spec=spec,
    )
    prepared = dict(payload["prepared_schedule"])
    prepared["prestart_sha256"] = "a" * 64
    payload["prepared_schedule"] = prepared
    payload["semantic_binding_sha256"] = inception_module._semantic_binding_sha256(
        precommit=precommit,
        spec=spec,
        prepared=prepared,
    )

    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="prestart digest mismatch",
    ):
        inception_module._validate_state(
            payload,
            precommit=precommit,
            spec=spec,
        )


def test_validate_state_rejects_boolean_alias_inside_source_spec(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    precommit, payload = _pristine_inception_state_payload(
        locator=locator,
        store=store,
        spec=spec,
    )
    source_spec_payload = dict(payload["source_spec"])
    assert source_spec_payload["evaluation_end_slot_ordinal"] == 1
    source_spec_payload["evaluation_end_slot_ordinal"] = True
    payload["source_spec"] = source_spec_payload

    with pytest.raises(
        CampaignInceptionConflictError,
        match="different collector source/run",
    ):
        inception_module._validate_state(
            payload,
            precommit=precommit,
            spec=spec,
        )


def test_validate_state_rejects_boolean_top_level_schema_version(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    precommit, payload = _pristine_inception_state_payload(
        locator=locator,
        store=store,
        spec=spec,
    )
    payload["schema_version"] = True

    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="state schema is noncanonical",
    ):
        inception_module._validate_state(
            payload,
            precommit=precommit,
            spec=spec,
        )


def _stage_pending_inception(
    *,
    locator: ForwardUniversePrecommitLocator,
    store: CollectorDeltaStore,
    spec: CampaignInceptionSourceSpec,
    publish_local_state: bool,
) -> None:
    """Model a process crash after durable PREPARE without rebinding product code."""

    manifest, witness = inception_module._resolve_precommit(locator)
    inception_module._validate_schedule_window(manifest, spec)
    precommit = inception_module._precommit_payload(manifest, witness)
    gate_binding = inception_module._gate_binding_sha256(
        precommit=precommit,
        spec=spec,
    )
    prepared = inception_module._CANONICAL_PRESTART_PREPARER(
        store,
        expected_store_path=spec.expected_store_path,
        expected_source_id=spec.source_id,
        expected_run_id=spec.run_id,
        expected_stream_epoch=spec.stream_epoch,
        anchor_at=spec.anchor_at,
        interval_seconds=spec.interval_seconds,
        max_items=spec.max_items,
        evaluation_start_slot_ordinal=spec.evaluation_start_slot_ordinal,
        evaluation_end_slot_ordinal=spec.evaluation_end_slot_ordinal,
        gate_binding_sha256=gate_binding,
    )
    payload = inception_module._new_state_payload(
        precommit=precommit,
        spec=spec,
        prepared=prepared,
    )
    tx_id, semantic, _prepared_payload = inception_module._validate_state(
        payload,
        precommit=precommit,
        spec=spec,
    )
    state_sha256 = inception_module._digest(payload)
    authority = inception_module._authority(
        locator=locator,
        witness=witness,
        campaign_id=manifest.campaign_id,
    )
    authority.prepare(
        tx_id=tx_id,
        observed_state_sha256=None,
        intended_state_sha256=state_sha256,
        semantic_binding_sha256=semantic,
    )
    if publish_local_state:
        inception_module._write_state(
            inception_module._state_path(locator.workspace, manifest.campaign_id),
            payload,
        )


def test_gate_authorization_is_exact_committed_authority_record(
    tmp_path: Path,
) -> None:
    manifest, locator, store, spec, authority_root = _setup(tmp_path)

    receipt = establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )

    assert receipt.evaluation_universe_sha256 == manifest.evaluation_universe_sha256
    assert receipt.source_snapshot_sha256 == manifest.source_snapshot_sha256
    assert receipt.observation_not_before == manifest.observation_not_before
    assert receipt.observation_not_after == manifest.observation_not_after
    authority = _inception_authority(
        locator=locator,
        receipt=receipt,
        authority_root=authority_root,
    )
    history = authority.read_history()
    assert history
    assert history[-1].phase is AuthorityPhase.COMMIT
    assert history[-1].record_sha256 == receipt.authority_record_sha256
    assert history[-1].intended_state_sha256 == receipt.receipt_sha256

    status = store._collector_schedule_start_gate_status(
        source_id=spec.source_id,
        run_id=spec.run_id,
    )
    assert status is not None
    assert status["authorization_sha256"] == history[-1].record_sha256

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
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    _stage_pending_inception(
        locator=locator,
        store=store,
        spec=spec,
        publish_local_state=False,
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
) -> None:
    _manifest_value, locator, store, spec, authority_root = _setup(tmp_path)
    _stage_pending_inception(
        locator=locator,
        store=store,
        spec=spec,
        publish_local_state=True,
    )

    assert _state_file(locator).is_file()
    status = store._collector_schedule_start_gate_status(
        source_id=spec.source_id,
        run_id=spec.run_id,
    )
    assert status is not None
    assert status["authorization_sha256"] is None

    pending_authority = MonotonicWorkspaceAuthority(
        workspace=locator.workspace,
        domain=AUTHORITY_DOMAIN,
        key=_manifest().campaign_id,
        authority_root=authority_root,
    )
    assert pending_authority.read_history()[-1].phase is AuthorityPhase.PREPARE

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
    assert pending_authority.read_history()[-1].phase is AuthorityPhase.COMMIT

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


def test_unrepresentable_schedule_window_is_rejected_before_gate(
    tmp_path: Path,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    oversized = replace(spec, evaluation_end_slot_ordinal=10**10_000)

    with pytest.raises(
        CampaignInceptionConflictError,
        match="representable prospective window",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=oversized,
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


@pytest.mark.parametrize(
    "alias_name",
    (
        "_CANONICAL_WITNESS_RESOLVER",
        "_CANONICAL_MANIFEST_LOADER",
        "_CANONICAL_PRESTART_PREPARER",
        "_CANONICAL_NEXT_SLOT",
        "_CANONICAL_GATE_STATUS",
        "_CANONICAL_GATE_AUTHORIZE",
        "_CANONICAL_SCHEDULE_ID",
        "_CANONICAL_SCHEDULE_DUE_AT",
        "_CANONICAL_STATE_READ_OPEN",
    ),
)
def test_inception_rejects_canonical_alias_rebind_before_hostile_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias_name: str,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    original = getattr(inception_module, alias_name)
    calls: list[str] = []

    def hostile(*args: object, **kwargs: object):
        calls.append(alias_name)
        return original(*args, **kwargs)

    monkeypatch.setattr(inception_module, alias_name, hostile)
    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="canonical dispatch authority is rebound",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
        )

    assert calls == []
    assert (
        store._collector_schedule_start_gate_status(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
        is None
    )


@pytest.mark.parametrize(
    "alias_name",
    (
        "_CANONICAL_PATH_EQUALITY",
        "_CANONICAL_PATH_FSPATH",
        "_CANONICAL_OS_FSPATH",
        "_CANONICAL_ABSPATH",
    ),
)
def test_inception_rejects_path_alias_rebind_before_prestart_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias_name: str,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    original = getattr(inception_module, alias_name)
    calls: list[str] = []

    def hostile(*args: object, **kwargs: object):
        calls.append(alias_name)
        return original(*args, **kwargs)

    monkeypatch.setattr(inception_module, alias_name, hostile)
    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="path dispatch authority is rebound or mutated",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
        )

    assert calls == []
    assert (
        store._collector_schedule_start_gate_status(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
        is None
    )


@pytest.mark.parametrize("name", ("stat", "fstat", "read"))
def test_inception_rejects_state_read_os_dispatch_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    original = getattr(inception_module.os, name)
    calls: list[str] = []

    def hostile(*args: object, **kwargs: object):
        calls.append(name)
        return original(*args, **kwargs)

    monkeypatch.setattr(inception_module.os, name, hostile)
    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="filesystem dispatch authority is rebound",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
        )

    assert calls == []


def test_inception_rejects_state_file_type_dispatch_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    original = inception_module.stat.S_ISREG
    calls: list[int] = []

    def hostile(mode: int) -> bool:
        calls.append(mode)
        return original(mode)

    monkeypatch.setattr(inception_module.stat, "S_ISREG", hostile)
    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="file-type dispatch authority is rebound",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
        )

    assert calls == []


def test_inception_rejects_os_fspath_rebind_before_path_canonicalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    original = inception_module.os.fspath
    hostile_calls: list[object] = []

    def hostile(value: object) -> str:
        hostile_calls.append(value)
        return original(value)

    monkeypatch.setattr(inception_module.os, "fspath", hostile)
    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="filesystem dispatch authority is rebound",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
        )

    assert hostile_calls == []
    assert (
        store._collector_schedule_start_gate_status(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
        is None
    )


@pytest.mark.parametrize(
    "method_name",
    ("__str__", "__truediv__", "mkdir", "unlink"),
)
def test_inception_rejects_concrete_path_dispatch_rebind_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    concrete_path_type = inception_module._CANONICAL_CONCRETE_PATH_TYPE
    original = getattr(concrete_path_type, method_name)
    calls: list[str] = []

    def hostile(*args: object, **kwargs: object):
        calls.append(method_name)
        return original(*args, **kwargs)

    monkeypatch.setattr(concrete_path_type, method_name, hostile)
    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="dynamic method authority drifted",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
        )

    assert calls == []


def test_inception_rejects_path_equality_rebind_before_prestart_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    had_own_equality = "__eq__" in vars(Path)
    original_own_equality = vars(Path).get("__eq__")
    hostile_calls: list[bool] = []

    def hostile_equality(_left: object, _right: object) -> bool:
        hostile_calls.append(True)
        return True

    setattr(Path, "__eq__", hostile_equality)
    try:
        with pytest.raises(
            CampaignInceptionIntegrityError,
            match=r"dynamic method authority drifted: Path\.__eq__",
        ):
            establish_campaign_inception(
                precommit_locator=locator,
                store=store,
                source_spec=spec,
            )
    finally:
        if had_own_equality:
            setattr(Path, "__eq__", original_own_equality)
        else:
            delattr(Path, "__eq__")

    assert hostile_calls == []
    assert (
        store._collector_schedule_start_gate_status(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
        is None
    )


@pytest.mark.parametrize(
    "helper_name",
    (
        "_resolve_precommit",
        "_write_state",
        "_prepared_payload_from_existing_gate",
        "_resolve_gate_and_authorize",
        "_issue_receipt",
    ),
)
def test_inception_rejects_helper_rebind_before_state_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    _manifest_value, locator, store, spec, _authority_root = _setup(tmp_path)
    original = getattr(inception_module, helper_name)
    calls: list[str] = []

    def hostile(*args: object, **kwargs: object):
        calls.append(helper_name)
        return original(*args, **kwargs)

    monkeypatch.setattr(inception_module, helper_name, hostile)
    with pytest.raises(
        CampaignInceptionIntegrityError,
        match="helper dispatch authority is rebound",
    ):
        establish_campaign_inception(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
        )

    assert calls == []
    assert (
        store._collector_schedule_start_gate_status(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
        is None
    )
