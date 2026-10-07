from __future__ import annotations

import pytest

from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
from autosport.scientific_registry import ResearchQuestion, ScientificRegistry


SHA_A = "a" * 64
T0 = "2026-09-20T00:00:00Z"
T1 = "2026-09-20T01:00:00Z"
T2 = "2026-09-20T02:00:00Z"


def _question(question_id: str, created_at: str) -> ResearchQuestion:
    return ResearchQuestion(
        question_id,
        f"Scientific registry monotonicity probe {question_id}",
        SHA_A,
        created_at,
    )


def test_valid_prefix_rollback_cannot_fork_scientific_registry(tmp_path, monkeypatch):
    authority_root = tmp_path / "machine-authority"
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(authority_root.resolve()))

    workspace = tmp_path / "workspace"
    registry = ScientificRegistry.initialize_pristine(workspace / "scientific.json")
    registry.append(_question("prefix", T0))
    prefix_bytes = registry.path.read_bytes()

    registry.append(_question("losing-result-branch", T1))
    losing_bytes = registry.path.read_bytes()
    assert losing_bytes != prefix_bytes

    # Simulate external valid-prefix restoration: the workspace bytes roll back,
    # while independent machine authority survives exactly as in the #722 attack.
    registry.path.write_bytes(prefix_bytes)

    with pytest.raises(MonotonicAuthorityRollbackError, match="rolled back|unproven|authority"):
        registry.append(_question("winning-fork", T2))

    assert registry.path.read_bytes() == prefix_bytes
    assert b"winning-fork" not in registry.path.read_bytes()


def test_valid_prefix_rollback_cannot_be_consumed_as_current_registry_truth(tmp_path, monkeypatch):
    authority_root = tmp_path / "machine-authority"
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(authority_root.resolve()))

    registry = ScientificRegistry.initialize_pristine(tmp_path / "workspace" / "scientific.json")
    registry.append(_question("prefix", T0))
    prefix_bytes = registry.path.read_bytes()

    registry.append(_question("current", T1))
    current_bytes = registry.path.read_bytes()
    assert current_bytes != prefix_bytes

    registry.path.write_bytes(prefix_bytes)
    with pytest.raises(MonotonicAuthorityRollbackError, match="rolled back|unproven|authority"):
        ScientificRegistry(registry.path)
    assert registry.path.read_bytes() == prefix_bytes

    registry.path.write_bytes(current_bytes)
    reopened = ScientificRegistry(registry.path)
    assert reopened.get("ResearchQuestion", "prefix") is not None
    assert reopened.get("ResearchQuestion", "current") is not None


def test_normal_scientific_registry_successors_remain_appendable(tmp_path, monkeypatch):
    authority_root = tmp_path / "machine-authority"
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(authority_root.resolve()))

    registry = ScientificRegistry.initialize_pristine(tmp_path / "workspace" / "scientific.json")
    registry.append(_question("first", T0))
    registry.append(_question("second", T1))

    reopened = ScientificRegistry(registry.path)
    assert reopened.get("ResearchQuestion", "first") is not None
    assert reopened.get("ResearchQuestion", "second") is not None

def test_historyless_nonempty_registry_first_read_establishes_monotonic_baseline(
    tmp_path,
    monkeypatch,
):
    authority_root = tmp_path / "machine-authority"
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(authority_root.resolve()))

    source = ScientificRegistry.initialize_pristine(
        tmp_path / "source-workspace" / "scientific.json"
    )
    source.append(_question("legacy-prefix", T0))
    legacy_prefix = source.path.read_bytes()

    legacy_path = tmp_path / "legacy-workspace" / "scientific.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(legacy_prefix)

    # A valid non-empty legacy image has no pre-existing machine authority. The
    # first fully validated read must establish one exact-byte TOFU baseline.
    reopened = ScientificRegistry(legacy_path)
    assert reopened.get("ResearchQuestion", "legacy-prefix") is not None

    source.append(_question("replacement", T1))
    replacement = source.path.read_bytes()
    assert replacement != legacy_prefix
    legacy_path.write_bytes(replacement)

    with pytest.raises(MonotonicAuthorityRollbackError, match="rolled back|unproven|authority"):
        ScientificRegistry(legacy_path)

    # Detection is fail-closed and does not rewrite the observed replacement.
    assert legacy_path.read_bytes() == replacement

