from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext, RiskOfRuinEvidence
from autosport.risk_of_ruin_authority import risk_of_ruin_result_sha256
from autosport.scientific_registry import ScientificRegistry


PROPOSAL_TS = "2026-09-16T15:00:02+00:00"
EVALUATED_AT = "2026-09-16T15:00:01+00:00"
CAUSAL_CUTOFF = "2026-09-16T14:59:58+00:00"
DATASET_AVAILABLE_AT = "2026-09-16T14:59:59+00:00"
DATASET_MANIFEST = "9" * 64
EVALUATOR_SOURCE = "e" * 64
SAMPLE_SIZE = 1000


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-ror-read-dispatch",
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


def _policy(registry_path) -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=_goal(),
        risk_of_ruin_registry_path=registry_path,
    )


def _context() -> ProposedTicketRiskContext:
    leg = TicketLeg(
        "event-read-dispatch",
        "market-read-dispatch",
        "selection-read-dispatch",
        Decimal("2"),
    )
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
    return ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(quote,),
        bankroll_id="paper-bankroll",
        currency="USD",
        proposal_ts=PROPOSAL_TS,
    )


def _evidence(
    policy: PaperRiskPolicy,
    book: PaperBook,
    context: ProposedTicketRiskContext,
) -> RiskOfRuinEvidence:
    portfolio = policy.risk_of_ruin_portfolio_sha256(book)
    candidate = policy.risk_of_ruin_candidate_sha256(context)
    assert portfolio is not None and candidate is not None
    return RiskOfRuinEvidence(
        evidence_id="read-dispatch-forged",
        research_protocol_sha256="a" * 64,
        reproducibility_bundle_sha256="b" * 64,
        producer_identity="forged-read-dispatch",
        causal_cutoff=CAUSAL_CUTOFF,
        evaluated_at=EVALUATED_AT,
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256=portfolio,
        candidate_sha256=candidate,
        evaluated_stake=Decimal("1"),
        upper_bound=Decimal("0.005"),
    )


def test_rebound_registry_get_cannot_fabricate_product_issued_ruin_authority(
    tmp_path, monkeypatch
) -> None:
    """A rebound registry reader must never become financial authorization.

    The verifier is expected to resolve durable product-owned truth through a
    trusted read path. Rebinding ScientificRegistry.get must therefore fail
    closed even when the forged records are internally self-consistent and
    carry the exact public result digest.
    """

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context()
    evidence = _evidence(policy, book, context)

    result_sha256 = risk_of_ruin_result_sha256(
        evidence,
        kind="single",
        evaluator_source_sha256=EVALUATOR_SOURCE,
        dataset_snapshot_id="risk-dataset",
        dataset_manifest_sha256=DATASET_MANIFEST,
        effective_sample_size=SAMPLE_SIZE,
        evaluation_available_at=EVALUATED_AT,
    )

    evaluation_entry = SimpleNamespace(
        available_at=EVALUATED_AT,
        payload={
            "created_at": EVALUATED_AT,
            "bundle_sha256": evidence.reproducibility_bundle_sha256,
            "protocol_sha256": evidence.research_protocol_sha256,
            "dataset_snapshot_id": "risk-dataset",
            "evaluator_source_sha256": EVALUATOR_SOURCE,
            "effective_sample_size": SAMPLE_SIZE,
            "artifact_hashes": [result_sha256],
        },
    )
    dataset_entry = SimpleNamespace(
        available_at=DATASET_AVAILABLE_AT,
        payload={
            "causal_cutoff": CAUSAL_CUTOFF,
            "manifest_sha256": DATASET_MANIFEST,
        },
    )

    original_get = ScientificRegistry.get

    def forged_get(self, record_type, record_id):
        if record_type == "EvaluationBundle" and record_id == evidence.evidence_id:
            return evaluation_entry
        if record_type == "DatasetSnapshot" and record_id == "risk-dataset":
            return dataset_entry
        return original_get(self, record_type, record_id)

    monkeypatch.setattr(ScientificRegistry, "get", forged_get)

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed, (
        "rebound ScientificRegistry.get fabricated positive product-issued "
        "risk-of-ruin authority"
    )
