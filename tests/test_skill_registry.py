import hashlib
import json
import time
from dataclasses import replace

import pytest

from autosport.agent_loop import AgentLoopRuntime
from autosport.learning_environment import CausalLearningEnvironment, EnvironmentIdentity
from autosport.skill_registry import (
    AGENT_LOOP_READ_ONLY_AUTHORITY_PROFILE,
    ConflictingSkillDefinitionError,
    ConflictingSkillRunError,
    SkillDefinition,
    SkillExecutionResult,
    SkillImplementationKind,
    SkillPermissionError,
    SkillRecoveryRequiredError,
    SkillRegistry,
    SkillRegistryError,
    SkillRunStatus,
    builtin_skill_definitions,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _slow_handler(_payload):
    time.sleep(2)
    return SkillExecutionResult(output={})


def _undeclared_mutation_handler(_payload):
    return SkillExecutionResult(output={}, applied_mutations=("LOCAL_WRITE",))


def _registry(tmp_path):
    registry = SkillRegistry.initialize(tmp_path / "skills.json")
    registry.install_builtin_definitions()
    return registry


def _definition(capability):
    return next(x for x in builtin_skill_definitions() if x.capability == capability)


def _invoke(
    registry,
    definition,
    *,
    call_id="call-1",
    payload=None,
    authority_profile_id=AGENT_LOOP_READ_ONLY_AUTHORITY_PROFILE,
    mutations=(),
    at="2026-09-19T04:00:00Z",
    compute=1,
):
    return registry.invoke(
        skill_id=definition.skill_id,
        version=definition.version,
        capability=definition.capability,
        definition_id=definition.definition_id,
        call_id=call_id,
        caller_loop_id="loop-1",
        caller_state_sha256=SHA_A,
        source_sha256=SHA_B,
        input_payload={} if payload is None else payload,
        authority_profile_id=authority_profile_id,
        requested_mutations=mutations,
        provenance=(("source_evidence", SHA_C),),
        requested_compute_units=compute,
        requested_data_units=0,
        requested_ai_units=0,
        at=at,
    )


def test_builtin_registry_requires_exact_capability_and_definition(tmp_path):
    registry = _registry(tmp_path)
    definition = _definition("diagnose_provider_gap")
    assert registry.resolve(
        skill_id=definition.skill_id,
        version=definition.version,
        capability=definition.capability,
        definition_id=definition.definition_id,
    ) == definition
    with pytest.raises(SkillRegistryError, match="identity/capability"):
        registry.resolve(
            skill_id=definition.skill_id,
            version=definition.version,
            capability="free_form_guess",
            definition_id=definition.definition_id,
        )


def test_provider_gap_run_is_durable_and_idempotent(tmp_path):
    registry = _registry(tmp_path)
    definition = _definition("diagnose_provider_gap")
    payload = {
        "provider_id": "provider-a",
        "as_of": "2026-09-19T03:59:00Z",
        "expected_fields": ["price", "status"],
        "observed_fields": ["price"],
    }
    first = _invoke(registry, definition, payload=payload)
    assert first.status is SkillRunStatus.SUCCEEDED
    assert first.output == {
        "provider_id": "provider-a",
        "as_of": "2026-09-19T03:59:00Z",
        "status": "GAP",
        "missing_fields": ["status"],
        "unexpected_fields": [],
    }
    assert first.emitted_evidence[0][0] == "PROVIDER_GAP_DIAGNOSTIC"

    second = _invoke(
        SkillRegistry(registry.path),
        definition,
        payload=payload,
        at="2026-09-19T04:05:00Z",
    )
    assert second == first


def test_same_call_identity_cannot_change_input(tmp_path):
    registry = _registry(tmp_path)
    definition = _definition("diagnose_provider_gap")
    base = {
        "provider_id": "provider-a",
        "as_of": "2026-09-19T03:59:00Z",
        "expected_fields": ["price"],
        "observed_fields": ["price"],
    }
    _invoke(registry, definition, payload=base)
    changed = dict(base)
    changed["observed_fields"] = []
    with pytest.raises(ConflictingSkillRunError, match="immutable request"):
        _invoke(registry, definition, payload=changed, at="2026-09-19T04:05:00Z")


def test_source_owned_authority_profile_blocks_forged_authority_and_tool(tmp_path):
    registry = SkillRegistry.initialize(tmp_path / "skills.json")
    needs_authority = SkillDefinition(
        skill_id="needs-authority",
        version="1.0.0",
        capability="needs_authority",
        purpose="Prove caller strings cannot mint authority",
        input_schema_sha256=SHA_A,
        output_schema_sha256=SHA_B,
        implementation_sha256=SHA_C,
        implementation_kind=SkillImplementationKind.REVIEWED_PLUGIN,
        required_authorities=("FAKE_ADMIN",),
        required_provenance=("source_evidence",),
    )
    registry.register(needs_authority)
    denied = _invoke(registry, needs_authority, call_id="forged-authority", payload={})
    assert denied.status is SkillRunStatus.DENIED
    assert denied.error_code == "MISSING_REQUIRED_AUTHORITY"
    assert denied.available_authorities == ("READ_ONLY_ANALYSIS",)
    assert denied.available_tools == ()

    needs_tool = SkillDefinition(
        skill_id="needs-tool",
        version="1.0.0",
        capability="needs_tool",
        purpose="Prove caller strings cannot mint tool permission",
        input_schema_sha256=SHA_A,
        output_schema_sha256=SHA_B,
        implementation_sha256=SHA_D,
        implementation_kind=SkillImplementationKind.REVIEWED_PLUGIN,
        required_authorities=("READ_ONLY_ANALYSIS",),
        required_tools=("FAKE_WRITE_TOOL",),
        required_provenance=("source_evidence",),
    )
    registry.register(needs_tool)
    denied_tool = _invoke(registry, needs_tool, call_id="forged-tool", payload={})
    assert denied_tool.status is SkillRunStatus.DENIED
    assert denied_tool.error_code == "MISSING_REQUIRED_TOOL"

    with pytest.raises(SkillPermissionError, match="unknown source-owned authority"):
        _invoke(
            registry,
            needs_authority,
            call_id="unknown-profile",
            payload={},
            authority_profile_id="caller-invented-admin",
        )


def test_protected_mutation_is_denied(tmp_path):
    registry = _registry(tmp_path)
    definition = _definition("diagnose_provider_gap")
    protected = _invoke(
        registry,
        definition,
        call_id="provider-write",
        payload={
            "provider_id": "provider-a",
            "as_of": "2026-09-19T03:59:00Z",
            "expected_fields": [],
            "observed_fields": [],
        },
        mutations=("PROVIDER_WRITE",),
    )
    assert protected.status is SkillRunStatus.DENIED
    assert protected.error_code == "NON_DELEGABLE_MUTATION"


def test_budget_is_fail_closed(tmp_path):
    registry = _registry(tmp_path)
    definition = _definition("inspect_drift")
    denied = _invoke(
        registry,
        definition,
        call_id="budget",
        payload={"scope": "model-a", "findings": []},
        compute=2,
    )
    assert denied.status is SkillRunStatus.DENIED
    assert denied.error_code == "COMPUTE_BUDGET_EXCEEDED"


def test_drift_skill_never_claims_global_stability(tmp_path):
    registry = _registry(tmp_path)
    definition = _definition("inspect_drift")
    run = _invoke(
        registry,
        definition,
        payload={
            "scope": "model-a",
            "findings": [
                {"metric": "mean", "triggered": False, "evidence_sha256": SHA_D},
                {"metric": "variance", "triggered": True, "evidence_sha256": SHA_C},
            ],
        },
    )
    assert run.status is SkillRunStatus.SUCCEEDED
    assert run.output["truth"] == "DIAGNOSTIC_ONLY_NOT_GLOBAL_MODEL_STABILITY"
    assert run.output["triggered_metrics"] == ["variance"]


def test_postmortem_emits_candidate_not_scientific_authority(tmp_path):
    registry = _registry(tmp_path)
    definition = _definition("produce_postmortem")
    run = _invoke(
        registry,
        definition,
        payload={
            "subject_id": "episode-17",
            "unresolved_codes": ["CALIBRATION"],
            "evidence_sha256": [SHA_D],
            "research_question_candidate": "Does calibration degrade after a league transition?",
        },
    )
    assert run.status is SkillRunStatus.SUCCEEDED
    assert (
        run.output["research_question_authority"]
        == "CANDIDATE_ONLY_CANONICAL_HANDOFF_REQUIRED"
    )
    assert (
        run.research_question_candidate
        == "Does calibration degrade after a league transition?"
    )
    assert run.research_question_candidate_sha256 is not None
    assert run.applied_mutations == ()


def test_dynamic_code_candidate_can_be_registered_but_never_executed(tmp_path):
    registry = SkillRegistry.initialize(tmp_path / "skills.json")
    definition = SkillDefinition(
        skill_id="generated-candidate",
        version="0.1.0-candidate",
        capability="candidate_analysis",
        purpose="Review-only generated candidate",
        input_schema_sha256=SHA_A,
        output_schema_sha256=SHA_B,
        implementation_sha256=SHA_C,
        implementation_kind=SkillImplementationKind.CANDIDATE_DYNAMIC_CODE,
        required_authorities=("READ_ONLY_ANALYSIS",),
        required_provenance=("source_evidence",),
    )
    registry.register(definition)
    with pytest.raises(SkillPermissionError, match="not executable"):
        _invoke(registry, definition, payload={})
    with pytest.raises(SkillPermissionError, match="runtime handler binding"):
        registry.bind_handler(
            definition, lambda payload: SkillExecutionResult(output={})
        )


def test_executable_builtin_definition_cannot_be_pre_registered_with_weaker_policy(tmp_path):
    registry = SkillRegistry.initialize(tmp_path / "skills.json")
    canonical = _definition("diagnose_provider_gap")
    forged = replace(
        canonical,
        required_authorities=(),
        required_provenance=(),
        compute_budget_units=99,
    )
    with pytest.raises(
        ConflictingSkillDefinitionError,
        match="exactly match source-owned contract",
    ):
        registry.register(forged)


def test_handler_timeout_terminates_and_persists_terminal_failure(tmp_path):
    registry = SkillRegistry.initialize(tmp_path / "skills.json")
    definition = SkillDefinition(
        skill_id="timeout-probe",
        version="1.0.0",
        capability="timeout_probe",
        purpose="Adversarial bounded-execution test",
        input_schema_sha256=SHA_A,
        output_schema_sha256=SHA_B,
        implementation_sha256=SHA_C,
        implementation_kind=SkillImplementationKind.REVIEWED_PLUGIN,
        required_authorities=("READ_ONLY_ANALYSIS",),
        required_provenance=("source_evidence",),
        timeout_seconds=1,
    )
    registry.register(definition)
    registry._handlers[definition.version_key] = _slow_handler
    started = time.monotonic()
    run = _invoke(registry, definition, payload={})
    elapsed = time.monotonic() - started
    assert run.status is SkillRunStatus.FAILED
    assert run.error_code == "HANDLER_TIMEOUT"
    assert run.completed_at is not None
    assert elapsed < 2


def test_definition_cannot_delegate_real_money_or_promotion():
    with pytest.raises(SkillRegistryError, match="protected mutation"):
        SkillDefinition(
            skill_id="unsafe",
            version="1.0.0",
            capability="unsafe",
            purpose="unsafe",
            input_schema_sha256=SHA_A,
            output_schema_sha256=SHA_B,
            implementation_sha256=SHA_C,
            implementation_kind=SkillImplementationKind.REVIEWED_PLUGIN,
            allowed_mutations=(
                "PROMOTION_DECISION",
                "REAL_MONEY_EXECUTION_ENABLE",
            ),
        )


def test_restart_marks_inflight_run_interrupted_and_forbids_blind_replay(tmp_path):
    registry = SkillRegistry.initialize(tmp_path / "skills.json")
    definition = SkillDefinition(
        skill_id="crash-probe",
        version="1.0.0",
        capability="crash_probe",
        purpose="Test crash-safe run evidence",
        input_schema_sha256=SHA_A,
        output_schema_sha256=SHA_B,
        implementation_sha256=SHA_C,
        implementation_kind=SkillImplementationKind.REVIEWED_PLUGIN,
        required_authorities=("READ_ONLY_ANALYSIS",),
        required_provenance=("source_evidence",),
    )
    registry.register(definition)
    registry._handlers[definition.version_key] = _undeclared_mutation_handler

    def crash_after_running_is_durable(_handler, _payload, _timeout):
        raise SystemExit(7)

    registry._execute_handler_bounded = crash_after_running_is_durable
    with pytest.raises(SystemExit):
        _invoke(registry, definition, payload={})

    restarted = SkillRegistry(registry.path)
    running = json.loads(registry.path.read_text(encoding="utf-8"))["runs"][0][
        "run_id"
    ]
    assert restarted.get_run(running).status is SkillRunStatus.RUNNING
    assert restarted.recover_incomplete(at="2026-09-19T04:10:00Z") == (running,)
    assert restarted.get_run(running).status is SkillRunStatus.INTERRUPTED
    with pytest.raises(SkillRecoveryRequiredError, match="blind replay"):
        _invoke(
            restarted,
            definition,
            payload={},
            at="2026-09-19T04:11:00Z",
        )


def test_handler_cannot_report_undeclared_mutation(tmp_path):
    registry = SkillRegistry.initialize(tmp_path / "skills.json")
    definition = SkillDefinition(
        skill_id="read-only",
        version="1.0.0",
        capability="read_only",
        purpose="Read-only test",
        input_schema_sha256=SHA_A,
        output_schema_sha256=SHA_B,
        implementation_sha256=SHA_C,
        implementation_kind=SkillImplementationKind.REVIEWED_PLUGIN,
        required_authorities=("READ_ONLY_ANALYSIS",),
        required_provenance=("source_evidence",),
    )
    registry.register(definition)
    registry._handlers[definition.version_key] = _undeclared_mutation_handler
    run = _invoke(registry, definition, payload={})
    assert run.status is SkillRunStatus.FAILED
    assert run.error_code == "HANDLER_ERROR_SKILLPERMISSIONERROR"


def test_state_digest_and_internal_run_digests_are_verified(tmp_path):
    registry = _registry(tmp_path)
    definition = _definition("diagnose_provider_gap")
    run = _invoke(
        registry,
        definition,
        payload={
            "provider_id": "provider-a",
            "as_of": "2026-09-19T03:59:00Z",
            "expected_fields": [],
            "observed_fields": [],
        },
    )
    state = json.loads(registry.path.read_text(encoding="utf-8"))
    state["runs"][0]["output"]["provider_id"] = "forged"
    body = {key: value for key, value in state.items() if key != "state_sha256"}
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    state["state_sha256"] = hashlib.sha256(canonical).hexdigest()
    registry.path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(
        SkillRegistryError, match="successful run output digest mismatch"
    ):
        SkillRegistry(registry.path)
    assert run.status is SkillRunStatus.SUCCEEDED


def test_agent_loop_skill_invocation_binds_exact_loop_snapshot(tmp_path):
    identity = EnvironmentIdentity(
        source_id="paper-source-v1",
        config_id="skill-registry-test-v1",
        data_id="causal-dataset-v1",
        protocol_id="agent-loop-protocol-v1",
        cutoff_ts="2026-09-19T05:00:00Z",
        seed=17,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="skill-registry-episode",
        policy_id="policy-v1",
        admissible_actions=frozenset({"WAIT"}),
    )
    runtime = AgentLoopRuntime.initialize_pristine(
        tmp_path / "agent-loop.json",
        loop_id="skill-loop-1",
        environment_checkpoint=environment.checkpoint(),
        policy_id="policy-v1",
        economic_goal_fingerprint=SHA_C,
        risk_fingerprint=SHA_D,
        source_sha256=SHA_B,
        config_sha256=SHA_A,
        at="2026-09-19T04:00:00Z",
    )
    registry = _registry(tmp_path)
    definition = _definition("diagnose_provider_gap")
    before = runtime.snapshot()
    run = runtime.invoke_skill(
        registry,
        skill_id=definition.skill_id,
        version=definition.version,
        capability=definition.capability,
        definition_id=definition.definition_id,
        call_id="loop-skill-1",
        input_payload={
            "provider_id": "provider-a",
            "as_of": "2026-09-19T04:00:00Z",
            "expected_fields": ["price"],
            "observed_fields": ["price"],
        },
        provenance=(("source_evidence", SHA_C),),
        at="2026-09-19T04:01:00Z",
    )
    assert run.status is SkillRunStatus.SUCCEEDED
    assert run.caller_loop_id == before.loop_id
    assert run.caller_state_sha256 == before.state_sha256
    assert run.source_sha256 == before.source_sha256
    assert runtime.snapshot().state_sha256 == before.state_sha256
