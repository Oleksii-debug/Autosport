from __future__ import annotations

import hashlib
import json

import pytest

from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
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



def _record_digest(entry: dict[str, object]) -> str:
    envelope = {
        "record_type": entry["record_type"],
        "record_id": entry["record_id"],
        "available_at": entry["available_at"],
        "payload": entry["payload"],
    }
    raw = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def test_reordering_individually_valid_records_is_rejected_by_monotonic_authority(
    tmp_path,
):
    """Per-record-valid reorder must not invert ScientificRegistry causal authority."""

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
    corrupted = path.read_bytes()

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="rolled back|unproven|authority",
    ):
        ScientificRegistry(path)
    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="rolled back|unproven|authority",
    ):
        ScientificRegistry.initialize_pristine(path)

    assert path.read_bytes() == corrupted


def test_deleting_committed_predecessor_is_rejected_by_monotonic_authority(tmp_path):
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
    corrupted = path.read_bytes()

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="rolled back|unproven|authority",
    ):
        ScientificRegistry(path)
    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="rolled back|unproven|authority",
    ):
        ScientificRegistry.initialize_pristine(path)

    assert path.read_bytes() == corrupted


def test_truncated_registry_cannot_be_pristine_reinitialized_over_history(tmp_path):
    """Corrupt durable history must fail closed instead of becoming a fresh registry."""

    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    registry.append(_question())
    original = path.read_bytes()
    assert original

    path.write_bytes(original[:-7])
    corrupted = path.read_bytes()

    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        ScientificRegistry(path)
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        ScientificRegistry.initialize_pristine(path)

    assert path.read_bytes() == corrupted


def test_payload_byte_tamper_with_stale_record_digest_fails_locally(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    registry.append(_question())

    state = json.loads(path.read_text(encoding="utf-8"))
    state["records"][0]["payload"]["statement"] = "Tampered question"
    stale_digest = state["records"][0]["record_sha256"]
    _rewrite_state(path, state)
    corrupted = path.read_bytes()

    assert _record_digest(state["records"][0]) != stale_digest
    for _attempt in range(2):
        with pytest.raises(
            ValueError,
            match="record digest mismatch",
        ):
            ScientificRegistry(path)
        assert path.read_bytes() == corrupted


def test_recomputed_record_digest_cannot_mint_tampered_scientific_authority(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    registry.append(_question())

    state = json.loads(path.read_text(encoding="utf-8"))
    entry = state["records"][0]
    original_digest = entry["record_sha256"]
    entry["payload"]["statement"] = "Tampered but locally rehashed question"
    entry["record_sha256"] = _record_digest(entry)
    assert entry["record_sha256"] != original_digest
    _rewrite_state(path, state)
    corrupted = path.read_bytes()

    messages: list[str] = []
    for _attempt in range(2):
        with pytest.raises(MonotonicAuthorityRollbackError) as caught:
            ScientificRegistry(path)
        messages.append(str(caught.value))
        assert path.read_bytes() == corrupted

    assert messages[0] == messages[1]
    with pytest.raises(MonotonicAuthorityRollbackError):
        ScientificRegistry.initialize_pristine(path)
    assert path.read_bytes() == corrupted


def test_exact_duplicate_durable_record_never_doubles_scientific_effect(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    registry.append(_question())

    state = json.loads(path.read_text(encoding="utf-8"))
    state["records"].append(dict(state["records"][0]))
    _rewrite_state(path, state)
    corrupted = path.read_bytes()

    for _attempt in range(2):
        with pytest.raises(
            ValueError,
            match="duplicate record identity",
        ):
            ScientificRegistry(path)
        assert path.read_bytes() == corrupted

    with pytest.raises(
        ValueError,
        match="duplicate record identity",
    ):
        ScientificRegistry.initialize_pristine(path)
    assert path.read_bytes() == corrupted


def test_rehashed_outer_identity_relabel_cannot_mint_scientific_authority(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    registry.append(_question())

    state = json.loads(path.read_text(encoding="utf-8"))
    entry = state["records"][0]
    assert entry["payload"]["question_id"] == "question-1"
    entry["record_id"] = "question-forged"
    entry["record_sha256"] = _record_digest(entry)
    _rewrite_state(path, state)
    corrupted = path.read_bytes()

    for _attempt in range(2):
        with pytest.raises(MonotonicAuthorityRollbackError):
            ScientificRegistry(path)
        assert path.read_bytes() == corrupted

    with pytest.raises(MonotonicAuthorityRollbackError):
        ScientificRegistry.initialize_pristine(path)
    assert path.read_bytes() == corrupted


def test_semantically_invalid_available_at_is_rejected_before_authority_recovery(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    registry.append(_question())

    state = json.loads(path.read_text(encoding="utf-8"))
    entry = state["records"][0]
    entry["available_at"] = 123
    entry["record_sha256"] = _record_digest(entry)
    _rewrite_state(path, state)
    corrupted = path.read_bytes()

    for _attempt in range(2):
        with pytest.raises(
            ValueError,
            match="available_at must be a non-empty canonical string",
        ):
            ScientificRegistry(path)
        assert path.read_bytes() == corrupted


def test_valid_registry_transplant_into_fresh_workspace_cannot_bootstrap_authority(
    tmp_path,
):
    source_path = tmp_path / "source-workspace" / "scientific_registry.json"
    source = ScientificRegistry.initialize_pristine(source_path)
    source.append(_question())
    transplanted_bytes = source_path.read_bytes()

    target_path = tmp_path / "fresh-workspace" / "scientific_registry.json"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(transplanted_bytes)

    for _attempt in range(2):
        with pytest.raises(
            MonotonicAuthorityRollbackError,
            match="authority|history|rollback|unproven",
        ):
            ScientificRegistry(target_path)
        assert target_path.read_bytes() == transplanted_bytes

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="authority|history|rollback|unproven",
    ):
        ScientificRegistry.initialize_pristine(target_path)
    assert target_path.read_bytes() == transplanted_bytes
