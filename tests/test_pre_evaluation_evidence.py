import json
import os
from pathlib import Path
import threading

import pytest

import autosport.pre_evaluation_evidence as pre_evaluation_evidence
from autosport.pre_evaluation_evidence import (
    CanonicalCandidateFacts,
    DecisionCode,
    PreEvaluationEvidenceAuthority,
    PreEvaluationEvidenceStore,
    PreEvaluationPolicy,
)


def facts(
    candidate_id: str,
    *,
    observed_at_ns: int = 900,
    config_enabled: bool = True,
    risk_required_micros: int = 10,
    risk_available_micros: int = 10,
    cost_estimate_micros: int = 4,
    cost_limit_micros: int = 4,
) -> CanonicalCandidateFacts:
    return CanonicalCandidateFacts(
        candidate_id=candidate_id,
        observed_at_ns=observed_at_ns,
        config_enabled=config_enabled,
        risk_required_micros=risk_required_micros,
        risk_available_micros=risk_available_micros,
        cost_estimate_micros=cost_estimate_micros,
        cost_limit_micros=cost_limit_micros,
        source_authority_id=f"canonical:{candidate_id}",
        source_revision="rev-1",
    )


def test_missing_canonical_state_safe_denies_and_zero_session_is_valid() -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    missing = authority.evaluate_session(
        session_id="s1",
        candidate_ids=["missing"],
        resolver=lambda _: None,
        evaluated_at_ns=1000,
    )
    slot = missing.slots[0]
    assert slot.decision_code is DecisionCode.SAFE_DENY_MISSING_STATE
    assert slot.dropped is True
    assert missing.dropped_candidate_ids == ("missing",)
    assert missing.eligible_count == 0

    empty = authority.evaluate_session(
        session_id="s1",
        candidate_ids=[],
        resolver=lambda _: None,
        evaluated_at_ns=1000,
    )
    assert empty.candidate_count == empty.eligible_count == empty.dropped_count == 0
    assert empty.dropped_candidate_ids == ()
    assert empty.total_risk_shortfall_micros == 0
    assert empty.total_cost_overage_micros == 0


def test_decision_and_all_veto_consequences_are_derived_from_canonical_facts() -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=50))
    canonical = facts(
        "c1",
        observed_at_ns=900,
        config_enabled=False,
        risk_required_micros=70,
        risk_available_micros=20,
        cost_estimate_micros=17,
        cost_limit_micros=5,
    )
    evidence = authority.evaluate_session(
        session_id="s1",
        candidate_ids=["c1"],
        resolver=lambda _: canonical,
        evaluated_at_ns=1000,
    )
    slot = evidence.slots[0]
    assert slot.decision_code is DecisionCode.CONFIG_VETO
    assert slot.config_veto is True
    assert slot.freshness_veto is True
    assert slot.risk_veto is True
    assert slot.cost_veto is True
    assert slot.risk_shortfall_micros == 50
    assert slot.cost_overage_micros == 12
    assert evidence.config_veto_count == 1
    assert evidence.freshness_veto_count == 1
    assert evidence.risk_veto_count == 1
    assert evidence.cost_veto_count == 1


def test_outcome_semantics_cannot_be_injected_as_canonical_facts() -> None:
    with pytest.raises(TypeError):
        CanonicalCandidateFacts(  # type: ignore[call-arg]
            candidate_id="c1",
            observed_at_ns=1,
            config_enabled=True,
            risk_required_micros=0,
            risk_available_micros=0,
            cost_estimate_micros=0,
            cost_limit_micros=0,
            source_authority_id="canonical:c1",
            source_revision="rev-1",
            decision_code="eligible",
        )

    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=5))
    with pytest.raises(TypeError):
        authority.evaluate_session(  # type: ignore[call-arg]
            session_id="s1",
            candidate_ids=["c1"],
            resolver=lambda _: facts("c1"),
            evaluated_at_ns=10,
            dropped_candidate_ids=(),
        )


def test_future_observation_fails_closed() -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=500))
    evidence = authority.evaluate_session(
        session_id="s1",
        candidate_ids=["future"],
        resolver=lambda _: facts("future", observed_at_ns=1001),
        evaluated_at_ns=1000,
    )
    slot = evidence.slots[0]
    assert slot.decision_code is DecisionCode.SAFE_DENY_INVALID_TIME
    assert slot.age_ns is None
    assert slot.dropped is True


def test_shard_composition_is_additive_and_empty_is_neutral() -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    by_id = {
        "a": facts("a"),
        "b": facts("b", risk_required_micros=20, risk_available_micros=5),
        "c": facts("c", cost_estimate_micros=9, cost_limit_micros=1),
    }

    left = authority.evaluate_session(
        session_id="s1",
        candidate_ids=["a", "b"],
        resolver=by_id.get,
        evaluated_at_ns=1000,
    )
    right = authority.evaluate_session(
        session_id="s1",
        candidate_ids=["c"],
        resolver=by_id.get,
        evaluated_at_ns=1000,
    )
    whole = authority.evaluate_session(
        session_id="s1",
        candidate_ids=["c", "a", "b"],
        resolver=by_id.get,
        evaluated_at_ns=1000,
    )
    empty = authority.evaluate_session(
        session_id="s1",
        candidate_ids=[],
        resolver=by_id.get,
        evaluated_at_ns=1000,
    )

    assert left.combine(right).to_payload() == whole.to_payload()
    assert whole.combine(empty).to_payload() == whole.to_payload()
    assert whole.candidate_count == 3
    assert whole.eligible_count == 1
    assert whole.dropped_count == 2
    assert whole.dropped_candidate_ids == ("b", "c")
    assert whole.total_risk_shortfall_micros == 15
    assert whole.total_cost_overage_micros == 8

    with pytest.raises(ValueError, match="duplicate candidate_id"):
        left.combine(left)


def test_store_concurrent_writers_do_not_share_fixed_temporary_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    evidence_a = authority.evaluate_session(
        session_id="s-a",
        candidate_ids=["a"],
        resolver=lambda candidate_id: facts(candidate_id),
        evaluated_at_ns=1000,
    )
    evidence_b = authority.evaluate_session(
        session_id="s-b",
        candidate_ids=["b"],
        resolver=lambda candidate_id: facts(candidate_id),
        evaluated_at_ns=1000,
    )
    store = PreEvaluationEvidenceStore(tmp_path / "evidence.json")
    fixed_temporary = store.path.with_name(f".{store.path.name}.tmp")
    barrier = threading.Barrier(2)
    original_open = Path.open

    class BarrierHandle:
        def __init__(self, handle) -> None:
            self._handle = handle

        def __enter__(self):
            entered = self._handle.__enter__()
            try:
                barrier.wait(timeout=5)
            except BaseException:
                self._handle.close()
                raise
            return entered

        def __exit__(self, exc_type, exc_value, traceback):
            return self._handle.__exit__(exc_type, exc_value, traceback)

    def controlled_open(path: Path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == fixed_temporary and mode == "wb":
            return BarrierHandle(handle)
        return handle

    monkeypatch.setattr(Path, "open", controlled_open)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def save(evidence) -> None:
        try:
            store.save(evidence)
        except BaseException as exc:
            with errors_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=save, args=(evidence_a,), daemon=True),
        threading.Thread(target=save, args=(evidence_b,), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert store.load() in (evidence_a, evidence_b)
    assert not fixed_temporary.exists()
    assert list(tmp_path.glob(f".{store.path.name}.*.tmp")) == []


def test_store_cleans_unique_temporary_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    evidence = authority.evaluate_session(
        session_id="s1",
        candidate_ids=["c1"],
        resolver=lambda candidate_id: facts(candidate_id),
        evaluated_at_ns=1000,
    )
    store = PreEvaluationEvidenceStore(tmp_path / "evidence.json")

    def fail_replace(source, destination) -> None:
        raise OSError("replace blocked")

    monkeypatch.setattr(pre_evaluation_evidence.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace blocked"):
        store.save(evidence)

    assert not store.path.exists()
    assert list(tmp_path.glob(f".{store.path.name}.*.tmp")) == []


def test_durable_restart_replays_exact_authority_and_rejects_semantic_tamper(
    tmp_path: Path,
) -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    canonical = facts("c1", risk_required_micros=30, risk_available_micros=20)
    evidence = authority.evaluate_session(
        session_id="s1",
        candidate_ids=["c1"],
        resolver=lambda _: canonical,
        evaluated_at_ns=1000,
    )
    store = PreEvaluationEvidenceStore(tmp_path / "evidence.json")
    store.save(evidence)

    loaded = store.load()
    assert loaded == evidence
    assert loaded.authority_id == evidence.authority_id
    assert loaded.authority_digest == evidence.authority_digest
    assert loaded.slots[0].evidence_digest == evidence.slots[0].evidence_digest

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["payload"]["slots"][0]["decision_code"] = "eligible"
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="semantic replay"):
        store.load()


def test_resolver_identity_mismatch_and_duplicate_request_fail_closed() -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=100))
    with pytest.raises(ValueError, match="different candidate"):
        authority.evaluate_session(
            session_id="s1",
            candidate_ids=["a"],
            resolver=lambda _: facts("b"),
            evaluated_at_ns=1000,
        )
    with pytest.raises(ValueError, match="duplicate candidate_id"):
        authority.evaluate_session(
            session_id="s1",
            candidate_ids=["a", "a"],
            resolver=lambda _: facts("a"),
            evaluated_at_ns=1000,
        )
