import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock

import autosport.pre_evaluation_evidence as evidence_module

import pytest

from autosport.pre_evaluation_evidence import (
    CanonicalCandidateFacts,
    DecisionCode,
    PreEvaluationEvidenceAuthority,
    PreEvaluationEvidenceStore,
    PreEvaluationPolicy,
    PreEvaluationSessionEvidence,
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


def test_parallel_writers_use_unique_temporary_files_and_complete_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce the shared .tmp race from predecessor PR #2207 on current main."""
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    evidence = [
        authority.evaluate_session(
            session_id=session_id,
            candidate_ids=[candidate_id],
            resolver=lambda name: facts(name),
            evaluated_at_ns=1000,
        )
        for session_id, candidate_id in (("s-a", "a"), ("s-b", "b"))
    ]
    store = PreEvaluationEvidenceStore(tmp_path / "evidence.json")
    original_replace = evidence_module.os.replace
    rendezvous = Barrier(2)
    seen: list[Path] = []
    lock = Lock()

    def concurrent_replace(source, destination):
        with lock:
            seen.append(Path(source))
        rendezvous.wait(timeout=5)
        return original_replace(source, destination)

    monkeypatch.setattr(evidence_module.os, "replace", concurrent_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(store.save, evidence))

    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(source.parent == tmp_path for source in seen)
    assert store.load() in evidence
    assert not list(tmp_path.glob(".evidence.json.*.tmp"))
    assert not (tmp_path / ".evidence.json.tmp").exists()



def test_windows_transient_replace_sharing_violation_recovers_without_temp_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    evidence = authority.evaluate_session(
        session_id="s-windows", candidate_ids=["c1"],
        resolver=lambda name: facts(name), evaluated_at_ns=1000,
    )
    store = PreEvaluationEvidenceStore(tmp_path / "evidence.json")
    native_replace = evidence_module.os.replace
    calls = []

    def sharing_then_success(source, destination):
        calls.append(Path(source))
        if len(calls) < 3:
            error = PermissionError("simulated Windows sharing violation")
            error.winerror = 32
            raise error
        return native_replace(source, destination)

    monkeypatch.setattr(evidence_module.os, "replace", sharing_then_success)
    store.save(evidence)
    assert len(calls) == 3
    assert calls[0] == calls[1] == calls[2]
    assert store.load() == evidence
    assert not list(tmp_path.glob(".evidence.json.*.tmp"))


def test_windows_persistent_replace_denial_is_bounded_and_keeps_previous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    original = authority.evaluate_session(
        session_id="existing", candidate_ids=["a"],
        resolver=lambda name: facts(name), evaluated_at_ns=1000,
    )
    replacement = authority.evaluate_session(
        session_id="candidate", candidate_ids=["b"],
        resolver=lambda name: facts(name), evaluated_at_ns=1000,
    )
    store = PreEvaluationEvidenceStore(tmp_path / "evidence.json")
    store.save(original)
    calls = []

    def permanently_denied(source, destination):
        calls.append(Path(source))
        error = PermissionError("persistent Windows access denial")
        error.winerror = 5
        raise error

    monkeypatch.setattr(evidence_module.os, "replace", permanently_denied)
    with pytest.raises(PermissionError, match="persistent Windows access denial"):
        store.save(replacement)
    assert len(calls) == 4
    assert store.load() == original
    assert not list(tmp_path.glob(".evidence.json.*.tmp"))



def test_failed_atomic_replace_preserves_prior_evidence_and_cleans_only_own_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    original = authority.evaluate_session(
        session_id="last-good", candidate_ids=["a"],
        resolver=lambda name: facts(name), evaluated_at_ns=1000,
    )
    replacement = authority.evaluate_session(
        session_id="new", candidate_ids=["b"],
        resolver=lambda name: facts(name), evaluated_at_ns=1000,
    )
    store = PreEvaluationEvidenceStore(tmp_path / "evidence.json")
    store.save(original)

    def fail_replace(_source, _destination):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(evidence_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated publication failure"):
        store.save(replacement)

    assert store.load() == original
    assert not list(tmp_path.glob(".evidence.json.*.tmp"))
    assert not (tmp_path / ".evidence.json.tmp").exists()


@pytest.mark.parametrize(
    ("location", "replacement"),
    (
        (("schema_version",), True),
        (("payload", "schema_version"), True),
        (("payload", "slots", 0, "eligible_for_evaluation"), 1),
        (("payload", "summary", "candidate_count"), True),
    ),
)
def test_store_rejects_bool_int_semantic_aliases(
    tmp_path: Path, location: tuple[object, ...], replacement: object,
) -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    evidence = authority.evaluate_session(
        session_id="s1", candidate_ids=["c1"],
        resolver=lambda name: facts(name), evaluated_at_ns=1000,
    )
    store = PreEvaluationEvidenceStore(tmp_path / "evidence.json")
    store.save(evidence)
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    target = raw
    for key in location[:-1]:
        target = target[key]
    target[location[-1]] = replacement
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        store.load()


def test_store_rejects_duplicate_json_keys_at_top_and_nested_depth(
    tmp_path: Path,
) -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    evidence = authority.evaluate_session(
        session_id="s1", candidate_ids=["c1"],
        resolver=lambda name: facts(name), evaluated_at_ns=1000,
    )
    store = PreEvaluationEvidenceStore(tmp_path / "evidence.json")
    store.save(evidence)
    canonical = store.path.read_text(encoding="utf-8")
    store.path.write_text('{"schema_version":1,' + canonical[1:], encoding="utf-8")
    with pytest.raises(ValueError, match="invalid pre-evaluation evidence file"):
        store.load()

    store.save(evidence)
    canonical = store.path.read_text(encoding="utf-8")
    key = '"candidate_id":"c1"'
    assert key in canonical
    store.path.write_text(
        canonical.replace(key, key + ',"candidate_id":"c1"', 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid pre-evaluation evidence file"):
        store.load()


def test_mutated_session_cannot_overwrite_last_good_evidence(tmp_path: Path) -> None:
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    last_good = authority.evaluate_session(
        session_id="last-good", candidate_ids=["a"],
        resolver=lambda name: facts(name), evaluated_at_ns=1000,
    )
    candidate = authority.evaluate_session(
        session_id="candidate", candidate_ids=["b"],
        resolver=lambda name: facts(name), evaluated_at_ns=1000,
    )
    store = PreEvaluationEvidenceStore(tmp_path / "evidence.json")
    store.save(last_good)
    object.__setattr__(candidate, "session_id", "mutated-after-validation")
    with pytest.raises(ValueError, match="slot session_id mismatch"):
        store.save(candidate)
    assert store.load() == last_good


def test_store_fences_subclassed_session_before_publication(tmp_path: Path) -> None:
    class Derived(PreEvaluationSessionEvidence):
        pass

    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    canonical = authority.evaluate_session(
        session_id="s1", candidate_ids=["c1"],
        resolver=lambda name: facts(name), evaluated_at_ns=1000,
    )
    derived = Derived(
        session_id=canonical.session_id,
        evaluated_at_ns=canonical.evaluated_at_ns,
        policy_max_age_ns=canonical.policy_max_age_ns,
        policy_digest=canonical.policy_digest,
        slots=canonical.slots,
    )
    store = PreEvaluationEvidenceStore(tmp_path / "evidence.json")
    with pytest.raises(TypeError, match="exact PreEvaluationSessionEvidence"):
        store.save(derived)
    assert not store.path.exists()
