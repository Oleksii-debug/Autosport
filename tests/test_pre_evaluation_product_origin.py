from decimal import Decimal
import json
from pathlib import Path

import pytest

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.portfolio_plan import PortfolioDependencyGraph
from autosport.pre_evaluation_binding import (
    PreEvaluationDenominatorContext,
    ProviderMemberIdentity,
    bind_pre_evaluation_session,
)
from autosport.pre_evaluation_evidence import (
    PreEvaluationEvidenceAuthority,
    PreEvaluationPolicy,
)
from autosport.pre_evaluation_product_origin import (
    PreEvaluationProductOrigin,
    PreEvaluationProductOriginError,
    assert_pre_evaluation_product_origin_authoritative,
    resolve_pre_evaluation_product_origin,
)
from autosport.pre_evaluation_semantics import ProviderSelectionBinding
from autosport.provider_observation_authority import (
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
    capture_parlay_complete_game_board,
)
from autosport import provider_observation_authority
from autosport.risk import PaperRiskPolicy


SHA_A = "a" * 64
SHA_C = "c" * 64


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-pre-evaluation-origin",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("0.10"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("1000"),
        max_risk_of_ruin=Decimal("1"),
        max_execution_slippage_fraction=Decimal("1"),
        max_quote_age_seconds=Decimal("3600"),
        max_concurrent_positions=10,
    )


def _risk_policy() -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=_goal(),
    )


def _snapshot() -> CompleteGameBoardSnapshot:
    request = CompleteGameBoardRequest(
        sport_key="soccer",
        bookmakers=("bookmaker-1",),
    )
    frame = {
        "type": "initial_state",
        "sport_key": "soccer",
        "snapshot_scope": "current_game_board",
        "snapshot_complete": True,
        "truncated": False,
        "resume_mode": "replace",
        "partial": False,
        "data": [
            {
                "event_id": "event-1",
                "bookmaker": "bookmaker-1",
                "kind": "game",
                "market_key": "h2h",
                "last_update": "2026-09-18T13:19:58Z",
            }
        ],
        "count": 1,
    }
    return CompleteGameBoardSnapshot(
        request=request,
        captured_at="2026-09-18T13:19:59Z",
        frame_json=json.dumps(frame),
    )


def _issued_snapshot(monkeypatch) -> CompleteGameBoardSnapshot:
    raw = _snapshot()
    monkeypatch.setattr(
        provider_observation_authority,
        "_read_production_initial_state",
        lambda request, *, api_key, timeout_seconds: raw.frame,
    )
    monkeypatch.setattr(
        provider_observation_authority,
        "_default_clock",
        lambda: raw.captured_at,
    )
    return capture_parlay_complete_game_board(
        api_key="fixture-key",
        request=raw.request,
    )


def _provider(
    snapshot: CompleteGameBoardSnapshot,
    *,
    selection_id: str = "bookmaker-1:h2h:home",
) -> ProviderSelectionBinding:
    return ProviderSelectionBinding(
        row_key="row-a",
        member_sha256=SHA_C,
        event_id="event-1",
        market_id="bookmaker-1:h2h",
        selection_id=selection_id,
        source_id=snapshot.request.source_id,
        source_at="2026-09-18T13:19:58Z",
    )


def _bound(
    snapshot: CompleteGameBoardSnapshot,
    provider: ProviderSelectionBinding,
):
    evidence = PreEvaluationEvidenceAuthority(
        PreEvaluationPolicy(max_age_ns=200)
    ).evaluate_session(
        session_id="session-1",
        candidate_ids=(provider.row_key,),
        resolver=lambda _: None,
        evaluated_at_ns=1000,
    )
    context = PreEvaluationDenominatorContext(
        session_id="session-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=SHA_A,
        provider_evidence_sha256=snapshot.evidence_sha256,
    )
    return bind_pre_evaluation_session(
        evidence,
        context=context,
        provider_members=(provider.identity,),
    )


def _resolution_inputs(snapshot: CompleteGameBoardSnapshot, provider: ProviderSelectionBinding):
    book = PaperBook("10000")
    intents = ()
    return {
        "snapshot": snapshot,
        "bound": _bound(snapshot, provider),
        "provider_selections": (provider,),
        "intents": intents,
        "risk_policy": _risk_policy(),
        "book": book,
        "dependency_graph": PortfolioDependencyGraph.for_inputs(book, intents),
    }


def test_directly_constructed_product_origin_is_not_authoritative() -> None:
    origin = PreEvaluationProductOrigin(
        bound_authority_digest=SHA_A,
        denominator_context_digest=SHA_A,
        provider_evidence_sha256=SHA_A,
        provider_bindings_sha256=SHA_A,
        intent_sha256s=(),
        durable_decision_context_hash=SHA_A,
        risk_policy_sha256=SHA_A,
        portfolio_sha256=SHA_A,
        dependency_graph_sha256=SHA_A,
        cost_contract_sha256=SHA_A,
    )

    with pytest.raises(PreEvaluationProductOriginError, match="not issued"):
        assert_pre_evaluation_product_origin_authoritative(origin)


def test_structurally_valid_but_unissued_provider_snapshot_cannot_mint_origin(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    provider = _provider(snapshot)
    inputs = _resolution_inputs(snapshot, provider)

    with pytest.raises(PreEvaluationProductOriginError, match="runtime-issued"):
        resolve_pre_evaluation_product_origin(
            **inputs,
            ledger=JsonlDecisionLedger(tmp_path / "ledger.jsonl"),
            material_action_id="material-action-1",
        )


def test_issued_provider_snapshot_cannot_authorize_absent_selection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    snapshot = _issued_snapshot(monkeypatch)
    provider = _provider(snapshot, selection_id="bookmaker-1:h2h:draw")
    inputs = _resolution_inputs(snapshot, provider)

    with pytest.raises(PreEvaluationProductOriginError, match="absent"):
        resolve_pre_evaluation_product_origin(
            **inputs,
            ledger=JsonlDecisionLedger(tmp_path / "ledger.jsonl"),
            material_action_id="material-action-1",
        )
