"""Scientific evidence projection must fail closed on adapter cutoff violations."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from autosport.learning_evidence_projection import verified_learning_projection


@pytest.mark.parametrize("available_at", (
    "2026-10-16T00:15:01Z",
    "2026-10-17T00:00:00+00:00",
    "2026-10-15T23:00:00",
    "not-a-timestamp",
))
def test_scientific_projection_rejects_noncausal_records(tmp_path, available_at):
    (tmp_path / "scientific-registry.json").write_text("{}", encoding="utf-8")
    cutoff = datetime(2026, 10, 16, 0, 15, tzinfo=timezone.utc)

    class FaultyRegistry:
        def __init__(self, path):
            self.path = path

        def causal_records(self, kind, *, as_of):
            assert as_of == cutoff.isoformat()
            if kind != "Experiment":
                return ()
            return (SimpleNamespace(
                record_id="synthetic-record",
                record_sha256="a" * 64,
                available_at=available_at,
                payload={"outcome": "POSITIVE"},
            ),)

    with pytest.raises(ValueError, match="scientific record"):
        verified_learning_projection(
            tmp_path, as_of=cutoff, registry_factory=FaultyRegistry
        )
