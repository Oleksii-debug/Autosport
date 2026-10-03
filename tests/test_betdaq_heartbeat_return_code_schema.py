from __future__ import annotations

import pytest

import autosport.betdaq_heartbeat_safety as heartbeat_module
from autosport.betdaq_heartbeat_safety import (
    BetdaqHeartbeatSafetyError,
    HeartbeatProviderEvidence,
)


@pytest.mark.parametrize("bad_code", ["462", 462.0, True, False, {}, []])
def test_provider_evidence_rejects_noncanonical_return_code(bad_code: object) -> None:
    with pytest.raises(BetdaqHeartbeatSafetyError, match="provider_return_code"):
        HeartbeatProviderEvidence(
            method="Pulse",
            observed_at="2026-09-23T00:00:00Z",
            response_sha256="a" * 64,
            provider_return_code=bad_code,  # type: ignore[arg-type]
            provider_performed_at=None,
            performed_action=None,
        )


@pytest.mark.parametrize("bad_code", ["462", 462.0, True, False, {}, []])
def test_durable_event_rejects_noncanonical_return_code_even_with_matching_digest(
    bad_code: object,
) -> None:
    body: dict[str, object] = {
        "sequence": 1,
        "generation_id": "b" * 64,
        "predecessor_generation_id": None,
        "account_context_id": "account-context",
        "process_instance_id": "c" * 64,
        "state": "DEGRADED_UNKNOWN",
        "threshold_ms": None,
        "registered_action": None,
        "operation": "Pulse",
        "observed_at": "2026-09-23T00:00:00Z",
        "provider_performed_at": None,
        "performed_action": None,
        "provider_return_code": bad_code,
        "response_sha256": None,
        "reconciliation_required": True,
        "external_pulse_masking_possible": True,
        "execution_write_authorized": False,
        "real_money_execution_authorized": False,
        "cross_session_equivalence_proven": False,
        "previous_event_sha256": None,
    }
    payload = {**body, "event_sha256": heartbeat_module._digest(body)}

    with pytest.raises(BetdaqHeartbeatSafetyError, match="provider_return_code"):
        heartbeat_module._event_from_payload(payload)


def test_canonical_integer_and_null_return_codes_remain_valid() -> None:
    for code in (None, 0, 462, -1):
        evidence = HeartbeatProviderEvidence(
            method="DeregisterHeartbeat",
            observed_at="2026-09-23T00:00:00Z",
            response_sha256="d" * 64,
            provider_return_code=code,
            provider_performed_at=None,
            performed_action=None,
        )
        assert evidence.provider_return_code == code
