from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import autosport.scientific_registry as registry_module
from autosport.scientific_registry import ResearchQuestion, ScientificRegistry


SHA_A = "a" * 64
T0 = "2026-01-01T00:00:00+00:00"


def test_interrupted_publication_preserves_reopenable_prior_state(tmp_path, monkeypatch):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    original = ResearchQuestion("question-original", "Frozen original question", SHA_A, T0)
    registry.append(original)

    def interrupted_publish(target, payload):
        target = Path(target)
        target.with_name(f".{target.name}.interrupted").write_text("{", encoding="utf-8")
        raise OSError("simulated interrupted atomic publication")

    monkeypatch.setattr(registry_module, "atomic_write_json", interrupted_publish)
    with pytest.raises(OSError, match="interrupted atomic publication"):
        registry.append(ResearchQuestion("question-new", "Must not publish", SHA_A, T0))

    reopened = ScientificRegistry(path)
    persisted = reopened.get("ResearchQuestion", "question-original")
    assert persisted is not None
    assert persisted.payload["statement"] == "Frozen original question"
    assert reopened.get("ResearchQuestion", "question-new") is None


def test_concurrent_readers_observe_only_valid_registry_state(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    expected_sha = registry.append(
        ResearchQuestion("question-concurrent", "Concurrent read question", SHA_A, T0)
    )

    def read_once(_: int) -> str:
        reopened = ScientificRegistry(path)
        entry = reopened.get("ResearchQuestion", "question-concurrent")
        assert entry is not None
        return entry.record_sha256

    with ThreadPoolExecutor(max_workers=8) as pool:
        observed = tuple(pool.map(read_once, range(32)))

    assert observed == (expected_sha,) * 32
