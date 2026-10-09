"""Plan 5 Section 8, isolated non-executing Windows lab protocol tests."""
import dataclasses
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from autosport.windows_lab_contract import (
    WindowsLabCampaign,
    WindowsLabContractError,
    WindowsLabObservation,
    WindowsLabTicket,
    source_level_lab_ticket,
)

SOURCE = "a" * 40
PACKAGE = "b" * 64
EVIDENCE = "c" * 64


def ticket():
    return source_level_lab_ticket(SOURCE, PACKAGE)


def observed(scenario="desktop_start_stop", status="PASS", evidence=EVIDENCE):
    return WindowsLabObservation(scenario, status, evidence)


def test_fixed_source_package_and_exact_dispatch_round_trip():
    actual = ticket()
    assert actual.repository == "Oleksii-debug/Autosport"
    assert actual.dispatch_ref == "refs/heads/main"
    assert actual.dispatch_event == "workflow_dispatch"
    assert actual.runner_profile == "dedicated_ephemeral_nonadmin_vm_no_host_mounts_or_credentials"
    assert WindowsLabTicket.from_json(actual.to_json()) == actual
    assert WindowsLabTicket.from_json(actual.to_json()).ticket_id == actual.ticket_id
    assert not actual.execution_authority
    assert not actual.human_tested
    assert not actual.nvda_verified
    assert not actual.target_machine_acceptance


@pytest.mark.parametrize("change", [
    {"repository": "attacker/fork"}, {"dispatch_event": "pull_request"},
    {"dispatch_event": "pull_request_target"}, {"dispatch_event": "push"},
    {"dispatch_ref": "refs/heads/attacker"}, {"dispatch_ref": "refs/pull/1/merge"},
    {"runner_profile": "public_shared_admin_runner"},
    {"source_sha": "A" * 40}, {"package_sha256": "a" * 63},
    {"execution_authority": True}, {"human_tested": True},
    {"nvda_verified": True}, {"target_machine_acceptance": True},
    {"scenarios": ("os_shell_command",)},
    {"scenarios": ("desktop_start_stop", "desktop_start_stop")},
    {"scenarios": ("desktop_start_stop",)},
    {"scenarios": ("desktop_start_stop", "keyboard_semantic_navigation")},
    {"scenarios": ("keyboard_semantic_navigation", "native_emergency_stop", "packaged_restart_recovery", "browser_semantic_readback")},
    {"scenarios": ("native_emergency_stop", "desktop_start_stop")},
    {"scenarios": ["desktop_start_stop"]},
    {"scenarios": ()},
])
def test_untrusted_dispatch_fork_authority_and_shape_fail_closed(change):
    with pytest.raises(WindowsLabContractError):
        dataclasses.replace(ticket(), **change)


@pytest.mark.parametrize("change", [
    {"source_sha": 4},
    {"package_sha256": b"x" * 64},
    {"dispatch_ref": ["refs/heads/main"]},
    {"repository": 100},
    {"runner_profile": object()},
    {"scenarios": (True,)},
    {"scenarios": ({"private": "secret"},)},
])
def test_noncanonical_primitive_ingress_fails_closed(change):
    with pytest.raises(WindowsLabContractError):
        dataclasses.replace(ticket(), **change)


def test_json_unknown_duplicate_truncated_and_oversized_fails_closed():
    original = ticket().to_json()
    hostile = [
        original[:-1], "x" * 17_000, "[]", "null",
        original.replace('"kind":"ticket"', '"kind":"ticket","kind":"ticket"'),
        original.replace('"kind":"ticket"', '"kind":"different"'),
        original.replace('"human_tested":false', '"human_tested":true'),
        original.replace('"ticket_id":', '"danger":"data","ticket_id":'),
        original.replace(SOURCE, "d" * 40, 1),
    ]
    for candidate in hostile:
        with pytest.raises(WindowsLabContractError):
            WindowsLabTicket.from_json(candidate)


def test_same_agent_observation_replay_is_exactly_idempotent():
    first = WindowsLabCampaign(ticket())
    a = observed()
    accepted = first.admit(a)
    retried = accepted.admit(a)
    assert retried is accepted
    assert len(retried.observations) == 1
    assert retried.source_bound_sha256 == accepted.source_bound_sha256
    assert retried.disposition == "UNVERIFIED_AGENT_EVIDENCE"
    assert not retried.ticket.execution_authority


def test_conflicting_agent_ack_never_overwrites_or_blind_retries():
    active = WindowsLabCampaign(ticket()).admit(observed())
    for second in (
        observed(status="FAIL"),
        observed(evidence="d" * 64),
        observed(status="WAIT"),
    ):
        with pytest.raises(WindowsLabContractError, match="conflicting replay"):
            active.admit(second)
        assert active.observations == (observed(),)


def test_scenario_order_and_scope_are_fenced_before_effect():
    campaign = WindowsLabCampaign(ticket())
    with pytest.raises(WindowsLabContractError, match="scenario ordering"):
        campaign.admit(observed("keyboard_semantic_navigation"))
    with pytest.raises(WindowsLabContractError):
        campaign.admit(observed("other"))
    assert campaign.observations == ()


def test_agent_observations_bounded_and_final_pass_cannot_self_promote():
    t = ticket()
    campaign = WindowsLabCampaign(t)
    for name in t.scenarios:
        campaign = campaign.admit(observed(name))
    assert len(campaign.observations) == 5
    assert campaign.disposition == "UNVERIFIED_AGENT_EVIDENCE"
    raw = json.loads(campaign.to_json())
    assert raw["nvda_verified"] is False
    assert raw["target_machine_acceptance"] is False
    assert raw["execution_authority"] is False
    assert raw["human_tested"] is False
    assert WindowsLabCampaign.from_json(campaign.to_json()) == campaign
    for bad in ({"status": "RUN"}, {"evidence_sha256": "not-hex"}, {"scenario": 3}):
        with pytest.raises(WindowsLabContractError):
            dataclasses.replace(observed(), **bad)


def test_crash_restart_rehydrates_exact_campaign_without_duplicate():
    before = WindowsLabCampaign(ticket()).admit(observed())
    payload = before.to_json()
    script = (
        "import sys;"
        "from autosport.windows_lab_contract import WindowsLabCampaign,WindowsLabObservation;"
        "s=WindowsLabCampaign.from_json(sys.stdin.read());"
        "r=s.admit(WindowsLabObservation('desktop_start_stop','PASS','c'*64));"
        "sys.stdout.write(r.to_json())"
    )
    ran = subprocess.run(
        [sys.executable, "-c", script],
        input=payload, text=True, capture_output=True, check=True,
        timeout=15,
    )
    after = WindowsLabCampaign.from_json(ran.stdout)
    assert after == before
    assert after.source_bound_sha256 == before.source_bound_sha256
    assert len(after.observations) == 1


def test_failure_injection_never_grants_trust_or_erases_original():
    base = WindowsLabCampaign(ticket()).admit(observed())
    raw = json.loads(base.to_json())
    variants = []
    for field, value in (
        ("source_bound_sha256", "d" * 64),
        ("disposition", "RELEASE_APPROVED"),
        ("nvda_verified", True),
        ("target_machine_acceptance", True),
        ("execution_authority", True),
        ("human_tested", True),
    ):
        altered = {**raw, field: value}
        variants.append(json.dumps(altered))
    tampered = {**raw, "observations": raw["observations"] * 2}
    variants.append(json.dumps(tampered))
    tampered = {**raw, "observations": [{"scenario": "desktop_start_stop", "status": "PASS", "evidence_sha256": "a" * 64}]}
    variants.append(json.dumps(tampered))
    tampered = {**raw, "extra_private_log": "PRIVATE_CANARY"}
    variants.append(json.dumps(tampered))
    for payload in variants:
        with pytest.raises(WindowsLabContractError) as e:
            WindowsLabCampaign.from_json(payload)
        assert "CANARY" not in str(e.value)
    assert WindowsLabCampaign.from_json(base.to_json()) == base


def test_one_active_ticket_and_exact_source_identity_across_restarts():
    c = WindowsLabCampaign(ticket()).admit(observed())
    resumed = WindowsLabCampaign.from_json(c.to_json())
    assert resumed.ticket.ticket_id == c.ticket.ticket_id
    assert resumed.admit(observed()) == c
    forged = json.loads(c.to_json())
    forged["ticket"]["package_sha256"] = "d" * 64
    with pytest.raises(WindowsLabContractError):
        WindowsLabCampaign.from_json(json.dumps(forged))
    forged["ticket"]["source_sha"] = "e" * 40
    with pytest.raises(WindowsLabContractError):
        WindowsLabCampaign.from_json(json.dumps(forged))


def test_finite_memory_bounded_reports_are_reproducible_under_stress():
    baseline = WindowsLabCampaign(ticket()).admit(observed())
    payload = baseline.to_json()
    assert len(payload.encode("utf-8")) <= 16_384
    def replay(_):
        result = WindowsLabCampaign.from_json(payload).admit(observed())
        return result.source_bound_sha256
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(replay, range(1000)))
    assert len(set(values)) == 1
    assert values[0] == baseline.source_bound_sha256


def test_duplicate_json_receipt_key_rejected_without_echoing_data():
    base = WindowsLabCampaign(ticket()).to_json()
    attack = base.replace('"kind":"campaign"', '"kind":"campaign","kind":"PRIVATE_CANARY"')
    with pytest.raises(WindowsLabContractError) as e:
        WindowsLabCampaign.from_json(attack)
    assert "CANARY" not in str(e.value)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True),
    ("execution_authority", 0),
    ("human_tested", 0),
    ("nvda_verified", 0),
    ("target_machine_acceptance", 0),
])
def test_canonical_json_scalar_types_cannot_forge_campaign_authority(field, value):
    canonical = WindowsLabCampaign(ticket()).to_json()
    raw = json.loads(canonical)
    raw[field] = value
    with pytest.raises(WindowsLabContractError):
        WindowsLabCampaign.from_json(json.dumps(raw))

