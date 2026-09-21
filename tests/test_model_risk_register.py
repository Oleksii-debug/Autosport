import json

import pytest

from autosport.model_risk_register import (
    ModelRiskRegisterError,
    OperatorPriority,
    RegisterEntryType,
    RiskRegisterEntry,
    RiskSeverity,
    RiskStatus,
    build_operator_risk_register_view,
)


def entry(**overrides):
    values = dict(
        entry_id="risk-001",
        entry_type=RegisterEntryType.INCIDENT,
        severity=RiskSeverity.HIGH,
        status=RiskStatus.OPEN,
        title="Збій причинної перевірки",
        summary="Кандидат заблоковано до перевірки доказів.",
        detected_at="2026-09-21T18:00:00+02:00",
        updated_at="2026-09-21T16:05:00Z",
        evidence_refs=("sha256:bbb", "sha256:aaa"),
        affected_authorities=("learning.promotion", "science.evaluation"),
        blocks_product_readiness=True,
        blocks_execution=False,
        owner_ref=None,
        model_or_strategy_ref=None,
        resolution_summary=None,
    )
    values.update(overrides)
    return RiskRegisterEntry(**values)


def test_entry_normalizes_time_and_set_like_refs_and_has_stable_digest():
    first = entry()
    second = entry(
        detected_at="2026-09-21T16:00:00Z",
        evidence_refs=("sha256:aaa", "sha256:bbb"),
        affected_authorities=("science.evaluation", "learning.promotion"),
    )

    assert first.detected_at == "2026-09-21T16:00:00.000000Z"
    assert first.updated_at == "2026-09-21T16:05:00.000000Z"
    assert first.evidence_refs == ("sha256:aaa", "sha256:bbb")
    assert first == second
    assert first.entry_sha256 == second.entry_sha256


def test_model_risk_requires_exact_model_or_strategy_reference():
    with pytest.raises(ModelRiskRegisterError, match="model_or_strategy_ref"):
        entry(entry_type=RegisterEntryType.MODEL_RISK)

    model = entry(
        entry_type=RegisterEntryType.MODEL_RISK,
        model_or_strategy_ref="model:challenger-17",
    )
    assert model.model_or_strategy_ref == "model:challenger-17"


def test_chronology_and_bool_aliases_fail_closed():
    with pytest.raises(ModelRiskRegisterError, match="cannot precede"):
        entry(updated_at="2026-09-21T15:59:59Z")
    with pytest.raises(ModelRiskRegisterError, match="must be a bool"):
        entry(blocks_product_readiness=1)


def test_duplicate_refs_fail_closed_instead_of_silently_shrinking_evidence():
    with pytest.raises(ModelRiskRegisterError, match="must not contain duplicates"):
        entry(evidence_refs=("evidence:1", "evidence:1"))


def test_execution_blocker_is_also_readiness_blocker():
    with pytest.raises(ModelRiskRegisterError, match="must also block product readiness"):
        entry(blocks_product_readiness=False, blocks_execution=True)


def test_resolution_contract_is_explicit_and_cannot_hide_live_blockers():
    with pytest.raises(ModelRiskRegisterError, match="require resolution_summary"):
        entry(status=RiskStatus.RESOLVED, blocks_product_readiness=False)

    with pytest.raises(ModelRiskRegisterError, match="cannot retain"):
        entry(
            status=RiskStatus.RESOLVED,
            resolution_summary="Виправлено та перевірено.",
        )

    resolved = entry(
        status=RiskStatus.RESOLVED,
        blocks_product_readiness=False,
        resolution_summary="Виправлено та перевірено.",
    )
    assert resolved.operator_priority is OperatorPriority.CLOSED

    with pytest.raises(ModelRiskRegisterError, match="allowed only for resolved"):
        entry(resolution_summary="Передчасне закриття")


def test_json_roundtrip_is_strict_canonical_and_hash_bound():
    original = entry()
    encoded = original.to_json()
    rebuilt = RiskRegisterEntry.from_json(encoded)
    assert rebuilt == original
    assert rebuilt.entry_sha256 == original.entry_sha256

    payload = json.loads(encoded)
    payload["entry"]["summary"] = "Переписано після підписання"
    tampered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ModelRiskRegisterError, match="entry_sha256"):
        RiskRegisterEntry.from_json(tampered)


def test_json_rejects_unknown_fields_duplicate_keys_and_noncanonical_encoding():
    original = entry()
    payload = json.loads(original.to_json())
    payload["unexpected"] = 1
    with pytest.raises(ModelRiskRegisterError, match="envelope fields"):
        RiskRegisterEntry.from_json(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )

    duplicate = original.to_json().replace(
        '"schema_version":1}', '"schema_version":1,"schema_version":1}'
    )
    with pytest.raises(ModelRiskRegisterError, match="JSON is invalid"):
        RiskRegisterEntry.from_json(duplicate)

    pretty = json.dumps(json.loads(original.to_json()), ensure_ascii=True, indent=2)
    with pytest.raises(ModelRiskRegisterError, match="canonical encoding"):
        RiskRegisterEntry.from_json(pretty)


def test_operator_view_prioritizes_blockers_and_is_order_deterministic():
    critical = entry(
        entry_id="risk-critical",
        severity=RiskSeverity.CRITICAL,
        blocks_execution=True,
        updated_at="2026-09-21T16:08:00Z",
    )
    low = entry(
        entry_id="risk-low",
        severity=RiskSeverity.LOW,
        blocks_product_readiness=False,
        updated_at="2026-09-21T16:09:00Z",
    )
    resolved = entry(
        entry_id="risk-resolved",
        severity=RiskSeverity.CRITICAL,
        status=RiskStatus.RESOLVED,
        blocks_product_readiness=False,
        resolution_summary="Закрито доказами.",
        updated_at="2026-09-21T16:10:00Z",
    )

    first = build_operator_risk_register_view(
        [resolved, low, critical], as_of="2026-09-21T16:11:00Z"
    )
    second = build_operator_risk_register_view(
        [critical, resolved, low], as_of="2026-09-21T18:11:00+02:00"
    )

    assert [row.entry_id for row in first.rows] == [
        "risk-critical",
        "risk-low",
        "risk-resolved",
    ]
    assert first.view_sha256 == second.view_sha256
    assert first.unresolved_count == 2
    assert first.critical_unresolved_count == 1
    assert first.readiness_blocking_ids == ("risk-critical",)
    assert first.execution_blocking_ids == ("risk-critical",)
    assert first.operator_attention_required is True
    assert first.execution_authorized is False
    assert first.readiness_claim_authorized is False
    assert first.real_money_execution is False


def test_operator_view_rejects_duplicate_current_ids_and_future_entry():
    current = entry()
    with pytest.raises(ModelRiskRegisterError, match="duplicate current"):
        build_operator_risk_register_view(
            [current, current], as_of="2026-09-21T16:10:00Z"
        )

    with pytest.raises(ModelRiskRegisterError, match="not available by as_of"):
        build_operator_risk_register_view(
            [current], as_of="2026-09-21T16:04:59Z"
        )


def test_operator_rows_expose_localization_keys_not_english_enum_labels():
    view = build_operator_risk_register_view(
        [entry()], as_of="2026-09-21T16:10:00Z"
    )
    row = view.rows[0]
    assert row.entry_type_key == "model_risk.entry_type.incident"
    assert row.severity_key == "model_risk.severity.high"
    assert row.status_key == "model_risk.status.open"
    assert row.priority_key == "model_risk.priority.urgent"
