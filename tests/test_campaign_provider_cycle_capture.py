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
    CampaignForwardUniverseCycleBindingError,
    resolve_campaign_forward_universe_cycle_authority,
)
from autosport.forward_evidence_completeness import ForwardEvidenceProtocolEnvelope
from autosport.provider_evaluation_universe import ProviderEvaluationUniverseStore
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
