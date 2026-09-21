from dataclasses import replace
from decimal import Decimal

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import (
    PaperRiskPolicy,
    ProposedTicketRiskContext,
    RiskOfRuinEvidence,
    RiskOfRuinVectorEvidence,
)
from autosport.risk_of_ruin_authority import risk_of_ruin_result_sha256
from autosport.scientific_registry import DatasetSnapshot, EvaluationBundleRef, ScientificRegistry


PROPOSAL_TS = "2026-09-16T15:00:02+00:00"
EVALUATED_AT = "2026-09-16T15:00:01+00:00"
CAUSAL_CUTOFF = "2026-09-16T14:59:58+00:00"


def _goal(*, max_risk_of_ruin: Decimal = Decimal("0.01")) -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-ror-product-authority",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("1"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("1000"),
        max_risk_of_ruin=max_risk_of_ruin,
        max_concurrent_positions=10,
    )


def _policy(registry_path=None, *, max_risk_of_ruin=Decimal("0.01")) -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=_goal(max_risk_of_ruin=max_risk_of_ruin),
        risk_of_ruin_registry_path=registry_path,
    )


def _context(index: int) -> ProposedTicketRiskContext:
    leg = TicketLeg(
        f"event-{index}",
        f"market-{index}",
        f"selection-{index}",
        Decimal("2"),
    )
    quote = MarketEvent(
        event_id=leg.event_id,
        market_id=leg.market_id,
        selection_id=leg.selection_id,
        decimal_odds=leg.locked_odds,
        observed_ts="2026-09-16T15:00:00+00:00",
        source_id="provider-1",
        sequence=index,
        source_ts="2026-09-16T14:59:59+00:00",
        ingest_ts="2026-09-16T15:00:01+00:00",
    )
    return ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(quote,),
        bankroll_id="paper-bankroll",
        currency="USD",
        proposal_ts=PROPOSAL_TS,
    )


def _registry(tmp_path) -> ScientificRegistry:
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="risk-dataset",
            manifest_sha256="9" * 64,
            source_identity="risk-fixture",
            license_identity="internal-test",
            causal_cutoff=CAUSAL_CUTOFF,
            available_at_utc="2026-09-16T14:59:59+00:00",
        )
    )
    return registry


def _issue(registry: ScientificRegistry, evidence, *, kind: str, created_at=EVALUATED_AT) -> None:
    registry.append(
        EvaluationBundleRef(
            evaluation_bundle_id=evidence.evidence_id,
            bundle_sha256=evidence.reproducibility_bundle_sha256,
            evaluator_source_sha256="e" * 64,
            dataset_snapshot_id="risk-dataset",
            protocol_sha256=evidence.research_protocol_sha256,
            artifact_hashes=(risk_of_ruin_result_sha256(evidence, kind=kind),),
            created_at=created_at,
            effective_sample_size=1000,
        )
    )


def _single_evidence(policy: PaperRiskPolicy, book: PaperBook, context: ProposedTicketRiskContext):
    portfolio = policy.risk_of_ruin_portfolio_sha256(book)
    candidate = policy.risk_of_ruin_candidate_sha256(context)
    assert portfolio is not None and candidate is not None
    return RiskOfRuinEvidence(
        evidence_id="issued-single-ror",
        research_protocol_sha256="a" * 64,
        reproducibility_bundle_sha256="b" * 64,
        producer_identity="canonical-risk-evaluator",
        causal_cutoff=CAUSAL_CUTOFF,
        evaluated_at=EVALUATED_AT,
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256=portfolio,
        candidate_sha256=candidate,
        evaluated_stake=Decimal("1"),
        upper_bound=Decimal("0.005"),
    )


def test_caller_constructed_single_evidence_fails_closed_without_issuance(tmp_path) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed
    assert "not product-issued" in decision.reason


def test_product_issued_single_evidence_survives_restart_and_tamper_fails(tmp_path) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    _issue(registry, evidence, kind="single")

    restarted = _policy(registry.path)
    allowed = restarted.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )
    assert allowed.allowed

    tampered = replace(evidence, upper_bound=Decimal("0.001"))
    rejected = restarted.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=tampered),
    )
    assert not rejected.allowed
    assert "result digest does not match" in rejected.reason


def test_bundle_created_after_proposal_cannot_retroactively_authorize(tmp_path) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = replace(_single_evidence(policy, book, context), evidence_id="future-bundle")
    _issue(registry, evidence, kind="single", created_at="2026-09-16T15:00:03+00:00")

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )
    assert not decision.allowed
    assert "not available at proposal time" in decision.reason


def test_vector_evidence_requires_exact_product_issued_result(tmp_path) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    relaxed = _policy(registry.path, max_risk_of_ruin=Decimal("1"))
    book = PaperBook("100")
    contexts = (_context(1), _context(2))
    signals = (Decimal("0.01"), Decimal("0.01"))
    baseline = relaxed.derive_goal_stake_vector(book, signals, contexts=contexts)
    assert baseline.action == "STAKE_VECTOR"

    portfolio = policy.risk_of_ruin_portfolio_sha256(book)
    candidates = policy.risk_of_ruin_candidate_vector_sha256(contexts)
    assert portfolio is not None and candidates is not None
    evidence = RiskOfRuinVectorEvidence(
        evidence_id="issued-vector-ror",
        research_protocol_sha256="c" * 64,
        reproducibility_bundle_sha256="d" * 64,
        producer_identity="canonical-vector-risk-evaluator",
        causal_cutoff=CAUSAL_CUTOFF,
        evaluated_at=EVALUATED_AT,
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256=portfolio,
        candidate_vector_sha256=candidates,
        evaluated_stakes=baseline.stakes,
        upper_bound=Decimal("0.005"),
    )

    forged = policy.derive_goal_stake_vector(
        book,
        signals,
        contexts=contexts,
        risk_of_ruin_vector_evidence=evidence,
    )
    assert forged.action != "STAKE_VECTOR"

    _issue(registry, evidence, kind="vector")
    issued = _policy(registry.path).derive_goal_stake_vector(
        book,
        signals,
        contexts=contexts,
        risk_of_ruin_vector_evidence=evidence,
    )
    assert issued.action == "STAKE_VECTOR"
    assert issued.stakes == baseline.stakes
