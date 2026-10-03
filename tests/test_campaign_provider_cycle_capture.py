from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import autosport.campaign_forward_universe_cycle_binding as binding_module
import autosport.campaign_provider_cycle_capture as capture_module
import autosport.provider_observation_authority as provider_module
from autosport.campaign_inception import (
    CampaignInceptionReceipt,
    CampaignInceptionSourceSpec,
    campaign_evaluation_plan_sha256,
    establish_campaign_inception,
)
from autosport.campaign_precommit_manifest import (
    CampaignPrecommitManifest,
    publish_campaign_precommit_manifest,
)
from autosport.campaign_provider_cycle_capture import (
    ARTIFACT_KIND,
    CampaignCompleteBoardCycleReceipt,
    CampaignProviderCycleCaptureIntegrityError,
    capture_campaign_complete_game_board,
)
from autosport.causal_collector import CollectorDeltaStore
from autosport.campaign_forward_universe_cycle_binding import (
    CampaignForwardEvidenceVerification,
    CampaignForwardUniverseCycleBindingError,
    resolve_campaign_forward_universe_cycle_authority,
    verify_campaign_forward_evidence,
)
from autosport.evaluation_universe import (
    AttritionReason,
    EvaluationRow,
    EvaluationUniverseLedger,
    FunnelStage,
    SlotState,
)
from autosport.forward_evaluation_universe_binding import (
    FORWARD_UNIVERSE_RULE_ID,
    FORWARD_UNIVERSE_RULE_SHA256,
    resolve_forward_universe_members,
)
from autosport.forward_evidence_completeness import (
    AuthoritativeSourceReceipt,
    CampaignCloseEnvelope,
    CampaignEvidence,
    CostEvidence,
    DecisionState,
    ForwardEvidenceProtocolEnvelope,
    ForwardOpportunityEnvelope,
    RevealBoundaryReceipt,
    UniverseResult,
    VerificationCode,
    build_cohort_root,
    verify_campaign,
)
from autosport.provider_evaluation_universe import (
    ProviderEvaluationUniverseStore,
    build_frozen_universe_from_complete_game_board,
    complete_game_board_member_specs,
)
from autosport.forward_universe_precommit_authority import (
    ForwardUniversePrecommitLocator,
)
from autosport.provider_observation_authority import (
    CompleteGameBoardEvidenceStore,
    CompleteGameBoardRequest,
    ProviderObservationUnsupportedError,
)


A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64
ZERO = "0" * 64
ONE = "1" * 64
CAPTURED_AT = "2100-01-01T06:00:00.500000Z"
PROTOCOL_SHA = "9" * 64
FRESHNESS_SHA = "8" * 64


@pytest.fixture(autouse=True)
def _private_provider_acquisition_origin():
    with provider_module._test_acquisition_origin(
        _capability=provider_module._TEST_ACQUISITION_CAPABILITY,
    ):
        with capture_module._test_campaign_clock_origin(
            _capability=capture_module._TEST_CAMPAIGN_CLOCK_CAPABILITY,
        ):
            yield


class _FakeSseResponse:
    def __init__(self, frame: dict[str, object]) -> None:
        self.status = 200
        self.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        encoded = json.dumps(
            frame,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self._lines = [
            b"event: initial_state\n",
            b"data: " + encoded + b"\n",
            b"\n",
        ]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def __iter__(self):
        return iter(self._lines)


def _frame() -> dict[str, object]:
    return {
        "type": "initial_state",
        "sport_key": "table_tennis",
        "snapshot_scope": "current_game_board",
        "snapshot_complete": True,
        "truncated": False,
        "resume_mode": "replace",
        "partial": False,
        "missing_books": [],
        "truncated_books": [],
        "snapshot_partial_reasons": [],
        "count": 3,
        "timestamp": 4102466400,
        "data": [
            {
                "event_id": "event-1",
                "bookmaker": "bovada",
                "kind": "game",
                "market_key": "h2h",
                "home_ml": -110,
                "away_ml": 105,
                "last_update": "2100-01-01T05:59:55Z",
            },
            {
                "event_id": "event-1",
                "bookmaker": "bovada",
                "kind": "game",
                "market_key": "spreads",
                "line": -1.5,
                "home_price": 120,
                "away_price": -125,
                "last_update": "2100-01-01T05:59:55Z",
            },
            {
                "event_id": "event-1",
                "bookmaker": "tenbet",
                "kind": "game",
                "market_key": "totals",
                "line": 74.5,
                "over_price": -105,
                "under_price": -110,
                "last_update": "2100-01-01T05:59:54Z",
            },
        ],
    }


def _empty_frame() -> dict[str, object]:
    return {
        "type": "initial_state",
        "sport_key": "table_tennis",
        "snapshot_scope": "current_game_board",
        "snapshot_complete": True,
        "truncated": False,
        "resume_mode": "replace",
        "partial": False,
        "missing_books": [],
        "truncated_books": [],
        "snapshot_partial_reasons": [],
        "count": 0,
        "timestamp": 4102466400,
        "data": [],
    }


def _request() -> CompleteGameBoardRequest:
    return CompleteGameBoardRequest(
        sport_key="table_tennis",
        bookmakers=("tenbet", "bovada"),
        max_age_s=600,
    )


def _manifest(*, evaluation_plan_sha256: str) -> CampaignPrecommitManifest:
    return CampaignPrecommitManifest(
        campaign_id="campaign-cycle-capture-test",
        source_id="parlayapi:table_tennis",
        source_snapshot_sha256=A,
        committed_at="2099-12-31T19:00:00Z",
        observation_not_before="2100-01-01T06:00:00Z",
        observation_not_after="2100-01-08T06:00:00Z",
        evaluation_universe_sha256=evaluation_plan_sha256,
        strategy_version_id="strategy-v17",
        champion_version_id="model-v42",
        baseline_version_id="market-baseline-v3",
        cost_contract_sha256=C,
        multiplicity_policy_sha256=D,
        stopping_policy_sha256=E,
        restart_policy_sha256=F,
        causal_evidence_policy_sha256=ZERO,
        config_sha256=ONE,
    )


def _setup(tmp_path: Path):
    store_path = tmp_path / "collector.db"
    store = CollectorDeltaStore(store_path)
    spec = CampaignInceptionSourceSpec(
        expected_store_path=store_path,
        source_id="parlayapi:table_tennis",
        run_id="run-1",
        stream_epoch="complete-board-epoch-1",
        anchor_at="2100-01-01T06:00:00+00:00",
        interval_seconds=10,
        max_items=250,
        evaluation_start_slot_ordinal=0,
        evaluation_end_slot_ordinal=1,
    )
    workspace = tmp_path / "workspace"
    evidence = workspace / "evidence"
    evidence.mkdir(parents=True)
    manifest_path = evidence / "precommit.json"
    authority_root = tmp_path / "machine-authority"
    publish_campaign_precommit_manifest(
        manifest_path,
        _manifest(
            evaluation_plan_sha256=campaign_evaluation_plan_sha256(spec),
        ),
        workspace=workspace,
        authority_root=authority_root,
    )
    locator = ForwardUniversePrecommitLocator(
        manifest_path=manifest_path,
        workspace=workspace,
        authority_root=authority_root,
    )
    provider_store = CompleteGameBoardEvidenceStore(
        workspace,
        authority_root=authority_root,
    )
    return locator, store, spec, provider_store


def _clock():
    values = iter(
        [
            "2100-01-01T06:00:00+00:00",
            "2100-01-01T06:00:01+00:00",
            "2100-01-01T06:00:02+00:00",
        ]
    )
    return lambda: next(values)


def test_provider_io_occurs_only_after_authorized_scheduled_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[int] = []

    def fake_urlopen(request, timeout):
        del request, timeout
        evidence = store.collector_cycle_evidence(
            source_id=spec.source_id,
            start_cycle_seq=1,
            end_cycle_seq=1,
        )
        assert len(evidence) == 1
        assert evidence[0]["run_id"] == spec.run_id
        assert evidence[0]["terminal"] is None
        gate = store._collector_schedule_start_gate_status(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
        assert gate is not None
        assert gate["authorization_sha256"] is not None
        provider_calls.append(1)
        return _FakeSseResponse(_frame())

    monkeypatch.setattr(provider_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)

    snapshot, receipt = capture_campaign_complete_game_board(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
        evidence_store=provider_store,
        request=_request(),
        api_key="secret-value",
        timeout_seconds=3.0,
        clock=_clock(),
    )

    assert provider_calls == [1]
    assert type(receipt) is CampaignCompleteBoardCycleReceipt
    assert receipt.provider_evidence_sha256 == snapshot.evidence_sha256
    assert receipt.provider_frame_sha256 == snapshot.frame_sha256
    assert receipt.source_id == spec.source_id
    assert receipt.run_id == spec.run_id
    assert receipt.cycle_seq == 1
    assert receipt.slot_ordinal == 0
    assert len(receipt.gate_binding_sha256) == 64
    assert receipt.artifact_kind == ARTIFACT_KIND
    assert len(receipt.collector_artifact_evidence_sha256) == 64
    assert len(receipt.receipt_sha256) == 64

    exact = store.collector_cycle_observation_artifact_evidence(
        source_id=spec.source_id,
        cycle_seq=receipt.cycle_seq,
        artifact_kind=ARTIFACT_KIND,
        artifact_sha256=snapshot.evidence_sha256,
    )
    assert exact["evidence_sha256"] == receipt.collector_artifact_evidence_sha256
    assert exact["authorization_sha256"] == receipt.campaign_authority_record_sha256
    assert exact["source_id"] == spec.source_id
    assert exact["run_id"] == spec.run_id
    assert exact["stream_epoch"] == spec.stream_epoch
    assert exact["cycle_seq"] == receipt.cycle_seq
    assert exact["slot_ordinal"] == receipt.slot_ordinal
    assert exact["attempted_at"] == "2100-01-01T06:00:00+00:00"
    assert exact["completed_at"] == "2100-01-01T06:00:01+00:00"
    assert exact["artifact_id"] == receipt.artifact_id




def test_campaign_capture_rejects_provider_store_from_other_workspace_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []

    def hostile_urlopen(*_args, **_kwargs):
        provider_calls.append("provider")
        raise AssertionError("provider I/O executed for cross-workspace evidence store")

    monkeypatch.setattr(provider_module, "urlopen", hostile_urlopen)
    alternate = CompleteGameBoardEvidenceStore(
        tmp_path / "other-workspace",
        authority_root=provider_store.authority_root,
    )
    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="workspace does not match campaign precommit workspace",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=alternate,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )
    assert provider_calls == []


def test_campaign_capture_rejects_provider_store_from_other_authority_root_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []

    def hostile_urlopen(*_args, **_kwargs):
        provider_calls.append("provider")
        raise AssertionError("provider I/O executed for cross-authority evidence store")

    monkeypatch.setattr(provider_module, "urlopen", hostile_urlopen)
    alternate = CompleteGameBoardEvidenceStore(
        provider_store.workspace,
        authority_root=tmp_path / "other-machine-authority",
    )
    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="authority root does not match campaign precommit authority",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=alternate,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )
    assert provider_calls == []


def test_campaign_capture_rejects_mutated_provider_evidence_root_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []

    def hostile_urlopen(*_args, **_kwargs):
        provider_calls.append("provider")
        raise AssertionError("provider I/O executed for redirected evidence root")

    monkeypatch.setattr(provider_module, "urlopen", hostile_urlopen)
    provider_store.root = tmp_path / "redirected-provider-evidence"
    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="root does not match canonical campaign workspace",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )
    assert provider_calls == []


def test_durable_resolver_rejects_cross_workspace_provider_evidence_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, timeout: _FakeSseResponse(_empty_frame()),
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)
    _snapshot, cycle_receipt = capture_campaign_complete_game_board(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
        evidence_store=provider_store,
        request=_request(),
        api_key="secret-value",
        timeout_seconds=3.0,
        clock=_clock(),
    )

    alternate = CompleteGameBoardEvidenceStore(
        tmp_path / "other-provider-workspace",
        authority_root=provider_store.authority_root,
    )
    universe_store = ProviderEvaluationUniverseStore(
        tmp_path / "unused-provider-universe-workspace",
        authority_id="unused-provider-universe",
        source_id=spec.source_id,
        authority_root=tmp_path / "unused-provider-universe-authority",
    )
    protocol = ForwardEvidenceProtocolEnvelope(
        campaign_id="campaign-cycle-capture-test",
        scientific_protocol_sha256=PROTOCOL_SHA,
        candidate_universe_rule_id=FORWARD_UNIVERSE_RULE_ID,
        candidate_universe_rule_sha256=FORWARD_UNIVERSE_RULE_SHA256,
        forward_evaluation_policy_sha256="7" * 64,
        runtime_identity_sha256="6" * 64,
        baseline_set_sha256="5" * 64,
        protective_metric_set_sha256="4" * 64,
        cost_policy_sha256="3" * 64,
        precommit_anchor_lower=datetime(2099, 12, 31, 19, 0, tzinfo=timezone.utc),
        precommit_anchor_upper=datetime(2099, 12, 31, 19, 30, tzinfo=timezone.utc),
    )
    with pytest.raises(
        CampaignForwardUniverseCycleBindingError,
        match="provider evidence routing does not match campaign precommit authority",
    ):
        resolve_campaign_forward_universe_cycle_authority(
            precommit_locator=locator,
            collector_store=store,
            source_spec=spec,
            cycle_receipt=cycle_receipt,
            provider_evidence_store=alternate,
            universe_store=universe_store,
            protocol=protocol,
            event_lifecycle=None,
        )


def test_durable_imported_provider_scope_guard_rejects_coordinated_source_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, _store, _spec, provider_store = _setup(tmp_path)
    alternate = CompleteGameBoardEvidenceStore(
        tmp_path / "coordinated-foreign-provider-workspace",
        authority_root=provider_store.authority_root,
    )
    hostile_calls: list[str] = []

    def forged_path_equality(_left, _right):
        hostile_calls.append("path-equality")
        return True

    def forged_path_join(_left, _right):
        hostile_calls.append("path-join")
        return alternate.root

    def forged_state_reader(_instance, _name):
        hostile_calls.append("state-reader")
        return alternate.__dict__

    class ForgedLocator:
        pass

    class ForgedEvidenceStore:
        DIRECTORY = CompleteGameBoardEvidenceStore.DIRECTORY

    monkeypatch.setattr(
        capture_module,
        "_CANONICAL_PATH_EQUALITY",
        forged_path_equality,
    )
    monkeypatch.setattr(
        capture_module,
        "_CANONICAL_PATH_EQUALITY_CODE",
        forged_path_equality.__code__,
    )
    monkeypatch.setattr(
        capture_module,
        "_CANONICAL_PATH_JOIN",
        forged_path_join,
    )
    monkeypatch.setattr(
        capture_module,
        "_CANONICAL_PATH_JOIN_CODE",
        forged_path_join.__code__,
    )
    monkeypatch.setattr(
        capture_module,
        "_CANONICAL_OBJECT_GETATTRIBUTE",
        forged_state_reader,
    )
    monkeypatch.setattr(
        capture_module,
        "ForwardUniversePrecommitLocator",
        ForgedLocator,
    )
    monkeypatch.setattr(
        capture_module,
        "CompleteGameBoardEvidenceStore",
        ForgedEvidenceStore,
    )
    monkeypatch.setattr(
        capture_module,
        "_PRECOMMIT_ROUTING_SEAMS",
        {},
    )
    monkeypatch.setattr(
        capture_module,
        "_PRECOMMIT_ROUTING_SEAM_ITEMS",
        (),
    )
    monkeypatch.setattr(
        capture_module,
        "_EVIDENCE_DIRECTORY_SURFACE",
        "redirected-provider-directory",
    )
    monkeypatch.setattr(
        capture_module,
        "_EVIDENCE_DIRECTORY",
        "redirected-provider-directory",
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="workspace does not match campaign precommit workspace",
    ):
        binding_module._PROVIDER_EVIDENCE_SCOPE(
            locator,
            alternate,
        )

    assert hostile_calls == []


def test_durable_resolver_scope_guard_survives_coordinated_source_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_empty_frame()),
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)
    _snapshot, cycle_receipt = capture_campaign_complete_game_board(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
        evidence_store=provider_store,
        request=_request(),
        api_key="secret-value",
        timeout_seconds=3.0,
        clock=_clock(),
    )

    alternate = CompleteGameBoardEvidenceStore(
        tmp_path / "coordinated-resolver-foreign-provider-workspace",
        authority_root=provider_store.authority_root,
    )
    universe_store = ProviderEvaluationUniverseStore(
        tmp_path / "coordinated-resolver-unused-provider-universe-workspace",
        authority_id="coordinated-resolver-unused-provider-universe",
        source_id=spec.source_id,
        authority_root=tmp_path / "coordinated-resolver-unused-provider-universe-authority",
    )
    protocol = ForwardEvidenceProtocolEnvelope(
        campaign_id="campaign-cycle-capture-test",
        scientific_protocol_sha256=PROTOCOL_SHA,
        candidate_universe_rule_id=FORWARD_UNIVERSE_RULE_ID,
        candidate_universe_rule_sha256=FORWARD_UNIVERSE_RULE_SHA256,
        forward_evaluation_policy_sha256="7" * 64,
        runtime_identity_sha256="6" * 64,
        baseline_set_sha256="5" * 64,
        protective_metric_set_sha256="4" * 64,
        cost_policy_sha256="3" * 64,
        precommit_anchor_lower=datetime(2099, 12, 31, 19, 0, tzinfo=timezone.utc),
        precommit_anchor_upper=datetime(2099, 12, 31, 19, 30, tzinfo=timezone.utc),
    )
    hostile_calls: list[str] = []

    def forged_path_equality(_left, _right):
        hostile_calls.append("path-equality")
        return True

    def forged_path_join(_left, _right):
        hostile_calls.append("path-join")
        return alternate.root

    monkeypatch.setattr(
        capture_module,
        "_CANONICAL_PATH_EQUALITY",
        forged_path_equality,
    )
    monkeypatch.setattr(
        capture_module,
        "_CANONICAL_PATH_EQUALITY_CODE",
        forged_path_equality.__code__,
    )
    monkeypatch.setattr(
        capture_module,
        "_CANONICAL_PATH_JOIN",
        forged_path_join,
    )
    monkeypatch.setattr(
        capture_module,
        "_CANONICAL_PATH_JOIN_CODE",
        forged_path_join.__code__,
    )

    with pytest.raises(
        CampaignForwardUniverseCycleBindingError,
        match="provider evidence routing does not match campaign precommit authority",
    ):
        resolve_campaign_forward_universe_cycle_authority(
            precommit_locator=locator,
            collector_store=store,
            source_spec=spec,
            cycle_receipt=cycle_receipt,
            provider_evidence_store=alternate,
            universe_store=universe_store,
            protocol=protocol,
            event_lifecycle=None,
        )

    assert hostile_calls == []


def test_public_durable_resolver_rejects_scope_guard_rebind_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile_calls: list[str] = []

    class ForgedScopeError(RuntimeError):
        pass

    def hostile_scope_guard(_locator, _store):
        hostile_calls.append("scope")
        return None

    monkeypatch.setattr(
        binding_module,
        "_PROVIDER_EVIDENCE_SCOPE",
        hostile_scope_guard,
    )
    monkeypatch.setattr(
        binding_module,
        "_PROVIDER_EVIDENCE_SCOPE_ERROR",
        ForgedScopeError,
    )
    monkeypatch.setattr(
        binding_module,
        "CampaignProviderCycleCaptureIntegrityError",
        ForgedScopeError,
    )

    with pytest.raises(
        CampaignForwardUniverseCycleBindingError,
        match="campaign forward-cycle authority witness globals changed",
    ):
        resolve_campaign_forward_universe_cycle_authority(
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            protocol=None,
            event_lifecycle=None,
        )

    assert hostile_calls == []


def test_first_clock_cannot_redirect_provider_evidence_authority_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []
    first = True

    def hostile_clock():
        nonlocal first
        if first:
            first = False
            provider_store.authority_root = tmp_path / "redirected-machine-authority"
        return "2100-01-01T06:00:00+00:00"

    def hostile_urlopen(*_args, **_kwargs):
        provider_calls.append("provider")
        raise AssertionError("provider I/O executed after trust-root redirect")

    monkeypatch.setattr(provider_module, "urlopen", hostile_urlopen)
    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="authority root does not match campaign precommit authority",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=hostile_clock,
        )
    assert provider_calls == []


def test_provider_callback_cannot_redirect_evidence_root_before_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    original_root = provider_store.root

    def fake_urlopen(request, timeout):
        del request, timeout
        provider_store.root = tmp_path / "redirected-after-provider-io"
        return _FakeSseResponse(_frame())

    monkeypatch.setattr(provider_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)
    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="root does not match canonical campaign workspace",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )
    assert not original_root.exists()


def test_campaign_capture_rejects_in_place_precommit_routing_witness_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []

    def hostile_urlopen(*_args, **_kwargs):
        provider_calls.append("provider")
        raise AssertionError("provider I/O executed with mutated routing witnesses")

    monkeypatch.setattr(provider_module, "urlopen", hostile_urlopen)
    original_workspace = capture_module._PRECOMMIT_ROUTING_SEAMS["workspace"]
    capture_module._PRECOMMIT_ROUTING_SEAMS["workspace"] = (
        capture_module._PRECOMMIT_ROUTING_SEAMS["authority_root"]
    )
    try:
        with pytest.raises(
            CampaignProviderCycleCaptureIntegrityError,
            match="campaign provider evidence routing authority changed",
        ):
            capture_campaign_complete_game_board(
                precommit_locator=locator,
                store=store,
                source_spec=spec,
                evidence_store=provider_store,
                request=_request(),
                api_key="secret-value",
                timeout_seconds=3.0,
                clock=_clock(),
            )
    finally:
        capture_module._PRECOMMIT_ROUTING_SEAMS["workspace"] = original_workspace
    assert provider_calls == []


def test_campaign_capture_rejects_evidence_directory_rebind_before_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []

    def hostile_urlopen(*_args, **_kwargs):
        provider_calls.append("provider")
        raise AssertionError("provider I/O executed with rebound evidence directory")

    monkeypatch.setattr(provider_module, "urlopen", hostile_urlopen)
    monkeypatch.setattr(
        CompleteGameBoardEvidenceStore,
        "DIRECTORY",
        "".join(("provider-", "complete-game-board")),
    )
    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="campaign provider evidence routing authority changed",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )
    assert provider_calls == []

def test_campaign_capture_uses_captured_path_equality_after_runtime_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    hostile_calls: list[str] = []

    def fake_urlopen(request, timeout):
        del request, timeout
        return _FakeSseResponse(_frame())

    def hostile_path_equality(_left, _right):
        hostile_calls.append("path-eq")
        return False

    monkeypatch.setattr(provider_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)
    monkeypatch.setattr(Path, "__eq__", hostile_path_equality)

    snapshot, receipt = capture_campaign_complete_game_board(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
        evidence_store=provider_store,
        request=_request(),
        api_key="secret-value",
        timeout_seconds=3.0,
        clock=_clock(),
    )

    assert receipt.provider_evidence_sha256 == snapshot.evidence_sha256
    assert hostile_calls == []

def test_provider_io_rejects_slot_zero_start_at_next_slot_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[int] = []

    def fake_urlopen(request, timeout):
        del request, timeout
        provider_calls.append(1)
        return _FakeSseResponse(_frame())

    monkeypatch.setattr(provider_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="fixed schedule slot window",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=lambda: "2100-01-01T06:00:10+00:00",
        )

    assert provider_calls == []


def test_provider_observation_cannot_cross_next_slot_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[int] = []

    def fake_urlopen(request, timeout):
        del request, timeout
        provider_calls.append(1)
        return _FakeSseResponse(_frame())

    monkeypatch.setattr(provider_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        provider_module,
        "_default_clock",
        lambda: "2100-01-01T06:00:10+00:00",
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="fixed schedule slot window",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=lambda: "2100-01-01T06:00:09+00:00",
        )

    assert provider_calls == [1]
    evidence = store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    )
    assert len(evidence) == 1
    assert evidence[0]["terminal"]["status"] == "LOCAL_FAILURE"


def test_later_successful_capture_cannot_define_forward_universe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )

    slot_zero = store._next_collector_schedule_slot(
        source_id=spec.source_id,
        run_id=spec.run_id,
    )
    assert slot_zero["slot_ordinal"] == 0
    first_cycle = store._begin_scheduled_collector_cycle(
        source_id=spec.source_id,
        run_id=spec.run_id,
        stream_epoch=spec.stream_epoch,
        max_items=spec.max_items,
        slot_ordinal=slot_zero["slot_ordinal"],
        due_at=slot_zero["due_at"],
        attempted_at=slot_zero["due_at"],
    )
    store._finish_collector_cycle(
        source_id=spec.source_id,
        cycle_seq=first_cycle,
        status="LOCAL_FAILURE",
        completed_at=slot_zero["due_at"],
        catalog_changes=(),
        observed_delta_ids=(),
        committed_delta_ids=(),
        duplicate_delta_ids=(),
        error_code="slot-zero-failure",
    )

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_empty_frame()),
    )
    monkeypatch.setattr(
        provider_module,
        "_default_clock",
        lambda: "2100-01-01T06:00:10.500000Z",
    )
    clock_values = iter(
        [
            "2100-01-01T06:00:10+00:00",
            "2100-01-01T06:00:11+00:00",
        ]
    )
    snapshot, cycle_receipt = capture_campaign_complete_game_board(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
        evidence_store=provider_store,
        request=_request(),
        api_key="secret-value",
        timeout_seconds=3.0,
        clock=lambda: next(clock_values),
    )

    assert cycle_receipt.slot_ordinal == 1
    assert cycle_receipt.provider_evidence_sha256 == snapshot.evidence_sha256

    universe_store = ProviderEvaluationUniverseStore(
        tmp_path / "later-slot-universe",
        authority_id="later-slot-authority",
        source_id=spec.source_id,
        authority_root=tmp_path / "later-slot-authority-root",
    )
    protocol = ForwardEvidenceProtocolEnvelope(
        campaign_id="campaign-cycle-capture-test",
        scientific_protocol_sha256=PROTOCOL_SHA,
        candidate_universe_rule_id=FORWARD_UNIVERSE_RULE_ID,
        candidate_universe_rule_sha256=FORWARD_UNIVERSE_RULE_SHA256,
        forward_evaluation_policy_sha256="7" * 64,
        runtime_identity_sha256="6" * 64,
        baseline_set_sha256="5" * 64,
        protective_metric_set_sha256="4" * 64,
        cost_policy_sha256="3" * 64,
        precommit_anchor_lower=datetime(2099, 12, 31, 19, 0, tzinfo=timezone.utc),
        precommit_anchor_upper=datetime(2099, 12, 31, 19, 30, tzinfo=timezone.utc),
    )

    with pytest.raises(
        CampaignForwardUniverseCycleBindingError,
        match="first precommitted evaluation slot",
    ):
        resolve_campaign_forward_universe_cycle_authority(
            precommit_locator=locator,
            collector_store=store,
            source_spec=spec,
            cycle_receipt=cycle_receipt,
            provider_evidence_store=provider_store,
            universe_store=universe_store,
            protocol=protocol,
            event_lifecycle=None,
        )


def test_cycle_bound_structural_verifier_replaces_caller_receipts_and_fixes_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_empty_frame()),
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)

    snapshot, cycle_receipt = capture_campaign_complete_game_board(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
        evidence_store=provider_store,
        request=_request(),
        api_key="secret-value",
        timeout_seconds=3.0,
        clock=_clock(),
    )
    members = complete_game_board_member_specs(snapshot, event_lifecycle=None)
    assert len(members) == 1
    member = members[0]
    row = EvaluationRow(
        row_key=member.row_key,
        campaign_id="campaign-cycle-capture-test",
        research_protocol_id="protocol-1",
        protocol_sha256=PROTOCOL_SHA,
        universe_id="universe-1",
        slot_state=SlotState.NO_EVENT,
        decision_stage=FunnelStage.OBSERVED_SLOT,
        attrition_reason=AttritionReason.NO_EVENT,
        sport=snapshot.request.sport_key,
        provider_id="parlayapi",
        source_id=snapshot.request.source_id,
        event_id=None,
        market_id=None,
        selection_id=None,
        source_at=member.source_at,
        received_at=CAPTURED_AT,
        committed_at=CAPTURED_AT,
        detection_at=None,
        decision_at=None,
        quote_set_sha256=None,
        freshness_policy_sha256=FRESHNESS_SHA,
        strategy_version_id="strategy-v17",
        model_version_id=None,
        config_sha256=ONE,
        portfolio_before_id="portfolio-1",
        economic_goal_id="goal-1",
        risk_policy_id="risk-1",
        terminal_space_proof_id=None,
        settlement_proof_id=None,
        execution_model_id=None,
        execution_run_id=None,
        execution_plan_id=None,
        execution_action_id=None,
        decision_quote_id=None,
        cost_contract_sha256=C,
        outcome_reveal_not_before=None,
        dependence_cluster_keys=("source:table_tennis",),
    )
    universe = build_frozen_universe_from_complete_game_board(
        snapshot=snapshot,
        event_lifecycle=None,
        authority_id="provider-universe-1",
        session_id="session-1",
        universe_id="universe-1",
        campaign_id="campaign-cycle-capture-test",
        research_protocol_id="protocol-1",
        protocol_sha256=PROTOCOL_SHA,
        evaluation_not_before="2100-01-01T06:00:02Z",
        frozen_at="2100-01-01T06:00:03Z",
        rows=(row,),
    )
    universe_store = ProviderEvaluationUniverseStore(
        tmp_path / "provider-universe-workspace",
        authority_id="provider-universe-1",
        source_id=spec.source_id,
        authority_root=tmp_path / "provider-universe-authority",
    )
    universe_store.save(EvaluationUniverseLedger(universe))

    protocol = ForwardEvidenceProtocolEnvelope(
        campaign_id="campaign-cycle-capture-test",
        scientific_protocol_sha256=PROTOCOL_SHA,
        candidate_universe_rule_id=FORWARD_UNIVERSE_RULE_ID,
        candidate_universe_rule_sha256=FORWARD_UNIVERSE_RULE_SHA256,
        forward_evaluation_policy_sha256="7" * 64,
        runtime_identity_sha256="6" * 64,
        baseline_set_sha256="5" * 64,
        protective_metric_set_sha256="4" * 64,
        cost_policy_sha256="3" * 64,
        precommit_anchor_lower=datetime(2099, 12, 31, 19, 0, tzinfo=timezone.utc),
        precommit_anchor_upper=datetime(2099, 12, 31, 19, 30, tzinfo=timezone.utc),
    )
    expectations = resolve_forward_universe_members(
        store=universe_store,
        protocol=protocol,
        precommit=locator,
    )
    assert len(expectations) == 1
    expected = expectations[0]
    opportunity = ForwardOpportunityEnvelope(
        campaign_id=protocol.campaign_id,
        protocol_sha256=protocol.protocol_sha256,
        candidate_sequence=1,
        opportunity_id=expected.opportunity_id,
        source_receipt_id=expected.source_receipt_id,
        source_receipt_sha256=expected.source_receipt_sha256,
        causal_cutoff=expected.causal_cutoff,
        observed_lower=expected.observed_lower,
        observed_upper=expected.observed_upper,
        universe_rule_result=expected.universe_rule_result,
        universe_rule_reason_code=expected.universe_rule_reason_code,
        provider_acquisition_state=expected.provider_acquisition_state,
        reveal_boundary_receipt_id="boundary-1",
        runtime_identity_sha256=protocol.runtime_identity_sha256,
        predecessor_opportunity_sha256="GENESIS",
        decision_state=DecisionState.NO_BET,
    )
    root = build_cohort_root(
        (opportunity,),
        anchor_lower=datetime(2100, 1, 1, 6, 0, 2, tzinfo=timezone.utc),
        anchor_upper=datetime(2100, 1, 1, 6, 0, 2, 100000, tzinfo=timezone.utc),
    )
    boundary = RevealBoundaryReceipt(
        receipt_id="boundary-1",
        campaign_id=protocol.campaign_id,
        event_or_market_id="empty-board-window",
        provider_or_authority_id="provider-universe-1",
        boundary_rule_id="test-empty-board-boundary-v1",
        source_receipt_id=expected.source_receipt_id,
        source_sha256=expected.source_receipt_sha256,
        boundary_lower=datetime(2100, 1, 1, 6, 0, 10, tzinfo=timezone.utc),
        boundary_upper=datetime(2100, 1, 1, 6, 0, 11, tzinfo=timezone.utc),
        uncertainty_basis="deterministic integration fixture",
    )
    close = CampaignCloseEnvelope(
        campaign_id=protocol.campaign_id,
        protocol_sha256=protocol.protocol_sha256,
        terminal_cohort_root_sha256=root.cohort_root_sha256,
        final_candidate_count=1,
        first_sequence=1,
        last_sequence=1,
        close_reason="integration fixture complete",
        anchor_lower=datetime(2100, 1, 1, 6, 0, 3, tzinfo=timezone.utc),
        anchor_upper=datetime(2100, 1, 1, 6, 0, 3, 100000, tzinfo=timezone.utc),
    )
    forged = AuthoritativeSourceReceipt(
        receipt_id="forged-receipt",
        receipt_sha256="f" * 64,
        campaign_id=protocol.campaign_id,
        opportunity_id="forged-opportunity",
        universe_rule_result=UniverseResult.EXCLUDED,
    )
    evidence = CampaignEvidence(
        protocol=protocol,
        opportunities=(opportunity,),
        cohort_roots=(root,),
        closes=(close,),
        reveal_boundaries=(boundary,),
        authoritative_receipts=(forged,),
        denominator_sequences=(1,),
        cost_evidence=(CostEvidence(1, True),),
    )

    raw = verify_campaign(evidence)
    assert raw.ok is False
    assert VerificationCode.COHORT_OMISSION_DETECTED in raw.codes

    result = verify_campaign_forward_evidence(
        precommit_locator=locator,
        collector_store=store,
        source_spec=spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_store,
        universe_store=universe_store,
        event_lifecycle=None,
        evidence=evidence,
    )

    assert type(result) is CampaignForwardEvidenceVerification
    assert result.structural_ok is True
    assert result.structural_codes == ("PASS",)
    assert result.verification_scope == "CYCLE_BOUND_PROVIDER_UNIVERSE_STRUCTURAL_ONLY"
    assert result.provider_universe_authority_resolved is True
    assert result.promotion_ready is False
    assert result.real_money_ready is False
    assert result.candidate_count == 1
    assert result.prospective_evaluation_plan_sha256 == campaign_evaluation_plan_sha256(spec)
    assert result.universe_sha256 == universe.universe_sha256
    assert result.membership_sha256 == universe.membership_sha256
    assert len(result.campaign_cycle_authority_sha256) == 64
    assert len(result.receipt_sha256) == 64


def test_delayed_collector_start_after_precommit_window_blocks_before_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: provider_calls.append("provider"),
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="START falls outside precommitted campaign observation window",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=lambda: "2100-01-09T06:00:00+00:00",
        )

    assert provider_calls == []
    assert store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    ) == ()


def test_capture_refuses_slot_after_frozen_evaluation_range_before_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    for ordinal in (0, 1):
        slot = store._next_collector_schedule_slot(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
        assert slot["slot_ordinal"] == ordinal
        cycle_seq = store._begin_scheduled_collector_cycle(
            source_id=spec.source_id,
            run_id=spec.run_id,
            stream_epoch=spec.stream_epoch,
            max_items=spec.max_items,
            slot_ordinal=slot["slot_ordinal"],
            due_at=slot["due_at"],
            attempted_at=slot["due_at"],
        )
        store._finish_collector_cycle(
            source_id=spec.source_id,
            cycle_seq=cycle_seq,
            status="LOCAL_FAILURE",
            completed_at=slot["due_at"],
            catalog_changes=(),
            observed_delta_ids=(),
            committed_delta_ids=(),
            duplicate_delta_ids=(),
            error_code="test-slot-retirement",
        )

    provider_calls: list[str] = []
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: provider_calls.append("provider"),
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="next slot does not match inception receipt",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=lambda: "2100-01-01T06:00:20+00:00",
        )

    assert provider_calls == []


def test_provider_observation_cannot_predate_authorized_cycle_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_frame()),
    )
    monkeypatch.setattr(
        provider_module,
        "_default_clock",
        lambda: "2100-01-01T05:59:59.999999Z",
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="predates authorized collector START",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )

    evidence = store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    )
    assert len(evidence) == 1
    terminal = evidence[0]["terminal"]
    assert terminal["status"] == "LOCAL_FAILURE"
    assert "observed_artifacts" not in terminal


def test_provider_observation_cannot_postdate_cycle_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_frame()),
    )
    monkeypatch.setattr(
        provider_module,
        "_default_clock",
        lambda: "2100-01-01T06:00:01.500000Z",
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="falls after collector cycle completion",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )

    evidence = store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    )
    assert len(evidence) == 1
    terminal = evidence[0]["terminal"]
    assert terminal["status"] == "LOCAL_FAILURE"
    assert "observed_artifacts" not in terminal


def test_durable_resolver_rejects_legacy_success_with_post_cycle_provider_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    campaign = establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    slot = store._next_collector_schedule_slot(
        source_id=spec.source_id,
        run_id=spec.run_id,
    )
    cycle_seq = store._begin_scheduled_collector_cycle(
        source_id=spec.source_id,
        run_id=spec.run_id,
        stream_epoch=spec.stream_epoch,
        max_items=spec.max_items,
        slot_ordinal=slot["slot_ordinal"],
        due_at=slot["due_at"],
        attempted_at="2100-01-01T06:00:00+00:00",
    )

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_frame()),
    )
    monkeypatch.setattr(
        provider_module,
        "_default_clock",
        lambda: "2100-01-01T06:00:02Z",
    )
    snapshot = provider_module.capture_parlay_complete_game_board(
        api_key="secret-value",
        request=_request(),
        timeout_seconds=3.0,
    )
    provider_store.save(snapshot)

    store._record_collector_cycle_observation_artifact(
        source_id=spec.source_id,
        cycle_seq=cycle_seq,
        artifact_kind=ARTIFACT_KIND,
        artifact_sha256=snapshot.evidence_sha256,
    )
    store._finish_collector_cycle(
        source_id=spec.source_id,
        cycle_seq=cycle_seq,
        status="SUCCESS",
        completed_at="2100-01-01T06:00:01+00:00",
        catalog_changes=(),
        observed_delta_ids=(),
        committed_delta_ids=(),
        duplicate_delta_ids=(),
    )
    collector_evidence = store.collector_cycle_observation_artifact_evidence(
        source_id=spec.source_id,
        cycle_seq=cycle_seq,
        artifact_kind=ARTIFACT_KIND,
        artifact_sha256=snapshot.evidence_sha256,
    )
    receipt = capture_module._issue_receipt(
        campaign=campaign,
        snapshot=snapshot,
        collector_evidence=collector_evidence,
    )

    universe_store = ProviderEvaluationUniverseStore(
        tmp_path / "workspace",
        authority_id="legacy-chronology-test-authority",
        source_id=spec.source_id,
        authority_root=tmp_path / "machine-authority",
    )
    protocol = ForwardEvidenceProtocolEnvelope(
        campaign_id=campaign.campaign_id,
        scientific_protocol_sha256=A,
        candidate_universe_rule_id="legacy-cycle-chronology",
        candidate_universe_rule_sha256=B,
        forward_evaluation_policy_sha256=C,
        runtime_identity_sha256=D,
        baseline_set_sha256=E,
        protective_metric_set_sha256=F,
        cost_policy_sha256=ZERO,
        precommit_anchor_lower=datetime(2099, 12, 31, tzinfo=timezone.utc),
        precommit_anchor_upper=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(
        CampaignForwardUniverseCycleBindingError,
        match="outside authorized collector cycle chronology",
    ):
        resolve_campaign_forward_universe_cycle_authority(
            precommit_locator=locator,
            collector_store=store,
            source_spec=spec,
            cycle_receipt=receipt,
            provider_evidence_store=provider_store,
            universe_store=universe_store,
            protocol=protocol,
            event_lifecycle=None,
        )


def test_durable_resolver_rejects_legacy_success_completed_after_campaign_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    campaign = establish_campaign_inception(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
    )
    slot = store._next_collector_schedule_slot(
        source_id=spec.source_id,
        run_id=spec.run_id,
    )
    cycle_seq = store._begin_scheduled_collector_cycle(
        source_id=spec.source_id,
        run_id=spec.run_id,
        stream_epoch=spec.stream_epoch,
        max_items=spec.max_items,
        slot_ordinal=slot["slot_ordinal"],
        due_at=slot["due_at"],
        attempted_at="2100-01-01T06:00:00+00:00",
    )

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_frame()),
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)
    snapshot = provider_module.capture_parlay_complete_game_board(
        api_key="secret-value",
        request=_request(),
        timeout_seconds=3.0,
    )
    provider_store.save(snapshot)
    store._record_collector_cycle_observation_artifact(
        source_id=spec.source_id,
        cycle_seq=cycle_seq,
        artifact_kind=ARTIFACT_KIND,
        artifact_sha256=snapshot.evidence_sha256,
    )
    store._finish_collector_cycle(
        source_id=spec.source_id,
        cycle_seq=cycle_seq,
        status="SUCCESS",
        completed_at="2100-01-09T06:00:00+00:00",
        catalog_changes=(),
        observed_delta_ids=(),
        committed_delta_ids=(),
        duplicate_delta_ids=(),
    )
    collector_evidence = store.collector_cycle_observation_artifact_evidence(
        source_id=spec.source_id,
        cycle_seq=cycle_seq,
        artifact_kind=ARTIFACT_KIND,
        artifact_sha256=snapshot.evidence_sha256,
    )
    receipt = capture_module._issue_receipt(
        campaign=campaign,
        snapshot=snapshot,
        collector_evidence=collector_evidence,
    )
    universe_store = ProviderEvaluationUniverseStore(
        tmp_path / "window-workspace",
        authority_id="legacy-window-test-authority",
        source_id=spec.source_id,
        authority_root=tmp_path / "window-machine-authority",
    )
    protocol = ForwardEvidenceProtocolEnvelope(
        campaign_id=campaign.campaign_id,
        scientific_protocol_sha256=A,
        candidate_universe_rule_id="legacy-cycle-window",
        candidate_universe_rule_sha256=B,
        forward_evaluation_policy_sha256=C,
        runtime_identity_sha256=D,
        baseline_set_sha256=E,
        protective_metric_set_sha256=F,
        cost_policy_sha256=ZERO,
        precommit_anchor_lower=datetime(2099, 12, 31, tzinfo=timezone.utc),
        precommit_anchor_upper=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(
        CampaignForwardUniverseCycleBindingError,
        match="outside precommitted campaign observation window",
    ):
        resolve_campaign_forward_universe_cycle_authority(
            precommit_locator=locator,
            collector_store=store,
            source_spec=spec,
            cycle_receipt=receipt,
            provider_evidence_store=provider_store,
            universe_store=universe_store,
            protocol=protocol,
            event_lifecycle=None,
        )


def test_provider_failure_records_terminal_failure_without_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)

    def fail_urlopen(_request, _timeout):
        raise OSError("forced provider transport failure")

    monkeypatch.setattr(provider_module, "urlopen", fail_urlopen)
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)

    with pytest.raises(
        ProviderObservationUnsupportedError,
        match="provider SSE initial-state acquisition failed",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )

    evidence = store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    )
    assert len(evidence) == 1
    terminal = evidence[0]["terminal"]
    assert terminal["status"] == "LOCAL_FAILURE"
    assert terminal["observed_deltas"] == []
    assert "observed_artifacts" not in terminal
    assert terminal["error_code"]
    assert (
        store._next_collector_schedule_slot(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )["slot_ordinal"]
        == 1
    )


def test_provider_failure_clock_regression_still_records_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)

    def fail_urlopen(_request, _timeout):
        raise OSError("forced provider transport failure")

    times = iter(
        [
            "2100-01-01T06:00:00+00:00",
            "2099-12-31T23:59:59+00:00",
        ]
    )
    monkeypatch.setattr(provider_module, "urlopen", fail_urlopen)
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)

    with pytest.raises(
        ProviderObservationUnsupportedError,
        match="provider SSE initial-state acquisition failed",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=lambda: next(times),
        )

    evidence = store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    )
    assert len(evidence) == 1
    terminal = evidence[0]["terminal"]
    assert terminal["status"] == "LOCAL_FAILURE"
    assert terminal["attempted_at"] == "2100-01-01T06:00:00+00:00"
    assert terminal["completed_at"] == terminal["attempted_at"]
    assert "observed_artifacts" not in terminal
    assert (
        store._next_collector_schedule_slot(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )["slot_ordinal"]
        == 1
    )


@pytest.mark.parametrize(
    "failure_clock_value",
    [
        RuntimeError("failure clock unavailable"),
        "not-an-instant",
    ],
)
def test_provider_failure_unusable_cleanup_clock_still_records_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_clock_value: object,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)

    def fail_urlopen(_request, _timeout):
        raise OSError("forced provider transport failure")

    calls = 0

    def cleanup_clock():
        nonlocal calls
        calls += 1
        if calls == 1:
            return "2100-01-01T06:00:00+00:00"
        if isinstance(failure_clock_value, BaseException):
            raise failure_clock_value
        return failure_clock_value

    monkeypatch.setattr(provider_module, "urlopen", fail_urlopen)
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)

    with pytest.raises(
        ProviderObservationUnsupportedError,
        match="provider SSE initial-state acquisition failed",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=cleanup_clock,
        )

    evidence = store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    )
    assert len(evidence) == 1
    terminal = evidence[0]["terminal"]
    assert terminal["status"] == "LOCAL_FAILURE"
    assert terminal["completed_at"] == terminal["attempted_at"]
    assert "observed_artifacts" not in terminal
    assert (
        store._next_collector_schedule_slot(
            source_id=spec.source_id,
            run_id=spec.run_id,
        )["slot_ordinal"]
        == 1
    )


def test_source_mismatch_rejects_before_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    calls: list[str] = []

    def fake_urlopen(_request, _timeout):
        calls.append("provider")
        return _FakeSseResponse(_frame())

    monkeypatch.setattr(provider_module, "urlopen", fake_urlopen)
    wrong_request = CompleteGameBoardRequest(
        sport_key="basketball_nba",
        bookmakers=("bovada",),
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="source_id does not match",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=wrong_request,
            api_key="secret-value",
            clock=_clock(),
        )

    assert calls == []


def test_cycle_receipt_is_not_caller_constructible() -> None:
    with pytest.raises(TypeError, match="resolver-issued"):
        CampaignCompleteBoardCycleReceipt()


def test_cycle_receipt_private_issuer_rejects_wrong_capability() -> None:
    with pytest.raises(TypeError, match="resolver-private"):
        CampaignCompleteBoardCycleReceipt._issue(
            {},
            _issuance_capability=object(),
        )


def test_cycle_receipt_private_issuer_rejects_subclass() -> None:
    class HostileReceipt(CampaignCompleteBoardCycleReceipt):
        pass

    with pytest.raises(TypeError, match="exact canonical class"):
        HostileReceipt._issue(
            {},
            _issuance_capability=capture_module._RECEIPT_ISSUANCE_CAPABILITY,
        )


def test_cycle_receipt_private_issuer_rejects_mapping_subclass() -> None:
    class HostilePayload(dict):
        pass

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="payload is noncanonical",
    ):
        CampaignCompleteBoardCycleReceipt._issue(
            HostilePayload(),
            _issuance_capability=capture_module._RECEIPT_ISSUANCE_CAPABILITY,
        )


def test_cycle_receipt_builder_uses_captured_issuer_only() -> None:
    names = capture_module._issue_receipt.__code__.co_names
    assert "_CANONICAL_CYCLE_RECEIPT_ISSUER_FUNCTION" in names
    assert "_CANONICAL_CYCLE_RECEIPT_CLASS" in names
    assert "_CANONICAL_RECEIPT_ISSUANCE_CAPABILITY" in names
    assert "_issue" not in names


def test_instance_rebound_collector_seam_rejects_before_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    calls: list[str] = []
    store.collector_cycle_observation_artifact_evidence = lambda **_kwargs: {}

    def fake_urlopen(_request, _timeout):
        calls.append("provider")
        return _FakeSseResponse(_frame())

    monkeypatch.setattr(provider_module, "urlopen", fake_urlopen)
    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="instance-rebound",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            clock=_clock(),
        )
    assert calls == []

@pytest.mark.parametrize(
    "alias_name",
    [
        "_CAPTURE",
        "_EVIDENCE_SAVE",
        "_NEXT_SLOT",
        "_BEGIN_SCHEDULED",
        "_RECORD_ARTIFACT",
        "_FINISH_CYCLE",
        "_RESOLVE_ARTIFACT",
        "establish_campaign_inception",
        "_issue_receipt",
    ],
)
def test_module_dispatch_rebind_rejects_before_hostile_or_provider_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias_name: str,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    calls: list[str] = []

    def hostile(*_args, **_kwargs):
        calls.append("hostile")
        raise AssertionError("hostile dispatch executed")

    def fake_urlopen(_request, _timeout):
        calls.append("provider")
        return _FakeSseResponse(_frame())

    monkeypatch.setattr(provider_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(capture_module, alias_name, hostile)
    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="dispatch authority is rebound",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            clock=_clock(),
        )
    assert calls == []


def test_clock_cannot_extend_campaign_window_by_rebinding_receipt_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []
    original = vars(CampaignInceptionReceipt)["observation_not_after"]

    def forged_not_after(_self):
        return "2200-01-01T00:00:00+00:00"

    mutated = False

    def mutating_clock() -> str:
        nonlocal mutated
        if not mutated:
            mutated = True
            type.__setattr__(
                CampaignInceptionReceipt,
                "observation_not_after",
                property(forged_not_after),
            )
        return "2100-01-01T06:00:00+00:00"

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: provider_calls.append("provider"),
    )
    try:
        with pytest.raises(
            CampaignProviderCycleCaptureIntegrityError,
            match="inception receipt field authority changed",
        ):
            capture_campaign_complete_game_board(
                precommit_locator=locator,
                store=store,
                source_spec=spec,
                evidence_store=provider_store,
                request=_request(),
                api_key="secret-value",
                timeout_seconds=3.0,
                clock=mutating_clock,
            )
    finally:
        type.__setattr__(
            CampaignInceptionReceipt,
            "observation_not_after",
            original,
        )

    assert provider_calls == []
    assert store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    ) == ()


def test_first_clock_cannot_mutate_provider_capture_dispatch_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []
    target = capture_module._CAPTURE
    original_code = target.__code__

    def hostile(*_args, **_kwargs):
        raise AssertionError("hostile provider capture executed")

    assert original_code.co_freevars == hostile.__code__.co_freevars

    def mutating_clock() -> str:
        target.__code__ = hostile.__code__
        return "2100-01-01T06:00:00+00:00"

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: provider_calls.append("provider"),
    )
    try:
        with pytest.raises(
            CampaignProviderCycleCaptureIntegrityError,
            match="dispatch code changed: _CAPTURE",
        ):
            capture_campaign_complete_game_board(
                precommit_locator=locator,
                store=store,
                source_spec=spec,
                evidence_store=provider_store,
                request=_request(),
                api_key="secret-value",
                timeout_seconds=3.0,
                clock=mutating_clock,
            )
    finally:
        target.__code__ = original_code

    assert provider_calls == []
    assert store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    ) == ()


def test_completion_clock_cannot_redirect_evidence_save_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []
    target = capture_module._EVIDENCE_SAVE
    original_code = target.__code__

    def hostile(*_args, **_kwargs):
        raise AssertionError("hostile evidence save executed")

    assert original_code.co_freevars == hostile.__code__.co_freevars
    values = iter(
        (
            "2100-01-01T06:00:00+00:00",
            "2100-01-01T06:00:01+00:00",
            "2100-01-01T06:00:02+00:00",
        )
    )
    calls = 0

    def mutating_clock() -> str:
        nonlocal calls
        calls += 1
        value = next(values)
        if calls == 2:
            target.__code__ = hostile.__code__
        return value

    def fake_urlopen(_request, _timeout):
        provider_calls.append("provider")
        return _FakeSseResponse(_frame())

    monkeypatch.setattr(provider_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)
    try:
        with pytest.raises(
            CampaignProviderCycleCaptureIntegrityError,
            match="dispatch code changed: _EVIDENCE_SAVE",
        ):
            capture_campaign_complete_game_board(
                precommit_locator=locator,
                store=store,
                source_spec=spec,
                evidence_store=provider_store,
                request=_request(),
                api_key="secret-value",
                timeout_seconds=3.0,
                clock=mutating_clock,
            )
    finally:
        target.__code__ = original_code

    assert provider_calls == ["provider"]
    evidence = store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    )
    assert len(evidence) == 1
    assert evidence[0]["terminal"]["status"] == "LOCAL_FAILURE"
    assert "observed_artifacts" not in evidence[0]["terminal"]


def test_module_dispatch_in_place_code_mutation_rejects_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    calls: list[str] = []

    def fake_urlopen(_request, _timeout):
        calls.append("provider")
        return _FakeSseResponse(_frame())

    def hostile(*_args, **_kwargs):
        raise AssertionError("hostile dispatch code executed")

    monkeypatch.setattr(provider_module, "urlopen", fake_urlopen)
    target = capture_module._NEXT_SLOT
    original_code = target.__code__
    assert original_code.co_freevars == hostile.__code__.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            CampaignProviderCycleCaptureIntegrityError,
            match="dispatch code changed: _NEXT_SLOT",
        ):
            capture_campaign_complete_game_board(
                precommit_locator=locator,
                store=store,
                source_spec=spec,
                evidence_store=provider_store,
                request=_request(),
                api_key="secret-value",
                clock=_clock(),
            )
    finally:
        target.__code__ = original_code
    assert calls == []



def test_first_clock_transitive_store_seam_code_mutation_fails_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []
    hostile_calls: list[str] = []
    target = capture_module._STORE_CLASS_SEAMS["_connect"]
    function = getattr(target, "__func__", target)
    original_code = function.__code__

    def hostile(_self):
        hostile_calls.append("connect")
        raise AssertionError("hostile collector connect executed")

    assert original_code.co_freevars == hostile.__code__.co_freevars

    def mutating_clock() -> str:
        function.__code__ = hostile.__code__
        return "2100-01-01T06:00:00+00:00"

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: provider_calls.append("provider"),
    )
    try:
        with pytest.raises(
            CampaignProviderCycleCaptureIntegrityError,
            match="collector campaign capture seam code changed: _connect",
        ):
            capture_campaign_complete_game_board(
                precommit_locator=locator,
                store=store,
                source_spec=spec,
                evidence_store=provider_store,
                request=_request(),
                api_key="secret-value",
                timeout_seconds=3.0,
                clock=mutating_clock,
            )
    finally:
        function.__code__ = original_code

    assert provider_calls == []
    assert hostile_calls == []
    assert store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    ) == ()


def test_failure_clock_transitive_store_seam_code_mutation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    hostile_calls: list[str] = []
    target = capture_module._STORE_CLASS_SEAMS["_connect"]
    function = getattr(target, "__func__", target)
    original_code = function.__code__
    clock_calls = 0

    def hostile(_self):
        hostile_calls.append("connect")
        raise AssertionError("hostile collector connect executed")

    assert original_code.co_freevars == hostile.__code__.co_freevars

    def fail_urlopen(_request, _timeout):
        raise OSError("forced provider transport failure")

    def mutating_clock() -> str:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 2:
            function.__code__ = hostile.__code__
        return (
            "2100-01-01T06:00:00+00:00"
            if clock_calls == 1
            else "2100-01-01T06:00:01+00:00"
        )

    monkeypatch.setattr(provider_module, "urlopen", fail_urlopen)
    try:
        with pytest.raises(
            ProviderObservationUnsupportedError,
            match="provider SSE initial-state acquisition failed",
        ) as exc_info:
            capture_campaign_complete_game_board(
                precommit_locator=locator,
                store=store,
                source_spec=spec,
                evidence_store=provider_store,
                request=_request(),
                api_key="secret-value",
                timeout_seconds=3.0,
                clock=mutating_clock,
            )
    finally:
        function.__code__ = original_code

    assert hostile_calls == []
    assert any(
        "failure terminal also failed" in note
        and "collector campaign capture seam code changed: _connect" in note
        for note in getattr(exc_info.value, "__notes__", ())
    )
    evidence = store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    )
    assert len(evidence) == 1
    assert evidence[0]["terminal"] is None


def test_saved_capture_rejects_schedule_deadline_dispatch_rebind_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    capture = capture_campaign_complete_game_board
    provider_calls: list[str] = []
    monkeypatch.setattr(
        capture_module,
        "_SCHEDULE_DUE_AT",
        lambda **_kwargs: "2100-01-08T06:00:00+00:00",
    )
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: provider_calls.append("provider"),
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="dispatch authority is rebound: _SCHEDULE_DUE_AT",
    ):
        capture(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )

    assert provider_calls == []


def test_first_clock_cannot_rebind_chronology_primitive_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []

    def mutating_clock() -> str:
        monkeypatch.setattr(capture_module, "datetime", object())
        return "2100-01-01T06:00:00+00:00"

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: provider_calls.append("provider"),
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="chronology/digest primitives changed",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=mutating_clock,
        )

    assert provider_calls == []
    assert store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    ) == ()


def test_completion_clock_cannot_mutate_sha256_primitive_before_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    hostile_calls: list[str] = []
    original_sha256 = capture_module.hashlib.sha256
    clock_calls = 0

    def hostile_sha256(*_args, **_kwargs):
        hostile_calls.append("sha256")
        raise AssertionError("hostile sha256 executed")

    def mutating_clock() -> str:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 2:
            monkeypatch.setattr(
                capture_module.hashlib,
                "sha256",
                hostile_sha256,
            )
        return (
            "2100-01-01T06:00:00+00:00"
            if clock_calls == 1
            else "2100-01-01T06:00:01+00:00"
        )

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_frame()),
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)

    try:
        with pytest.raises(
            CampaignProviderCycleCaptureIntegrityError,
            match="chronology/digest primitives changed",
        ):
            capture_campaign_complete_game_board(
                precommit_locator=locator,
                store=store,
                source_spec=spec,
                evidence_store=provider_store,
                request=_request(),
                api_key="secret-value",
                timeout_seconds=3.0,
                clock=mutating_clock,
            )
    finally:
        monkeypatch.setattr(
            capture_module.hashlib,
            "sha256",
            original_sha256,
        )

    assert hostile_calls == []
    evidence = store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    )
    assert len(evidence) == 1
    assert evidence[0]["terminal"] is None


def test_receipt_field_descriptor_rebind_rejects_before_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []

    monkeypatch.setattr(
        capture_module.CampaignCompleteBoardCycleReceipt,
        "campaign_id",
        property(lambda _self: "forged-campaign"),
    )
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: provider_calls.append("provider"),
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="receipt field authority changed",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )

    assert provider_calls == []
    assert store.collector_cycle_evidence(
        source_id=spec.source_id,
        start_cycle_seq=1,
        end_cycle_seq=1,
    ) == ()


def test_receipt_field_witness_rebind_rejects_before_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    provider_calls: list[str] = []

    monkeypatch.setattr(capture_module, "_RECEIPT_FIELD_NAMES", ())
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: provider_calls.append("provider"),
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="_RECEIPT_FIELD_NAMES",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )

    assert provider_calls == []



def test_completion_clock_cannot_rebind_provider_snapshot_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_frame()),
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)
    original = vars(provider_module.CompleteGameBoardSnapshot)["evidence_sha256"]
    calls = 0

    def mutating_clock() -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            type.__setattr__(
                provider_module.CompleteGameBoardSnapshot,
                "evidence_sha256",
                property(lambda _self: "f" * 64),
            )
            return "2100-01-01T06:00:01+00:00"
        return "2100-01-01T06:00:00+00:00"

    try:
        with pytest.raises(
            CampaignProviderCycleCaptureIntegrityError,
            match="campaign provider acquisition authority changed",
        ):
            capture_campaign_complete_game_board(
                precommit_locator=locator,
                store=store,
                source_spec=spec,
                evidence_store=provider_store,
                request=_request(),
                api_key="secret-value",
                timeout_seconds=3.0,
                clock=mutating_clock,
            )
    finally:
        type.__setattr__(
            provider_module.CompleteGameBoardSnapshot,
            "evidence_sha256",
            original,
        )


def test_completion_clock_cannot_double_rebind_provider_assertion_witness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_frame()),
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)
    hostile_calls: list[str] = []
    calls = 0

    def hostile_assert(_snapshot) -> None:
        hostile_calls.append("assert")
        raise AssertionError("hostile provider assertion executed")

    def mutating_clock() -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            monkeypatch.setattr(
                provider_module,
                "_CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE",
                hostile_assert,
            )
            monkeypatch.setattr(
                capture_module,
                "_PROVIDER_CANONICAL_ASSERT",
                hostile_assert,
            )
            monkeypatch.setattr(
                capture_module,
                "_PROVIDER_CANONICAL_ASSERT_CODE",
                hostile_assert.__code__,
            )
            return "2100-01-01T06:00:01+00:00"
        return "2100-01-01T06:00:00+00:00"

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="campaign provider acquisition authority changed",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=mutating_clock,
        )

    assert hostile_calls == []


def test_public_provider_assert_rebind_is_not_used_by_durable_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_frame()),
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)
    hostile_calls: list[str] = []

    def hostile_assert(_snapshot) -> None:
        hostile_calls.append("assert")
        raise AssertionError("hostile public assertion executed")

    monkeypatch.setattr(
        provider_module,
        "assert_complete_game_board_authoritative",
        hostile_assert,
    )
    snapshot, receipt = capture_campaign_complete_game_board(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
        evidence_store=provider_store,
        request=_request(),
        api_key="secret-value",
        timeout_seconds=3.0,
        clock=_clock(),
    )

    assert receipt.provider_evidence_sha256 == snapshot.evidence_sha256
    assert hostile_calls == []



def test_campaign_test_clock_context_rejects_wrong_capability():
    with pytest.raises(TypeError, match="private capability"):
        with capture_module._test_campaign_clock_origin(_capability=object()):
            pass


def test_production_capture_rejects_caller_clock_without_test_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_frame()),
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)

    token = capture_module._CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN.set(None)
    try:
        with pytest.raises(
            CampaignProviderCycleCaptureIntegrityError,
            match="clock must be product-owned",
        ):
            capture_campaign_complete_game_board(
                precommit_locator=locator,
                store=store,
                source_spec=spec,
                evidence_store=provider_store,
                request=_request(),
                api_key="secret-value",
                timeout_seconds=3.0,
                clock=_clock(),
            )
    finally:
        capture_module._CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN.reset(token)


def test_production_capture_accepts_only_canonical_default_clock_identity(
    tmp_path: Path,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    token = capture_module._CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN.set(None)
    try:
        sealed = capture_module.capture_campaign_complete_game_board
        closure = {
            name: cell.cell_contents
            for name, cell in zip(
                sealed.__code__.co_freevars,
                sealed.__closure__ or (),
            )
        }
        implementation = closure["expected_capture"]
        assert implementation.__defaults__ is None
        assert (
            implementation.__kwdefaults__["clock"]
            is capture_module._CANONICAL_CAMPAIGN_CLOCK
        )
    finally:
        capture_module._CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN.reset(token)


def test_sealed_campaign_capture_does_not_expose_unsealed_delegate() -> None:
    sealed = capture_module.capture_campaign_complete_game_board

    assert not hasattr(sealed, "__wrapped__")
    assert sealed.__name__ == "capture_campaign_complete_game_board"


def test_saved_campaign_capture_rejects_public_surface_rebind_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = capture_module.capture_campaign_complete_game_board
    hostile_calls: list[str] = []

    def hostile(**_kwargs):
        hostile_calls.append("capture")
        raise AssertionError("hostile public capture executed")

    monkeypatch.setattr(
        capture_module,
        "capture_campaign_complete_game_board",
        hostile,
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="public capture surface changed",
    ):
        capture(
            precommit_locator=None,
            store=None,
            source_spec=None,
            evidence_store=None,
            request=None,
            api_key="secret-value",
        )

    assert hostile_calls == []


def test_campaign_capture_rejects_mid_call_public_surface_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    capture = capture_module.capture_campaign_complete_game_board
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_frame()),
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)

    values = iter(
        [
            "2100-01-01T06:00:00+00:00",
            "2100-01-01T06:00:01+00:00",
            "2100-01-01T06:00:02+00:00",
        ]
    )
    calls = 0

    def mutating_clock() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            monkeypatch.setattr(
                capture_module,
                "capture_campaign_complete_game_board",
                lambda **_kwargs: None,
            )
        return next(values)

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="public capture surface changed",
    ):
        capture(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=mutating_clock,
        )


def test_campaign_clock_witness_double_rebind_fails_before_clock_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    calls: list[str] = []

    def hostile_clock() -> str:
        calls.append("clock")
        raise AssertionError("hostile clock executed")

    monkeypatch.setattr(
        capture_module,
        "_CANONICAL_CAMPAIGN_CLOCK",
        hostile_clock,
    )
    monkeypatch.setattr(
        capture_module,
        "_CANONICAL_CAMPAIGN_CLOCK_CODE",
        hostile_clock.__code__,
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="dispatch authority is rebound|campaign provider acquisition authority changed",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
        )

    assert calls == []



def test_completion_clock_cannot_rebind_cycle_receipt_issuer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_frame()),
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)
    hostile_calls: list[str] = []
    calls = 0

    def hostile_issuer(*_args, **_kwargs):
        hostile_calls.append("issuer")
        raise AssertionError("hostile receipt issuer executed")

    def mutating_clock() -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            monkeypatch.setattr(
                capture_module,
                "_CANONICAL_CYCLE_RECEIPT_ISSUER_FUNCTION",
                hostile_issuer,
            )
            monkeypatch.setattr(
                capture_module,
                "_CANONICAL_CYCLE_RECEIPT_ISSUER_CODE",
                hostile_issuer.__code__,
            )
            return "2100-01-01T06:00:01+00:00"
        return "2100-01-01T06:00:00+00:00"

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="campaign cycle receipt issuance authority changed",
    ):
        capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=mutating_clock,
        )

    assert hostile_calls == []



def test_provider_scope_reflection_helper_rebind_fails_closed(
    tmp_path: Path,
) -> None:
    locator, _store, _spec, provider_store = _setup(tmp_path)
    helper_globals = capture_module._CANONICAL_GETATTR_STATIC_GLOBALS
    dependency = next(
        item
        for item in capture_module._CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS
        if item[2] is not None
    )
    name, original, _code = dependency
    hostile_calls: list[str] = []

    def hostile(*_args, **_kwargs):
        hostile_calls.append(name)
        raise AssertionError("hostile inspect helper executed")

    helper_globals[name] = hostile
    try:
        with pytest.raises(
            CampaignProviderCycleCaptureIntegrityError,
            match="provider evidence scope authority executable changed",
        ):
            binding_module._PROVIDER_EVIDENCE_SCOPE(
                locator,
                provider_store,
            )
    finally:
        helper_globals[name] = original

    assert hostile_calls == []


def test_public_capture_rejects_reflection_helper_rebind_before_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    capture = capture_module.capture_campaign_complete_game_board
    helper_globals = capture_module._CANONICAL_GETATTR_STATIC_GLOBALS
    dependency = next(
        item
        for item in capture_module._CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS
        if item[2] is not None
    )
    name, original, _code = dependency
    hostile_calls: list[str] = []
    provider_calls: list[str] = []

    def hostile(*_args, **_kwargs):
        hostile_calls.append(name)
        raise AssertionError("hostile inspect helper executed")

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda *_args, **_kwargs: provider_calls.append("provider"),
    )
    helper_globals[name] = hostile
    try:
        with pytest.raises(
            CampaignProviderCycleCaptureIntegrityError,
            match="campaign provider-cycle reflection dispatch changed",
        ):
            capture(
                precommit_locator=locator,
                store=store,
                source_spec=spec,
                evidence_store=provider_store,
                request=_request(),
                api_key="secret-value",
                timeout_seconds=3.0,
                clock=_clock(),
            )
    finally:
        helper_globals[name] = original

    assert hostile_calls == []
    assert provider_calls == []


def test_public_capture_rejects_reflection_dependency_witness_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    capture = capture_module.capture_campaign_complete_game_board
    monkeypatch.setattr(
        capture_module,
        "_CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS",
        (),
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="dispatch authority is rebound: _CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS",
    ):
        capture(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )



def test_capture_does_not_dispatch_through_shadowed_globals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, _provider_store = _setup(tmp_path)
    hostile_calls: list[str] = []

    def hostile_globals():
        hostile_calls.append("globals")
        raise AssertionError("hostile globals executed")

    monkeypatch.setattr(capture_module, "globals", hostile_globals, raising=False)

    with pytest.raises(TypeError, match="evidence_store must be the exact"):
        capture_module.capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=None,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )

    assert hostile_calls == []


def test_capture_does_not_dispatch_through_shadowed_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    hostile_calls: list[str] = []

    def hostile_vars(_value):
        hostile_calls.append("vars")
        raise AssertionError("hostile vars executed")

    monkeypatch.setattr(capture_module, "vars", hostile_vars, raising=False)
    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda _request, _timeout: _FakeSseResponse(_frame()),
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)

    snapshot, receipt = capture_module.capture_campaign_complete_game_board(
        precommit_locator=locator,
        store=store,
        source_spec=spec,
        evidence_store=provider_store,
        request=_request(),
        api_key="secret-value",
        timeout_seconds=3.0,
        clock=_clock(),
    )

    assert hostile_calls == []
    assert receipt.provider_evidence_sha256 == snapshot.evidence_sha256



def test_capture_does_not_dispatch_through_shadowed_type_before_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile_calls: list[str] = []

    def hostile_type(_value):
        hostile_calls.append("type")
        raise AssertionError("hostile type executed")

    monkeypatch.setattr(capture_module, "type", hostile_type, raising=False)

    with pytest.raises(TypeError, match="source_spec must be exact"):
        capture_module.capture_campaign_complete_game_board(
            precommit_locator=None,
            store=None,
            source_spec=None,
            evidence_store=None,
            request=None,
            api_key="secret-value",
        )

    assert hostile_calls == []


def test_capture_does_not_dispatch_through_shadowed_callable_before_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    hostile_calls: list[str] = []

    def hostile_callable(_value):
        hostile_calls.append("callable")
        raise AssertionError("hostile callable executed")

    monkeypatch.setattr(capture_module, "callable", hostile_callable, raising=False)

    with pytest.raises(TypeError, match="clock must be callable"):
        capture_module.capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            clock=object(),
        )

    assert hostile_calls == []


def test_capture_does_not_dispatch_through_shadowed_getattr_before_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, _provider_store = _setup(tmp_path)
    hostile_calls: list[str] = []

    def hostile_getattr(*_args, **_kwargs):
        hostile_calls.append("getattr")
        raise AssertionError("hostile getattr executed")

    monkeypatch.setattr(capture_module, "getattr", hostile_getattr, raising=False)

    with pytest.raises(TypeError, match="evidence_store must be the exact"):
        capture_module.capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=None,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )

    assert hostile_calls == []


def test_imported_provider_scope_guard_captures_type_and_getattr_primitives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, _store, _spec, provider_store = _setup(tmp_path)
    hostile_calls: list[str] = []

    def hostile(*_args, **_kwargs):
        hostile_calls.append("primitive")
        raise AssertionError("hostile builtin executed")

    monkeypatch.setattr(capture_module, "type", hostile, raising=False)
    monkeypatch.setattr(capture_module, "getattr", hostile, raising=False)

    binding_module._PROVIDER_EVIDENCE_SCOPE(locator, provider_store)

    assert hostile_calls == []



def test_imported_scope_captures_remaining_builtin_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, _store, _spec, provider_store = _setup(tmp_path)
    hostile_calls: list[str] = []

    def hostile(*_args, **_kwargs):
        hostile_calls.append("builtin")
        raise AssertionError("hostile builtin executed")

    monkeypatch.setattr(capture_module, "any", hostile, raising=False)
    monkeypatch.setattr(capture_module, "set", hostile, raising=False)
    monkeypatch.setattr(capture_module, "dict", hostile, raising=False)
    monkeypatch.setattr(capture_module, "TypeError", hostile, raising=False)

    binding_module._PROVIDER_EVIDENCE_SCOPE(locator, provider_store)

    assert hostile_calls == []


def test_public_capture_rejects_shadowed_tuple_before_hostile_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    capture = capture_module.capture_campaign_complete_game_board
    hostile_calls: list[str] = []

    def hostile_tuple(*_args, **_kwargs):
        hostile_calls.append("tuple")
        raise AssertionError("hostile tuple executed")

    monkeypatch.setattr(capture_module, "tuple", hostile_tuple, raising=False)

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="campaign provider-cycle builtin dispatch shadowed",
    ):
        capture(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=_clock(),
        )

    assert hostile_calls == []


def test_clock_cannot_shadow_any_before_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator, store, spec, provider_store = _setup(tmp_path)
    hostile_calls: list[str] = []
    provider_calls: list[str] = []

    def hostile_any(*_args, **_kwargs):
        hostile_calls.append("any")
        raise AssertionError("hostile any executed")

    def mutating_clock() -> str:
        monkeypatch.setattr(capture_module, "any", hostile_any, raising=False)
        return "2100-01-01T06:00:00+00:00"

    monkeypatch.setattr(
        provider_module,
        "urlopen",
        lambda *_args, **_kwargs: provider_calls.append("provider"),
    )

    with pytest.raises(
        CampaignProviderCycleCaptureIntegrityError,
        match="campaign provider-cycle builtin dispatch shadowed: any",
    ):
        capture_module.capture_campaign_complete_game_board(
            precommit_locator=locator,
            store=store,
            source_spec=spec,
            evidence_store=provider_store,
            request=_request(),
            api_key="secret-value",
            timeout_seconds=3.0,
            clock=mutating_clock,
        )

    assert hostile_calls == []
    assert provider_calls == []
