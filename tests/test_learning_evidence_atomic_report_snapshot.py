"""Source-only falsifiers for truthful multi-registry agent-learning reports."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from autosport.learning_evidence_projection import verified_learning_projection

NOW = datetime(2026, 10, 16, 0, 15, tzinfo=timezone.utc)


class EmptyRegistry:
    def __init__(self, path):
        self.path = path

    def causal_records(self, record_type, *, as_of):
        assert as_of == NOW.isoformat()
        return ()


def test_atomic_registry_replacement_between_science_types_is_not_a_qualified_report(tmp_path):
    source = tmp_path / "scientific-registry.json"
    source.write_text("{}", encoding="utf-8")

    class MutatingRegistry(EmptyRegistry):
        def causal_records(self, record_type, *, as_of):
            if record_type == "Experiment":
                source.write_text('{"new_record":"unexpected"}', encoding="utf-8")
            return ()

    with pytest.raises(ValueError, match="changed during causal report"):
        verified_learning_projection(
            tmp_path, as_of=NOW, registry_factory=MutatingRegistry
        )


def test_verified_learning_snapshot_returns_exact_source_hash_not_private_bytes(tmp_path):
    source = tmp_path / "scientific-registry.json"
    source.write_text('{"private":"secret-token"}', encoding="utf-8")
    report = verified_learning_projection(
        tmp_path, as_of=NOW, registry_factory=EmptyRegistry
    )
    assert report["verified_source_sha256"][source.name] == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    assert "secret-token" not in json.dumps(report)


def test_registry_cannot_start_with_no_supervisor_then_silently_include_new_supervisor(tmp_path):
    source = tmp_path / "scientific-registry.json"
    source.write_text("{}", encoding="utf-8")

    class MutatingRegistry(EmptyRegistry):
        def causal_records(self, record_type, *, as_of):
            if record_type == "Experiment":
                (tmp_path / "research-supervisor.json").write_text(
                    "{}", encoding="utf-8"
                )
            return ()

    supervisor = lambda path, registry: SimpleNamespace(list_runs=lambda: ())
    with pytest.raises(ValueError, match="changed during causal report"):
        verified_learning_projection(
            tmp_path, as_of=NOW, registry_factory=MutatingRegistry,
            supervisor_factory=supervisor,
        )


def test_symlink_learning_source_fails_without_following_foreign_file(tmp_path):
    outside = tmp_path.parent / "outside-learning-source.json"
    outside.write_text('{"private":"hidden"}', encoding="utf-8")
    (tmp_path / "scientific-registry.json").symlink_to(outside)
    with pytest.raises(ValueError, match="ordinary independent file"):
        verified_learning_projection(tmp_path, as_of=NOW)


def test_missing_or_incomplete_scientific_sources_are_not_treated_as_training(tmp_path):
    report = verified_learning_projection(tmp_path, as_of=NOW)
    assert report["science"]["state"] == "NOT_PRESENT"
    assert report["verified_source_sha256"] == {}
    assert report["proven_live_model_learning"] is False
    assert report["economic_improvement_proven_by_this_report"] is False
