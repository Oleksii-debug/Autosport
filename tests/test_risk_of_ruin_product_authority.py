from dataclasses import replace
from decimal import Decimal
import subprocess
import sys


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


def test_decision_ledger_and_risk_cold_import_without_authority_cycle() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import autosport.decision_ledger as decision_ledger; "
                "import autosport.risk as risk; "
                "assert decision_ledger.DecisionRecord is not None; "
                "assert risk.PaperRiskPolicy is not None"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


PROPOSAL_TS = "2026-09-16T15:00:02+00:00"
EVALUATED_AT = "2026-09-16T15:00:01+00:00"
CAUSAL_CUTOFF = "2026-09-16T14:59:58+00:00"
DATASET_MANIFEST = "9" * 64
EVALUATOR_SOURCE = "e" * 64
SAMPLE_SIZE = 1000


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


def _registry(tmp_path, *, outcome_reveal_after=None) -> ScientificRegistry:
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="risk-dataset",
            manifest_sha256=DATASET_MANIFEST,
            source_identity="risk-fixture",
            license_identity="internal-test",
            causal_cutoff=CAUSAL_CUTOFF,
            available_at_utc="2026-09-16T14:59:59+00:00",
            outcome_reveal_after=outcome_reveal_after,
        )
    )
    return registry


def _issue(
    registry: ScientificRegistry,
    evidence,
    *,
    kind: str,
    created_at=EVALUATED_AT,
    evaluator_source=EVALUATOR_SOURCE,
    artifact_evaluator_source=None,
) -> None:
    artifact_evaluator_source = artifact_evaluator_source or evaluator_source
    registry.append(
        EvaluationBundleRef(
            evaluation_bundle_id=evidence.evidence_id,
            bundle_sha256=evidence.reproducibility_bundle_sha256,
            evaluator_source_sha256=evaluator_source,
            dataset_snapshot_id="risk-dataset",
            protocol_sha256=evidence.research_protocol_sha256,
            artifact_hashes=(
                risk_of_ruin_result_sha256(
                    evidence,
                    kind=kind,
                    evaluator_source_sha256=artifact_evaluator_source,
                    dataset_snapshot_id="risk-dataset",
                    dataset_manifest_sha256=DATASET_MANIFEST,
                    effective_sample_size=SAMPLE_SIZE,
                    evaluation_available_at=created_at,
                ),
            ),
            created_at=created_at,
            effective_sample_size=SAMPLE_SIZE,
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


def test_caller_cannot_mint_single_candidate_risk_of_ruin_authority() -> None:
    """Freeze #955: public hashes plus a caller bound must not grant authority."""

    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    portfolio = policy.risk_of_ruin_portfolio_sha256(book)
    candidate = policy.risk_of_ruin_candidate_sha256(context)
    assert portfolio is not None and candidate is not None

    caller_minted = RiskOfRuinEvidence(
        evidence_id="caller-minted-ror",
        research_protocol_sha256="a" * 64,
        reproducibility_bundle_sha256="b" * 64,
        producer_identity="caller-claims-to-be-risk-model",
        causal_cutoff=CAUSAL_CUTOFF,
        evaluated_at=EVALUATED_AT,
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256=portfolio,
        candidate_sha256=candidate,
        evaluated_stake=Decimal("1"),
        upper_bound=Decimal("0"),
    )

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=caller_minted),
    )

    assert not decision.allowed


def test_caller_cannot_mint_vector_risk_of_ruin_authority() -> None:
    """Freeze #955: a caller-authored whole-vector witness is assertion-only."""

    policy = _policy()
    book = PaperBook("100")
    contexts = (_context(1), _context(2))
    portfolio = policy.risk_of_ruin_portfolio_sha256(book)
    candidate_vector = policy.risk_of_ruin_candidate_vector_sha256(contexts)
    assert portfolio is not None and candidate_vector is not None

    caller_minted = RiskOfRuinVectorEvidence(
        evidence_id="caller-minted-vector-ror",
        research_protocol_sha256="c" * 64,
        reproducibility_bundle_sha256="d" * 64,
        producer_identity="caller-claims-to-be-vector-risk-model",
        causal_cutoff=CAUSAL_CUTOFF,
        evaluated_at=EVALUATED_AT,
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256=portfolio,
        candidate_vector_sha256=candidate_vector,
        evaluated_stakes=(Decimal("1"), Decimal("1")),
        upper_bound=Decimal("0"),
    )

    decision = policy.derive_goal_stake_vector(
        book,
        (Decimal("0.01"), Decimal("0.01")),
        contexts=contexts,
        risk_of_ruin_vector_evidence=caller_minted,
    )

    assert decision.action != "STAKE_VECTOR"


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


def test_public_registry_rows_remain_assertion_only_after_restart(tmp_path) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    _issue(registry, evidence, kind="single")

    restarted = _policy(registry.path)
    rejected = restarted.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )
    assert not rejected.allowed
    assert "lacks canonical product-issued evaluator authority" in rejected.reason

    tampered = replace(evidence, upper_bound=Decimal("0.001"))
    tampered_rejected = restarted.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=tampered),
    )
    assert not tampered_rejected.allowed
    assert "result digest does not match" in tampered_rejected.reason


def test_copied_result_digest_cannot_substitute_evaluator_method_identity(tmp_path) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = replace(_single_evidence(policy, book, context), evidence_id="method-substitution")
    _issue(
        registry,
        evidence,
        kind="single",
        evaluator_source="f" * 64,
        artifact_evaluator_source=EVALUATOR_SOURCE,
    )

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )
    assert not decision.allowed
    assert "result digest does not match" in decision.reason


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


def test_dataset_outcomes_must_be_revealed_by_evaluation_time(tmp_path) -> None:
    registry = _registry(
        tmp_path,
        outcome_reveal_after="2026-09-16T15:00:03+00:00",
    )
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = replace(
        _single_evidence(policy, book, context),
        evidence_id="future-outcome",
    )
    _issue(registry, evidence, kind="single")

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed
    assert "outcomes were not causally available at evaluation time" in decision.reason


def test_rebound_registry_read_dispatch_cannot_retarget_captured_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = replace(
        _single_evidence(policy, book, context),
        evidence_id="rebound-read-dispatch",
    )
    _issue(registry, evidence, kind="single")

    monkeypatch.setattr(
        ScientificRegistry,
        "_read",
        lambda self: {"schema_version": 1, "records": []},
    )

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed
    assert "lacks canonical product-issued evaluator authority" in decision.reason


def test_rebound_registry_get_fails_closed_before_authority_use(tmp_path, monkeypatch) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = replace(
        _single_evidence(policy, book, context),
        evidence_id="rebound-read",
    )
    _issue(registry, evidence, kind="single")

    monkeypatch.setattr(
        ScientificRegistry,
        "get",
        lambda self, record_type, record_id: None,
    )

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed
    assert "durable authority is invalid" in decision.reason


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
    asserted_only = _policy(registry.path).derive_goal_stake_vector(
        book,
        signals,
        contexts=contexts,
        risk_of_ruin_vector_evidence=evidence,
    )
    assert asserted_only.action != "STAKE_VECTOR"
