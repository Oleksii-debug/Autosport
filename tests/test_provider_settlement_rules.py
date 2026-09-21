from __future__ import annotations

import pytest

from autosport.provider_settlement_rules import (
    ProviderSettlementRuleCatalog,
    ProviderSettlementRuleError,
    ProviderSettlementRuleVersion,
    SettlementRuleApplicability,
)


def _rule(**overrides: object) -> ProviderSettlementRuleVersion:
    values: dict[str, object] = {
        "venue_id": "venue-a",
        "rule_set_id": "football-standard",
        "rule_version": "2026.09",
        "effective_from": "2026-09-01T00:00:00+00:00",
        "effective_until": "2026-10-01T00:00:00+00:00",
        "observed_at": "2026-09-20T00:00:00+00:00",
        "source_ref": "provider-rules://football/2026.09",
        "source_payload_sha256": "a" * 64,
    }
    values.update(overrides)
    return ProviderSettlementRuleVersion(**values)  # type: ignore[arg-type]


def test_exact_rule_version_is_applicable_only_inside_effective_interval() -> None:
    rule = _rule()

    assert rule.applicability(
        venue_id="venue-a",
        rule_set_id="football-standard",
        rule_version="2026.09",
        event_time="2026-09-21T12:00:00+00:00",
    ) is SettlementRuleApplicability.APPLICABLE
    assert rule.applicability(
        venue_id="venue-a",
        rule_set_id="football-standard",
        rule_version="2026.09",
        event_time="2026-10-01T00:00:00+00:00",
    ) is SettlementRuleApplicability.NOT_EFFECTIVE
    assert rule.settlement_authorized is False


def test_advance_noticed_rule_is_valid_but_not_effective_early() -> None:
    rule = _rule(
        effective_from="2026-10-01T00:00:00+00:00",
        effective_until="2026-11-01T00:00:00+00:00",
        observed_at="2026-09-20T00:00:00+00:00",
    )
    assert rule.applicability(
        venue_id="venue-a",
        rule_set_id="football-standard",
        rule_version="2026.09",
        event_time="2026-09-30T23:59:59+00:00",
    ) is SettlementRuleApplicability.NOT_EFFECTIVE


def test_missing_foreign_or_changed_version_never_becomes_applicable() -> None:
    rule = _rule()

    assert rule.applicability(
        venue_id="venue-a",
        rule_set_id="football-standard",
        rule_version=None,
        event_time="2026-09-21T12:00:00+00:00",
    ) is SettlementRuleApplicability.UNKNOWN
    assert rule.applicability(
        venue_id="venue-b",
        rule_set_id="football-standard",
        rule_version="2026.09",
        event_time="2026-09-21T12:00:00+00:00",
    ) is SettlementRuleApplicability.UNKNOWN
    assert rule.applicability(
        venue_id="venue-a",
        rule_set_id="football-standard",
        rule_version="2026.10",
        event_time="2026-09-21T12:00:00+00:00",
    ) is SettlementRuleApplicability.VERSION_MISMATCH


def test_rule_identity_binds_version_interval_and_source() -> None:
    rule = _rule()
    assert len(rule.rule_id) == 64
    assert rule.rule_id != _rule(rule_version="2026.10").rule_id
    assert rule.rule_id != _rule(source_payload_sha256="b" * 64).rule_id
    assert rule.rule_id != _rule(effective_until=None).rule_id


def test_catalog_is_deterministic_and_rejects_duplicate_version_identity() -> None:
    first = _rule()
    second = _rule(
        venue_id="venue-b",
        rule_version="2026.08",
        source_ref="provider-rules://football/2026.08",
        source_payload_sha256="b" * 64,
    )
    left = ProviderSettlementRuleCatalog(
        rules=(first, second), as_of="2026-09-21T00:00:00+00:00"
    )
    right = ProviderSettlementRuleCatalog(
        rules=(second, first), as_of="2026-09-21T00:00:00+00:00"
    )
    assert left.to_canonical_dict() == right.to_canonical_dict()
    assert left.catalog_id == right.catalog_id
    assert left.settlement_authorized is False

    with pytest.raises(ProviderSettlementRuleError, match="duplicate settlement rule version"):
        ProviderSettlementRuleCatalog(
            rules=(first, _rule(source_payload_sha256="c" * 64)),
            as_of="2026-09-21T00:00:00+00:00",
        )


def test_catalog_rejects_future_observation_and_noncanonical_members() -> None:
    with pytest.raises(ProviderSettlementRuleError, match="after catalog as_of"):
        ProviderSettlementRuleCatalog(
            rules=(_rule(observed_at="2026-09-22T00:00:00+00:00"),),
            as_of="2026-09-21T00:00:00+00:00",
        )
    with pytest.raises(ProviderSettlementRuleError, match="non-empty tuple"):
        ProviderSettlementRuleCatalog(rules=(), as_of="2026-09-21T00:00:00+00:00")

    class DerivedRule(ProviderSettlementRuleVersion):
        pass

    base = _rule()
    derived = DerivedRule(**{
        "venue_id": base.venue_id,
        "rule_set_id": base.rule_set_id,
        "rule_version": base.rule_version,
        "effective_from": base.effective_from,
        "effective_until": base.effective_until,
        "observed_at": base.observed_at,
        "source_ref": base.source_ref,
        "source_payload_sha256": base.source_payload_sha256,
    })
    with pytest.raises(ProviderSettlementRuleError, match="exact ProviderSettlementRuleVersion"):
        ProviderSettlementRuleCatalog(
            rules=(derived,), as_of="2026-09-21T00:00:00+00:00"
        )


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"effective_from": "2026-09-01T00:00:00"}, "timezone offset"),
        ({"effective_until": "2026-09-01T00:00:00+00:00"}, "after effective_from"),
        ({"observed_at": "2026-09-20T00:00:00"}, "timezone offset"),
        ({"source_payload_sha256": "A" * 64}, "lowercase 64-character"),
        ({"schema_version": True}, "schema_version"),
        ({"schema_version": 1.0}, "schema_version"),
    ],
)
def test_rule_rejects_noncanonical_evidence(
    overrides: dict[str, object], match: str
) -> None:
    with pytest.raises(ProviderSettlementRuleError, match=match):
        _rule(**overrides)
