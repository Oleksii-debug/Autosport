from __future__ import annotations

import json

import pytest

from autosport.scientific_registry import Hypothesis, ResearchQuestion, ScientificRegistry


SHA_A = "a" * 64
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T00:01:00+00:00"


def _question() -> ResearchQuestion:
    return ResearchQuestion("question-1", "Frozen question", SHA_A, T0)


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        "hypothesis-1",
        "question-1",
        "Frozen hypothesis",
        "candidate beats baseline",
        "candidate does not beat baseline",
        "roi",
        ("max_drawdown",),
        T1,
    )


def _rewrite_state(path, state: dict[str, object]) -> None:
    path.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_reordering_individually_valid_records_must_fail_closed(tmp_path):
    """A byte-level reorder must not invert ScientificRegistry causal authority."""

    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    registry.append(_question())
    registry.append(_hypothesis())
    assert registry.causal_precedes(
        "ResearchQuestion",
        "question-1",
        "Hypothesis",
        "hypothesis-1",
    )

    state = json.loads(path.read_text(encoding="utf-8"))
    original_digests = [item["record_sha256"] for item in state["records"]]
    state["records"].reverse()
    _rewrite_state(path, state)

    tampered = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(item["record_sha256"] for item in tampered["records"]) == sorted(
        original_digests
    )

    with pytest.raises(ValueError):
        ScientificRegistry(path)


def test_deleting_committed_predecessor_must_not_leave_valid_dependent_history(tmp_path):
    """Deleting a valid predecessor cannot silently shrink scientific history."""

    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    registry.append(_question())
    registry.append(_hypothesis())

    state = json.loads(path.read_text(encoding="utf-8"))
    state["records"] = [
        item
        for item in state["records"]
        if not (
            item["record_type"] == "ResearchQuestion"
            and item["record_id"] == "question-1"
        )
    ]
    _rewrite_state(path, state)

    with pytest.raises(ValueError):
        ScientificRegistry(path)


def test_truncated_registry_cannot_be_pristine_reinitialized_over_history(tmp_path):
    """Corrupt durable history must fail closed instead of becoming a fresh registry."""

    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    registry.append(_question())
    original = path.read_bytes()
    assert original

    path.write_bytes(original[:-7])
    corrupted = path.read_bytes()

    with pytest.raises(ValueError):
        ScientificRegistry(path)
    with pytest.raises(ValueError):
        ScientificRegistry.initialize_pristine(path)

    assert path.read_bytes() == corrupted
