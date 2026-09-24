from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import inspect
import json
from pathlib import Path
import tempfile

import pytest

import autosport.model_compute_intent_route_authority as subject
from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeRouteRequest,
    ComputeRoutingPolicy,
    ComputeTier,
    DataClassification,
    ModelComputeRouterStore,
)
from autosport.monotonic_workspace_authority import (
    MonotonicAuthorityRollbackError,
    MonotonicWorkspaceAuthority,
)


def _canonical_intent(*, suffix: str):
    impl_path = Path(__file__).with_name("_test_portfolio_plan_impl.py")
    spec = importlib.util.spec_from_file_location(
        f"_intent_route_fixture_{suffix}",
        impl_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    goal = module.PortfolioPlanTests._goal()
    return module.PortfolioPlanTests._intent(
        goal,
        suffix=suffix,
        strategy_class=module.StrategyClass.ARBITRAGE,
    )


def _proposal(intent) -> datetime:
    raw = intent.risk_context.proposal_ts
    assert raw is not None
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _issue(
    authority: subject.ModelComputeIntentRouteAuthorityStore,
    router: ModelComputeRouterStore,
    intent,
    *,
    request_id: str = "intent-route-1",
    required_capability: str = "intent-economics",
    max_cost: Decimal = Decimal("0"),
):
    return authority.issue_request(
        intent=intent,
        router_store=router,
        request_id=request_id,
        required_capability=required_capability,
        data_classification=DataClassification.PUBLIC,
        allow_cloud=False,
        max_cost=max_cost,
        decision_timeout=timedelta(seconds=5),
        response_ttl_seconds=Decimal("5"),
        baseline_candidate_id="local-deterministic",
    )


def _route(
    router: ModelComputeRouterStore,
    request: ComputeRouteRequest,
):
    candidate = ComputeCandidate(
        candidate_id="local-deterministic",
        tier=ComputeTier.DETERMINISTIC,
        backend_id="deterministic-engine",
        model_id="no-model",
        config_sha256="c" * 64,
        capabilities=(request.required_capability,),
        estimated_cost=Decimal("0"),
        estimated_latency_seconds=Decimal("0"),
    )
    return router.route(
        request,
        (candidate,),
        ComputeRoutingPolicy(
            policy_id="intent-route-policy",
            policy_version=1,
        ),
        as_of=request.created_at,
    )


def _workspace():
    root = tempfile.TemporaryDirectory()
    root_path = Path(root.name)
    return root, root_path / "workspace"


def test_machine_authority_root_cannot_be_retargeted_after_local_state_loss(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="root-binding")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(workspace)
        request = _issue(authority, router, intent)
        _route(router, request)

        canonical_root = authority._authority.authority_root
        alternate_root = workspace.parent / "alternate-machine-authority"
        monkeypatch.setattr(
            subject,
            "_product_monotonic_authority_root",
            lambda: alternate_root,
        )

        authority.path.unlink()

        with pytest.raises(TypeError, match="authority_root"):
            subject.ModelComputeIntentRouteAuthorityStore(
                workspace,
                authority_root=alternate_root,  # type: ignore[call-arg]
            )

        with pytest.raises(
            MonotonicAuthorityRollbackError,
            match="missing, rolled back, or unproven",
        ):
            subject.ModelComputeIntentRouteAuthorityStore(workspace)

        assert authority._authority.authority_root == canonical_root
        assert not alternate_root.exists()


def test_private_canonical_root_alias_cannot_retarget_after_local_state_loss(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="private-root-alias")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(workspace)
        request = _issue(authority, router, intent)
        _route(router, request)

        canonical_root = authority._authority.authority_root
        alternate_root = workspace.parent / "private-alias-machine-authority"
        monkeypatch.setattr(
            subject,
            "_CANONICAL_PRODUCT_MONOTONIC_AUTHORITY_ROOT",
            lambda: alternate_root,
        )
        authority.path.unlink()

        with pytest.raises(
            MonotonicAuthorityRollbackError,
            match="missing, rolled back, or unproven",
        ):
            subject.ModelComputeIntentRouteAuthorityStore(workspace)

        assert authority._authority.authority_root == canonical_root
        assert not alternate_root.exists()


def test_live_authority_replacement_cannot_rebootstrap_after_local_state_loss(
    monkeypatch,
) -> None:
    first_intent = _canonical_intent(suffix="runtime-binding-a")
    second_intent = _canonical_intent(suffix="runtime-binding-b")
    issued_at = max(_proposal(first_intent), _proposal(second_intent)) + timedelta(
        seconds=2
    )
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        store = subject.ModelComputeIntentRouteAuthorityStore(workspace)
        first_request = _issue(store, router, first_intent)
        _route(router, first_request)

        canonical_authority = store._authority
        alternate_root = workspace.parent / "runtime-retarget-authority"
        alternate_authority = MonotonicWorkspaceAuthority(
            workspace=workspace,
            workspace_instance_id=canonical_authority.workspace_instance_id,
            domain=subject.AUTHORITY_DOMAIN,
            key=subject.AUTHORITY_KEY,
            authority_root=alternate_root,
        )
        store._authority = alternate_authority
        store.path.unlink()

        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="runtime binding changed",
        ):
            _issue(
                store,
                router,
                second_intent,
                request_id="intent-route-2",
            )

        assert canonical_authority.read_history()
        assert router.get_request("intent-route-2") is None
        assert not store.path.exists()
        assert not alternate_authority.records_dir.exists()


def test_authority_prepare_shadow_cannot_bypass_monotonic_dispatch(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="authority-dispatch")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        store = subject.ModelComputeIntentRouteAuthorityStore(workspace)
        authority = store._authority
        calls: list[str] = []

        def forged_prepare(**_kwargs):
            calls.append("prepare")
            raise AssertionError("forged prepare must never run")

        authority.prepare = forged_prepare
        try:
            with pytest.raises(
                subject.ModelComputeIntentRouteAuthorityError,
                match="runtime binding changed",
            ):
                _issue(store, router, intent)
        finally:
            del authority.prepare

        assert calls == []
        assert not store.path.exists()
        assert router.get_request("intent-route-1") is None

        request = _issue(store, router, intent)
        assert request.request_id == "intent-route-1"


def test_observed_state_digest_shadow_cannot_mask_deleted_state(
    monkeypatch,
) -> None:
    first_intent = _canonical_intent(suffix="state-read-observed-a")
    second_intent = _canonical_intent(suffix="state-read-observed-b")
    issued_at = max(
        _proposal(first_intent),
        _proposal(second_intent),
    ) + timedelta(seconds=2)
    monkeypatch.setattr(
        subject,
        "_authority_now",
        lambda: _time_text(issued_at),
    )

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        store = subject.ModelComputeIntentRouteAuthorityStore(workspace)
        first = _issue(store, router, first_intent)
        _route(router, first)
        observed = subject.sha256_file(store.path)
        assert observed is not None
        store.path.unlink()

        store._observed_sha256 = lambda: observed
        try:
            with pytest.raises(
                subject.ModelComputeIntentRouteAuthorityError,
                match="state-read dispatch changed",
            ):
                _issue(
                    store,
                    router,
                    second_intent,
                    request_id="intent-route-2",
                )
        finally:
            del store._observed_sha256

        assert not store.path.exists()
        with pytest.raises(MonotonicAuthorityRollbackError):
            subject.ModelComputeIntentRouteAuthorityStore(workspace)


def test_loader_shadow_cannot_inject_preexisting_router_origin(
    monkeypatch,
) -> None:
    caller_intent = _canonical_intent(suffix="state-read-load-caller")
    issued_intent = _canonical_intent(suffix="state-read-load-issued")
    issued_at = max(
        _proposal(caller_intent),
        _proposal(issued_intent),
    ) + timedelta(seconds=2)
    monkeypatch.setattr(
        subject,
        "_authority_now",
        lambda: _time_text(issued_at),
    )

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        caller_request = ComputeRouteRequest(
            request_id="caller-preexisting-load",
            created_at=_time_text(issued_at),
            decision_deadline=_time_text(
                issued_at + timedelta(seconds=5)
            ),
            required_capability="intent-economics",
            data_classification=DataClassification.PUBLIC,
            allow_cloud=False,
            max_cost=Decimal("0"),
            response_ttl_seconds=Decimal("5"),
            baseline_candidate_id="local-deterministic",
            decision_input_sha256=caller_intent.intent_sha256,
            decision_evidence_sha256=(
                caller_intent.evidence.evidence_sha256
            ),
        )
        _route(router, caller_request)

        store = subject.ModelComputeIntentRouteAuthorityStore(workspace)
        caller_identity = subject._intent_identity(caller_intent)
        forged_record = subject.ModelComputeIntentRouteRecord(
            router_store_relpath=store._router_relpath(router),
            intent_id=caller_identity["intent_id"],
            intent_sha256=caller_identity["intent_sha256"],
            intent_audit_sha256=caller_identity["intent_audit_sha256"],
            opportunity_id=caller_identity["opportunity_id"],
            opportunity_evidence_sha256=caller_identity[
                "opportunity_evidence_sha256"
            ],
            candidate_sha256=caller_identity["candidate_sha256"],
            proposal_ts=caller_identity["proposal_ts"],
            issued_at=caller_request.created_at,
            request=caller_request.payload(),
            request_sha256=subject._digest(
                caller_request.payload()
            ),
        )

        store._load = lambda: (forged_record,)
        try:
            with pytest.raises(
                subject.ModelComputeIntentRouteAuthorityError,
                match="state-read dispatch changed",
            ):
                _issue(
                    store,
                    router,
                    issued_intent,
                    request_id="intent-route-2",
                )
        finally:
            del store._load

        assert not store.path.exists()
        issued = _issue(
            store,
            router,
            issued_intent,
            request_id="intent-route-2",
        )
        _route(router, issued)

        reopened_router = ModelComputeRouterStore(
            workspace / "router.json"
        )
        reopened_store = (
            subject.ModelComputeIntentRouteAuthorityStore(workspace)
        )
        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="issuance is missing",
        ):
            reopened_store.resolve_current(
                intent=caller_intent,
                router_store=reopened_router,
                request_id=caller_request.request_id,
            )
        resolved = reopened_store.resolve_current(
            intent=issued_intent,
            router_store=reopened_router,
            request_id=issued.request_id,
        )
        assert resolved.request == issued.payload()


def test_digest_helper_global_spoof_cannot_mask_deleted_state(
    monkeypatch,
) -> None:
    first_intent = _canonical_intent(suffix="state-global-digest-a")
    second_intent = _canonical_intent(suffix="state-global-digest-b")
    issued_at = max(
        _proposal(first_intent),
        _proposal(second_intent),
    ) + timedelta(seconds=2)
    monkeypatch.setattr(
        subject,
        "_authority_now",
        lambda: _time_text(issued_at),
    )

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        store = subject.ModelComputeIntentRouteAuthorityStore(workspace)
        first = _issue(
            store,
            router,
            first_intent,
            request_id="intent-route-a",
        )
        _route(router, first)
        canonical_sha256_file = subject.sha256_file
        observed = canonical_sha256_file(store.path)
        store.path.unlink()
        calls: list[str] = []

        def forged_sha256_file(_path) -> str:
            calls.append("sha256-file")
            return observed

        monkeypatch.setattr(
            subject,
            "sha256_file",
            forged_sha256_file,
        )
        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="state-read dependency graph changed",
        ):
            _issue(
                store,
                router,
                second_intent,
                request_id="intent-route-b",
            )
        assert calls == []

        monkeypatch.setattr(
            subject,
            "sha256_file",
            canonical_sha256_file,
        )
        assert not store.path.exists()
        assert router.get_request("intent-route-b") is None
        with pytest.raises(MonotonicAuthorityRollbackError):
            subject.ModelComputeIntentRouteAuthorityStore(workspace)


def test_strict_json_global_injection_cannot_persist_prerouted_origin(
    monkeypatch,
) -> None:
    first_intent = _canonical_intent(suffix="state-global-parser-a")
    caller_intent = _canonical_intent(suffix="state-global-parser-caller")
    issued_intent = _canonical_intent(suffix="state-global-parser-b")
    issued_at = max(
        _proposal(first_intent),
        _proposal(caller_intent),
        _proposal(issued_intent),
    ) + timedelta(seconds=2)
    monkeypatch.setattr(
        subject,
        "_authority_now",
        lambda: _time_text(issued_at),
    )

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        store = subject.ModelComputeIntentRouteAuthorityStore(workspace)
        first = _issue(
            store,
            router,
            first_intent,
            request_id="intent-route-a",
        )
        _route(router, first)

        caller_request = ComputeRouteRequest(
            request_id="caller-preexisting-parser",
            created_at=_time_text(issued_at),
            decision_deadline=_time_text(
                issued_at + timedelta(seconds=5)
            ),
            required_capability="intent-economics",
            data_classification=DataClassification.PUBLIC,
            allow_cloud=False,
            max_cost=Decimal("0"),
            response_ttl_seconds=Decimal("5"),
            baseline_candidate_id="local-deterministic",
            decision_input_sha256=caller_intent.intent_sha256,
            decision_evidence_sha256=(
                caller_intent.evidence.evidence_sha256
            ),
        )
        _route(router, caller_request)
        caller_identity = subject._intent_identity(caller_intent)
        forged_record = subject.ModelComputeIntentRouteRecord(
            router_store_relpath=store._router_relpath(router),
            intent_id=caller_identity["intent_id"],
            intent_sha256=caller_identity["intent_sha256"],
            intent_audit_sha256=caller_identity["intent_audit_sha256"],
            opportunity_id=caller_identity["opportunity_id"],
            opportunity_evidence_sha256=caller_identity[
                "opportunity_evidence_sha256"
            ],
            candidate_sha256=caller_identity["candidate_sha256"],
            proposal_ts=caller_identity["proposal_ts"],
            issued_at=caller_request.created_at,
            request=caller_request.payload(),
            request_sha256=subject._digest(
                caller_request.payload()
            ),
        )

        canonical_state_text = store.path.read_text(encoding="utf-8")
        forged_state = json.loads(canonical_state_text)
        forged_state["records"].append(forged_record.to_dict())
        forged_state["records"].sort(key=lambda item: item["request_id"])
        canonical_strict_json_loads = subject.strict_json_loads
        calls: list[str] = []

        def forged_strict_json_loads(_text: str):
            calls.append("strict-json")
            return json.loads(json.dumps(forged_state))

        monkeypatch.setattr(
            subject,
            "strict_json_loads",
            forged_strict_json_loads,
        )
        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="state-read dependency graph changed",
        ):
            _issue(
                store,
                router,
                issued_intent,
                request_id="intent-route-b",
            )
        assert calls == []

        monkeypatch.setattr(
            subject,
            "strict_json_loads",
            canonical_strict_json_loads,
        )
        assert store.path.read_text(encoding="utf-8") == canonical_state_text
        assert router.get_request("intent-route-b") is None

        reopened = subject.ModelComputeIntentRouteAuthorityStore(workspace)
        resolved = reopened.resolve_current(
            intent=first_intent,
            router_store=router,
            request_id=first.request_id,
        )
        assert resolved.request == first.payload()
        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="issuance is missing",
        ):
            reopened.resolve_current(
                intent=caller_intent,
                router_store=router,
                request_id=caller_request.request_id,
            )


def test_state_reader_rejects_json_scanner_lower_dispatch_rebind(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="state-global-json-scanner")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(
        subject,
        "_authority_now",
        lambda: _time_text(issued_at),
    )

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        store = subject.ModelComputeIntentRouteAuthorityStore(workspace)
        request = _issue(store, router, intent)
        _route(router, request)

        json_module = subject.strict_json_loads.__globals__["json"]
        scanner_module = json_module.scanner
        canonical_make_scanner = scanner_module.make_scanner
        calls: list[str] = []

        def forged_make_scanner(*args, **kwargs):
            calls.append("make-scanner")
            return canonical_make_scanner(*args, **kwargs)

        monkeypatch.setattr(
            scanner_module,
            "make_scanner",
            forged_make_scanner,
        )
        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="state-read dependency graph changed",
        ):
            store.resolve_current(
                intent=intent,
                router_store=router,
                request_id=request.request_id,
            )
        assert calls == []


def test_issue_route_restart_and_resolve_exact_origin(monkeypatch) -> None:
    intent = _canonical_intent(suffix="origin")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        request = _issue(authority, router, intent)

        assert request.created_at == _time_text(issued_at)
        assert request.decision_deadline == _time_text(
            issued_at + timedelta(seconds=5)
        )
        assert request.decision_input_sha256 == intent.intent_sha256
        assert (
            request.decision_evidence_sha256
            == intent.evidence.evidence_sha256
        )
        _route(router, request)

        reopened_router = ModelComputeRouterStore(workspace / "router.json")
        reopened_authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        record = reopened_authority.resolve_current(
            intent=intent,
            router_store=reopened_router,
            request_id=request.request_id,
        )

        assert record.request == request.payload()
        assert record.request_sha256 == subject._digest(request.payload())
        assert record.intent_sha256 == intent.intent_sha256
        assert record.intent_audit_sha256 == subject._digest(
            intent.audit_payload()
        )
        assert record.issued_at == request.created_at
        assert len(record.authority_sha256) == 64


def test_public_issuer_has_no_caller_absolute_timestamp_or_digest_surface() -> None:
    parameters = set(
        inspect.signature(
            subject.ModelComputeIntentRouteAuthorityStore.issue_request
        ).parameters
    )
    assert not parameters.intersection(
        {
            "created_at",
            "issued_at",
            "decision_deadline",
            "decision_input_sha256",
            "decision_evidence_sha256",
            "intent_sha256",
            "evidence_sha256",
            "request",
        }
    )


def test_future_dated_intent_proposal_fails_before_state_publish(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="future")
    issued_at = _proposal(intent) - timedelta(seconds=1)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )

        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="proposal cannot be in the future",
        ):
            _issue(authority, router, intent)

        assert not authority.path.exists()
        assert router.get_request("intent-route-1") is None


def test_existing_router_request_cannot_be_backfilled_with_origin_authority(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="backfill")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        caller_request = ComputeRouteRequest(
            request_id="intent-route-1",
            created_at=_time_text(issued_at),
            decision_deadline=_time_text(
                issued_at + timedelta(seconds=5)
            ),
            required_capability="intent-economics",
            data_classification=DataClassification.PUBLIC,
            allow_cloud=False,
            max_cost=Decimal("0"),
            response_ttl_seconds=Decimal("5"),
            baseline_candidate_id="local-deterministic",
            decision_input_sha256=intent.intent_sha256,
            decision_evidence_sha256=intent.evidence.evidence_sha256,
        )
        _route(router, caller_request)
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )

        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="cannot be backfilled",
        ):
            _issue(authority, router, intent)

        assert not authority.path.exists()


def test_state_payload_rebind_cannot_backfill_preexisting_router_origin(
    monkeypatch,
) -> None:
    caller_intent = _canonical_intent(suffix="payload-caller")
    issued_intent = _canonical_intent(suffix="payload-issued")
    issued_at = max(
        _proposal(caller_intent),
        _proposal(issued_intent),
    ) + timedelta(seconds=2)
    monkeypatch.setattr(
        subject,
        "_authority_now",
        lambda: _time_text(issued_at),
    )

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        caller_request = ComputeRouteRequest(
            request_id="caller-preexisting",
            created_at=_time_text(issued_at),
            decision_deadline=_time_text(
                issued_at + timedelta(seconds=5)
            ),
            required_capability="intent-economics",
            data_classification=DataClassification.PUBLIC,
            allow_cloud=False,
            max_cost=Decimal("0"),
            response_ttl_seconds=Decimal("5"),
            baseline_candidate_id="local-deterministic",
            decision_input_sha256=caller_intent.intent_sha256,
            decision_evidence_sha256=(
                caller_intent.evidence.evidence_sha256
            ),
        )
        _route(router, caller_request)

        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="cannot be backfilled",
        ):
            _issue(
                authority,
                router,
                caller_intent,
                request_id=caller_request.request_id,
            )

        caller_identity = subject._intent_identity(caller_intent)
        forged_record = subject.ModelComputeIntentRouteRecord(
            router_store_relpath=authority._router_relpath(router),
            intent_id=caller_identity["intent_id"],
            intent_sha256=caller_identity["intent_sha256"],
            intent_audit_sha256=caller_identity["intent_audit_sha256"],
            opportunity_id=caller_identity["opportunity_id"],
            opportunity_evidence_sha256=caller_identity[
                "opportunity_evidence_sha256"
            ],
            candidate_sha256=caller_identity["candidate_sha256"],
            proposal_ts=caller_identity["proposal_ts"],
            issued_at=caller_request.created_at,
            request=caller_request.payload(),
            request_sha256=subject._digest(
                caller_request.payload()
            ),
        )
        payload_calls: list[tuple[str, ...]] = []

        def forged_state_payload(records):
            payload_calls.append(
                tuple(item.request_id for item in records)
            )
            ordered = tuple(
                sorted(
                    (*records, forged_record),
                    key=lambda item: item.request_id,
                )
            )
            return {
                "schema": subject.SCHEMA,
                "schema_version": subject.SCHEMA_VERSION,
                "records": [item.to_dict() for item in ordered],
            }

        monkeypatch.setattr(
            subject,
            "_state_payload",
            forged_state_payload,
            raising=False,
        )

        issued = _issue(
            authority,
            router,
            issued_intent,
            request_id="intent-route-2",
        )
        _route(router, issued)

        reopened_router = ModelComputeRouterStore(
            workspace / "router.json"
        )
        reopened_authority = (
            subject.ModelComputeIntentRouteAuthorityStore(workspace)
        )
        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="issuance is missing",
        ):
            reopened_authority.resolve_current(
                intent=caller_intent,
                router_store=reopened_router,
                request_id=caller_request.request_id,
            )

        resolved = reopened_authority.resolve_current(
            intent=issued_intent,
            router_store=reopened_router,
            request_id=issued.request_id,
        )
        assert resolved.request == issued.payload()
        assert payload_calls == []



def test_same_id_retry_is_idempotent_and_cannot_retimestamp(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="retry")
    first_at = _proposal(intent) + timedelta(seconds=1)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(first_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        first = _issue(authority, router, intent)
        _route(router, first)

        second_at = _proposal(intent) + timedelta(seconds=4)
        monkeypatch.setattr(
            subject,
            "_authority_now",
            lambda: _time_text(second_at),
        )
        second = _issue(authority, router, intent)

        assert second.payload() == first.payload()
        assert second.created_at == _time_text(first_at)


def test_same_request_id_cannot_be_rebound_to_another_intent(
    monkeypatch,
) -> None:
    first_intent = _canonical_intent(suffix="intent-a")
    other_intent = _canonical_intent(suffix="intent-b")
    issued_at = min(
        _proposal(first_intent),
        _proposal(other_intent),
    ) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        first = _issue(authority, router, first_intent)
        _route(router, first)

        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="immutable across intent identity",
        ):
            _issue(authority, router, other_intent)


def test_resolve_rejects_cross_intent_substitution(monkeypatch) -> None:
    first_intent = _canonical_intent(suffix="resolve-a")
    other_intent = _canonical_intent(suffix="resolve-b")
    issued_at = min(
        _proposal(first_intent),
        _proposal(other_intent),
    ) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        request = _issue(authority, router, first_intent)
        _route(router, request)

        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="does not match canonical intent",
        ):
            authority.resolve_current(
                intent=other_intent,
                router_store=router,
                request_id=request.request_id,
            )


def test_resolve_rejects_same_id_router_request_substitution(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="route-substitution")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        issued = _issue(authority, router, intent)

        substituted = ComputeRouteRequest(
            request_id=issued.request_id,
            created_at=issued.created_at,
            decision_deadline=issued.decision_deadline,
            required_capability="different-capability",
            data_classification=issued.data_classification,
            allow_cloud=issued.allow_cloud,
            max_cost=issued.max_cost,
            response_ttl_seconds=issued.response_ttl_seconds,
            baseline_candidate_id=issued.baseline_candidate_id,
            decision_input_sha256=issued.decision_input_sha256,
            decision_evidence_sha256=issued.decision_evidence_sha256,
        )
        candidate = ComputeCandidate(
            candidate_id="local-deterministic",
            tier=ComputeTier.DETERMINISTIC,
            backend_id="deterministic-engine",
            model_id="no-model",
            config_sha256="c" * 64,
            capabilities=("different-capability",),
            estimated_cost=Decimal("0"),
            estimated_latency_seconds=Decimal("0"),
        )
        router.route(
            substituted,
            (candidate,),
            ComputeRoutingPolicy(
                policy_id="intent-route-policy",
                policy_version=1,
            ),
            as_of=substituted.created_at,
        )

        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="differs from product issuance",
        ):
            authority.resolve_current(
                intent=intent,
                router_store=router,
                request_id=issued.request_id,
            )


def test_resolve_requires_router_request_to_exist(monkeypatch) -> None:
    intent = _canonical_intent(suffix="missing-router")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        request = _issue(authority, router, intent)

        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="missing from canonical router",
        ):
            authority.resolve_current(
                intent=intent,
                router_store=router,
                request_id=request.request_id,
            )


def test_timestamp_only_historical_resolution_fails_closed(monkeypatch) -> None:
    intent = _canonical_intent(suffix="cutoff")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        request = _issue(authority, router, intent)
        _route(router, request)

        for decision_at in (
            issued_at - timedelta(microseconds=1),
            issued_at,
            issued_at + timedelta(days=365),
        ):
            with pytest.raises(
                subject.ModelComputeIntentRouteAuthorityError,
                match="timestamp-only historical",
            ):
                authority.resolve(
                    intent=intent,
                    router_store=router,
                    request_id=request.request_id,
                    decision_at=decision_at,
                )

        resolved = authority.resolve_current(
            intent=intent,
            router_store=router,
            request_id=request.request_id,
        )
        assert resolved.request_id == request.request_id

def test_router_instance_get_request_shadow_is_rejected(monkeypatch) -> None:
    intent = _canonical_intent(suffix="shadow")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        setattr(
            router,
            "get_request",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("shadow must never run")
            ),
        )
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )

        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="method shadow",
        ):
            _issue(authority, router, intent)


def test_opportunity_intent_module_rebind_cannot_redefine_canonical_origin(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="intent-class-rebind")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    class ReboundOpportunityIntent:
        def __init__(self, canonical) -> None:
            self._canonical = canonical

        def __getattr__(self, name: str):
            return getattr(self._canonical, name)

    forged = ReboundOpportunityIntent(intent)
    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        monkeypatch.setattr(
            subject,
            "OpportunityIntent",
            ReboundOpportunityIntent,
        )

        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="exact canonical OpportunityIntent",
        ):
            _issue(authority, router, forged)

        assert not authority.path.exists()
        assert router.get_request("intent-route-1") is None


def test_router_store_module_rebind_cannot_redefine_canonical_origin(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="router-class-rebind")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    class ReboundRouterStore:
        def __init__(self, canonical: ModelComputeRouterStore) -> None:
            self._canonical = canonical
            self.path = canonical.path

        def get_request(self, request_id: str):
            return self._canonical.get_request(request_id)

    temporary, workspace = _workspace()
    with temporary:
        canonical_router = ModelComputeRouterStore(workspace / "router.json")
        forged_router = ReboundRouterStore(canonical_router)
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        monkeypatch.setattr(
            subject,
            "ModelComputeRouterStore",
            ReboundRouterStore,
        )

        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="exact canonical ModelComputeRouterStore",
        ):
            _issue(authority, forged_router, intent)  # type: ignore[arg-type]

        assert not authority.path.exists()
        assert canonical_router.get_request("intent-route-1") is None


def test_route_request_module_rebind_does_not_control_product_construction(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="request-class-rebind")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    class ReboundRouteRequest:
        def __init__(self, **kwargs) -> None:
            self._canonical = ComputeRouteRequest(**kwargs)

        def __getattr__(self, name: str):
            return getattr(self._canonical, name)

        def payload(self):
            return self._canonical.payload()

        @classmethod
        def from_payload(cls, raw):
            item = object.__new__(cls)
            item._canonical = ComputeRouteRequest.from_payload(raw)
            return item

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        monkeypatch.setattr(
            subject,
            "ComputeRouteRequest",
            ReboundRouteRequest,
        )

        issued = _issue(authority, router, intent)

        assert type(issued) is ComputeRouteRequest
        assert issued.decision_input_sha256 == intent.intent_sha256
        assert issued.decision_evidence_sha256 == intent.evidence.evidence_sha256


def test_record_class_module_rebind_cannot_forge_durable_origin(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="record-class-rebind")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))
    canonical_record_class = subject.ModelComputeIntentRouteRecord

    class ReboundIntentRouteRecord:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError(
                "live record class must not construct product origin authority"
            )

        @classmethod
        def from_dict(cls, _raw):
            raise AssertionError(
                "live record class must not parse durable origin authority"
            )

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        monkeypatch.setattr(
            subject,
            "ModelComputeIntentRouteRecord",
            ReboundIntentRouteRecord,
        )

        issued = _issue(authority, router, intent)
        assert type(authority._records[0]) is canonical_record_class
        _route(router, issued)

        reopened = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        resolved = reopened.resolve_current(
            intent=intent,
            router_store=router,
            request_id=issued.request_id,
        )

        assert type(resolved) is canonical_record_class
        assert resolved.request == issued.payload()
        assert resolved.intent_sha256 == intent.intent_sha256
        assert (
            resolved.opportunity_evidence_sha256
            == intent.evidence.evidence_sha256
        )


def test_module_namespace_rebind_cannot_create_second_origin_authority(
    monkeypatch,
) -> None:
    first_intent = _canonical_intent(suffix="namespace-a")
    other_intent = _canonical_intent(suffix="namespace-b")
    issued_at = max(
        _proposal(first_intent),
        _proposal(other_intent),
    ) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))
    canonical_file_name = subject.FILE_NAME
    canonical_domain = subject.AUTHORITY_DOMAIN
    canonical_key = subject.AUTHORITY_KEY

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        first_store = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        first = _issue(first_store, router, first_intent)
        assert first.request_id == "intent-route-1"

        alternate_file_name = "alternate-intent-route-authority.json"
        monkeypatch.setattr(subject, "FILE_NAME", alternate_file_name)
        monkeypatch.setattr(
            subject,
            "AUTHORITY_DOMAIN",
            canonical_domain + ".alternate",
        )
        monkeypatch.setattr(
            subject,
            "AUTHORITY_KEY",
            canonical_key + "-alternate",
        )

        second_store = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        assert second_store.path == first_store.path
        assert second_store.path.name == canonical_file_name
        assert second_store._authority.domain == canonical_domain
        assert second_store._authority.key == canonical_key
        assert not (workspace / alternate_file_name).exists()

        with pytest.raises(
            subject.ModelComputeIntentRouteAuthorityError,
            match="immutable across intent identity",
        ):
            _issue(second_store, router, other_intent)

        assert not (workspace / alternate_file_name).exists()
        assert second_store._records[0].intent_id == first_intent.intent_id


def test_deleted_authority_state_fails_monotonic_rollback_fence(
    monkeypatch,
) -> None:
    intent = _canonical_intent(suffix="rollback")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        _issue(authority, router, intent)
        authority.path.unlink()

        with pytest.raises(MonotonicAuthorityRollbackError):
            subject.ModelComputeIntentRouteAuthorityStore(
                workspace,
                )


def test_tampered_authority_bytes_fail_monotonic_fence(monkeypatch) -> None:
    intent = _canonical_intent(suffix="tamper")
    issued_at = _proposal(intent) + timedelta(seconds=2)
    monkeypatch.setattr(subject, "_authority_now", lambda: _time_text(issued_at))

    temporary, workspace = _workspace()
    with temporary:
        router = ModelComputeRouterStore(workspace / "router.json")
        authority = subject.ModelComputeIntentRouteAuthorityStore(
            workspace,
        )
        _issue(authority, router, intent)

        raw = json.loads(authority.path.read_text(encoding="utf-8"))
        raw["records"][0]["intent_id"] = "forged-intent"
        authority.path.write_text(
            json.dumps(raw, sort_keys=True),
            encoding="utf-8",
        )

        with pytest.raises(MonotonicAuthorityRollbackError):
            subject.ModelComputeIntentRouteAuthorityStore(
                workspace,
                )
