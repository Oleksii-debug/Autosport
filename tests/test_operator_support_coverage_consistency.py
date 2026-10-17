"""Source-only checks that exported coverage claims match report observations."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from autosport.operator_support import OperatorSupportTrace, SupportExportError, verify_report_pair


@pytest.mark.parametrize("mutation", [
    "status", "first", "age", "tail", "lost", "fault",
    "heartbeat", "activity", "total", "status_counts", "agent_activity",
])
def test_report_pair_reconciles_liveness_and_activity(tmp_path, mutation):
    moment = [datetime(2026, 10, 16, tzinfo=timezone.utc)]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=lambda: moment[0])
    try:
        trace.heartbeat()
        trace.record(component="agent", action="learning.observed", status="active")
        moment[0] += timedelta(seconds=9)
        diagnostic, agents = trace.export_reports(tmp_path, selected_strategy="observe-only-v1")
        d = json.loads(diagnostic.read_text(encoding="utf-8"))
        a = json.loads(agents.read_text(encoding="utf-8"))
        assert d["coverage"]["recorder_status"] == "PARTIAL_HEARTBEAT_GAPS"
        changes = {
            "status": lambda: d["coverage"].update(recorder_status="HEALTHY"),
            "first": lambda: d["coverage"].update(first_heartbeat_utc=None),
            "age": lambda: d["coverage"].update(last_verified_heartbeat_age_seconds=0),
            "tail": lambda: d["coverage"].update(tail_seconds_without_verified_heartbeat=0),
            "lost": lambda: d["coverage"].update(lost_activity_count=2),
            "fault": lambda: d["coverage"].update(recorder_fault_present=True),
            "heartbeat": lambda: d["seconds"][0].update(heartbeat=False),
            "activity": lambda: d["seconds"][0].update(activities=0),
            "total": lambda: d["coverage"].update(activity_count=0),
            "status_counts": lambda: d.update(status_counts={}),
            "agent_activity": lambda: a.update(instrumented_agent_activity=[]),
        }
        changes[mutation]()
        d.pop("report_pair_sha256")
        a.pop("report_pair_sha256")
        digest = hashlib.sha256(json.dumps(
            {"diagnostics": d, "agents": a}, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        d["report_pair_sha256"] = digest
        a["report_pair_sha256"] = digest
        diagnostic.write_text(json.dumps(d), encoding="utf-8")
        agents.write_text(json.dumps(a), encoding="utf-8")
        with pytest.raises(SupportExportError, match="diagnostic"):
            verify_report_pair(diagnostic, agents)
    finally:
        trace.close()

@pytest.mark.parametrize("mutation", [
    "unlisted_action", "unlisted_component", "unlisted_status",
    "extra_activity_field", "missing_sequence", "zero_sequence",
    "boolean_sequence", "string_sequence", "negative_cycle",
    "null_cycle", "extra_second_field",
])
def test_report_pair_rejects_forged_observation_even_with_new_digest(tmp_path, mutation):
    # A user-supplied support pair can be rehashed; its event vocabulary,
    # shape and journal sequence still must agree with the instrumented writer.
    moment = datetime(2026, 10, 17, tzinfo=timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=lambda: moment)
    try:
        trace.heartbeat()
        trace.record(component="agent", action="learning.observed", status="active")
        diagnostic, agent_path = trace.export_reports(
            tmp_path, selected_strategy="observe-only-v1"
        )
        d = json.loads(diagnostic.read_text(encoding="utf-8"))
        a = json.loads(agent_path.read_text(encoding="utf-8"))
        event = d["activities"][0]
        if mutation == "unlisted_action":
            event["action"] = "learning.successfully_trained"
        elif mutation == "unlisted_component":
            event["component"] = "unverified-agent"
        elif mutation == "unlisted_status":
            event["status"] = "proven_profitable"
            d["status_counts"] = {"proven_profitable": 1}
        elif mutation == "extra_activity_field":
            event["private_note"] = "not a writer-authorized export field"
        elif mutation == "missing_sequence":
            del event["sequence"]
        elif mutation == "zero_sequence":
            event["sequence"] = 0
        elif mutation == "boolean_sequence":
            event["sequence"] = True
        elif mutation == "string_sequence":
            event["sequence"] = "3"
        elif mutation == "negative_cycle":
            event["cycle"] = -1
        elif mutation == "null_cycle":
            event["cycle"] = None
        elif mutation == "extra_second_field":
            d["seconds"][0]["unsupported_data"] = "not an observed signal"
        # Avoid an unrelated cross-report mismatch masking the missing guard.
        a["instrumented_agent_activity"] = (
            [dict(event)] if event["component"] == "agent" else []
        )
        d.pop("report_pair_sha256")
        a.pop("report_pair_sha256")
        digest = hashlib.sha256(json.dumps(
            {"diagnostics": d, "agents": a}, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        d["report_pair_sha256"] = digest
        a["report_pair_sha256"] = digest
        diagnostic.write_text(json.dumps(d), encoding="utf-8")
        agent_path.write_text(json.dumps(a), encoding="utf-8")
        with pytest.raises(SupportExportError, match="diagnostic"):
            verify_report_pair(diagnostic, agent_path)
    finally:
        trace.close()

@pytest.mark.parametrize("mutation", [
    "diagnostics_injected_field", "agents_fabricated_training",
    "coverage_injected_field", "learning_fabricated_proof",
    "diagnostics_future_schema", "agents_boolean_schema",
    "agents_missing_field", "coverage_missing_field",
])
def test_report_pair_rejects_injected_claims_and_schema_even_when_rehashed(tmp_path, mutation):
    clock = datetime(2026, 10, 17, tzinfo=timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=lambda: clock)
    try:
        trace.heartbeat()
        trace.record(component="agent", action="learning.observed", status="active")
        diagnostic, agent_path = trace.export_reports(
            tmp_path, selected_strategy="observe-only-v1"
        )
        verify_report_pair(diagnostic, agent_path)
        d = json.loads(diagnostic.read_text(encoding="utf-8"))
        a = json.loads(agent_path.read_text(encoding="utf-8"))
        if mutation == "diagnostics_injected_field":
            d["raw_private_token"] = "must not be accepted"
        elif mutation == "agents_fabricated_training":
            a["agent_training_succeeded"] = True
        elif mutation == "coverage_injected_field":
            d["coverage"]["complete_os_visibility"] = True
        elif mutation == "learning_fabricated_proof":
            a["verified_learning_evidence"]["verified_training_success"] = True
        elif mutation == "diagnostics_future_schema":
            d["schema_version"] = 2
        elif mutation == "agents_boolean_schema":
            a["schema_version"] = True
        elif mutation == "agents_missing_field":
            del a["known_limits_uk"]
        elif mutation == "coverage_missing_field":
            del d["coverage"]["event_time_basis"]
        d.pop("report_pair_sha256")
        a.pop("report_pair_sha256")
        digest = hashlib.sha256(json.dumps(
            {"diagnostics": d, "agents": a}, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        d["report_pair_sha256"] = digest
        a["report_pair_sha256"] = digest
        diagnostic.write_text(json.dumps(d), encoding="utf-8")
        agent_path.write_text(json.dumps(a), encoding="utf-8")
        with pytest.raises(SupportExportError, match="(report|schema)"):
            verify_report_pair(diagnostic, agent_path)
    finally:
        trace.close()


@pytest.mark.parametrize("channel", ("experiment", "research", "agent"))
@pytest.mark.parametrize("invalid_time", (
    "2026-10-18T00:00:01Z", "2026-10-16T00:00:00", "not-a-date",
))
def test_report_pair_rejects_rehashed_noncausal_science(
    tmp_path, channel, invalid_time,
):
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=lambda: now)
    try:
        diagnostic, agent_path = trace.export_reports(
            tmp_path, selected_strategy="observe-only-v1"
        )
        d = json.loads(diagnostic.read_text(encoding="utf-8"))
        a = json.loads(agent_path.read_text(encoding="utf-8"))
        evidence = a["verified_learning_evidence"]
        if channel == "experiment":
            evidence["science"].update(
                state="CANONICAL_REGISTRY_VERIFIED", records_total=1,
                types=[{"type": "Experiment", "count": 1, "recent": [{
                    "record_identity_sha256": "a" * 64,
                    "record_sha256": "b" * 64, "available_at": invalid_time,
                }]}],
            )
        elif channel == "research":
            evidence["science"].update(
                state="CANONICAL_REGISTRY_VERIFIED", records_total=0, types=[],
            )
            evidence["research_runs"] = {
                "state": "VERIFIED_SUPERVISOR_SNAPSHOTS", "count": 1,
                "recent": [{"updated_at": invalid_time}],
                "status_counts": {"SUCCEEDED": 1},
            }
        else:
            evidence["agent_loop"] = {
                "state": "VERIFIED_RESTART_SNAPSHOT",
                "updated_at": invalid_time,
            }
        d.pop("report_pair_sha256")
        a.pop("report_pair_sha256")
        digest = hashlib.sha256(json.dumps(
            {"diagnostics": d, "agents": a}, sort_keys=True,
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        d["report_pair_sha256"] = a["report_pair_sha256"] = digest
        diagnostic.write_text(json.dumps(d), encoding="utf-8")
        agent_path.write_text(json.dumps(a), encoding="utf-8")
        with pytest.raises(SupportExportError, match="scientific report"):
            verify_report_pair(diagnostic, agent_path)
    finally:
        trace.close()


def test_report_pair_accepts_past_science_with_timezone_offset(tmp_path):
    now = datetime(2026, 10, 17, tzinfo=timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=lambda: now)
    try:
        diagnostic, agent_path = trace.export_reports(
            tmp_path, selected_strategy="observe-only-v1"
        )
        d = json.loads(diagnostic.read_text(encoding="utf-8"))
        a = json.loads(agent_path.read_text(encoding="utf-8"))
        evidence = a["verified_learning_evidence"]
        evidence["science"].update(
            state="CANONICAL_REGISTRY_VERIFIED", records_total=1,
            types=[{"type": "Experiment", "count": 1, "recent": [{
                "record_identity_sha256": "a" * 64,
                "record_sha256": "b" * 64,
                "available_at": "2026-10-17T01:00:00+02:00",
            }]}],
        )
        evidence["research_runs"] = {
            "state": "VERIFIED_SUPERVISOR_SNAPSHOTS", "count": 1,
            "recent": [{"updated_at": "2026-10-16T22:00:00Z"}],
            "status_counts": {"SUCCEEDED": 1},
        }
        evidence["agent_loop"] = {
            "state": "VERIFIED_RESTART_SNAPSHOT",
            "updated_at": "2026-10-16T23:15:00Z",
        }
        d.pop("report_pair_sha256")
        a.pop("report_pair_sha256")
        digest = hashlib.sha256(json.dumps(
            {"diagnostics": d, "agents": a}, sort_keys=True,
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        d["report_pair_sha256"] = a["report_pair_sha256"] = digest
        diagnostic.write_text(json.dumps(d), encoding="utf-8")
        agent_path.write_text(json.dumps(a), encoding="utf-8")
        assert verify_report_pair(diagnostic, agent_path) == digest
    finally:
        trace.close()
