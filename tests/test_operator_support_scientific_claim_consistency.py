"""Verifier-only fixture tests; do not assert real training or provider evidence."""
from datetime import datetime, timezone
import hashlib
import json

import pytest

from autosport.learning_evidence_projection import _RESEARCH_TYPES
from autosport.operator_support import (
    OperatorSupportTrace, SupportExportError, verify_report_pair,
)


def _projection(_workspace, *, as_of):
    groups = [{"type": name, "count": 0, "recent": []} for name in _RESEARCH_TYPES]
    exp = next(group for group in groups if group["type"] == "Experiment")
    exp["count"] = 1
    exp["recent"] = [{
        "record_identity_sha256": "a" * 64,
        "record_sha256": "b" * 64,
        "available_at": "2026-10-16T23:59:59+00:00",
    }]
    return {
        "science": {
            "state": "CANONICAL_REGISTRY_VERIFIED",
            "cutoff_utc": as_of.isoformat(),
            "records_total": 1,
            "types": groups,
            "experiment_outcomes": {"NEGATIVE": 1},
            "promotion_decisions": {},
            "promotion_evidence_validity": {},
            "promotion_evidence_guardrail_failures": 0,
            "promotion_evidence_holdout_missing": 0,
            "evidence_scope": "causal_records_by_availability_not_training_performance_proof",
        },
        "research_runs": {"state": "NOT_PRESENT"},
        "agent_loop": {"state": "NOT_PRESENT"},
        "knowledge_retention": {"status": "NOT_EVALUATED"},
        "proven_live_model_learning": False,
        "economic_improvement_proven_by_this_report": False,
        "has_live_execution_authority": False,
        "verified_source_sha256": {"scientific-registry.json": "c" * 64},
    }


def _pair(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "autosport.operator_support.verified_learning_projection", _projection,
    )
    root = tmp_path / "workspace"
    root.mkdir()
    trace = OperatorSupportTrace(
        root, auto_start=False,
        clock=lambda: datetime(2026, 10, 17, tzinfo=timezone.utc),
    )
    try:
        trace.heartbeat()
        return trace.export_reports(
            tmp_path, selected_strategy="observe-only-v1",
        )
    finally:
        trace.close()


def _rehash(d_path, a_path, d, a):
    d.pop("report_pair_sha256")
    a.pop("report_pair_sha256")
    digest = hashlib.sha256(json.dumps(
        {"diagnostics": d, "agents": a},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    d["report_pair_sha256"] = a["report_pair_sha256"] = digest
    d_path.write_text(json.dumps(d), encoding="utf-8")
    a_path.write_text(json.dumps(a), encoding="utf-8")


def test_negative_science_fixture_reconciles_without_learning_claim(tmp_path, monkeypatch):
    diagnostic, agents = _pair(tmp_path, monkeypatch)
    assert verify_report_pair(diagnostic, agents)
    evidence = json.loads(agents.read_text(encoding="utf-8"))["verified_learning_evidence"]
    assert evidence["science"]["experiment_outcomes"] == {"NEGATIVE": 1}
    assert evidence["proven_live_model_learning"] is False


@pytest.mark.parametrize("mutation", (
    "invent_positive", "invent_promotion", "invent_valid_evidence",
    "inflated_total", "boolean_total", "negative_group", "boolean_group",
    "missing_group", "duplicate_group", "empty_recent",
    "unapproved_outcome", "unapproved_scope", "injected_claim",
    "unsupported_guardrails", "unsupported_holdout",
    "raw_record_identity", "bad_record_digest", "extra_record_field",
))
def test_rehashed_science_cannot_inflate_aggregate_or_claim(
    tmp_path, monkeypatch, mutation,
):
    d_path, a_path = _pair(tmp_path, monkeypatch)
    d = json.loads(d_path.read_text(encoding="utf-8"))
    a = json.loads(a_path.read_text(encoding="utf-8"))
    science = a["verified_learning_evidence"]["science"]
    groups = science["types"]
    experiment = next(g for g in groups if g["type"] == "Experiment")
    if mutation == "invent_positive":
        science["experiment_outcomes"] = {"POSITIVE": 2}
    elif mutation == "invent_promotion":
        science["promotion_decisions"] = {"PROMOTE": 1}
    elif mutation == "invent_valid_evidence":
        science["promotion_evidence_validity"] = {"ELIGIBLE": 1}
    elif mutation == "inflated_total":
        science["records_total"] = 8
    elif mutation == "boolean_total":
        science["records_total"] = True
    elif mutation == "negative_group":
        experiment["count"] = -1
    elif mutation == "boolean_group":
        experiment["count"] = True
    elif mutation == "missing_group":
        groups.pop()
    elif mutation == "duplicate_group":
        groups[0]["type"] = groups[1]["type"]
    elif mutation == "empty_recent":
        experiment["recent"] = []
    elif mutation == "unapproved_outcome":
        science["experiment_outcomes"] = {"PROVEN_PROFITABLE": 1}
    elif mutation == "unapproved_scope":
        science["evidence_scope"] = "trained_model_economic_success"
    elif mutation == "injected_claim":
        science["model_training_completed"] = True
    elif mutation == "unsupported_guardrails":
        science["promotion_evidence_guardrail_failures"] = 1
    elif mutation == "unsupported_holdout":
        science["promotion_evidence_holdout_missing"] = 1
    elif mutation == "raw_record_identity":
        experiment["recent"][0]["record_identity_sha256"] = "identifier"
    elif mutation == "bad_record_digest":
        experiment["recent"][0]["record_sha256"] = "not-a-hash"
    elif mutation == "extra_record_field":
        experiment["recent"][0]["unexpected"] = "unsupported"
    _rehash(d_path, a_path, d, a)
    with pytest.raises(SupportExportError, match="scientific report"):
        verify_report_pair(d_path, a_path)
