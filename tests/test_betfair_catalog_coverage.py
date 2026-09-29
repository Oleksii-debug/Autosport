from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
import urllib.request as _urllib_request

import pytest

from autosport.betfair_account_identity import (
    build_betfair_authenticated_client,
    resolve_betfair_authenticated_account_identity,
)
from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BETTING_JSON_RPC_ENDPOINT,
    BetfairSessionCredentials,
)
from autosport.betfair_catalog_coverage import (
    BetfairCatalogCoverageError,
    create_catalog_coverage_plan,
    pending_catalog_coverage_leaves,
    record_catalog_coverage_acquisition,
    record_catalog_coverage_failure,
    resolve_catalog_coverage,
    split_catalog_coverage_request,
)
from autosport.betfair_discovery_provenance import BetfairDiscoveryVisibilityScope
from autosport.betfair_discovery_transport_origin import (
    acquire_authenticated_betfair_discovery,
)
from autosport.betfair_multisport_catalog import (
    build_list_market_catalogue_request,
)
from autosport.causal_collector import CollectorDeltaStore
from autosport.source_universe_commitment import build_source_universe_commitment


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        assert limit >= len(self._payload)
        return self._payload


def _market(market_id: str, event_id: str, start: str) -> dict[str, object]:
    return {
        "marketId": market_id,
        "marketName": "Match Odds",
        "marketStartTime": start,
        "eventType": {"id": "1"},
        "event": {"id": event_id},
        "description": {"marketType": "MATCH_ODDS"},
    }


class _CoverageOpener:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.empty_e2 = False

    def open(self, request, data=None, timeout: float = 0):
        assert data is None
        assert timeout > 0
        body = json.loads(request.data.decode("utf-8"))
        method = body["method"]
        params = body["params"]
        self.calls.append(
            {
                "url": request.full_url,
                "method": method,
                "params": params,
                "id": body["id"],
            }
        )
        if method == "AccountAPING/v1.0/getAccountDetails":
            assert request.full_url == ACCOUNT_JSON_RPC_ENDPOINT
            result = {
                "currencyCode": "EUR",
                "localeCode": "en",
                "region": "GBR",
                "timezone": "Europe/London",
            }
        elif method == "SportsAPING/v1.0/listMarketCatalogue":
            assert request.full_url == BETTING_JSON_RPC_ENDPOINT
            market_filter = params["filter"]
            event_ids = market_filter.get("eventIds", [])
            if event_ids == ["e1", "e2"]:
                result = [
                    _market("1.100", "e1", "2026-10-01T10:10:00Z"),
                    _market("1.200", "e2", "2026-10-01T10:20:00Z"),
                ]
            elif event_ids == ["e1"]:
                result = [
                    _market("1.100", "e1", "2026-10-01T10:10:00Z"),
                ]
            elif event_ids == ["e2"]:
                result = (
                    []
                    if self.empty_e2
                    else [_market("1.200", "e2", "2026-10-01T10:20:00Z")]
                )
            elif event_ids == ["single"]:
                result = [
                    _market("1.999", "single", "2026-10-01T10:00:00Z"),
                ]
            else:
                raise AssertionError(f"unexpected eventIds {event_ids!r}")
        else:
            raise AssertionError(f"unexpected method {method!r}")
        raw = json.dumps(
            {"jsonrpc": "2.0", "id": body["id"], "result": result},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _Response(raw)


def _successful_source_window(store: CollectorDeltaStore):
    cycle_seq = store._begin_collector_cycle(
        source_id="source-x",
        run_id="run-coverage",
        stream_epoch="epoch-1",
        attempted_at="2026-09-29T15:00:00+00:00",
    )
    store._finish_collector_cycle(
        source_id="source-x",
        cycle_seq=cycle_seq,
        status="SUCCESS",
        completed_at="2026-09-29T15:00:01+00:00",
        catalog_changes=(),
        observed_delta_ids=(),
        committed_delta_ids=(),
        duplicate_delta_ids=(),
    )
    return build_source_universe_commitment(
        store,
        expected_store_path=store.path,
        source_id="source-x",
        start_cycle_seq=cycle_seq,
        end_cycle_seq=cycle_seq,
    )


def _client_and_scope(monkeypatch: pytest.MonkeyPatch):
    opener = _CoverageOpener()

    def fake_do_open(_self, _http_class, request, **_kwargs):
        response = opener.open(
            request,
            timeout=getattr(request, "timeout", 0),
        )
        response.code = 200
        response.msg = "OK"
        return response

    monkeypatch.setattr(
        _urllib_request.AbstractHTTPHandler,
        "do_open",
        fake_do_open,
    )
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="caller-label-not-authority",
    )
    identity = resolve_betfair_authenticated_account_identity(client)
    scope = BetfairDiscoveryVisibilityScope(
        account_scope_ref=identity.session_context_id,
        application_scope_ref="autosport-live-betfair-app-context",
        key_class="LIVE",
        jurisdiction="UK",
    )
    return client, scope, opener


def _root_request():
    return build_list_market_catalogue_request(
        event_type_ids=("1",),
        event_ids=("e1", "e2"),
        market_type_codes=("MATCH_ODDS",),
        max_results=2,
        market_start_from="2026-10-01T10:00:00Z",
        market_start_to="2026-10-01T11:00:00Z",
    )


def test_saturated_root_expands_to_reconciled_categorical_children_and_live_completion(
    tmp_path,
    monkeypatch,
):
    store = CollectorDeltaStore(tmp_path / "collector.db")
    source = _successful_source_window(store)
    client, scope, opener = _client_and_scope(monkeypatch)
    root_request = _root_request()

    plan = create_catalog_coverage_plan(
        store,
        expected_store_path=store.path,
        source_universe=source,
        expected_source_id="source-x",
        expected_start_cycle_seq=1,
        expected_end_cycle_seq=1,
        client=client,
        visibility_scope=scope,
        causal_cutoff=datetime(2099, 1, 1, tzinfo=timezone.utc),
        root_request=root_request,
    )

    pending = pending_catalog_coverage_leaves(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
    )
    assert len(pending) == 1
    root = pending[0]
    assert root.parent_leaf_id is None

    root_acquisition = acquire_authenticated_betfair_discovery(
        client, root.request
    )
    root_result = record_catalog_coverage_acquisition(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
        leaf_id=root.leaf_id,
        acquisition=root_acquisition,
    )
    assert root_result.status == "SATURATED_SPLIT"
    assert len(root_result.child_leaf_ids) == 2

    children = pending_catalog_coverage_leaves(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
    )
    assert len(children) == 2
    assert all(child.parent_leaf_id == root.leaf_id for child in children)

    child_acquisitions = []
    statuses = set()
    for child in children:
        acquisition = acquire_authenticated_betfair_discovery(
            client, child.request
        )
        child_acquisitions.append(acquisition)
        result = record_catalog_coverage_acquisition(
            store,
            expected_store_path=store.path,
            plan_id=plan.plan_id,
            leaf_id=child.leaf_id,
            acquisition=acquisition,
        )
        statuses.add(result.status)
    assert statuses == {"SUCCESS"}
    assert pending_catalog_coverage_leaves(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
    ) == ()

    live = resolve_catalog_coverage(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
        live_acquisitions=(
            root_acquisition,
            *child_acquisitions,
        ),
    )
    assert live.durable_partition_complete is True
    assert live.provider_visible_scope_complete is True
    assert live.reason == "PROVIDER_VISIBLE_SCOPE_COMPLETE"
    assert live.saturated_split_count == 1
    assert live.success_count == 2
    assert live.empty_count == 0
    assert live.promotion_ready is False

    restarted_truth = resolve_catalog_coverage(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
    )
    assert restarted_truth.durable_partition_complete is True
    assert restarted_truth.provider_visible_scope_complete is False
    assert restarted_truth.reason == "AUTHENTICATED_REACQUISITION_REQUIRED"

    catalogue_calls = [
        call
        for call in opener.calls
        if call["method"] == "SportsAPING/v1.0/listMarketCatalogue"
    ]
    assert len(catalogue_calls) == 3


def test_partition_prefers_provider_identity_and_time_only_scope_fails_closed():
    root = _root_request()
    split = split_catalog_coverage_request(root)
    assert split is not None
    left, right = split
    assert left.rpc_params()["filter"]["eventIds"] == ["e1"]
    assert right.rpc_params()["filter"]["eventIds"] == ["e2"]
    assert left.rpc_params()["filter"]["marketStartTime"] == (
        right.rpc_params()["filter"]["marketStartTime"]
    )

    one_event = build_list_market_catalogue_request(
        event_type_ids=("1",),
        event_ids=("single",),
        market_type_codes=("MATCH_ODDS",),
        max_results=2,
        market_start_from="2026-10-01T10:00:00.000000Z",
        market_start_to="2026-10-01T10:00:00.000010Z",
    )
    # Betfair does not publish endpoint inclusion semantics for this filter.
    # Until overlap-guarded market-identity reconciliation exists, inventing a
    # half-open microsecond split would risk a silent boundary omission.
    assert split_catalog_coverage_request(one_event) is None


def test_parent_market_missing_from_categorical_child_fails_reconciliation(
    tmp_path,
    monkeypatch,
):
    store = CollectorDeltaStore(tmp_path / "collector.db")
    source = _successful_source_window(store)
    client, scope, opener = _client_and_scope(monkeypatch)
    plan = create_catalog_coverage_plan(
        store,
        expected_store_path=store.path,
        source_universe=source,
        expected_source_id="source-x",
        expected_start_cycle_seq=1,
        expected_end_cycle_seq=1,
        client=client,
        visibility_scope=scope,
        causal_cutoff=datetime(2099, 1, 1, tzinfo=timezone.utc),
        root_request=_root_request(),
    )
    root = pending_catalog_coverage_leaves(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
    )[0]
    root_acquisition = acquire_authenticated_betfair_discovery(client, root.request)
    record_catalog_coverage_acquisition(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
        leaf_id=root.leaf_id,
        acquisition=root_acquisition,
    )

    opener.empty_e2 = True
    child_acquisitions = []
    for child in pending_catalog_coverage_leaves(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
    ):
        acquisition = acquire_authenticated_betfair_discovery(client, child.request)
        child_acquisitions.append(acquisition)
        record_catalog_coverage_acquisition(
            store,
            expected_store_path=store.path,
            plan_id=plan.plan_id,
            leaf_id=child.leaf_id,
            acquisition=acquisition,
        )

    with pytest.raises(
        BetfairCatalogCoverageError,
        match="parent observation is not reconciled",
    ):
        resolve_catalog_coverage(
            store,
            expected_store_path=store.path,
            plan_id=plan.plan_id,
            live_acquisitions=(root_acquisition, *child_acquisitions),
        )


def test_provider_failure_terminal_survives_and_blocks_completion(
    tmp_path,
    monkeypatch,
):
    store = CollectorDeltaStore(tmp_path / "collector.db")
    source = _successful_source_window(store)
    client, scope, _opener = _client_and_scope(monkeypatch)
    plan = create_catalog_coverage_plan(
        store,
        expected_store_path=store.path,
        source_universe=source,
        expected_source_id="source-x",
        expected_start_cycle_seq=1,
        expected_end_cycle_seq=1,
        client=client,
        visibility_scope=scope,
        causal_cutoff=datetime(2099, 1, 1, tzinfo=timezone.utc),
        root_request=_root_request(),
    )
    root = pending_catalog_coverage_leaves(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
    )[0]

    record_catalog_coverage_failure(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
        leaf_id=root.leaf_id,
        error_code="PROVIDER_UNAVAILABLE",
    )

    truth = resolve_catalog_coverage(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
    )
    assert truth.failure_count == 1
    assert truth.durable_partition_complete is False
    assert truth.provider_visible_scope_complete is False
    assert truth.reason == "DURABLE_COVERAGE_INCOMPLETE"


def test_unsplittable_saturation_is_explicit_not_false_complete(
    tmp_path,
    monkeypatch,
):
    store = CollectorDeltaStore(tmp_path / "collector.db")
    source = _successful_source_window(store)
    client, scope, _opener = _client_and_scope(monkeypatch)
    request = build_list_market_catalogue_request(
        event_type_ids=("1",),
        event_ids=("single",),
        market_type_codes=("MATCH_ODDS",),
        max_results=1,
        market_start_from="2026-10-01T10:00:00Z",
        market_start_to="2026-10-01T10:00:00Z",
    )
    plan = create_catalog_coverage_plan(
        store,
        expected_store_path=store.path,
        source_universe=source,
        expected_source_id="source-x",
        expected_start_cycle_seq=1,
        expected_end_cycle_seq=1,
        client=client,
        visibility_scope=scope,
        causal_cutoff=datetime(2099, 1, 1, tzinfo=timezone.utc),
        root_request=request,
    )
    root = pending_catalog_coverage_leaves(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
    )[0]
    acquisition = acquire_authenticated_betfair_discovery(client, root.request)
    result = record_catalog_coverage_acquisition(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
        leaf_id=root.leaf_id,
        acquisition=acquisition,
    )
    assert result.status == "SATURATED_UNSPLITTABLE"

    truth = resolve_catalog_coverage(
        store,
        expected_store_path=store.path,
        plan_id=plan.plan_id,
        live_acquisitions=(acquisition,),
    )
    assert truth.unsplittable_count == 1
    assert truth.provider_visible_scope_complete is False


def test_plan_and_leaf_rows_are_sql_immutable(tmp_path, monkeypatch):
    store = CollectorDeltaStore(tmp_path / "collector.db")
    source = _successful_source_window(store)
    client, scope, _opener = _client_and_scope(monkeypatch)
    plan = create_catalog_coverage_plan(
        store,
        expected_store_path=store.path,
        source_universe=source,
        expected_source_id="source-x",
        expected_start_cycle_seq=1,
        expected_end_cycle_seq=1,
        client=client,
        visibility_scope=scope,
        causal_cutoff=datetime(2099, 1, 1, tzinfo=timezone.utc),
        root_request=_root_request(),
    )

    connection = sqlite3.connect(store.path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                "UPDATE betfair_catalog_coverage_plans_v1 "
                "SET payload_json='{}' WHERE plan_id=?",
                (plan.plan_id,),
            )
    finally:
        connection.close()


def test_visibility_scope_cannot_relabel_authenticated_session(
    tmp_path,
    monkeypatch,
):
    store = CollectorDeltaStore(tmp_path / "collector.db")
    source = _successful_source_window(store)
    client, _scope, _opener = _client_and_scope(monkeypatch)
    forged_scope = BetfairDiscoveryVisibilityScope(
        account_scope_ref="caller-account-alias",
        application_scope_ref="autosport-live-betfair-app-context",
        key_class="LIVE",
        jurisdiction="UK",
    )

    with pytest.raises(
        BetfairCatalogCoverageError,
        match="session context",
    ):
        create_catalog_coverage_plan(
            store,
            expected_store_path=store.path,
            source_universe=source,
            expected_source_id="source-x",
            expected_start_cycle_seq=1,
            expected_end_cycle_seq=1,
            client=client,
            visibility_scope=forged_scope,
            causal_cutoff=datetime(2099, 1, 1, tzinfo=timezone.utc),
            root_request=_root_request(),
        )
