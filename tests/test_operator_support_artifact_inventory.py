"""Negative tests for rehashed operator artifact inventory metadata."""
from datetime import datetime, timezone
import hashlib
import json
import pytest

from autosport.operator_support import (
    OperatorSupportTrace, SupportExportError, verify_report_pair,
)


@pytest.mark.parametrize("mutation", [
    "missing", "order", "identity", "purpose", "state", "extra",
    "bool_size", "negative_size", "missing_size", "absent_extra",
])
def test_inventory_contract_cannot_be_rehashed(tmp_path, mutation):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "decisions.jsonl").write_text("synthetic", encoding="utf-8")
    trace = OperatorSupportTrace(
        workspace, auto_start=False,
        clock=lambda: datetime(2026, 10, 17, tzinfo=timezone.utc),
    )
    try:
        diag, agents_path = trace.export_reports(
            tmp_path, selected_strategy="observe-only-v1"
        )
        d = json.loads(diag.read_text(encoding="utf-8"))
        a = json.loads(agents_path.read_text(encoding="utf-8"))
        items = a["memory_and_learning_artifacts"]
        assert items[0]["state"] == "PRESENT_UNVERIFIED"
        assert items[-1]["state"] == "NOT_PRESENT"
        operations = {
            "missing": lambda: items.pop(),
            "order": lambda: items.reverse(),
            "identity": lambda: items[0].update(type="unknown.json"),
            "purpose": lambda: items[0].update(purpose_uk="unverified"),
            "state": lambda: items[0].update(state="VERIFIED"),
            "extra": lambda: items[0].update(extra=True),
            "bool_size": lambda: items[0].update(size_bytes=True),
            "negative_size": lambda: items[0].update(size_bytes=-1),
            "missing_size": lambda: items[0].pop("size_bytes"),
            "absent_extra": lambda: items[-1].update(size_bytes=0),
        }
        operations[mutation]()
        d.pop("report_pair_sha256")
        a.pop("report_pair_sha256")
        digest = hashlib.sha256(json.dumps(
            {"diagnostics": d, "agents": a},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")).hexdigest()
        d["report_pair_sha256"] = digest
        a["report_pair_sha256"] = digest
        diag.write_text(json.dumps(d), encoding="utf-8")
        agents_path.write_text(json.dumps(a), encoding="utf-8")
        with pytest.raises(SupportExportError, match="agent artifact"):
            verify_report_pair(diag, agents_path)
    finally:
        trace.close()
