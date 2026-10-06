import hashlib
import json
import time
from dataclasses import replace

import pytest

import autosport.skill_registry as skill_registry_module

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


def _large_result_handler(_payload):
    return SkillExecutionResult(output={"payload": "x" * (1024 * 1024)})


def _partial_spool_stall_handler(_payload):
    def stalled_write_text(self, data, *args, **kwargs):
        with self.open("w", encoding="utf-8") as handle:
            handle.write(data[:1])
            handle.flush()
        time.sleep(60)
        return len(data)

    # Child-local mutation only. The real bounded transport captures the Path
    # class but serialization/file publication still happens inside the killable
    # child, so a partial write must remain covered by the handler deadline.
    skill_registry_module.Path.write_text = stalled_write_text
    return SkillExecutionResult(output={"payload": "never-authoritative"})


def _undeclared_mutation_handler(_payload):
    return SkillExecutionResult(output={}, applied_mutations=("LOCAL_WRITE",))


def _undeclared_tool_and_evidence_handler(_payload):
    return SkillExecutionResult(
        output={},
        used_tools=("FAKE_WRITE_TOOL",),
        emitted_evidence=(("UNDECLARED_EVIDENCE", SHA_D),),
    )


def _budget_overrun_handler(payload):
    kind = payload["kind"]
    return SkillExecutionResult(
        output={"kind": kind},
        consumed_compute_units=2 if kind == "compute" else 0,
        consumed_data_units=2 if kind == "data" else 0,
        consumed_ai_units=2 if kind == "ai" else 0,
    )


def _consume_two_compute(_payload):
    return SkillExecutionResult(output={}, consumed_compute_units=2)


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
    data=0,
    ai=0,
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
        requested_data_units=data,
        requested_ai_units=ai,
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


@pytest.mark.parametrize("kind", ("compute", "data", "ai"))
def test_handler_cannot_exceed_immutable_call_budget(tmp_path, kind):
    registry = SkillRegistry.initialize(tmp_path / "skills.json")
    definition = SkillDefinition(
        skill_id="budget-overrun",
        version="1.0.0",
        capability="budget_overrun",
        purpose="Adversarial per-call budget test",
        input_schema_sha256=SHA_A,
        output_schema_sha256=SHA_B,
        implementation_sha256=SHA_C,
        implementation_kind=SkillImplementationKind.REVIEWED_PLUGIN,
        required_authorities=("READ_ONLY_ANALYSIS",),
        required_provenance=("source_evidence",),
        compute_budget_units=2,
        data_budget_units=2,
        ai_budget_units=2,
    )
    registry.register(definition)
    registry._handlers[definition.version_key] = _budget_overrun_handler
    run = _invoke(
        registry,
        definition,
        call_id=f"budget-overrun-{kind}",
        payload={"kind": kind},
        compute=1,
        data=1,
        ai=1,
    )
    assert run.status is SkillRunStatus.FAILED
    assert run.error_code == "HANDLER_ERROR_SKILLPERMISSIONERROR"
    expected = {
        "compute": (2, 0, 0),
        "data": (0, 2, 0),
        "ai": (0, 0, 2),
    }[kind]
    assert (
        run.consumed_compute_units,
        run.consumed_data_units,
        run.consumed_ai_units,
    ) == expected
    assert SkillRegistry(registry.path).get_run(run.run_id) == run


def test_durable_readback_rejects_consumption_above_requested_budget(tmp_path):
    registry = SkillRegistry.initialize(tmp_path / "skills.json")
    definition = SkillDefinition(
        skill_id="budget-readback",
        version="1.0.0",
        capability="budget_readback",
        purpose="Prove requested budget is durable authority",
        input_schema_sha256=SHA_A,
        output_schema_sha256=SHA_B,
        implementation_sha256=SHA_D,
        implementation_kind=SkillImplementationKind.REVIEWED_PLUGIN,
        required_authorities=("READ_ONLY_ANALYSIS",),
        required_provenance=("source_evidence",),
        compute_budget_units=2,
    )
    registry.register(definition)
    registry._handlers[definition.version_key] = _consume_two_compute
    run = _invoke(registry, definition, payload={}, compute=2)
    assert run.status is SkillRunStatus.SUCCEEDED

    state = json.loads(registry.path.read_text(encoding="utf-8"))
    state["runs"][0]["requested_compute_units"] = 1
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
    with pytest.raises(SkillRegistryError, match="consumed budget exceeds"):
        SkillRegistry(registry.path)


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


def test_handler_result_pipe_is_drained_before_process_exit_wait(monkeypatch):
    expected = SkillExecutionResult(output={"payload": "x" * 4096})
    events = []

    class FakeReceiver:
        def __init__(self):
            self.ready = False
            self.drained = False
            self.closed = False

        def poll(self):
            events.append("poll")
            return self.ready

        def recv(self):
            events.append("recv")
            self.drained = True
            process.alive = False
            return "OK", expected

        def close(self):
            self.closed = True

    class FakeSender:
        def close(self):
            return None

    receiver = FakeReceiver()

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.killed = False
            self.closed = False
            self.join_timeouts = []

        def start(self):
            receiver.ready = True

        def join(self, timeout=None):
            self.join_timeouts.append(timeout)
            events.append("join")
            # Simulate a child whose handler has finished but whose result send
            # fills the OS pipe. It cannot exit until the parent drains recv().
            if receiver.drained:
                self.alive = False

        def is_alive(self):
            return self.alive

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def close(self):
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, FakeSender()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert error is None
    assert result == expected
    assert process.killed is False
    assert process.closed is True
    assert receiver.closed is True
    assert "recv" in events
    assert "join" not in events[: events.index("recv")]


def test_handler_large_result_crosses_transport_without_false_timeout():
    result, error = SkillRegistry._execute_handler_bounded(
        _large_result_handler,
        {},
        10,
    )

    assert error is None
    assert result is not None
    assert len(result.output["payload"]) == 1024 * 1024


def test_handler_oversized_result_is_bounded_before_spool_publication(
    tmp_path, monkeypatch
):
    spool_path = tmp_path / "handler-result.json"
    max_bytes = 256
    monkeypatch.setattr(
        skill_registry_module,
        "_HANDLER_RESULT_SPOOL_MAX_BYTES",
        max_bytes,
    )

    real_write_text = skill_registry_module.Path.write_text
    published_sizes = []

    def bounded_write_text(self, data, *args, **kwargs):
        encoded_size = len(data.encode("utf-8"))
        published_sizes.append(encoded_size)
        assert encoded_size <= max_bytes
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(
        skill_registry_module.Path,
        "write_text",
        bounded_write_text,
    )

    def oversized_handler(_payload):
        return SkillExecutionResult(output={"payload": "x" * 4096})

    skill_registry_module._skill_handler_spooled_process(
        oversized_handler,
        {},
        str(spool_path),
    )

    result, error = skill_registry_module._decode_handler_result_spool(spool_path)
    assert result is None
    assert error == "HANDLER_RESULT_TOO_LARGE"
    assert published_sizes
    assert spool_path.stat().st_size <= max_bytes


def test_handler_oversized_error_record_is_bounded_before_spool_publication(
    tmp_path, monkeypatch
):
    spool_path = tmp_path / "handler-error.json"
    max_bytes = 256
    monkeypatch.setattr(
        skill_registry_module,
        "_HANDLER_RESULT_SPOOL_MAX_BYTES",
        max_bytes,
    )

    real_write_text = skill_registry_module.Path.write_text
    published_sizes = []

    def bounded_write_text(self, data, *args, **kwargs):
        encoded_size = len(data.encode("utf-8"))
        published_sizes.append(encoded_size)
        assert encoded_size <= max_bytes
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(
        skill_registry_module.Path,
        "write_text",
        bounded_write_text,
    )

    def failing_handler(_payload):
        raise RuntimeError("simulated handler failure")

    def oversized_error_canonicalizer(record):
        if record.get("kind") == "ERROR":
            return "x" * 4096
        return json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    skill_registry_module._skill_handler_spooled_process(
        failing_handler,
        {},
        str(spool_path),
        _canonicalize=oversized_error_canonicalizer,
    )

    result, error = skill_registry_module._decode_handler_result_spool(spool_path)
    assert result is None
    assert error == "HANDLER_RESULT_TOO_LARGE"
    assert published_sizes
    assert spool_path.stat().st_size <= max_bytes


def test_handler_partial_result_spool_write_remains_timeout_bounded():
    started = time.monotonic()
    result, error = SkillRegistry._execute_handler_bounded(
        _partial_spool_stall_handler,
        {},
        1,
    )
    elapsed = time.monotonic() - started

    assert result is None
    assert error == "HANDLER_TIMEOUT"
    assert elapsed < 3


def test_handler_spool_decode_baseexception_cleans_spool_before_reraise(
    tmp_path, monkeypatch
):
    spool_path = tmp_path / "handler-result.json"

    class FakeProcess:
        def __init__(self):
            self.closed = False

        def start(self):
            return None

        def join(self, timeout=None):
            assert timeout == 1

        def is_alive(self):
            return False

        def close(self):
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_spooled_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert args[2] == str(spool_path)
            assert daemon is True
            return process

    def fake_mkstemp(*, prefix, suffix):
        assert prefix == "autosport-skill-result-"
        assert suffix == ".json"
        descriptor = skill_registry_module.os.open(
            spool_path,
            skill_registry_module.os.O_CREAT
            | skill_registry_module.os.O_RDWR,
        )
        return descriptor, str(spool_path)

    monkeypatch.setattr(skill_registry_module.tempfile, "mkstemp", fake_mkstemp)

    def interrupt_decode(_path):
        raise KeyboardInterrupt("simulated parent decode interruption")

    monkeypatch.setattr(
        skill_registry_module,
        "_decode_handler_result_spool",
        interrupt_decode,
    )

    with pytest.raises(KeyboardInterrupt, match="simulated parent decode interruption"):
        skill_registry_module._execute_handler_spooled_bounded(
            FakeContext(),
            _slow_handler,
            {},
            1,
        )

    assert process.closed is True
    assert not spool_path.exists()


@pytest.mark.parametrize(
    ("stop_mode", "expected_error"),
    (
        ("stop_failed", "HANDLER_TIMEOUT_STOP_FAILED"),
        ("handle_close_failed", "HANDLER_TIMEOUT_HANDLE_CLOSE_FAILED"),
    ),
)
def test_handler_spool_cleanup_failure_does_not_mask_process_stop_truth(
    tmp_path, monkeypatch, stop_mode, expected_error
):
    spool_path = tmp_path / "handler-result.json"

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.closed = False

        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.alive

        def kill(self):
            if stop_mode == "stop_failed":
                raise OSError("simulated kill failure")
            self.alive = False

        def terminate(self):
            if stop_mode == "stop_failed":
                raise OSError("simulated terminate failure")
            self.alive = False

        def close(self):
            if stop_mode == "handle_close_failed":
                raise OSError("simulated process handle close failure")
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_spooled_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert args[2] == str(spool_path)
            assert daemon is True
            return process

    def fake_mkstemp(*, prefix, suffix):
        assert prefix == "autosport-skill-result-"
        assert suffix == ".json"
        descriptor = skill_registry_module.os.open(
            spool_path,
            skill_registry_module.os.O_CREAT
            | skill_registry_module.os.O_RDWR,
        )
        return descriptor, str(spool_path)

    monkeypatch.setattr(skill_registry_module.tempfile, "mkstemp", fake_mkstemp)
    cleanup_attempts = []

    def fail_spool_cleanup(path, *, suppress_base_exceptions=False):
        assert suppress_base_exceptions is False
        cleanup_attempts.append(path)
        return False

    monkeypatch.setattr(
        skill_registry_module,
        "_remove_handler_result_spool",
        fail_spool_cleanup,
    )

    try:
        result, error = skill_registry_module._execute_handler_spooled_bounded(
            FakeContext(),
            _slow_handler,
            {},
            1,
        )

        assert result is None
        assert error == expected_error
        if stop_mode == "stop_failed":
            # A possibly-live child still owns the original private spool path;
            # unlinking it could let the child recreate a less-private file.
            assert cleanup_attempts == []
            assert spool_path.exists()
        else:
            # The child is stopped; only its handle failed to close, so spool
            # cleanup is safe to attempt and must not mask stronger truth.
            assert cleanup_attempts == [spool_path]
        assert process.closed is False
    finally:
        spool_path.unlink(missing_ok=True)


def test_stop_failed_invocation_remains_recovery_required(
    tmp_path, monkeypatch
):
    registry = _registry(tmp_path)
    definition = _definition("diagnose_provider_gap")

    monkeypatch.setattr(
        SkillRegistry,
        "_execute_handler_bounded",
        staticmethod(
            lambda handler, payload, timeout_seconds: (
                None,
                "HANDLER_TIMEOUT_STOP_FAILED",
            )
        ),
    )

    interrupted = _invoke(
        registry,
        definition,
        call_id="stop-failed-recovery",
    )

    assert interrupted.status is SkillRunStatus.INTERRUPTED
    assert interrupted.error_code == "HANDLER_TIMEOUT_STOP_FAILED"
    assert interrupted.completed_at == "2026-09-19T04:00:00Z"

    with pytest.raises(SkillRecoveryRequiredError, match="recovery"):
        _invoke(
            SkillRegistry(registry.path),
            definition,
            call_id="stop-failed-recovery",
            at="2026-09-19T04:05:00Z",
        )


def test_handler_spool_cleanup_failure_still_invalidates_success(
    tmp_path, monkeypatch
):
    spool_path = tmp_path / "handler-result.json"
    expected = SkillExecutionResult(output={})

    class FakeProcess:
        def __init__(self):
            self.closed = False

        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

        def close(self):
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_spooled_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert args[2] == str(spool_path)
            assert daemon is True
            return process

    def fake_mkstemp(*, prefix, suffix):
        assert prefix == "autosport-skill-result-"
        assert suffix == ".json"
        descriptor = skill_registry_module.os.open(
            spool_path,
            skill_registry_module.os.O_CREAT
            | skill_registry_module.os.O_RDWR,
        )
        return descriptor, str(spool_path)

    monkeypatch.setattr(skill_registry_module.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(
        skill_registry_module,
        "_decode_handler_result_spool",
        lambda _path: (expected, None),
    )
    monkeypatch.setattr(
        skill_registry_module,
        "_remove_handler_result_spool",
        lambda _path, *, suppress_base_exceptions=False: False,
    )

    try:
        result, error = skill_registry_module._execute_handler_spooled_bounded(
            FakeContext(),
            _slow_handler,
            {},
            1,
        )

        assert result is None
        assert error == "HANDLER_RESULT_SPOOL_CLEANUP_FAILED"
        assert process.closed is True
    finally:
        spool_path.unlink(missing_ok=True)


def test_handler_timeout_cleanup_hard_kills_before_bounded_reap(monkeypatch):
    class FakeEndpoint:
        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.killed = False
            self.join_timeouts = []

        def start(self):
            return None

        def join(self, timeout=None):
            self.join_timeouts.append(timeout)

        def is_alive(self):
            return self.alive

        def kill(self):
            self.killed = True
            self.alive = False

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return FakeEndpoint(), FakeEndpoint()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_TIMEOUT"
    assert process.killed is True
    assert process.join_timeouts[0] == 1
    assert len(process.join_timeouts) == 2
    assert 0 < process.join_timeouts[1] < 1


def test_handler_timeout_kill_failure_falls_back_to_bounded_terminate(monkeypatch):
    class FakeEndpoint:
        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.terminated = False
            self.join_timeouts = []

        def start(self):
            return None

        def join(self, timeout=None):
            self.join_timeouts.append(timeout)

        def is_alive(self):
            return self.alive

        def kill(self):
            raise OSError("simulated kill race/failure")

        def terminate(self):
            self.terminated = True
            self.alive = False

        def close(self):
            self.closed = True

    process = FakeProcess()
    process.closed = False

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return FakeEndpoint(), FakeEndpoint()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_TIMEOUT"
    assert process.terminated is True
    assert process.closed is True
    assert process.join_timeouts[0] == 1
    assert len(process.join_timeouts) == 3
    assert all(0 < timeout < 1 for timeout in process.join_timeouts[1:])


def test_handler_timeout_stop_failure_is_distinct_and_leaves_handle_open(monkeypatch):
    class FakeEndpoint:
        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.join_timeouts = []
            self.closed = False

        def start(self):
            return None

        def join(self, timeout=None):
            self.join_timeouts.append(timeout)

        def is_alive(self):
            return self.alive

        def kill(self):
            raise OSError("simulated kill failure")

        def terminate(self):
            raise OSError("simulated terminate failure")

        def close(self):
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return FakeEndpoint(), FakeEndpoint()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_TIMEOUT_STOP_FAILED"
    assert process.is_alive() is True
    assert process.closed is False
    assert len(process.join_timeouts) == 3
    assert process.join_timeouts[0] == 1
    assert all(0 < timeout < 1 for timeout in process.join_timeouts[1:])


def test_handler_timeout_handle_close_failure_is_terminal_infrastructure_truth(monkeypatch):
    class FakeEndpoint:
        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.join_timeouts = []

        def start(self):
            return None

        def join(self, timeout=None):
            self.join_timeouts.append(timeout)

        def is_alive(self):
            return self.alive

        def kill(self):
            self.alive = False

        def close(self):
            raise ValueError("simulated process handle close failure")

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return FakeEndpoint(), FakeEndpoint()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_TIMEOUT_HANDLE_CLOSE_FAILED"
    assert process.is_alive() is False
    assert len(process.join_timeouts) == 2
    assert process.join_timeouts[0] == 1
    assert 0 < process.join_timeouts[1] < 1


def test_handler_result_handle_close_failure_cannot_escape_or_claim_success(monkeypatch):
    expected = SkillExecutionResult(output={})

    class FakeReceiver:
        def close(self):
            return None

        def poll(self):
            return True

        def recv(self):
            return "OK", expected

    class FakeSender:
        def close(self):
            return None

    class FakeProcess:
        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

        def close(self):
            raise OSError("simulated process handle close failure")

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return FakeReceiver(), FakeSender()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_PROCESS_HANDLE_CLOSE_FAILED"


def test_handler_unexpected_process_handle_close_failure_becomes_terminal_truth(monkeypatch):
    expected = SkillExecutionResult(output={})

    class FakeReceiver:
        def close(self):
            return None

        def poll(self):
            return True

        def recv(self):
            return "OK", expected

    class FakeSender:
        def close(self):
            return None

    class FakeProcess:
        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

        def close(self):
            raise RuntimeError("simulated unexpected process handle close failure")

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return FakeReceiver(), FakeSender()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return FakeProcess()

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_PROCESS_HANDLE_CLOSE_FAILED"


@pytest.mark.parametrize("failure_point", ("poll", "recv"))
@pytest.mark.parametrize("failure_type", (ValueError, RuntimeError))
def test_handler_result_pipe_failure_becomes_terminal_truth(monkeypatch, failure_point, failure_type):
    class FakeReceiver:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        def poll(self):
            if failure_point == "poll":
                raise failure_type("simulated result pipe failure during poll")
            return True

        def recv(self):
            if failure_point == "recv":
                raise failure_type("simulated result pipe failure during recv")
            raise AssertionError("unexpected recv")

    class FakeSender:
        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.closed = False

        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

        def close(self):
            self.closed = True

    receiver = FakeReceiver()
    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, FakeSender()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_RESULT_UNAVAILABLE"
    assert receiver.closed is True
    assert process.closed is True


def test_handler_process_construction_failure_closes_both_pipe_endpoints(monkeypatch):
    class FakeEndpoint:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    receiver = FakeEndpoint()
    sender = FakeEndpoint()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, sender

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            raise RuntimeError("simulated process construction failure")

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_PROCESS_CONSTRUCTION_RUNTIMEERROR"
    assert receiver.closed is True
    assert sender.closed is True


def test_handler_spawn_failure_attempts_all_cleanup_even_when_pipe_close_fails(monkeypatch):
    class FailingReceiver:
        def __init__(self):
            self.close_attempted = False

        def close(self):
            self.close_attempted = True
            raise RuntimeError("simulated receiver close failure")

    class Sender:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    receiver = FailingReceiver()
    sender = Sender()

    class FakeProcess:
        def __init__(self):
            self.closed = False

        def start(self):
            raise RuntimeError("simulated spawn failure")

        def close(self):
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, sender

        @staticmethod
        def Process(*, target, args, daemon):
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_START_PIPE_CLOSE_FAILED"
    assert receiver.close_attempted is True
    assert sender.closed is True
    assert process.closed is True


def test_handler_spawn_failure_stops_partially_started_child(monkeypatch):
    class FakeEndpoint:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    receiver = FakeEndpoint()
    sender = FakeEndpoint()

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.killed = False
            self.closed = False

        def start(self):
            raise RuntimeError("simulated partial spawn failure")

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.alive

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def close(self):
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            return receiver, sender

        @staticmethod
        def Process(*, target, args, daemon):
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_START_RUNTIMEERROR"
    assert process.killed is True
    assert process.closed is True
    assert receiver.closed is True
    assert sender.closed is True


@pytest.mark.parametrize(
    ("close_fails", "expected_error"),
    (
        (False, "HANDLER_START_RUNTIMEERROR"),
        (True, "HANDLER_START_HANDLE_CLOSE_FAILED"),
    ),
)
def test_handler_spawn_failure_closes_process_handle_and_preserves_terminal_truth(
    monkeypatch, close_fails, expected_error
):
    class FakeEndpoint:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    receiver = FakeEndpoint()
    sender = FakeEndpoint()

    class FakeProcess:
        def __init__(self):
            self.closed = False

        def start(self):
            raise RuntimeError("simulated spawn failure")

        def close(self):
            if close_fails:
                raise RuntimeError("simulated process handle close failure")
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, sender

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == expected_error
    assert receiver.closed is True
    assert sender.closed is True
    assert process.closed is (not close_fails)


def test_handler_sender_close_failure_stops_child_and_becomes_terminal_truth(monkeypatch):
    class FakeReceiver:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeSender:
        def close(self):
            raise RuntimeError("simulated parent sender close failure")

    receiver = FakeReceiver()

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.killed = False
            self.closed = False

        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.alive

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def close(self):
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, FakeSender()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_RESULT_PIPE_CLOSE_FAILED"
    assert receiver.closed is True
    assert process.killed is True
    assert process.closed is True


def test_handler_parent_keyboard_interrupt_reaps_child_and_reraises(monkeypatch):
    class FakeEndpoint:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.join_calls = 0
            self.killed = False
            self.closed = False

        def start(self):
            return None

        def join(self, timeout=None):
            self.join_calls += 1
            if self.join_calls == 1:
                raise KeyboardInterrupt()

        def is_alive(self):
            return self.alive

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def close(self):
            self.closed = True

    receiver = FakeEndpoint()
    sender = FakeEndpoint()
    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, sender

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    with pytest.raises(KeyboardInterrupt):
        SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert process.killed is True
    assert process.closed is True
    assert process.join_calls == 2
    assert receiver.closed is True
    assert sender.closed is True


def test_handler_initial_join_failure_stops_child_and_becomes_terminal_truth(monkeypatch):
    class FakeEndpoint:
        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.join_calls = 0
            self.killed = False
            self.closed = False

        def start(self):
            return None

        def join(self, timeout=None):
            self.join_calls += 1
            if self.join_calls == 1:
                raise RuntimeError("simulated initial join failure")

        def is_alive(self):
            return self.alive

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def close(self):
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return FakeEndpoint(), FakeEndpoint()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_PROCESS_JOIN_FAILED"
    assert process.killed is True
    assert process.closed is True
    assert process.join_calls == 2


def test_handler_initial_state_query_failure_stops_child_and_becomes_terminal_truth(monkeypatch):
    class FakeEndpoint:
        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.state_calls = 0
            self.killed = False
            self.closed = False

        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            self.state_calls += 1
            if self.state_calls == 1:
                raise RuntimeError("simulated initial state query failure")
            return self.alive

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def close(self):
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return FakeEndpoint(), FakeEndpoint()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_PROCESS_STATE_UNAVAILABLE"
    assert process.killed is True
    assert process.closed is True
    assert process.state_calls == 2



def test_handler_context_failure_after_running_is_durable_terminal_truth(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    definition = _definition("diagnose_provider_gap")

    def fail_context(_method):
        raise RuntimeError("simulated multiprocessing context failure")

    monkeypatch.setattr(skill_registry_module.multiprocessing, "get_context", fail_context)

    run = _invoke(registry, definition, payload={})

    assert run.status is SkillRunStatus.FAILED
    assert run.error_code == "HANDLER_INFRASTRUCTURE_RUNTIMEERROR"
    assert run.completed_at is not None
    assert SkillRegistry(registry.path).get_run(run.run_id) == run

def test_handler_timeout_receiver_close_failure_is_terminal_cleanup_truth(monkeypatch):
    class FakeReceiver:
        def close(self):
            raise RuntimeError("simulated receiver close failure")

    class FakeSender:
        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.alive = True
            self.closed = False

        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.alive

        def kill(self):
            self.alive = False

        def terminate(self):
            self.alive = False

        def close(self):
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return FakeReceiver(), FakeSender()

        @staticmethod
        def Process(*, target, args, daemon):
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_TIMEOUT_PIPE_CLOSE_FAILED"
    assert process.closed is True


def test_handler_success_receiver_close_failure_cannot_claim_success(monkeypatch):
    expected = SkillExecutionResult(output={})

    class FakeReceiver:
        def poll(self):
            return True

        def recv(self):
            return "OK", expected

        def close(self):
            raise RuntimeError("simulated receiver close failure")

    class FakeSender:
        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.closed = False

        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

        def close(self):
            self.closed = True

    process = FakeProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return FakeReceiver(), FakeSender()

        @staticmethod
        def Process(*, target, args, daemon):
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_RESULT_PIPE_CLOSE_FAILED"
    assert process.closed is True


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
    assert run.applied_mutations == ("LOCAL_WRITE",)
    assert SkillRegistry(registry.path).get_run(run.run_id) == run


def test_failed_run_preserves_undeclared_tool_and_evidence_facts(tmp_path):
    registry = SkillRegistry.initialize(tmp_path / "skills.json")
    definition = SkillDefinition(
        skill_id="read-only-tool-probe",
        version="1.0.0",
        capability="read_only_tool_probe",
        purpose="Preserve violation evidence without granting tool authority",
        input_schema_sha256=SHA_A,
        output_schema_sha256=SHA_B,
        implementation_sha256=SHA_D,
        implementation_kind=SkillImplementationKind.REVIEWED_PLUGIN,
        required_authorities=("READ_ONLY_ANALYSIS",),
        required_provenance=("source_evidence",),
    )
    registry.register(definition)
    registry._handlers[definition.version_key] = _undeclared_tool_and_evidence_handler
    run = _invoke(registry, definition, call_id="undeclared-tool-evidence", payload={})
    assert run.status is SkillRunStatus.FAILED
    assert run.error_code == "HANDLER_ERROR_SKILLPERMISSIONERROR"
    assert run.used_tools == ("FAKE_WRITE_TOOL",)
    assert run.emitted_evidence == (("UNDECLARED_EVIDENCE", SHA_D),)
    assert SkillRegistry(registry.path).get_run(run.run_id) == run


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

def test_handler_interrupt_cleanup_baseexception_cannot_mask_original(monkeypatch):
    class InterruptingEndpoint:
        def close(self):
            raise SystemExit("cleanup pipe close")

    class InterruptingProcess:
        def __init__(self):
            self.alive = True
            self.join_calls = 0

        def start(self):
            return None

        def join(self, timeout=None):
            self.join_calls += 1
            if self.join_calls == 1:
                raise KeyboardInterrupt("original parent interrupt")

        def is_alive(self):
            return self.alive

        def kill(self):
            raise SystemExit("cleanup kill")

        def terminate(self):
            self.alive = False

        def close(self):
            raise SystemExit("cleanup process close")

    process = InterruptingProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return InterruptingEndpoint(), InterruptingEndpoint()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            assert args[0] is _slow_handler
            assert args[1] == {}
            assert daemon is True
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    with pytest.raises(KeyboardInterrupt, match="original parent interrupt"):
        SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert process.alive is False
    assert process.join_calls >= 2


def test_handler_construction_interrupt_cleanup_baseexception_cannot_mask_original(
    monkeypatch,
):
    class InterruptingEndpoint:
        def close(self):
            raise SystemExit("cleanup pipe close")

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return InterruptingEndpoint(), InterruptingEndpoint()

        @staticmethod
        def Process(*, target, args, daemon):
            raise KeyboardInterrupt("original construction interrupt")

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    with pytest.raises(KeyboardInterrupt, match="original construction interrupt"):
        SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)


def test_handler_result_interrupt_cleanup_baseexception_cannot_mask_original(
    monkeypatch,
):
    class InterruptingReceiver:
        def poll(self):
            raise KeyboardInterrupt("original result interrupt")

        def close(self):
            raise SystemExit("cleanup receiver close")

    class Sender:
        def close(self):
            return None

    class InterruptingProcess:
        def start(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

        def close(self):
            raise SystemExit("cleanup process close")

    process = InterruptingProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return InterruptingReceiver(), Sender()

        @staticmethod
        def Process(*, target, args, daemon):
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    with pytest.raises(KeyboardInterrupt, match="original result interrupt"):
        SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)


def test_handler_deadline_clock_failure_reaps_child_and_returns_terminal_truth(
    monkeypatch,
):
    class Receiver:
        def __init__(self):
            self.closed = False

        def poll(self):
            return False

        def close(self):
            self.closed = True

    class Sender:
        def close(self):
            return None

    class Process:
        def __init__(self):
            self.alive = True
            self.killed = False
            self.closed = False

        def start(self):
            return None

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.alive

        def close(self):
            self.closed = True

    receiver = Receiver()
    process = Process()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, Sender()

        @staticmethod
        def Process(*, target, args, daemon):
            assert target is skill_registry_module._skill_handler_process
            return process

    def fail_clock():
        raise RuntimeError("simulated monotonic failure")

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )
    monkeypatch.setattr(skill_registry_module.time, "monotonic", fail_clock)

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_PROCESS_CLOCK_UNAVAILABLE"
    assert process.killed is True
    assert process.alive is False
    assert process.closed is True
    assert receiver.closed is True


def test_handler_poll_loop_clock_failure_reaps_child(monkeypatch):
    class Receiver:
        def __init__(self):
            self.closed = False

        def poll(self):
            return False

        def close(self):
            self.closed = True

    class Sender:
        def close(self):
            return None

    class Process:
        def __init__(self):
            self.alive = True
            self.killed = False
            self.closed = False

        def start(self):
            return None

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.alive

        def close(self):
            self.closed = True

    receiver = Receiver()
    process = Process()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, Sender()

        @staticmethod
        def Process(*, target, args, daemon):
            return process

    clock_calls = 0

    def fail_second_clock():
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 1:
            return 100.0
        raise RuntimeError("simulated loop clock failure")

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )
    monkeypatch.setattr(skill_registry_module.time, "monotonic", fail_second_clock)

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_PROCESS_CLOCK_UNAVAILABLE"
    assert clock_calls == 2
    assert process.killed is True
    assert process.closed is True
    assert receiver.closed is True


def test_handler_result_reap_clock_failure_cannot_claim_success(monkeypatch):
    expected = SkillExecutionResult(output={"large": "done"})

    class Receiver:
        def __init__(self):
            self.closed = False

        def poll(self):
            return True

        def recv(self):
            return ("OK", expected)

        def close(self):
            self.closed = True

    class Sender:
        def close(self):
            return None

    class Process:
        def __init__(self):
            self.alive = True
            self.killed = False
            self.closed = False

        def start(self):
            return None

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.alive

        def close(self):
            self.closed = True

    receiver = Receiver()
    process = Process()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, Sender()

        @staticmethod
        def Process(*, target, args, daemon):
            return process

    clock_calls = 0

    def fail_second_clock():
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 1:
            return 100.0
        raise RuntimeError("simulated post-result clock failure")

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )
    monkeypatch.setattr(skill_registry_module.time, "monotonic", fail_second_clock)

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_PROCESS_CLOCK_UNAVAILABLE"
    assert clock_calls == 2
    assert process.killed is True
    assert process.closed is True
    assert receiver.closed is True


def test_handler_clock_interrupt_cleanup_cannot_mask_original(monkeypatch):
    class InterruptingReceiver:
        def poll(self):
            return False

        def close(self):
            raise SystemExit("cleanup pipe close")

    class Sender:
        def close(self):
            return None

    class InterruptingProcess:
        def start(self):
            return None

        def kill(self):
            raise SystemExit("cleanup kill")

        def terminate(self):
            raise SystemExit("cleanup terminate")

        def join(self, timeout=None):
            raise SystemExit("cleanup join")

        def is_alive(self):
            return False

        def close(self):
            raise SystemExit("cleanup process close")

    process = InterruptingProcess()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return InterruptingReceiver(), Sender()

        @staticmethod
        def Process(*, target, args, daemon):
            return process

    def interrupt_clock():
        raise KeyboardInterrupt("original clock interrupt")

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )
    monkeypatch.setattr(skill_registry_module.time, "monotonic", interrupt_clock)

    with pytest.raises(KeyboardInterrupt, match="original clock interrupt"):
        SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)


@pytest.mark.parametrize("clock_value", [float("nan"), float("inf"), float("-inf")])
def test_handler_invalid_deadline_clock_value_reaps_child(monkeypatch, clock_value):
    class Receiver:
        def __init__(self):
            self.closed = False

        def poll(self):
            return False

        def close(self):
            self.closed = True

    class Sender:
        def close(self):
            return None

    class Process:
        def __init__(self):
            self.alive = True
            self.killed = False
            self.closed = False

        def start(self):
            return None

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.alive

        def close(self):
            self.closed = True

    receiver = Receiver()
    process = Process()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, Sender()

        @staticmethod
        def Process(*, target, args, daemon):
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )
    monkeypatch.setattr(
        skill_registry_module.time, "monotonic", lambda: clock_value
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_PROCESS_CLOCK_UNAVAILABLE"
    assert process.killed is True
    assert process.closed is True
    assert receiver.closed is True


def test_handler_hostile_clock_subclass_is_rejected_before_arithmetic(monkeypatch):
    class HostileFloat(float):
        def __add__(self, other):
            raise AssertionError("hostile clock arithmetic executed")

    class Receiver:
        def __init__(self):
            self.closed = False

        def poll(self):
            return False

        def close(self):
            self.closed = True

    class Sender:
        def close(self):
            return None

    class Process:
        def __init__(self):
            self.alive = True
            self.killed = False
            self.closed = False

        def start(self):
            return None

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.alive

        def close(self):
            self.closed = True

    receiver = Receiver()
    process = Process()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, Sender()

        @staticmethod
        def Process(*, target, args, daemon):
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )
    monkeypatch.setattr(
        skill_registry_module.time, "monotonic", lambda: HostileFloat(100.0)
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_PROCESS_CLOCK_UNAVAILABLE"
    assert process.killed is True
    assert process.closed is True
    assert receiver.closed is True


def test_handler_poll_method_lookup_failure_reaps_child(monkeypatch):
    class Receiver:
        def __init__(self):
            self.closed = False

        @property
        def poll(self):
            raise RuntimeError("simulated poll lookup failure")

        def close(self):
            self.closed = True

    class Sender:
        def close(self):
            return None

    class Process:
        def __init__(self):
            self.alive = True
            self.killed = False
            self.closed = False

        def start(self):
            return None

        def kill(self):
            self.killed = True
            self.alive = False

        def terminate(self):
            self.alive = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self.alive

        def close(self):
            self.closed = True

    receiver = Receiver()
    process = Process()

    class FakeContext:
        @staticmethod
        def Pipe(*, duplex):
            assert duplex is False
            return receiver, Sender()

        @staticmethod
        def Process(*, target, args, daemon):
            return process

    monkeypatch.setattr(
        skill_registry_module.multiprocessing,
        "get_context",
        lambda method: FakeContext(),
    )

    result, error = SkillRegistry._execute_handler_bounded(_slow_handler, {}, 1)

    assert result is None
    assert error == "HANDLER_RESULT_UNAVAILABLE"
    assert process.killed is True
    assert process.closed is True
    assert receiver.closed is True
