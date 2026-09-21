from dataclasses import fields, replace

import pytest

import autosport.provider_settlement_rules as settlement_rules
from autosport.provider_settlement_rules import (
    ProviderSettlementRuleSelection,
    ProviderSettlementRule,
    ProviderSettlementRuleError,
    ProviderSettlementRuleTimeline,
    ProviderSettlementRulebook,
    build_provider_settlement_timeline,
)


_SOURCE_A = "a" * 64
_SOURCE_B = "b" * 64
_SOURCE_C = "c" * 64


def _rule(
    *,
    market: str = "synthetic.match_result",
    scenario: str = "synthetic.interrupted",
    treatment: str = "synthetic.void",
) -> ProviderSettlementRule:
    return ProviderSettlementRule(
        market_family=market,
        scenario_code=scenario,
        treatment_code=treatment,
    )


def _book(
    *,
    provider: str = "synthetic-provider-a",
    version: str = "rules-1",
    start: str = "2026-01-01T00:00:00+00:00",
    end: str | None = "2026-06-01T00:00:00+00:00",
    source_hash: str = _SOURCE_A,
    rules: tuple[ProviderSettlementRule, ...] | None = None,
) -> ProviderSettlementRulebook:
    return ProviderSettlementRulebook(
        provider_id=provider,
        rulebook_version=version,
        effective_from=start,
        effective_until=end,
        source_ref=f"synthetic://{provider}/{version}",
        source_payload_sha256=source_hash,
        rules=rules or (_rule(),),
    )


def test_provider_specific_scenario_may_differ_without_cross_provider_inference() -> None:
    provider_a = _book(rules=(_rule(treatment="synthetic.void"),))
    provider_b = _book(
        provider="synthetic-provider-b",
        source_hash=_SOURCE_B,
        rules=(_rule(treatment="synthetic.loss"),),
    )

    timeline_a = ProviderSettlementRuleTimeline(provider_a.provider_id, (provider_a,))
    timeline_b = ProviderSettlementRuleTimeline(provider_b.provider_id, (provider_b,))
    evaluated_at = "2026-03-01T12:00:00+00:00"

    selection_a = timeline_a.select_rule(
        evaluated_at=evaluated_at,
        market_family="synthetic.match_result",
        scenario_code="synthetic.interrupted",
    )
    selection_b = timeline_b.select_rule(
        evaluated_at=evaluated_at,
        market_family="synthetic.match_result",
        scenario_code="synthetic.interrupted",
    )

    assert selection_a.treatment_code == "synthetic.void"
    assert selection_b.treatment_code == "synthetic.loss"
    assert selection_a.rulebook_id != selection_b.rulebook_id
    assert selection_a.selection_id != selection_b.selection_id
    with pytest.raises(ProviderSettlementRuleError, match="exact rulebook identity"):
        selection_a.verify_rulebook(provider_b)


def test_rule_version_boundary_selects_by_evaluation_instant() -> None:
    v1 = _book(
        version="rules-1",
        end="2026-06-01T00:00:00+00:00",
        rules=(_rule(treatment="synthetic.void"),),
    )
    v2 = _book(
        version="rules-2",
        start="2026-06-01T00:00:00+00:00",
        end=None,
        source_hash=_SOURCE_B,
        rules=(_rule(treatment="synthetic.push"),),
    )
    timeline = ProviderSettlementRuleTimeline(v1.provider_id, (v1, v2))

    before = timeline.select_rule(
        evaluated_at="2026-05-31T23:59:59.999999+00:00",
        market_family="synthetic.match_result",
        scenario_code="synthetic.interrupted",
    )
    boundary = timeline.select_rule(
        evaluated_at="2026-06-01T00:00:00+00:00",
        market_family="synthetic.match_result",
        scenario_code="synthetic.interrupted",
    )

    assert before.rulebook_version == "rules-1"
    assert before.treatment_code == "synthetic.void"
    assert boundary.rulebook_version == "rules-2"
    assert boundary.treatment_code == "synthetic.push"


def test_later_rulebook_cannot_reinterpret_structural_selection() -> None:
    v1 = _book(
        version="rules-1",
        end="2026-06-01T00:00:00+00:00",
        rules=(_rule(treatment="synthetic.void"),),
    )
    v2 = _book(
        version="rules-2",
        start="2026-06-01T00:00:00+00:00",
        end=None,
        source_hash=_SOURCE_B,
        rules=(_rule(treatment="synthetic.loss"),),
    )
    timeline = ProviderSettlementRuleTimeline(v1.provider_id, (v1, v2))

    structural = timeline.select_rule(
        evaluated_at="2026-05-01T12:00:00+00:00",
        market_family="synthetic.match_result",
        scenario_code="synthetic.interrupted",
    )

    structural.verify_rulebook(v1)
    assert timeline.select(structural.evaluated_at) is v1
    with pytest.raises(ProviderSettlementRuleError, match="exact rulebook identity"):
        structural.verify_rulebook(v2)


def test_caller_time_selection_is_not_accepted_position_authority() -> None:
    book = _book()
    timeline = ProviderSettlementRuleTimeline(book.provider_id, (book,))

    selection = timeline.select_rule(
        evaluated_at="2026-03-01T00:00:00+00:00",
        market_family="synthetic.match_result",
        scenario_code="synthetic.interrupted",
    )

    assert isinstance(selection, ProviderSettlementRuleSelection)
    assert selection.evaluated_at == "2026-03-01T00:00:00+00:00"
    assert not hasattr(timeline, "bind")
    assert not hasattr(settlement_rules, "ProviderSettlementBinding")
    selection_fields = {field.name for field in fields(ProviderSettlementRuleSelection)}
    assert "evaluated_at" in selection_fields
    assert not (
        selection_fields
        & {"accepted_at", "position_id", "execution_id", "provider_ack", "receipt_id"}
    )


def test_rulebook_source_or_semantics_change_exact_identity() -> None:
    original = _book()
    changed_source = _book(source_hash=_SOURCE_C)
    changed_rule = _book(rules=(_rule(treatment="synthetic.push"),))
    reordered_rules_a = _book(
        rules=(
            _rule(market="synthetic.total", scenario="synthetic.cancelled"),
            _rule(),
        )
    )
    reordered_rules_b = _book(
        rules=tuple(reversed(reordered_rules_a.rules)),
    )

    assert original.rulebook_id != changed_source.rulebook_id
    assert original.rulebook_id != changed_rule.rulebook_id
    assert reordered_rules_a.rulebook_id == reordered_rules_b.rulebook_id
    assert original.rulebook_id == replace(original).rulebook_id


def test_timeline_rejects_overlaps_open_ended_predecessor_and_duplicate_versions() -> None:
    overlapping_v1 = _book(end="2026-07-01T00:00:00+00:00")
    overlapping_v2 = _book(
        version="rules-2",
        start="2026-06-01T00:00:00+00:00",
        end=None,
        source_hash=_SOURCE_B,
    )
    with pytest.raises(ProviderSettlementRuleError, match="must not overlap"):
        ProviderSettlementRuleTimeline(
            overlapping_v1.provider_id,
            (overlapping_v1, overlapping_v2),
        )

    open_v1 = _book(end=None)
    with pytest.raises(ProviderSettlementRuleError, match="open-ended"):
        ProviderSettlementRuleTimeline(
            open_v1.provider_id,
            (
                open_v1,
                _book(
                    version="rules-2",
                    start="2026-06-01T00:00:00+00:00",
                    end=None,
                    source_hash=_SOURCE_B,
                ),
            ),
        )

    duplicate_version = _book(
        version="rules-1",
        start="2026-06-01T00:00:00+00:00",
        end=None,
        source_hash=_SOURCE_B,
    )
    with pytest.raises(ProviderSettlementRuleError, match="duplicate rulebook_version"):
        ProviderSettlementRuleTimeline(
            duplicate_version.provider_id,
            (_book(), duplicate_version),
        )


def test_timeline_gap_fails_closed_instead_of_guessing_nearest_rulebook() -> None:
    v1 = _book(end="2026-04-01T00:00:00+00:00")
    v2 = _book(
        version="rules-2",
        start="2026-05-01T00:00:00+00:00",
        end=None,
        source_hash=_SOURCE_B,
    )
    timeline = ProviderSettlementRuleTimeline(v1.provider_id, (v1, v2))

    with pytest.raises(ProviderSettlementRuleError, match="exactly one rulebook"):
        timeline.select("2026-04-15T00:00:00+00:00")


def test_build_helper_orders_versions_but_keeps_gap_semantics() -> None:
    v1 = _book(end="2026-04-01T00:00:00+00:00")
    v2 = _book(
        version="rules-2",
        start="2026-05-01T00:00:00+00:00",
        end=None,
        source_hash=_SOURCE_B,
    )

    timeline = build_provider_settlement_timeline(v1.provider_id, (v2, v1))

    assert timeline.rulebooks == (v1, v2)
    with pytest.raises(ProviderSettlementRuleError, match="exactly one rulebook"):
        timeline.select("2026-04-15T00:00:00+00:00")


def test_semantically_equal_timestamp_aliases_share_canonical_identities() -> None:
    alias_z = _book(
        start="2026-01-01T00:00:00Z",
        end="2026-06-01T00:00:00Z",
    )
    alias_offset = _book(
        start="2026-01-01T01:00:00+01:00",
        end="2026-06-01T02:00:00+02:00",
    )
    alias_fraction = _book(
        start="2026-01-01T00:00:00.000000+00:00",
        end="2026-06-01T00:00:00.000000+00:00",
    )

    assert alias_z.rulebook_id == alias_offset.rulebook_id == alias_fraction.rulebook_id

    timeline = ProviderSettlementRuleTimeline(alias_z.provider_id, (alias_z,))
    selection_z = timeline.select_rule(
        evaluated_at="2026-03-01T00:00:00Z",
        market_family="synthetic.match_result",
        scenario_code="synthetic.interrupted",
    )
    selection_offset = timeline.select_rule(
        evaluated_at="2026-03-01T01:00:00+01:00",
        market_family="synthetic.match_result",
        scenario_code="synthetic.interrupted",
    )
    selection_fraction = timeline.select_rule(
        evaluated_at="2026-03-01T00:00:00.000000+00:00",
        market_family="synthetic.match_result",
        scenario_code="synthetic.interrupted",
    )

    assert (
        selection_z.selection_id
        == selection_offset.selection_id
        == selection_fraction.selection_id
    )

    different_instant = timeline.select_rule(
        evaluated_at="2026-03-01T00:00:00.000001+00:00",
        market_family="synthetic.match_result",
        scenario_code="synthetic.interrupted",
    )
    assert different_instant.selection_id != selection_z.selection_id


def test_structural_selection_detects_rule_identity_tampering() -> None:
    book = _book()
    timeline = ProviderSettlementRuleTimeline(book.provider_id, (book,))
    selection = timeline.select_rule(
        evaluated_at="2026-03-01T00:00:00+00:00",
        market_family="synthetic.match_result",
        scenario_code="synthetic.interrupted",
    )

    tampered = replace(selection, treatment_code="synthetic.loss")
    with pytest.raises(ProviderSettlementRuleError, match="exact rule identity"):
        tampered.verify_rulebook(book)

    tampered_rule_id = replace(selection, rule_id="f" * 64)
    with pytest.raises(ProviderSettlementRuleError, match="exact rule identity"):
        tampered_rule_id.verify_rulebook(book)


def test_duplicate_rule_key_and_malformed_provenance_are_rejected() -> None:
    duplicate = _rule()
    with pytest.raises(ProviderSettlementRuleError, match="duplicate market/scenario"):
        _book(rules=(duplicate, duplicate))
    with pytest.raises(ProviderSettlementRuleError, match="SHA-256"):
        _book(source_hash="not-a-digest")
    with pytest.raises(ProviderSettlementRuleError, match="timezone"):
        _book(start="2026-01-01T00:00:00")
    with pytest.raises(ProviderSettlementRuleError, match="later"):
        _book(
            start="2026-06-01T00:00:00+00:00",
            end="2026-06-01T00:00:00+00:00",
        )


def test_rulebook_requires_exact_rule_key_and_never_falls_back() -> None:
    book = _book()
    with pytest.raises(ProviderSettlementRuleError, match="no exact rule"):
        book.rule_for("synthetic.match_result", "synthetic.unknown")
    with pytest.raises(ProviderSettlementRuleError, match="no exact rule"):
        book.rule_for("synthetic.other_market", "synthetic.interrupted")


def test_contract_contains_no_money_execution_or_receipt_authority_fields() -> None:
    names = {
        field.name
        for cls in (
            ProviderSettlementRule,
            ProviderSettlementRulebook,
            ProviderSettlementRuleSelection,
        )
        for field in fields(cls)
    }
    forbidden = {
        "stake",
        "amount",
        "payout",
        "odds",
        "balance",
        "credential",
        "token",
        "receipt",
        "provider_ack",
        "execution_id",
        "real_money",
    }

    assert not (names & forbidden)
