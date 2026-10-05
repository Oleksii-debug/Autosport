from dataclasses import replace
from decimal import Decimal
import subprocess
import sys

import autosport.risk as risk_module
import autosport.risk_of_ruin_authority as authority_module
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



def test_risk_state_graph_ignores_rebound_paperbook_economic_roots(monkeypatch) -> None:
    book = PaperBook("100")
    context = _context(1)
    book.open_ticket(
        context.legs,
        Decimal("1"),
        placed_at=PROPOSAL_TS,
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    baseline_state = PaperRiskPolicy._book_state(book)
    baseline_metrics = PaperRiskPolicy._historical_risk_metrics(book)
    baseline_shadow = PaperRiskPolicy._shadow_book_for_allocation(book)
    assert baseline_state is not None
    assert baseline_metrics is not None
    assert baseline_shadow is not None

    calls = {
        "validate": 0,
        "lifecycle": 0,
        "debit": 0,
        "settlement": 0,
    }

    def hostile_validate(cls, candidate):
        calls["validate"] += 1
        raise AssertionError("live PaperBook state validator reached risk graph")

    def hostile_lifecycle(cls, entry):
        calls["lifecycle"] += 1
        raise AssertionError("live PaperBook lifecycle validator reached risk graph")

    def hostile_debit(cls, balance, amount):
        calls["debit"] += 1
        raise AssertionError("live PaperBook debit dispatch reached risk graph")

    def hostile_settlement(cls, ticket, balance, winners, voids):
        calls["settlement"] += 1
        raise AssertionError("live PaperBook settlement dispatch reached risk graph")

    monkeypatch.setattr(PaperBook, "_validate_loaded_state", classmethod(hostile_validate))
    monkeypatch.setattr(
        PaperBook,
        "_validate_lifecycle_entry",
        classmethod(hostile_lifecycle),
    )
    monkeypatch.setattr(PaperBook, "_debit_balance", classmethod(hostile_debit))
    monkeypatch.setattr(
        PaperBook,
        "_settlement_result",
        classmethod(hostile_settlement),
    )

    assert PaperRiskPolicy._book_state(book) == baseline_state
    assert PaperRiskPolicy._historical_risk_metrics(book) == baseline_metrics
    shadow = PaperRiskPolicy._shadow_book_for_allocation(book)
    assert shadow is not None
    assert shadow.balance == baseline_shadow.balance
    assert calls == {
        "validate": 0,
        "lifecycle": 0,
        "debit": 0,
        "settlement": 0,
    }



def test_single_evidence_ingress_ignores_rebound_validator(monkeypatch) -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    calls = {"validator": 0}

    def hostile_validator(self):
        calls["validator"] += 1
        raise AssertionError("live RiskOfRuinEvidence validator executed")

    monkeypatch.setattr(RiskOfRuinEvidence, "__post_init__", hostile_validator)

    rebound_context = replace(context, risk_of_ruin_evidence=evidence)
    decision = policy.evaluate(book, Decimal("1"), context=rebound_context)

    assert not decision.allowed
    assert calls == {"validator": 0}


def test_policy_state_ignores_rebound_economic_goal_validator(monkeypatch) -> None:
    policy = _policy(max_risk_of_ruin=Decimal("1"))
    book = PaperBook("100")
    context = _context(1)
    calls = {"validator": 0}

    def hostile_validator(self):
        calls["validator"] += 1
        raise AssertionError("live EconomicGoalContract validator executed")

    monkeypatch.setattr(EconomicGoalContract, "__post_init__", hostile_validator)

    decision = policy.evaluate(book, Decimal("1"), context=context)

    assert decision.allowed
    assert calls == {"validator": 0}


def test_vector_evidence_ingress_ignores_rebound_validator(monkeypatch) -> None:
    policy = _policy()
    relaxed = _policy(max_risk_of_ruin=Decimal("1"))
    book = PaperBook("100")
    contexts = (_context(1), _context(2))
    signals = (Decimal("0.01"), Decimal("0.01"))
    baseline = relaxed.derive_goal_stake_vector(book, signals, contexts=contexts)
    assert baseline.action == "STAKE_VECTOR"

    portfolio = policy.risk_of_ruin_portfolio_sha256(book)
    candidate_vector = policy.risk_of_ruin_candidate_vector_sha256(contexts)
    assert portfolio is not None
    assert candidate_vector is not None
    evidence = RiskOfRuinVectorEvidence(
        evidence_id="validator-rebind-vector",
        research_protocol_sha256="c" * 64,
        reproducibility_bundle_sha256="d" * 64,
        producer_identity="canonical-vector-risk-evaluator",
        causal_cutoff=CAUSAL_CUTOFF,
        evaluated_at=EVALUATED_AT,
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256=portfolio,
        candidate_vector_sha256=candidate_vector,
        evaluated_stakes=baseline.stakes,
        upper_bound=Decimal("0.005"),
    )
    calls = {"validator": 0}

    def hostile_validator(self):
        calls["validator"] += 1
        raise AssertionError("live RiskOfRuinVectorEvidence validator executed")

    monkeypatch.setattr(
        RiskOfRuinVectorEvidence,
        "__post_init__",
        hostile_validator,
    )

    decision = policy.derive_goal_stake_vector(
        book,
        signals,
        contexts=contexts,
        risk_of_ruin_vector_evidence=evidence,
    )

    assert decision.action != "STAKE_VECTOR"
    assert calls == {"validator": 0}


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
    assert "durable authority is invalid" in decision.reason


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


def test_rebound_policy_authority_helper_cannot_grant_positive_stake(
    monkeypatch,
) -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    attacker_calls = 0

    def forged_verifier(*args, **kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        del args, kwargs
        return True, "forged positive authority"

    monkeypatch.setattr(
        risk_module,
        "_verify_product_risk_of_ruin_authority",
        forged_verifier,
    )

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed
    assert "product authority dispatch changed" in decision.reason
    assert attacker_calls == 0


def test_rebound_transitive_authority_verifier_cannot_grant_positive_stake(
    monkeypatch,
) -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    attacker_calls = 0

    def forged_verifier(*args, **kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        del args, kwargs
        return True, "forged transitive authority"

    monkeypatch.setattr(
        authority_module,
        "verify_risk_of_ruin_authority",
        forged_verifier,
    )

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed
    assert "product authority verifier changed" in decision.reason
    assert attacker_calls == 0


def test_policy_authority_gate_preserves_current_main_root_identity() -> None:
    assert PaperRiskPolicy.__bases__ == (object,)
    assert type(PaperRiskPolicy).__module__ == risk_module.__name__
    assert "_PaperRiskPolicyCore" not in vars(risk_module)
    assert "risk_of_ruin_registry_path" in PaperRiskPolicy.__slots__
    assert "_PRODUCT_RISK_AUTHORITY_DISPATCH" not in vars(risk_module)

    for name in (
        "_risk_of_ruin_evidence_decision",
        "_risk_of_ruin_vector_evidence_decision",
    ):
        descriptor = vars(PaperRiskPolicy)[name]
        assert isinstance(descriptor, classmethod)
        method = descriptor.__func__
        assert method.__closure__ is None
        assert "_PRODUCT_RISK_AUTHORITY_DISPATCH" not in method.__code__.co_names


def test_vector_rebound_policy_authority_helper_cannot_grant_positive_stake(
    monkeypatch,
) -> None:
    policy = _policy()
    relaxed = _policy(max_risk_of_ruin=Decimal("1"))
    book = PaperBook("100")
    contexts = (_context(1), _context(2))
    signals = (Decimal("0.01"), Decimal("0.01"))
    baseline = relaxed.derive_goal_stake_vector(book, signals, contexts=contexts)
    assert baseline.action == "STAKE_VECTOR"

    portfolio = policy.risk_of_ruin_portfolio_sha256(book)
    candidates = policy.risk_of_ruin_candidate_vector_sha256(contexts)
    assert portfolio is not None and candidates is not None
    evidence = RiskOfRuinVectorEvidence(
        evidence_id="rebound-vector-authority-helper",
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
    attacker_calls = 0

    def forged_verifier(*args, **kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        del args, kwargs
        return True, "forged positive vector authority"

    monkeypatch.setattr(
        risk_module,
        "_verify_product_risk_of_ruin_authority",
        forged_verifier,
    )

    decision = policy.derive_goal_stake_vector(
        book,
        signals,
        contexts=contexts,
        risk_of_ruin_vector_evidence=evidence,
    )

    assert decision.action != "STAKE_VECTOR"
    assert "product authority dispatch changed" in decision.reason
    assert attacker_calls == 0


def test_injected_module_dispatch_handle_cannot_replace_policy_closure(
    monkeypatch,
) -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    attacker_calls = 0

    def forged_dispatch(*args, **kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        del args, kwargs
        return True, "forged dispatcher"

    monkeypatch.setattr(
        risk_module,
        "_PRODUCT_RISK_AUTHORITY_DISPATCH",
        forged_dispatch,
        raising=False,
    )

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed
    assert attacker_calls == 0


def test_policy_authority_helper_code_swap_cannot_grant_positive_stake(
    monkeypatch,
) -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    canonical = risk_module._verify_product_risk_of_ruin_authority

    def forged_verifier(*args, **kwargs):
        del args, kwargs
        return True, "forged positive authority"

    monkeypatch.setattr(canonical, "__code__", forged_verifier.__code__)

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed
    assert "product authority dispatch changed" in decision.reason


def test_registry_read_code_swap_fails_closed_before_authority_use(
    tmp_path,
    monkeypatch,
) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = replace(
        _single_evidence(policy, book, context),
        evidence_id="registry-read-code-swap",
    )
    _issue(registry, evidence, kind="single")
    canonical = ScientificRegistry._read

    def forged_read(self):
        del self
        return {"schema_version": 1, "records": []}

    monkeypatch.setattr(canonical, "__code__", forged_read.__code__)

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed
    assert "durable authority is invalid" in decision.reason


def test_registry_validator_code_swap_fails_closed_before_authority_use(
    tmp_path,
    monkeypatch,
) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = replace(
        _single_evidence(policy, book, context),
        evidence_id="registry-validator-code-swap",
    )
    _issue(registry, evidence, kind="single")
    canonical = ScientificRegistry._validate_entry

    def forged_validate(raw):
        del raw
        return None

    monkeypatch.setattr(canonical, "__code__", forged_validate.__code__)

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed
    assert "durable authority is invalid" in decision.reason


def test_rebound_product_evaluator_resolver_fails_before_attacker_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = replace(
        _single_evidence(policy, book, context),
        evidence_id="rebound-product-resolver",
    )
    _issue(registry, evidence, kind="single")
    attacker_calls = 0

    def forged_resolver(*args, **kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        del args, kwargs
        raise AssertionError("forged resolver executed")

    monkeypatch.setattr(
        authority_module,
        "_resolve_product_evaluator_result",
        forged_resolver,
    )

    decision = policy.evaluate(
        book,
        Decimal("1"),
        context=replace(context, risk_of_ruin_evidence=evidence),
    )

    assert not decision.allowed
    assert "lacks canonical product-issued evaluator authority" in decision.reason
    assert attacker_calls == 0



class _ForgedDecimal(Decimal):
    def is_finite(self):
        return True

    def __format__(self, spec):
        del spec
        return "0"


def test_decimal_subclasses_cannot_enter_risk_authority() -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    canonical = _single_evidence(policy, book, context)

    try:
        replace(canonical, evaluated_stake=_ForgedDecimal("1"))
    except ValueError as exc:
        assert "evaluated_stake" in str(exc)
    else:
        raise AssertionError("Decimal subclass entered single risk evidence")

    try:
        replace(canonical, upper_bound=_ForgedDecimal("0.005"))
    except ValueError as exc:
        assert "upper_bound" in str(exc)
    else:
        raise AssertionError("Decimal subclass entered single risk evidence")

    contexts = (_context(1), _context(2))
    relaxed = _policy(max_risk_of_ruin=Decimal("1"))
    baseline = relaxed.derive_goal_stake_vector(
        book,
        (Decimal("0.01"), Decimal("0.01")),
        contexts=contexts,
    )
    assert baseline.action == "STAKE_VECTOR"
    portfolio = policy.risk_of_ruin_portfolio_sha256(book)
    candidates = policy.risk_of_ruin_candidate_vector_sha256(contexts)
    assert portfolio is not None and candidates is not None

    vector = RiskOfRuinVectorEvidence(
        evidence_id="exact-decimal-vector",
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

    try:
        replace(vector, evaluated_stakes=(_ForgedDecimal("1"), Decimal("1")))
    except ValueError as exc:
        assert "evaluated_stakes" in str(exc)
    else:
        raise AssertionError("Decimal subclass entered vector risk evidence")

    try:
        replace(vector, upper_bound=_ForgedDecimal("0.005"))
    except ValueError as exc:
        assert "upper_bound" in str(exc)
    else:
        raise AssertionError("Decimal subclass entered vector risk evidence")



def test_risk_evidence_validation_ignores_public_helper_rebinding(
    monkeypatch,
) -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)

    monkeypatch.setattr(risk_module, "_canonical_context_text", lambda *args: "")
    monkeypatch.setattr(
        risk_module,
        "_canonical_context_timestamp",
        lambda *args: ("", None),
    )
    monkeypatch.setattr(risk_module, "_canonical_sha256", lambda *args: "")
    monkeypatch.setattr(risk_module, "Decimal", _ForgedDecimal)
    monkeypatch.setattr(risk_module, "datetime", object())

    object.__setattr__(evidence, "causal_cutoff", "2026-09-16T15:00:02+00:00")
    object.__setattr__(evidence, "evaluated_at", "2026-09-16T15:00:01+00:00")

    try:
        risk_module._CANONICAL_RISK_OF_RUIN_EVIDENCE_VALIDATOR(evidence)
    except ValueError as exc:
        assert "causal cutoff must not be after evaluation time" in str(exc)
    else:
        raise AssertionError("rebound public helpers weakened canonical validation")


def test_result_digest_ignores_public_semantic_constant_rebinding(
    monkeypatch,
) -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    kwargs = dict(
        kind="single",
        evaluator_source_sha256=EVALUATOR_SOURCE,
        dataset_snapshot_id="risk-dataset",
        dataset_manifest_sha256=DATASET_MANIFEST,
        effective_sample_size=SAMPLE_SIZE,
        evaluation_available_at=EVALUATED_AT,
    )
    expected = risk_of_ruin_result_sha256(evidence, **kwargs)

    monkeypatch.setattr(authority_module, "_AUTHORITY_KIND", "forged-kind")
    monkeypatch.setattr(authority_module, "_BOUND_SEMANTICS", "forged-bound")
    monkeypatch.setattr(
        authority_module,
        "_CONFIDENCE_SEMANTICS",
        "forged-confidence",
    )
    monkeypatch.setattr(authority_module, "RiskTargetKind", object())
    monkeypatch.setattr(authority_module, "IssuedRiskOfRuinResult", object())

    assert risk_of_ruin_result_sha256(evidence, **kwargs) == expected


def test_result_digest_ignores_public_authority_helper_rebinding(
    monkeypatch,
) -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)

    kwargs = dict(
        kind="single",
        evaluator_source_sha256=EVALUATOR_SOURCE,
        dataset_snapshot_id="risk-dataset",
        dataset_manifest_sha256=DATASET_MANIFEST,
        effective_sample_size=SAMPLE_SIZE,
        evaluation_available_at=EVALUATED_AT,
    )
    expected = risk_of_ruin_result_sha256(evidence, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("rebound authority helper executed")

    for name in (
        "_fixed_point_materialization_length",
        "_canonical_decimal",
        "_bounded_text",
        "_canonical_sha256",
        "_instant",
        "_payload",
        "Path",
    ):
        monkeypatch.setattr(authority_module, name, forbidden)
    monkeypatch.setattr(authority_module.json, "dumps", forbidden)
    monkeypatch.setattr(authority_module.hashlib, "sha256", forbidden)

    assert risk_of_ruin_result_sha256(evidence, **kwargs) == expected


def test_registry_authority_ignores_public_type_and_reader_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    registry = _registry(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("rebound registry authority dependency executed")

    monkeypatch.setattr(authority_module, "ScientificRegistry", forbidden)
    monkeypatch.setattr(authority_module, "RegistryEntry", forbidden)
    monkeypatch.setattr(
        authority_module,
        "require_scientific_registry_read_authority",
        forbidden,
    )

    state = authority_module._read_exact_registry_state(registry)
    entry = authority_module._entry_from_state(
        state,
        "DatasetSnapshot",
        "risk-dataset",
    )

    assert entry is not None
    assert type(entry) is authority_module._CANONICAL_REGISTRY_ENTRY_TYPE


def test_result_digest_revalidates_exact_evidence_causal_order() -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    object.__setattr__(evidence, "causal_cutoff", "2026-09-16T15:00:02+00:00")
    object.__setattr__(evidence, "evaluated_at", "2026-09-16T15:00:01+00:00")

    try:
        risk_of_ruin_result_sha256(
            evidence,
            kind="single",
            evaluator_source_sha256=EVALUATOR_SOURCE,
            dataset_snapshot_id="risk-dataset",
            dataset_manifest_sha256=DATASET_MANIFEST,
            effective_sample_size=SAMPLE_SIZE,
            evaluation_available_at=EVALUATED_AT,
        )
    except ValueError as exc:
        assert "causal cutoff must not be after evaluation time" in str(exc)
    else:
        raise AssertionError("post-construction causal-order corruption entered digest")


def test_public_verifier_ignores_registry_path_method_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    _issue(registry, evidence, kind="single")

    def forbidden(*args, **kwargs):
        raise AssertionError("rebound registry Path method executed")

    monkeypatch.setattr(authority_module.Path, "expanduser", forbidden)
    monkeypatch.setattr(authority_module.Path, "resolve", forbidden)
    monkeypatch.setattr(authority_module.Path, "is_file", forbidden)

    allowed, reason = authority_module.verify_risk_of_ruin_authority(
        registry.path,
        evidence,
        kind="single",
        available_by=PROPOSAL_TS,
    )

    assert not allowed
    assert "canonical product-issued evaluator authority" in reason


def test_public_verifier_ignores_rebound_registry_helper_exports(
    tmp_path,
    monkeypatch,
) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    _issue(registry, evidence, kind="single")

    calls = 0

    def forged(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("rebound registry helper executed")

    monkeypatch.setattr(authority_module, "_read_exact_registry_state", forged)
    monkeypatch.setattr(authority_module, "_entry_from_state", forged)

    allowed, reason = authority_module.verify_risk_of_ruin_authority(
        registry.path,
        evidence,
        kind="single",
        available_by=PROPOSAL_TS,
    )

    assert not allowed
    assert calls == 0
    assert "canonical product-issued evaluator authority" in reason


def test_public_verifier_ignores_rebound_result_digest_export(
    tmp_path,
    monkeypatch,
) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    _issue(registry, evidence, kind="single")

    calls = 0

    def forged_digest(*args, **kwargs):
        nonlocal calls
        calls += 1
        return "0" * 64

    monkeypatch.setattr(
        authority_module,
        "risk_of_ruin_result_sha256",
        forged_digest,
    )

    allowed, reason = authority_module.verify_risk_of_ruin_authority(
        registry.path,
        evidence,
        kind="single",
        available_by=PROPOSAL_TS,
    )

    assert not allowed
    assert calls == 0
    assert "canonical product-issued evaluator authority" in reason


def test_public_verifier_uses_stable_evidence_type_witnesses(
    tmp_path,
    monkeypatch,
) -> None:
    registry = _registry(tmp_path)
    policy = _policy(registry.path)
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)
    _issue(registry, evidence, kind="single")

    class ForgedEvidence:
        pass

    monkeypatch.setattr(risk_module, "RiskOfRuinEvidence", ForgedEvidence)
    monkeypatch.setattr(risk_module, "RiskOfRuinVectorEvidence", ForgedEvidence)

    allowed, reason = authority_module.verify_risk_of_ruin_authority(
        registry.path,
        evidence,
        kind="single",
        available_by=PROPOSAL_TS,
    )

    assert not allowed
    assert "canonical product-issued evaluator authority" in reason or "not product-issued" in reason or "invalid" in reason


def test_result_digest_rejects_arbitrary_evidence_before_attribute_reads() -> None:
    class HostileEvidence:
        reads = 0

        def __getattr__(self, name: str) -> object:
            type(self).reads += 1
            raise AssertionError(f"unexpected evidence attribute read: {name}")

    hostile = HostileEvidence()
    HostileEvidence.reads = 0

    try:
        risk_of_ruin_result_sha256(
            hostile,
            kind="single",
            evaluator_source_sha256=EVALUATOR_SOURCE,
            dataset_snapshot_id="risk-dataset",
            dataset_manifest_sha256=DATASET_MANIFEST,
            effective_sample_size=SAMPLE_SIZE,
            evaluation_available_at=EVALUATED_AT,
        )
    except ValueError as exc:
        assert "canonical RiskOfRuinEvidence" in str(exc)
    else:
        raise AssertionError("arbitrary evidence entered canonical authority digest")

    assert HostileEvidence.reads == 0


def test_result_digest_rejects_decimal_subclass_payload() -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    canonical = _single_evidence(policy, book, context)

    forged = object.__new__(RiskOfRuinEvidence)
    for field in (
        "evidence_id",
        "research_protocol_sha256",
        "reproducibility_bundle_sha256",
        "producer_identity",
        "causal_cutoff",
        "evaluated_at",
        "bankroll_id",
        "currency",
        "base_portfolio_sha256",
        "candidate_sha256",
        "upper_bound",
    ):
        object.__setattr__(forged, field, getattr(canonical, field))
    object.__setattr__(forged, "evaluated_stake", _ForgedDecimal("1"))

    try:
        risk_of_ruin_result_sha256(
            forged,
            kind="single",
            evaluator_source_sha256=EVALUATOR_SOURCE,
            dataset_snapshot_id="risk-dataset",
            dataset_manifest_sha256=DATASET_MANIFEST,
            effective_sample_size=SAMPLE_SIZE,
            evaluation_available_at=EVALUATED_AT,
        )
    except ValueError as exc:
        assert "finite Decimal" in str(exc)
    else:
        raise AssertionError("Decimal subclass entered canonical authority digest")


class _RiskEvidenceSubclass(RiskOfRuinEvidence):
    pass


class _RiskVectorEvidenceSubclass(RiskOfRuinVectorEvidence):
    pass


def test_risk_evidence_subclasses_cannot_enter_policy_authority() -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    canonical = _single_evidence(policy, book, context)
    subclass = _RiskEvidenceSubclass(
        evidence_id=canonical.evidence_id,
        research_protocol_sha256=canonical.research_protocol_sha256,
        reproducibility_bundle_sha256=canonical.reproducibility_bundle_sha256,
        producer_identity=canonical.producer_identity,
        causal_cutoff=canonical.causal_cutoff,
        evaluated_at=canonical.evaluated_at,
        bankroll_id=canonical.bankroll_id,
        currency=canonical.currency,
        base_portfolio_sha256=canonical.base_portfolio_sha256,
        candidate_sha256=canonical.candidate_sha256,
        evaluated_stake=canonical.evaluated_stake,
        upper_bound=canonical.upper_bound,
    )

    try:
        replace(context, risk_of_ruin_evidence=subclass)
    except ValueError as exc:
        assert "canonical RiskOfRuinEvidence" in str(exc)
    else:
        raise AssertionError("RiskOfRuinEvidence subclass entered policy context")

    try:
        replace(context, risk_of_ruin_upper_bound=_ForgedDecimal("0.001"))
    except ValueError as exc:
        assert "risk_of_ruin_upper_bound" in str(exc)
    else:
        raise AssertionError("Decimal subclass entered scalar ruin ingress")


def test_vector_evidence_subclass_cannot_enter_policy_authority() -> None:
    policy = _policy()
    relaxed = _policy(max_risk_of_ruin=Decimal("1"))
    book = PaperBook("100")
    contexts = (_context(1), _context(2))
    signals = (Decimal("0.01"), Decimal("0.01"))
    baseline = relaxed.derive_goal_stake_vector(book, signals, contexts=contexts)
    assert baseline.action == "STAKE_VECTOR"

    portfolio = policy.risk_of_ruin_portfolio_sha256(book)
    candidates = policy.risk_of_ruin_candidate_vector_sha256(contexts)
    assert portfolio is not None and candidates is not None

    subclass = _RiskVectorEvidenceSubclass(
        evidence_id="vector-subclass",
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

    decision = policy.derive_goal_stake_vector(
        book,
        signals,
        contexts=contexts,
        risk_of_ruin_vector_evidence=subclass,
    )
    assert decision.action != "STAKE_VECTOR"
    assert "vector evidence is invalid" in decision.reason


def test_authority_resource_bounds_ignore_public_constant_rebinding(
    monkeypatch,
) -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = replace(
        _single_evidence(policy, book, context),
        evaluated_stake=Decimal("1E+600"),
    )

    monkeypatch.setattr(
        authority_module,
        "_MAX_FIXED_POINT_MATERIALIZATION_LENGTH",
        10_000,
    )
    monkeypatch.setattr(authority_module, "_MAX_AUTHORITY_TIMESTAMP_LENGTH", 10_000)
    monkeypatch.setattr(authority_module, "_MAX_SUPPORTED_EVALUATED_STAKES", 1_000_000)
    monkeypatch.setattr(
        authority_module,
        "_MAX_SUPPORTED_EFFECTIVE_SAMPLE_SIZE",
        1_000_000,
    )

    try:
        risk_of_ruin_result_sha256(
            evidence,
            kind="single",
            evaluator_source_sha256=EVALUATOR_SOURCE,
            dataset_snapshot_id="risk-dataset",
            dataset_manifest_sha256=DATASET_MANIFEST,
            effective_sample_size=SAMPLE_SIZE,
            evaluation_available_at=EVALUATED_AT,
        )
    except ValueError as exc:
        assert "fixed-point representation exceeds supported canonical size" in str(exc)
    else:
        raise AssertionError("rebound public limits expanded authority envelope")


def test_result_digest_rejects_oversize_fixed_point_stake() -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = replace(
        _single_evidence(policy, book, context),
        evaluated_stake=Decimal("1E+600"),
    )

    try:
        risk_of_ruin_result_sha256(
            evidence,
            kind="single",
            evaluator_source_sha256=EVALUATOR_SOURCE,
            dataset_snapshot_id="risk-dataset",
            dataset_manifest_sha256=DATASET_MANIFEST,
            effective_sample_size=SAMPLE_SIZE,
            evaluation_available_at=EVALUATED_AT,
        )
    except ValueError as exc:
        assert "fixed-point representation exceeds supported canonical size" in str(exc)
    else:
        raise AssertionError("oversize stake entered canonical authority digest")


def test_result_digest_rejects_oversize_fixed_point_probability_bound() -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = replace(
        _single_evidence(policy, book, context),
        upper_bound=Decimal("1E-600"),
    )

    try:
        risk_of_ruin_result_sha256(
            evidence,
            kind="single",
            evaluator_source_sha256=EVALUATOR_SOURCE,
            dataset_snapshot_id="risk-dataset",
            dataset_manifest_sha256=DATASET_MANIFEST,
            effective_sample_size=SAMPLE_SIZE,
            evaluation_available_at=EVALUATED_AT,
        )
    except ValueError as exc:
        assert "fixed-point representation exceeds supported canonical size" in str(exc)
    else:
        raise AssertionError("oversize probability bound entered canonical authority digest")


def test_result_digest_rejects_oversize_vector_before_materialization() -> None:
    policy = _policy()
    book = PaperBook("100")
    portfolio = policy.risk_of_ruin_portfolio_sha256(book)
    assert portfolio is not None
    evidence = RiskOfRuinVectorEvidence(
        evidence_id="oversize-vector",
        research_protocol_sha256="c" * 64,
        reproducibility_bundle_sha256="d" * 64,
        producer_identity="canonical-vector-risk-evaluator",
        causal_cutoff=CAUSAL_CUTOFF,
        evaluated_at=EVALUATED_AT,
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256=portfolio,
        candidate_vector_sha256="f" * 64,
        evaluated_stakes=(Decimal("0"),) * 10_000 + (Decimal("1"),),
        upper_bound=Decimal("0.005"),
    )

    try:
        risk_of_ruin_result_sha256(
            evidence,
            kind="vector",
            evaluator_source_sha256=EVALUATOR_SOURCE,
            dataset_snapshot_id="risk-dataset",
            dataset_manifest_sha256=DATASET_MANIFEST,
            effective_sample_size=SAMPLE_SIZE,
            evaluation_available_at=EVALUATED_AT,
        )
    except ValueError as exc:
        assert "stake vector exceeds supported size" in str(exc)
    else:
        raise AssertionError("oversize stake vector entered canonical authority digest")


class _ForgedInt(int):
    pass


def test_result_digest_requires_exact_bounded_effective_sample_size() -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    evidence = _single_evidence(policy, book, context)

    for invalid in (_ForgedInt(1), 10_001):
        try:
            risk_of_ruin_result_sha256(
                evidence,
                kind="single",
                evaluator_source_sha256=EVALUATOR_SOURCE,
                dataset_snapshot_id="risk-dataset",
                dataset_manifest_sha256=DATASET_MANIFEST,
                effective_sample_size=invalid,
                evaluation_available_at=EVALUATED_AT,
            )
        except ValueError as exc:
            assert "effective_sample_size must be an exact integer" in str(exc)
        else:
            raise AssertionError(
                "unsupported effective sample size entered authority digest"
            )


def test_result_digest_rejects_unbounded_dataset_and_evidence_identity() -> None:
    policy = _policy()
    book = PaperBook("100")
    context = _context(1)
    canonical = _single_evidence(policy, book, context)

    try:
        risk_of_ruin_result_sha256(
            canonical,
            kind="single",
            evaluator_source_sha256=EVALUATOR_SOURCE,
            dataset_snapshot_id="d" * 513,
            dataset_manifest_sha256=DATASET_MANIFEST,
            effective_sample_size=SAMPLE_SIZE,
            evaluation_available_at=EVALUATED_AT,
        )
    except ValueError as exc:
        assert "dataset_snapshot_id must be bounded canonical text" in str(exc)
    else:
        raise AssertionError("oversize dataset identity entered authority digest")

    oversized_evidence = replace(canonical, evidence_id="e" * 513)
    try:
        risk_of_ruin_result_sha256(
            oversized_evidence,
            kind="single",
            evaluator_source_sha256=EVALUATOR_SOURCE,
            dataset_snapshot_id="risk-dataset",
            dataset_manifest_sha256=DATASET_MANIFEST,
            effective_sample_size=SAMPLE_SIZE,
            evaluation_available_at=EVALUATED_AT,
        )
    except ValueError as exc:
        assert "risk-of-ruin evidence_id must be bounded canonical text" in str(exc)
    else:
        raise AssertionError("oversize evidence identity entered authority digest")



def test_risk_root_rejects_captured_paperbook_binding_replacement(monkeypatch) -> None:
    policy = _policy(max_risk_of_ruin=Decimal("1"))
    book = PaperBook("100")
    context = _context(1)
    calls = {"validate": 0}

    def forged_validate(candidate):
        calls["validate"] += 1
        return None

    monkeypatch.setattr(
        risk_module,
        "_CANONICAL_PAPERBOOK_VALIDATE_LOADED_STATE",
        forged_validate,
    )

    decision = policy.evaluate(book, Decimal("1"), context=context)

    assert not decision.allowed
    assert calls == {"validate": 0}
    assert "authority" in decision.reason or "invalid" in decision.reason


def test_risk_helper_witnesses_bind_to_executing_policy_type(monkeypatch) -> None:
    policy = _policy(max_risk_of_ruin=Decimal("1"))
    book = PaperBook("100")
    context = _context(1)

    # The module export is a mutable namespace binding and must not choose which
    # class surface the already-created policy instance authenticates.
    monkeypatch.setattr(risk_module, "PaperRiskPolicy", object())

    decision = policy.evaluate(book, Decimal("1"), context=context)

    assert decision.allowed


def test_risk_helper_witness_bytecode_does_not_read_policy_module_global() -> None:
    for method_name in ("evaluate", "derive_goal_stake", "derive_goal_stake_vector"):
        method = getattr(PaperRiskPolicy, method_name)
        assert "PaperRiskPolicy" not in method.__code__.co_names
