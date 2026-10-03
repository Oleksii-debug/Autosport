from __future__ import annotations

import json
from pathlib import Path

import pytest

import autosport.campaign_provider_cycle_capture as capture_module
import autosport.provider_observation_authority as provider_module
from autosport.campaign_inception import CampaignInceptionSourceSpec
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


def _manifest() -> CampaignPrecommitManifest:
    return CampaignPrecommitManifest(
        campaign_id="campaign-cycle-capture-test",
        source_id="parlayapi:table_tennis",
        source_snapshot_sha256=A,
        committed_at="2099-12-31T19:00:00Z",
        observation_not_before="2100-01-01T06:00:00Z",
        observation_not_after="2100-01-08T06:00:00Z",
        evaluation_universe_sha256=B,
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
    workspace = tmp_path / "workspace"
    evidence = workspace / "evidence"
    evidence.mkdir(parents=True)
    manifest_path = evidence / "precommit.json"
    authority_root = tmp_path / "machine-authority"
    publish_campaign_precommit_manifest(
        manifest_path,
        _manifest(),
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
        source_id="parlayapi:table_tennis",
        run_id="run-1",
        stream_epoch="complete-board-epoch-1",
        anchor_at="2100-01-01T06:00:00+00:00",
        interval_seconds=10,
        max_items=250,
        evaluation_start_slot_ordinal=0,
        evaluation_end_slot_ordinal=1,
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

