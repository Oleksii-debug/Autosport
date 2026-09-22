from dataclasses import fields, replace

import pytest

from autosport.provider_settlement_rules import (
    ProviderSettlementRule,
    ProviderSettlementRuleSelection,
    ProviderSettlementRulebook,
)
from autosport.provider_settlement_rule_drift import (
    LatestRulesetSignal,
    ProviderSettlementRuleDriftError,
    ProviderSettlementRuleSnapshotAssertion,
    QuarantineReason,
    RulesetDisposition,
    assess_settlement_rule_drift,
)


_SCOPE = "1" * 64
_INFO = "2" * 64


def _book(
    *,
    version: str = "v1",
    treatment: str = "synthetic.void",
    source: str = "a" * 64,
    provider: str = "synthetic-provider-a",
) -> ProviderSettlementRulebook:
    rule = ProviderSettlementRule(
        market_family="synthetic.match_result",
        scenario_code="synthetic.interrupted",
        treatment_code=treatment,
    )
    return ProviderSettlementRulebook(
        provider_id=provider,
        rulebook_version=version,
        effective_from="2026-01-01T00:00:00+00:00",
        effective_until=None,
        source_ref=f"synthetic://{provider}/{version}",
        source_payload_sha256=source,
        rules=(rule,),
    )


def _selection(book: ProviderSettlementRulebook) -> ProviderSettlementRuleSelection:
    rule = book.rules[0]
    return ProviderSettlementRuleSelection(
        provider_id=book.provider_id,
        rulebook_version=book.rulebook_version,
        rulebook_id=book.rulebook_id,
        evaluated_at="2026-09-01T00:00:00+00:00",
        market_family=rule.market_family,
        scenario_code=rule.scenario_code,
        treatment_code=rule.treatment_code,
        rule_id=rule.rule_id,
    )


def _assertion(
    book: ProviderSettlementRulebook | None = None,
) -> ProviderSettlementRuleSnapshotAssertion:
    bound = book or _book()
    return ProviderSettlementRuleSnapshotAssertion.from_selection(
        _selection(bound),
        market_scope_sha256=_SCOPE,
        market_info_sha256=_INFO,
    )


def test_exact_bound_snapshot_is_usable() -> None:
    book = _book()
    assessment = assess_settlement_rule_drift(
        _assertion(book),
        bound_rulebook=book,
        latest_rulebook=book,
        current_market_scope_sha256=_SCOPE,
        current_market_info_sha256=_INFO,
    )

    assert assessment.disposition is RulesetDisposition.USE_BOUND_SNAPSHOT
    assert assessment.latest_signal is LatestRulesetSignal.SAME_AS_BOUND
    assert assessment.bound_treatment_code == "synthetic.void"


def test_changed_latest_never_replaces_bound_treatment() -> None:
    old = _book()
    latest = _book(version="v2", treatment="synthetic.loss", source="b" * 64)

    assessment = assess_settlement_rule_drift(
        _assertion(old),
        bound_rulebook=old,
        latest_rulebook=latest,
        current_market_scope_sha256=_SCOPE,
        current_market_info_sha256=_INFO,
    )

    assert assessment.disposition is RulesetDisposition.USE_BOUND_SNAPSHOT
    assert assessment.latest_signal is LatestRulesetSignal.LATEST_DIFFERS
    assert assessment.bound_treatment_code == "synthetic.void"
    assert assessment.latest_rulebook_id == latest.rulebook_id


def test_missing_latest_is_explicit_but_does_not_replace_bound_snapshot() -> None:
    book = _book()
    assessment = assess_settlement_rule_drift(
        _assertion(book),
        bound_rulebook=book,
        latest_rulebook=None,
        current_market_scope_sha256=_SCOPE,
        current_market_info_sha256=_INFO,
    )

    assert assessment.disposition is RulesetDisposition.USE_BOUND_SNAPSHOT
    assert assessment.latest_signal is LatestRulesetSignal.LATEST_UNKNOWN
    assert assessment.bound_treatment_code == "synthetic.void"


def test_unrelated_latest_is_diagnostic_only() -> None:
    bound = _book()
    unrelated = _book(
        provider="synthetic-provider-b",
        version="other",
        source="c" * 64,
    )

    assessment = assess_settlement_rule_drift(
        _assertion(bound),
        bound_rulebook=bound,
        latest_rulebook=unrelated,
        current_market_scope_sha256=_SCOPE,
        current_market_info_sha256=_INFO,
    )

    assert assessment.disposition is RulesetDisposition.USE_BOUND_SNAPSHOT
    assert assessment.latest_signal is LatestRulesetSignal.LATEST_UNRELATED
    assert assessment.bound_treatment_code == "synthetic.void"


@pytest.mark.parametrize(
    ("bound", "reason"),
    [
        (None, QuarantineReason.BOUND_SNAPSHOT_MISSING),
        (
            _book(version="wrong", source="d" * 64),
            QuarantineReason.BOUND_SNAPSHOT_MISMATCH,
        ),
    ],
)
def test_missing_or_wrong_bound_snapshot_quarantines(
    bound: ProviderSettlementRulebook | None,
    reason: QuarantineReason,
) -> None:
    assessment = assess_settlement_rule_drift(
        _assertion(),
        bound_rulebook=bound,
        latest_rulebook=None,
        current_market_scope_sha256=_SCOPE,
        current_market_info_sha256=_INFO,
    )

    assert assessment.disposition is RulesetDisposition.QUARANTINE
    assert assessment.quarantine_reason is reason
    assert assessment.bound_treatment_code is None


def test_scope_drift_quarantines() -> None:
    book = _book()
    assessment = assess_settlement_rule_drift(
        _assertion(book),
        bound_rulebook=book,
        latest_rulebook=book,
        current_market_scope_sha256="3" * 64,
        current_market_info_sha256=_INFO,
    )

    assert assessment.quarantine_reason is QuarantineReason.MARKET_SCOPE_DRIFT
    assert assessment.bound_treatment_code is None


def test_market_info_drift_quarantines() -> None:
    book = _book()
    assessment = assess_settlement_rule_drift(
        _assertion(book),
        bound_rulebook=book,
        latest_rulebook=book,
        current_market_scope_sha256=_SCOPE,
        current_market_info_sha256="4" * 64,
    )

    assert assessment.quarantine_reason is QuarantineReason.MARKET_INFO_DRIFT
    assert assessment.bound_treatment_code is None


def test_assertion_detects_selection_id_substitution() -> None:
    assertion = _assertion()
    with pytest.raises(ProviderSettlementRuleDriftError, match="selection_id"):
        replace(assertion, selection_id="f" * 64)


def test_assertion_identity_binds_market_info_and_scope() -> None:
    assertion = _assertion()
    changed_info = replace(assertion, market_info_sha256="5" * 64)
    changed_scope = replace(assertion, market_scope_sha256="6" * 64)

    assert len(
        {
            assertion.assertion_id,
            changed_info.assertion_id,
            changed_scope.assertion_id,
        }
    ) == 3


def test_malformed_digests_fail_closed() -> None:
    book = _book()
    with pytest.raises(
        ProviderSettlementRuleDriftError,
        match="market_scope_sha256",
    ):
        ProviderSettlementRuleSnapshotAssertion.from_selection(
            _selection(book),
            market_scope_sha256="bad",
            market_info_sha256=_INFO,
        )


def test_duck_typed_selection_rejected() -> None:
    class FakeSelection:
        pass

    with pytest.raises(
        ProviderSettlementRuleDriftError,
        match="exact ProviderSettlementRuleSelection",
    ):
        ProviderSettlementRuleSnapshotAssertion.from_selection(
            FakeSelection(),  # type: ignore[arg-type]
            market_scope_sha256=_SCOPE,
            market_info_sha256=_INFO,
        )


def test_invalid_latest_type_rejected() -> None:
    book = _book()
    with pytest.raises(
        ProviderSettlementRuleDriftError,
        match="latest_rulebook",
    ):
        assess_settlement_rule_drift(
            _assertion(book),
            bound_rulebook=book,
            latest_rulebook=object(),  # type: ignore[arg-type]
            current_market_scope_sha256=_SCOPE,
            current_market_info_sha256=_INFO,
        )


def test_quarantine_does_not_expose_treatment() -> None:
    book = _book()
    assessment = assess_settlement_rule_drift(
        _assertion(book),
        bound_rulebook=book,
        latest_rulebook=book,
        current_market_scope_sha256="7" * 64,
        current_market_info_sha256=_INFO,
    )

    assert assessment.bound_rulebook_id is None
    assert assessment.bound_rule_id is None
    assert assessment.bound_treatment_code is None


def test_contract_has_no_money_execution_or_receipt_authority_fields() -> None:
    names = {
        field.name
        for field in fields(ProviderSettlementRuleSnapshotAssertion)
    }
    forbidden = {
        "stake",
        "amount",
        "payout",
        "odds",
        "receipt",
        "provider_ack",
        "execution_id",
        "real_money",
        "accepted_at",
        "order_id",
    }

    assert not (names & forbidden)
