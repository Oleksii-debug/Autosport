from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.domain import PaperTicket, TicketLeg
from autosport.portfolio_joint_stress import (
    JointDependenceGrade,
    JointDependenceRelation,
    JointScenarioEvaluation,
    JointStressProtocol,
    JointStressResult,
    JointStressScenario,
    StructuralDependencyKind,
    evaluate_joint_stress,
)


A = "a" * 64
B = "b" * 64
NOW = "2026-09-22T12:00:00+00:00"


def ticket(
    ticket_id: str,
    selection_id: str,
    *,
    event_id: str = "event-1",
    market_id: str = "market-1",
    provider: str = "provider-a",
    stake: str = "10",
    odds: str = "2",
    currency: str = "GBP",
    exchange_side: str | None = None,
) -> PaperTicket:
    return PaperTicket(
        ticket_id=ticket_id,
        stake=Decimal(stake),
        legs=(
            TicketLeg(
                event_id=event_id,
                market_id=market_id,
                selection_id=selection_id,
                locked_odds=Decimal(odds),
                sport="soccer",
                exchange_side=exchange_side,
            ),
        ),
        placed_at=NOW,
        provider_source_ids=(provider,),
        provider_accounts=((provider, f"acct-{provider}"),),
        bankroll_id="bankroll-1",
        currency=currency,
    )


def scenario(
    scenario_id: str,
    tickets: tuple[PaperTicket, ...],
    outcomes: tuple[str, ...],
    *,
    relation_ids: tuple[str, ...] = (),
    assumption_sha256: str = A,
) -> JointStressScenario:
    assert len(tickets) == len(outcomes)
    return JointStressScenario(
        scenario_id=scenario_id,
        settlements=tuple(
            (item.legs[0].quote_key, outcome)
            for item, outcome in zip(tickets, outcomes, strict=True)
        ),
        relation_ids=relation_ids,
        committed_at=NOW,
        assumption_sha256=assumption_sha256,
        reason="precommitted adverse joint state",
    )


def protocol(
    *,
    tickets: tuple[PaperTicket, ...],
    relations: tuple[JointDependenceRelation, ...] = (),
    scenarios: tuple[JointStressScenario, ...] | None = None,
) -> JointStressProtocol:
    if scenarios is None:
        scenarios = (
            scenario(
                "all-loss",
                tickets,
                tuple("loss" for _ in tickets),
                relation_ids=tuple(
                    relation.relation_id
                    for relation in relations
                    if relation.grade is not JointDependenceGrade.UNKNOWN_DEPENDENCE
                ),
            ),
        )
    return JointStressProtocol(
        protocol_id="joint-tail-v1",
        protocol_version="1",
        causal_cutoff=NOW,
        relations=relations,
        scenarios=scenarios,
    )


def test_lay_ticket_fails_closed_before_back_only_portfolio_settlement() -> None:
    tickets = (
        ticket(
            "lay-ticket",
            "home",
            odds="3",
            exchange_side="lay",
        ),
    )

    with pytest.raises(ValueError, match="LAY ticket economics"):
        evaluate_joint_stress(
            tickets=tickets,
            protocol=protocol(tickets=tickets),
        )


def test_explicit_back_ticket_remains_supported() -> None:
    tickets = (
        ticket(
            "back-ticket",
            "home",
            odds="3",
            exchange_side="back",
        ),
    )
    result = evaluate_joint_stress(
        tickets=tickets,
        protocol=protocol(tickets=tickets),
    )

    assert result.worst_observed_profit == Decimal("-10")


def test_same_market_across_providers_is_structurally_linked() -> None:
    tickets = (
        ticket("t-a", "home", provider="provider-a"),
        ticket("t-b", "away", provider="provider-b"),
    )
    result = evaluate_joint_stress(tickets=tickets, protocol=protocol(tickets=tickets))

    kinds = {group.kind for group in result.structural_groups}
    assert StructuralDependencyKind.SHARED_MARKET in kinds
    assert StructuralDependencyKind.SHARED_EVENT in kinds
    assert result.diversification_credit_authorized is False
    assert result.risk_reduction_authorized is False


def test_same_underlying_quote_across_providers_concentrates_loss() -> None:
    tickets = (
        ticket("t-a", "home", provider="provider-a"),
        ticket("t-b", "home", provider="provider-b"),
    )
    stress = JointStressScenario(
        scenario_id="shared-loss",
        settlements=((tickets[0].legs[0].quote_key, "loss"),),
        relation_ids=(),
        committed_at=NOW,
        assumption_sha256=A,
        reason="same underlying outcome loses",
    )
    result = evaluate_joint_stress(
        tickets=tickets,
        protocol=protocol(tickets=tickets, scenarios=(stress,)),
    )

    assert result.worst_observed_profit == Decimal("-20")
    assert any(
        group.kind is StructuralDependencyKind.SHARED_QUOTE
        and group.member_ticket_ids == ("t-a", "t-b")
        for group in result.structural_groups
    )


def test_pairwise_zero_empirical_relation_cannot_erase_joint_tail_loss() -> None:
    tickets = (
        ticket("t-1", "s-1", market_id="m-1"),
        ticket("t-2", "s-2", market_id="m-2"),
        ticket("t-3", "s-3", market_id="m-3"),
    )
    relation = JointDependenceRelation(
        relation_id="pairwise-zero-is-not-independent",
        member_ticket_ids=("t-3", "t-1", "t-2"),
        grade=JointDependenceGrade.EMPIRICAL_DEPENDENCE,
        reason="pairwise coefficient alone does not prove mutual independence",
        evidence_sha256=B,
        evidence_available_at=NOW,
        empirical_coefficient=Decimal("0"),
    )
    stress = scenario(
        "common-tail-loss",
        tickets,
        ("loss", "loss", "loss"),
        relation_ids=(relation.relation_id,),
    )
    result = evaluate_joint_stress(
        tickets=tickets,
        protocol=protocol(
            tickets=tickets,
            relations=(relation,),
            scenarios=(stress,),
        ),
    )

    assert result.worst_observed_profit == Decimal("-30")
    assert result.diversification_credit_authorized is False
    assert result.exact_terminal_authority is False


def test_adding_correlated_third_exposure_cannot_improve_observed_worst_case() -> None:
    two = (
        ticket("t-1", "s-1", market_id="m-1"),
        ticket("t-2", "s-2", market_id="m-2"),
    )
    three = two + (ticket("t-3", "s-3", market_id="m-3"),)

    result_two = evaluate_joint_stress(tickets=two, protocol=protocol(tickets=two))
    result_three = evaluate_joint_stress(tickets=three, protocol=protocol(tickets=three))

    assert result_two.worst_observed_profit == Decimal("-20")
    assert result_three.worst_observed_profit == Decimal("-30")
    assert result_three.worst_observed_profit <= result_two.worst_observed_profit


def test_unknown_dependence_remains_explicit_and_never_becomes_independence() -> None:
    tickets = (
        ticket("t-1", "s-1", market_id="m-1"),
        ticket("t-2", "s-2", market_id="m-2"),
    )
    unknown = JointDependenceRelation(
        relation_id="unknown-link",
        member_ticket_ids=("t-1", "t-2"),
        grade=JointDependenceGrade.UNKNOWN_DEPENDENCE,
        reason="no qualified dependence evidence",
    )
    result = evaluate_joint_stress(
        tickets=tickets,
        protocol=protocol(tickets=tickets, relations=(unknown,)),
    )

    assert result.unresolved_relation_ids == ("unknown-link",)
    assert result.diversification_credit_authorized is False
    assert result.financial_permission_expansion_authorized is False


def test_structural_dependency_is_derived_not_overridden_by_low_empirical_value() -> None:
    tickets = (
        ticket("t-a", "home", provider="provider-a"),
        ticket("t-b", "away", provider="provider-b"),
    )
    relation = JointDependenceRelation(
        relation_id="low-empirical",
        member_ticket_ids=("t-a", "t-b"),
        grade=JointDependenceGrade.EMPIRICAL_DEPENDENCE,
        reason="historical estimate is descriptive only",
        evidence_sha256=B,
        evidence_available_at=NOW,
        empirical_coefficient=Decimal("-0.9"),
    )
    stress = scenario(
        "both-lose-stress",
        tickets,
        ("loss", "loss"),
        relation_ids=(relation.relation_id,),
    )
    result = evaluate_joint_stress(
        tickets=tickets,
        protocol=protocol(
            tickets=tickets,
            relations=(relation,),
            scenarios=(stress,),
        ),
    )

    assert result.worst_observed_profit == Decimal("-20")
    assert any(
        group.kind is StructuralDependencyKind.SHARED_MARKET
        for group in result.structural_groups
    )


def test_parlay_overlap_is_mechanically_visible_as_shared_event() -> None:
    shared = TicketLeg(
        event_id="event-1",
        market_id="market-1",
        selection_id="home",
        locked_odds=Decimal("2"),
        sport="soccer",
    )
    parlay = PaperTicket(
        ticket_id="parlay",
        stake=Decimal("10"),
        legs=(
            shared,
            TicketLeg(
                event_id="event-2",
                market_id="market-2",
                selection_id="over",
                locked_odds=Decimal("2"),
                sport="soccer",
            ),
        ),
        placed_at=NOW,
        provider_source_ids=("provider-a",),
        provider_accounts=(("provider-a", "acct-provider-a"),),
        bankroll_id="bankroll-1",
        currency="GBP",
    )
    single = ticket("single", "home")
    stress = JointStressScenario(
        scenario_id="shared-leg-loss",
        settlements=(
            (shared.quote_key, "loss"),
            (parlay.legs[1].quote_key, "win"),
        ),
        relation_ids=(),
        committed_at=NOW,
        assumption_sha256=A,
        reason="shared leg loses",
    )
    result = evaluate_joint_stress(
        tickets=(single, parlay),
        protocol=protocol(tickets=(single, parlay), scenarios=(stress,)),
    )

    assert result.worst_observed_profit == Decimal("-20")
    assert any(
        group.member_ticket_ids == ("parlay", "single")
        for group in result.structural_groups
    )


def test_every_scenario_must_cover_exact_quote_set() -> None:
    tickets = (
        ticket("t-1", "s-1", market_id="m-1"),
        ticket("t-2", "s-2", market_id="m-2"),
    )
    incomplete = JointStressScenario(
        scenario_id="incomplete",
        settlements=((tickets[0].legs[0].quote_key, "loss"),),
        relation_ids=(),
        committed_at=NOW,
        assumption_sha256=A,
        reason="missing one quote",
    )
    with pytest.raises(ValueError, match="exact portfolio quote-key set"):
        evaluate_joint_stress(
            tickets=tickets,
            protocol=protocol(tickets=tickets, scenarios=(incomplete,)),
        )


def test_extra_scenario_quote_is_rejected() -> None:
    tickets = (ticket("t-1", "s-1"),)
    stress = JointStressScenario(
        scenario_id="extra",
        settlements=(
            (tickets[0].legs[0].quote_key, "loss"),
            ("soccer|other|market|selection", "loss"),
        ),
        relation_ids=(),
        committed_at=NOW,
        assumption_sha256=A,
        reason="extra unrelated quote",
    )
    with pytest.raises(ValueError, match="exact portfolio quote-key set"):
        evaluate_joint_stress(
            tickets=tickets,
            protocol=protocol(tickets=tickets, scenarios=(stress,)),
        )


def test_cross_currency_aggregation_fails_without_fx_authority() -> None:
    tickets = (
        ticket("gbp", "s-1", market_id="m-1", currency="GBP"),
        ticket("eur", "s-2", market_id="m-2", currency="EUR"),
    )
    with pytest.raises(ValueError, match="canonical FX authority"):
        evaluate_joint_stress(tickets=tickets, protocol=protocol(tickets=tickets))


def test_relation_cannot_reference_ticket_outside_scope() -> None:
    tickets = (
        ticket("t-1", "s-1", market_id="m-1"),
        ticket("t-2", "s-2", market_id="m-2"),
    )
    relation = JointDependenceRelation(
        relation_id="foreign-ticket",
        member_ticket_ids=("t-1", "not-in-scope"),
        grade=JointDependenceGrade.UNKNOWN_DEPENDENCE,
        reason="scope mismatch",
    )
    with pytest.raises(ValueError, match="outside stress scope"):
        evaluate_joint_stress(
            tickets=tickets,
            protocol=protocol(tickets=tickets, relations=(relation,)),
        )


def test_ticket_and_protocol_input_permutation_preserve_result_identity() -> None:
    tickets = (
        ticket("t-1", "s-1", market_id="m-1"),
        ticket("t-2", "s-2", market_id="m-2"),
        ticket("t-3", "s-3", market_id="m-3"),
    )
    empirical = JointDependenceRelation(
        relation_id="empirical",
        member_ticket_ids=("t-2", "t-1"),
        grade=JointDependenceGrade.EMPIRICAL_DEPENDENCE,
        reason="descriptive second moment",
        evidence_sha256=B,
        evidence_available_at=NOW,
        empirical_coefficient=Decimal("0"),
    )
    unknown = JointDependenceRelation(
        relation_id="unknown",
        member_ticket_ids=("t-3", "t-2"),
        grade=JointDependenceGrade.UNKNOWN_DEPENDENCE,
        reason="unresolved tail relation",
    )
    all_loss = scenario(
        "z-loss",
        tickets,
        ("loss", "loss", "loss"),
        relation_ids=(empirical.relation_id,),
    )
    mixed = scenario(
        "a-mixed",
        tickets,
        ("win", "loss", "loss"),
        relation_ids=(empirical.relation_id,),
        assumption_sha256=B,
    )
    left_protocol = protocol(
        tickets=tickets,
        relations=(unknown, empirical),
        scenarios=(all_loss, mixed),
    )
    right_protocol = protocol(
        tickets=tickets,
        relations=(empirical, unknown),
        scenarios=(
            replace(
                mixed,
                settlements=tuple(reversed(mixed.settlements)),
            ),
            replace(
                all_loss,
                settlements=tuple(reversed(all_loss.settlements)),
            ),
        ),
    )

    left = evaluate_joint_stress(tickets=tickets, protocol=left_protocol)
    right = evaluate_joint_stress(
        tickets=tuple(reversed(tickets)),
        protocol=right_protocol,
    )

    assert left.protocol_sha256 == right.protocol_sha256
    assert left.portfolio_scope_sha256 == right.portfolio_scope_sha256
    assert left.result_sha256 == right.result_sha256


def test_result_identity_changes_when_stress_state_changes() -> None:
    tickets = (
        ticket("t-1", "s-1", market_id="m-1"),
        ticket("t-2", "s-2", market_id="m-2"),
    )
    all_loss = scenario("state", tickets, ("loss", "loss"))
    one_win = scenario("state", tickets, ("win", "loss"))

    left = evaluate_joint_stress(
        tickets=tickets,
        protocol=protocol(tickets=tickets, scenarios=(all_loss,)),
    )
    right = evaluate_joint_stress(
        tickets=tickets,
        protocol=protocol(tickets=tickets, scenarios=(one_win,)),
    )

    assert left.result_sha256 != right.result_sha256
    assert left.worst_observed_profit == Decimal("-20")
    assert right.worst_observed_profit == Decimal("0")


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("1.01"), Decimal("-1.01")])
def test_empirical_coefficient_must_be_finite_and_bounded(bad: Decimal) -> None:
    with pytest.raises(ValueError):
        JointDependenceRelation(
            relation_id="bad-rho",
            member_ticket_ids=("t-1", "t-2"),
            grade=JointDependenceGrade.EMPIRICAL_DEPENDENCE,
            reason="invalid coefficient",
            evidence_sha256=B,
            evidence_available_at=NOW,
            empirical_coefficient=bad,
        )


def test_unknown_dependence_cannot_carry_fabricated_coefficient_or_evidence() -> None:
    with pytest.raises(ValueError, match="unknown dependence"):
        JointDependenceRelation(
            relation_id="unknown",
            member_ticket_ids=("t-1", "t-2"),
            grade=JointDependenceGrade.UNKNOWN_DEPENDENCE,
            reason="unknown",
            evidence_sha256=A,
            evidence_available_at=NOW,
            empirical_coefficient=Decimal("0"),
        )


def test_stress_assumption_cannot_masquerade_as_empirical_correlation() -> None:
    with pytest.raises(ValueError, match="must not masquerade"):
        JointDependenceRelation(
            relation_id="stress",
            member_ticket_ids=("t-1", "t-2"),
            grade=JointDependenceGrade.STRESS_ASSUMPTION,
            reason="adverse scenario family",
            evidence_sha256=A,
            evidence_available_at=NOW,
            empirical_coefficient=Decimal("0"),
        )


def test_protocol_rejects_unexercised_empirical_relation() -> None:
    tickets = (
        ticket("t-1", "s-1", market_id="m-1"),
        ticket("t-2", "s-2", market_id="m-2"),
    )
    relation = JointDependenceRelation(
        relation_id="empirical",
        member_ticket_ids=("t-1", "t-2"),
        grade=JointDependenceGrade.EMPIRICAL_DEPENDENCE,
        reason="must be exercised",
        evidence_sha256=B,
        evidence_available_at=NOW,
        empirical_coefficient=Decimal("0.1"),
    )
    stress = scenario("loss", tickets, ("loss", "loss"), relation_ids=())

    with pytest.raises(ValueError, match="must be exercised"):
        protocol(tickets=tickets, relations=(relation,), scenarios=(stress,))


def test_duplicate_ticket_identity_is_rejected() -> None:
    first = ticket("same", "s-1", market_id="m-1")
    second = ticket("same", "s-2", market_id="m-2")
    tickets = (first, second)

    with pytest.raises(ValueError, match="ticket identities must be unique"):
        evaluate_joint_stress(tickets=tickets, protocol=protocol(tickets=tickets))


def test_direct_result_construction_cannot_mint_product_stress_evidence() -> None:
    with pytest.raises(TypeError, match="must be issued"):
        JointStressResult(
            protocol_sha256=A,
            portfolio_scope_sha256=B,
            currency="GBP",
            ticket_ids=("t-1",),
            quote_keys=("q-1",),
            structural_groups=(),
            evaluations=(),
            unresolved_relation_ids=(),
            worst_observed_profit=Decimal("-1"),
            best_observed_profit=Decimal("1"),
        )


def test_machine_stress_result_never_expands_external_permission() -> None:
    tickets = (ticket("t-1", "s-1"),)
    result = evaluate_joint_stress(tickets=tickets, protocol=protocol(tickets=tickets))

    assert result.diversification_credit_authorized is False
    assert result.risk_reduction_authorized is False
    assert result.financial_permission_expansion_authorized is False
    assert result.exact_terminal_authority is False

def test_empirical_evidence_after_causal_cutoff_is_rejected() -> None:
    tickets = (
        ticket("t-1", "s-1", market_id="m-1"),
        ticket("t-2", "s-2", market_id="m-2"),
    )
    relation = JointDependenceRelation(
        relation_id="future-evidence",
        member_ticket_ids=("t-1", "t-2"),
        grade=JointDependenceGrade.EMPIRICAL_DEPENDENCE,
        reason="must not leak future evidence",
        evidence_sha256=B,
        evidence_available_at="2026-09-22T12:00:01+00:00",
        empirical_coefficient=Decimal("0"),
    )
    stress = scenario(
        "loss",
        tickets,
        ("loss", "loss"),
        relation_ids=(relation.relation_id,),
    )

    with pytest.raises(ValueError, match="after causal_cutoff"):
        JointStressProtocol(
            protocol_id="future-leak",
            protocol_version="1",
            causal_cutoff=NOW,
            relations=(relation,),
            scenarios=(stress,),
        )


def test_scenario_committed_after_causal_cutoff_is_rejected() -> None:
    tickets = (ticket("t-1", "s-1"),)
    stress = replace(
        scenario("late", tickets, ("loss",)),
        committed_at="2026-09-22T12:00:01+00:00",
    )

    with pytest.raises(ValueError, match="committed no later"):
        JointStressProtocol(
            protocol_id="late-scenario",
            protocol_version="1",
            causal_cutoff=NOW,
            relations=(),
            scenarios=(stress,),
        )


def test_product_issued_result_cannot_transfer_issuance_via_dataclass_replace() -> None:
    tickets = (ticket("t-1", "s-1"),)
    result = evaluate_joint_stress(tickets=tickets, protocol=protocol(tickets=tickets))
    original = result.evaluations[0]
    forged = JointScenarioEvaluation(
        scenario_id=original.scenario_id,
        scenario_sha256=original.scenario_sha256,
        profit=Decimal("999"),
    )

    with pytest.raises(TypeError, match="must be issued"):
        replace(
            result,
            evaluations=(forged,),
            worst_observed_profit=Decimal("999"),
            best_observed_profit=Decimal("999"),
        )

    with pytest.raises(TypeError, match="must be issued"):
        replace(result, protocol_sha256=B)

