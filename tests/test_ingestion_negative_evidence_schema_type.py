from __future__ import annotations

import json

import pytest

from autosport.ingestion_negative_evidence import project_ingestion_negative_evidence


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_status_schema_version_requires_exact_integer(tmp_path, schema_version) -> None:
    status_path = tmp_path / "continuous_observation_status.json"
    status_path.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "kind": "autosport_continuous_local_observation",
                "run_id": "run-1",
                "source_id": "source-1",
                "state": "stopped",
                "attempted_cycles": 1,
                "successful_cycles": 1,
                "last_error_kind": None,
                "stop_reason": "bounded_complete",
            }
        ),
        encoding="utf-8",
    )

    evidence = project_ingestion_negative_evidence(status_path)

    assert evidence.evidence_state == "invalid"
    assert evidence.has_negative_evidence is True
    assert evidence.reason_codes == ("status_unsupported_schema",)
