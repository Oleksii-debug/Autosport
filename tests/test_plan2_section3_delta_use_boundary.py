"""Plan 2 Section 3: adversarial use-boundary and exact JSON-wire regression.

All effects stay hypothetical PAPER proposals: these tests do not place a bet,
call providers or grant a model/agent financial permission.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.paper import PaperBook
from autosport.portfolio_delta import PortfolioDelta
from autosport.portfolio_plan import (
    EvidenceTruth,
    PortfolioAction,
    PortfolioDependencyGraph,
    PortfolioPlan,
)
from autosport.risk import PaperRiskPolicy


def _proposal():
    book = PaperBook(Decimal("100.00"))
    policy = PaperRiskPolicy(
        economic_goal=EconomicGoalContract(
            goal_id="exact-wire-owner",
            revision=1,
            bankroll_id="paper-only",
            currency="EUR",
        ),
    )
    source = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    assert source is not None
    plan = PortfolioPlan(
        decision_ts="2026-10-08T12:00:00+00:00",
        action=PortfolioAction.STAKE_VECTOR,
        stakes=(Decimal("4.25"),),
        intent_ids=("paper-intent",),
        intent_sha256s=("a" * 64,),
        opportunity_classes=("live_price_movement",),
        portfolio_sha256=source,
        dependency_graph=PortfolioDependencyGraph(
            portfolio_sha256=source,
            intent_sha256s=("a" * 64,),
            candidate_sha256s=("b" * 64,),
        ),
        terminal_economics=None,
        economic_goal_contract_sha256=provenance_for(
            policy.economic_goal
        ).contract_sha256,
        risk_policy_sha256=policy.provenance_sha256,
        portfolio_truth=EvidenceTruth.EXACT,
        reason="non-executing candidate",
    )
    return book, plan, policy


@pytest.mark.parametrize("noncanonical_version", [True, 1.0, "1"])
def test_schema_version_type_coercion_never_passes_readback(
    noncanonical_version,
) -> None:
    book, plan, policy = _proposal()
    canonical = PortfolioDelta.derive(book, plan, policy).to_dict()
    altered = {**canonical, "schema_version": noncanonical_version}
    assert altered["delta_sha256"] == canonical["delta_sha256"]
    with pytest.raises(ValueError, match="stale, tampered or noncanonical"):
        PortfolioDelta.readback(
            altered, book=book, plan=plan, risk_policy=policy
        )
    assert book.balance == Decimal("100.00") and not book.tickets


def test_nested_wire_types_and_exact_positive_roundtrip() -> None:
    book, plan, policy = _proposal()
    original = PortfolioDelta.derive(book, plan, policy)
    canonical = original.to_dict()
    assert PortfolioDelta.readback(
        canonical, book=book, plan=plan, risk_policy=policy
    ) == original
    altered = {**canonical, "stake_vector": tuple(canonical["stake_vector"])}
    with pytest.raises(ValueError, match="stale, tampered or noncanonical"):
        PortfolioDelta.readback(
            altered, book=book, plan=plan, risk_policy=policy
        )


def test_postconstruction_wait_zero_flip_is_not_positive_authority() -> None:
    book, plan, policy = _proposal()
    object.__setattr__(plan, "action", PortfolioAction.ZERO)
    with pytest.raises(ValueError, match="WAIT/ZERO"):
        PortfolioDelta.derive(book, plan, policy)
    assert book.balance == Decimal("100.00") and not book.tickets


def test_postconstruction_dependency_identity_drift_fails_closed() -> None:
    book, plan, policy = _proposal()
    graph = plan.dependency_graph
    assert graph is not None
    object.__setattr__(graph, "candidate_sha256s", ("b" * 64, "c" * 64))
    with pytest.raises(ValueError, match="matching cardinality"):
        PortfolioDelta.derive(book, plan, policy)
    assert book.balance == Decimal("100.00") and not book.tickets


def test_postconstruction_intent_rebinding_fails_closed() -> None:
    book, plan, policy = _proposal()
    graph = plan.dependency_graph
    assert graph is not None
    object.__setattr__(graph, "intent_sha256s", ("d" * 64,))
    with pytest.raises(ValueError, match="exact intent vector"):
        PortfolioDelta.derive(book, plan, policy)


def test_malicious_decimal_subclass_is_rejected_before_virtual_call() -> None:
    class HostileDecimal(Decimal):
        def is_finite(self):
            raise AssertionError("untrusted numeric virtual method executed")

    book, plan, policy = _proposal()
    object.__setattr__(plan, "stakes", (HostileDecimal("4.25"),))
    with pytest.raises(ValueError, match="exact canonical Decimals"):
        PortfolioDelta.derive(book, plan, policy)


def test_unicode_restart_preserves_proposal_without_debit(tmp_path) -> None:
    book, plan, policy = _proposal()
    saved = PortfolioDelta.derive(book, plan, policy).to_dict()
    path = tmp_path / "Паперова каса розділ 3.json"
    book.save(path)
    recovered = PaperBook.load(path)
    result = PortfolioDelta.readback(
        saved, book=recovered, plan=plan, risk_policy=policy
    )
    assert result.hypothetical_cash_after == Decimal("95.75")
    assert recovered.balance == Decimal("100.00")
    assert not recovered.tickets
