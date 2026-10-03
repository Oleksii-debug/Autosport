from __future__ import annotations

import json
from contextvars import ContextVar

import pytest

from autosport import campaign_forward_universe_cycle_binding as forward_module
from autosport.paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)
from autosport import paper_campaign_admission as admission_module
from autosport import paper_campaign_forward_admission as forward_admission_module
from autosport.paper_campaign_forward_admission import (
    PaperCampaignForwardAdmissionError,
    admit_forward_verified,
)
from paper_campaign_admission_test_support import AdmissionFixture


def _admissions(fixture: AdmissionFixture) -> dict[str, object]:
    raw = json.loads(
        (fixture.workspace / "paper-campaign-admission.json").read_text(
            encoding="utf-8"
        )
    )
    return raw["admissions"]


def _closure_function(root, *, module_name: str, function_name: str):
    seen: set[int] = set()
    pending = [root]
    while pending:
        candidate = pending.pop()
        identity = id(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        if (
            getattr(candidate, "__module__", None) == module_name
            and getattr(candidate, "__name__", None) == function_name
        ):
            return candidate
        for cell in getattr(candidate, "__closure__", ()) or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if callable(value):
                pending.append(value)
    raise AssertionError(
        f"closure function {module_name}.{function_name} was not reachable"
    )


def _forward_context_var() -> ContextVar:
    closure = admission_module._CURRENT_FORWARD_VERIFICATION.__closure__ or ()
    return next(
        cell.cell_contents
        for cell in closure
        if type(cell.cell_contents) is ContextVar
    )


def test_legacy_admit_cannot_claim_forward_verification(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()

    with pytest.raises(
        PaperCampaignAdmissionError,
        match="cannot claim campaign forward verification authority",
    ):
        fixture.admit(
            coordinator,
            decision_payload={
                "campaign_forward_verification": {
                    "receipt_sha256": "a" * 64,
                }
            },
        )

    assert _admissions(fixture) == {}


def test_legacy_admit_cannot_claim_forward_action_parameter(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()

    with pytest.raises(
        PaperCampaignAdmissionError,
        match="cannot claim campaign forward verification authority",
    ):
        fixture.admit(
            coordinator,
            action_parameters=(
                ("campaign_forward_verification_receipt_sha256", "a" * 64),
            ),
        )

    assert _admissions(fixture) == {}


def test_forward_admission_resolves_before_prepared(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()

    with pytest.raises((TypeError, RuntimeError)):
        admit_forward_verified(
            coordinator,
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            event_lifecycle=None,
            evidence=None,
            admission_id="admission-1",
            observation=fixture.observation,
            action_type="PAPER_PROPOSAL",
            decision_action="OPEN_PAPER_TICKET",
            decision_at="2026-09-20T05:00:05+00:00",
            at="2026-09-20T05:00:05+00:00",
            replay_run_id="admission-run",
            agent="admission-test",
            execution_decision_id=fixture.execution_decision_id,
            execution_run_id=fixture.execution_run_id,
            execution_attempt_id=fixture.execution_attempt_id,
            execution_ticket_id=fixture.execution_ticket_id,
        )

    assert _admissions(fixture) == {}


def test_forward_admission_rejects_verifier_surface_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    hostile_calls: list[str] = []

    def hostile_verify(**_kwargs):
        hostile_calls.append("verify")
        raise AssertionError("hostile forward verifier executed")

    monkeypatch.setattr(
        forward_module,
        "verify_campaign_forward_evidence",
        hostile_verify,
    )

    with pytest.raises(
        PaperCampaignForwardAdmissionError,
        match="authority surface changed",
    ):
        admit_forward_verified(
            coordinator,
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            event_lifecycle=None,
            evidence=None,
            admission_id="admission-1",
            observation=fixture.observation,
            action_type="PAPER_PROPOSAL",
            decision_action="OPEN_PAPER_TICKET",
            decision_at="2026-09-20T05:00:05+00:00",
            at="2026-09-20T05:00:05+00:00",
            replay_run_id="admission-run",
            agent="admission-test",
            execution_decision_id=fixture.execution_decision_id,
            execution_run_id=fixture.execution_run_id,
            execution_attempt_id=fixture.execution_attempt_id,
            execution_ticket_id=fixture.execution_ticket_id,
        )

    assert hostile_calls == []
    assert _admissions(fixture) == {}


def test_forward_admission_rejects_legacy_admit_surface_rebind(
    tmp_path,
    monkeypatch,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    hostile_calls: list[str] = []

    def hostile_admit(*_args, **_kwargs):
        hostile_calls.append("admit")
        raise AssertionError("hostile admission executed")

    monkeypatch.setattr(PaperCampaignAdmissionCoordinator, "admit", hostile_admit)

    with pytest.raises(
        PaperCampaignForwardAdmissionError,
        match="authority surface changed",
    ):
        admit_forward_verified(
            coordinator,
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            event_lifecycle=None,
            evidence=None,
            admission_id="admission-1",
            observation=fixture.observation,
            action_type="PAPER_PROPOSAL",
            decision_action="OPEN_PAPER_TICKET",
            decision_at="2026-09-20T05:00:05+00:00",
            at="2026-09-20T05:00:05+00:00",
            replay_run_id="admission-run",
            agent="admission-test",
            execution_decision_id=fixture.execution_decision_id,
            execution_run_id=fixture.execution_run_id,
            execution_attempt_id=fixture.execution_attempt_id,
            execution_ticket_id=fixture.execution_ticket_id,
        )

    assert hostile_calls == []
    assert _admissions(fixture) == {}


def test_forward_admission_rejects_public_surface_rebind_via_captured_reference(
    tmp_path,
    monkeypatch,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    captured = admit_forward_verified

    def hostile_public(*_args, **_kwargs):
        raise AssertionError("hostile public forward admission executed")

    monkeypatch.setattr(
        forward_admission_module,
        "admit_forward_verified",
        hostile_public,
    )

    with pytest.raises(
        PaperCampaignForwardAdmissionError,
        match="public surface changed",
    ):
        captured(
            coordinator,
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            event_lifecycle=None,
            evidence=None,
            admission_id="admission-1",
            observation=fixture.observation,
            action_type="PAPER_PROPOSAL",
            decision_action="OPEN_PAPER_TICKET",
            decision_at="2026-09-20T05:00:05+00:00",
            at="2026-09-20T05:00:05+00:00",
            replay_run_id="admission-run",
            agent="admission-test",
            execution_decision_id=fixture.execution_decision_id,
            execution_run_id=fixture.execution_run_id,
            execution_attempt_id=fixture.execution_attempt_id,
            execution_ticket_id=fixture.execution_ticket_id,
        )

    assert _admissions(fixture) == {}


def test_forward_admission_rejects_builtin_shadow_before_dispatch(
    tmp_path,
    monkeypatch,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    hostile_calls: list[str] = []

    def hostile_type(*_args, **_kwargs):
        hostile_calls.append("type")
        raise AssertionError("hostile type executed")

    monkeypatch.setattr(
        forward_admission_module,
        "type",
        hostile_type,
        raising=False,
    )

    with pytest.raises(
        PaperCampaignForwardAdmissionError,
        match="builtin dispatch shadowed",
    ):
        admit_forward_verified(
            coordinator,
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            event_lifecycle=None,
            evidence=None,
            admission_id="admission-1",
            observation=fixture.observation,
            action_type="PAPER_PROPOSAL",
            decision_action="OPEN_PAPER_TICKET",
            decision_at="2026-09-20T05:00:05+00:00",
            at="2026-09-20T05:00:05+00:00",
            replay_run_id="admission-run",
            agent="admission-test",
            execution_decision_id=fixture.execution_decision_id,
            execution_run_id=fixture.execution_run_id,
            execution_attempt_id=fixture.execution_attempt_id,
            execution_ticket_id=fixture.execution_ticket_id,
        )

    assert hostile_calls == []
    assert _admissions(fixture) == {}


def test_legacy_admit_rejects_forward_routing_constant_rebind(
    tmp_path,
    monkeypatch,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()

    monkeypatch.setattr(
        admission_module,
        "_FORWARD_VERIFICATION_DECISION_FIELD",
        "hostile_forward_verification",
    )

    with pytest.raises(
        PaperCampaignAdmissionError,
        match="forward-verification routing changed",
    ):
        fixture.admit(coordinator)

    assert _admissions(fixture) == {}


def test_legacy_admit_rejects_forward_context_reader_rebind(
    tmp_path,
    monkeypatch,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()

    monkeypatch.setattr(
        admission_module,
        "_CURRENT_FORWARD_VERIFICATION",
        lambda: {"receipt_sha256": "a" * 64},
    )

    with pytest.raises(
        PaperCampaignAdmissionError,
        match="forward-verification routing changed",
    ):
        fixture.admit(coordinator)

    assert _admissions(fixture) == {}


def test_forward_context_has_no_public_setter():
    assert not hasattr(admission_module, "_ACTIVE_FORWARD_VERIFICATION")
    assert not hasattr(
        admission_module,
        "_install_forward_verification_runner",
    )


def test_extracted_forward_context_runner_cannot_bypass_canonical_verifier(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    invoked: list[str] = []
    runner = _closure_function(
        admit_forward_verified,
        module_name=admission_module.__name__,
        function_name="run",
    )

    def hostile_invoke():
        invoked.append("admit")
        return fixture.admit(coordinator)

    with pytest.raises((TypeError, RuntimeError)):
        runner((None,) * 8, hostile_invoke)

    assert invoked == []
    assert _admissions(fixture) == {}


def test_extracted_forward_context_var_cannot_forge_verification(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    context = _forward_context_var()
    token = context.set(
        (
            (None,) * 8,
            "a" * 64,
        )
    )
    try:
        with pytest.raises((TypeError, RuntimeError)):
            fixture.admit(coordinator)
    finally:
        context.reset(token)

    assert _admissions(fixture) == {}


def test_forward_authority_revalidates_before_journal_mutation(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    state_path = fixture.workspace / "paper-campaign-admission.json"
    before = state_path.read_bytes()
    context = _forward_context_var()
    token = context.set(
        (
            (None,) * 8,
            "a" * 64,
        )
    )
    try:
        with pytest.raises((TypeError, RuntimeError)):
            coordinator._write({})
    finally:
        context.reset(token)

    assert state_path.read_bytes() == before
    assert _admissions(fixture) == {}


@pytest.mark.parametrize("name", ["str", "bool", "type", "len", "dict", "tuple"])
def test_legacy_admit_rejects_admission_builtin_shadow_before_authority_dispatch(
    tmp_path,
    monkeypatch,
    name,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    hostile_calls: list[str] = []

    def hostile(*_args, **_kwargs):
        hostile_calls.append(name)
        raise AssertionError(f"hostile {name} executed")

    monkeypatch.setattr(admission_module, name, hostile, raising=False)
    with pytest.raises(
        PaperCampaignAdmissionError,
        match="builtin dispatch changed",
    ):
        fixture.admit(coordinator)

    assert hostile_calls == []
    assert _admissions(fixture) == {}


@pytest.mark.parametrize(
    ("surface", "attribute"),
    [("json", "dumps"), ("json", "loads"), ("hashlib", "sha256")],
)
def test_legacy_admit_rejects_digest_json_surface_rebind_before_authority_dispatch(
    tmp_path,
    monkeypatch,
    surface,
    attribute,
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    hostile_calls: list[str] = []

    def hostile(*_args, **_kwargs):
        hostile_calls.append(attribute)
        raise AssertionError(f"hostile {surface}.{attribute} executed")

    owner = getattr(admission_module, surface)
    monkeypatch.setattr(owner, attribute, hostile)
    with pytest.raises(
        PaperCampaignAdmissionError,
        match="digest/JSON authority changed",
    ):
        fixture.admit(coordinator)

    assert hostile_calls == []
    assert _admissions(fixture) == {}
