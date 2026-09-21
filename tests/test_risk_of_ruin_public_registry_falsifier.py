from dataclasses import replace
from decimal import Decimal

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext, RiskOfRuinEvidence
from autosport.risk_of_ruin_authority import risk_of_ruin_result_sha256
from autosport.scientific_registry import DatasetSnapshot, EvaluationBundleRef, ScientificRegistry


PROPOSAL_TS = "2026-09-16T15:00:02+00:00"
EVALUATED_AT = "2026-09-16T15:00:01+00:00"
CAUSAL_CUTOFF = "2026-09-16T14:59:58+00:00"
DATASET_MANIFEST = "9" * 64
EVALUATOR_SOURCE = "e" * 64
SAMPLE_SIZE = 1000


def test_public_registry_construction_cannot_mint_product_risk_authority(tmp_path) -> None:
    """A caller-owned registry file must never become product-issued authority."""

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "caller-owned-scientific-registry.json"
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="caller-risk-dataset",
            manifest_sha256=DATASET_MANIFEST,
            source_identity="caller-controlled",
            license_identity="caller-controlled",
            causal_cutoff=CAUSAL_CUTOFF,
            available_at_utc="2026-09-16T14:59:59+00:00",
        )
    )

    goal = EconomicGoalContract(
        goal_id="goal-public-registry-mint-falsifier",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("1"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("1000"),
        max_risk_of_ruin=Decimal("0.01"),
        max_concurrent_positions=10,
    )
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
        risk_of_ruin_registry_path=registry.path,
    )
    book = PaperBook("100")
    leg = TicketLeg("event-1", "market-1", "selection-1", Decimal("2"))
    quote = MarketEvent(
        event_id=leg.event_id,
        market_id=leg.market_id,
        selection_id=leg.selection_id,
        decimal_odds=leg.locked_odds,
        observed_ts="2026-09-16T15:00:00+00:00",
        source_id="provider-1",
        sequence=1,
        source_ts="2026-09-16T14:59:59+00:00",
        ingest_ts="2026-09-16T15:00:01+00:00",
    )
    context = ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(quote,),
        bankroll_id="paper-bankroll",
        currency="USD",
        proposal_ts=PROPOSAL_TS,
    )

    portfolio = policy.risk_of_ruin_portfolio_sha256(book)
    candidate = policy.risk_of_ruin_candidate_sha256(context)
    assert portfolio is not None
    assert candidate is not None

    evidence = RiskOfRuinEvidence(
        evidence_id="caller-minted-risk-authority",
        research_protocol_sha256="a" * 64,
        reproducibility_bundle_sha256="b" * 64,
        producer_identity="caller-controlled-evaluator",
        causal_cutoff=CAUSAL_CUTOFF,
        evaluated_at=EVALUATED_AT,
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256=portfolio,
        candidate_sha256=candidate,
        evaluated_stake=Decimal("1"),
        upper_bound=Decimal("0"),
    )
    result_sha256 = risk_of_ruin_result_sha256(
        evidence,
        kind="single",
        evaluator_source_sha256=EVALUATOR_SOURCE,
        dataset_snapshot_id="caller-risk-dataset",
        dataset_manifest_sha256=DATASET_MANIFEST,
        effective_sample_size=SAMPLE_SIZE,
        evaluation_available_at=EVALUATED_AT,
    )
    registry.append(
        EvaluationBundleRef(
            evaluation_bundle_id=evidence.evidence_id,
            bundle_sha256=evidence.reproducibility_bundle_sha256,
            evaluator_source_sha256=EVALUATOR_SOURCE,
            dataset_snapshot_id="caller-risk-dataset",
            protocol_sha256=evidence.research_protocol_sha256,
            artifact_hashes=(result_sha256,),
            created_at=EVALUATED_AT,
            effective_sample_size=SAMPLE_SIZE,
        )
    )

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed, (
        "caller-created ScientificRegistry + public append must not mint "
        "product-issued risk-of-ruin authority"
    )
