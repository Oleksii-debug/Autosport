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
