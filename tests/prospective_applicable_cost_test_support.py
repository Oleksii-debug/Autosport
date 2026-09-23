from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import importlib.util
from pathlib import Path
import tempfile

from autosport.model_compute_router import (
    ComputeCandidate,
    ComputeRouteRequest,
    ComputeRoutingPolicy,
    ComputeTier,
    DataClassification,
    ModelComputeRouterStore,
)
from autosport.paper import PaperBook


def _load_portfolio_fixture():
    impl_path = Path(__file__).with_name("_test_portfolio_plan_impl.py")
    spec = importlib.util.spec_from_file_location(
        "_applicable_cost_real_fixture_shared", impl_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def canonical_applicable_cost_case():
    module = _load_portfolio_fixture()
    goal = module.PortfolioPlanTests._goal()
    policy = module.PortfolioPlanTests._policy(goal)
    book = PaperBook("1000")
    intent = module.PortfolioPlanTests._intent(
        goal,
        suffix="aggregate-sealed-real",
        strategy_class=module.StrategyClass.PREDICTIVE_EDGE,
    )
    graph = module.PortfolioPlanTests._graph(book, (intent,))
    plan = module.build_portfolio_plan(
        book,
        (intent,),
        policy,
        module.PortfolioPlanTests.DECISION_TS,
        dependency_graph=graph,
    )
    proposal_ts = intent.risk_context.proposal_ts
    assert proposal_ts is not None
    decision_at = datetime.fromisoformat(proposal_ts.replace("Z", "+00:00"))

    with tempfile.TemporaryDirectory() as tmp:
        store = ModelComputeRouterStore(Path(tmp) / "router.json")
        candidate = ComputeCandidate(
            candidate_id="aggregate-sealed-deterministic",
            tier=ComputeTier.DETERMINISTIC,
            backend_id="aggregate-engine",
            model_id="no-model",
            config_sha256="c" * 64,
            capabilities=("intent-economics",),
            estimated_cost=Decimal("0"),
            estimated_latency_seconds=Decimal("0"),
        )
        request = ComputeRouteRequest(
            request_id="aggregate-sealed-real-route",
            created_at=intent.evidence.observed_at,
            decision_deadline=proposal_ts,
            required_capability="intent-economics",
            data_classification=DataClassification.PUBLIC,
            allow_cloud=False,
            max_cost=Decimal("0"),
            response_ttl_seconds=Decimal("1"),
            baseline_candidate_id=candidate.candidate_id,
            decision_input_sha256=intent.intent_sha256,
            decision_evidence_sha256=intent.evidence.evidence_sha256,
        )
        store.route(
            request,
            (candidate,),
            ComputeRoutingPolicy(
                policy_id="aggregate-sealed-cost-policy",
                policy_version=1,
            ),
            as_of=intent.evidence.observed_at,
        )
        yield intent, plan, store, request, decision_at
