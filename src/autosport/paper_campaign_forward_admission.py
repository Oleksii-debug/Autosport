"""Compose canonical cycle-bound forward verification into PAPER admission.

No second campaign store, scheduler, observation ledger, execution authority, or
promotion authority is introduced here. The canonical #1639 verification is
re-resolved before #708 PREPARED; its resolver-issued identity is carried by the
existing #708 intent/DecisionLedger contract.
"""

from __future__ import annotations

import inspect
from typing import Mapping

from . import campaign_forward_universe_cycle_binding as _forward
from . import paper_campaign_admission as _admission


class PaperCampaignForwardAdmissionError(RuntimeError):
    """Raised when forward verification cannot authorize PAPER campaign admission."""


def _build_sealed_forward_admission():
    module_globals = globals()
    inspect_module = inspect
    getattr_static = inspect.getattr_static
    forward_module = _forward
    admission_module = _admission
    expected_error = PaperCampaignForwardAdmissionError

    coordinator_type = admission_module.PaperCampaignAdmissionCoordinator
    legacy_admit = getattr_static(coordinator_type, "admit")
    legacy_admit_code = getattr(legacy_admit, "__code__", None)
    forward_context_reader = admission_module._CURRENT_FORWARD_VERIFICATION
    forward_context_reader_code = getattr(
        forward_context_reader,
        "__code__",
        None,
    )
    installer = admission_module._install_forward_verification_runner
    installer_code = getattr(installer, "__code__", None)
    run_with_forward = None
    run_with_forward_code = None
    decision_field = admission_module._FORWARD_VERIFICATION_DECISION_FIELD
    action_parameter = admission_module._FORWARD_VERIFICATION_ACTION_PARAMETER

    verify = forward_module.verify_campaign_forward_evidence
    verify_code = getattr(verify, "__code__", None)
    receipt_type = forward_module.CampaignForwardEvidenceVerification
    cycle_receipt_type = forward_module.CampaignCompleteBoardCycleReceipt
    expected_scope = "CYCLE_BOUND_PROVIDER_UNIVERSE_STRUCTURAL_ONLY"

    exact_type = type
    exact_dict = dict
    exact_str = str
    exact_int = int
    exact_len = len
    exact_any = any
    exact_getattr = getattr
    exact_tuple = tuple
    exact_type_error = TypeError

    receipt_field_names = (
        "schema_version",
        "campaign_id",
        "source_id",
        "campaign_receipt_sha256",
        "protocol_sha256",
        "structural_result_sha256",
        "structural_ok",
        "structural_codes",
        "terminal_root_sha256",
        "candidate_count",
        "campaign_cycle_authority_sha256",
        "prospective_evaluation_plan_sha256",
        "universe_sha256",
        "membership_sha256",
        "verification_scope",
        "provider_universe_authority_resolved",
        "promotion_ready",
        "real_money_ready",
        "receipt_sha256",
    )
    receipt_field_descriptors = exact_tuple(
        (name, getattr_static(receipt_type, name))
        for name in receipt_field_names
    )
    cycle_identity_field_names = (
        "campaign_id",
        "source_id",
        "campaign_receipt_sha256",
    )
    cycle_identity_field_descriptors = exact_tuple(
        (name, getattr_static(cycle_receipt_type, name))
        for name in cycle_identity_field_names
    )

    hex_chars = frozenset("0123456789abcdef")
    unshadowed_builtins = (
        "type",
        "dict",
        "str",
        "int",
        "len",
        "any",
        "getattr",
        "tuple",
        "TypeError",
    )
    holder: dict[str, object] = {}

    def require_surfaces() -> None:
        if exact_any(name in module_globals for name in unshadowed_builtins):
            raise expected_error(
                "campaign forward admission builtin dispatch shadowed"
            )
        if (
            module_globals.get("inspect") is not inspect_module
            or inspect_module.getattr_static is not getattr_static
            or module_globals.get("_forward") is not forward_module
            or module_globals.get("_admission") is not admission_module
            or module_globals.get("PaperCampaignForwardAdmissionError")
            is not expected_error
            or admission_module.PaperCampaignAdmissionCoordinator
            is not coordinator_type
            or getattr_static(coordinator_type, "admit") is not legacy_admit
            or exact_getattr(legacy_admit, "__code__", None)
            is not legacy_admit_code
            or admission_module._CURRENT_FORWARD_VERIFICATION
            is not forward_context_reader
            or exact_getattr(forward_context_reader, "__code__", None)
            is not forward_context_reader_code
            or exact_getattr(installer, "__code__", None)
            is not installer_code
            or exact_getattr(resolve_request, "__code__", None)
            is not resolve_request_code
            or exact_getattr(run_with_forward, "__code__", None)
            is not run_with_forward_code
            or exact_getattr(
                admission_module,
                "_install_forward_verification_runner",
                None,
            )
            is not None
            or admission_module._FORWARD_VERIFICATION_DECISION_FIELD
            != decision_field
            or admission_module._FORWARD_VERIFICATION_ACTION_PARAMETER
            != action_parameter
            or forward_module.verify_campaign_forward_evidence is not verify
            or exact_getattr(verify, "__code__", None) is not verify_code
            or forward_module.CampaignForwardEvidenceVerification
            is not receipt_type
            or forward_module.CampaignCompleteBoardCycleReceipt
            is not cycle_receipt_type
            or exact_any(
                getattr_static(receipt_type, name) is not descriptor
                for name, descriptor in receipt_field_descriptors
            )
            or exact_any(
                getattr_static(cycle_receipt_type, name) is not descriptor
                for name, descriptor in cycle_identity_field_descriptors
            )
        ):
            raise expected_error(
                "campaign forward admission authority surface changed"
            )

    def read_stored_fields(
        value: object,
        *,
        expected_type: type,
        descriptors: tuple[tuple[str, object], ...],
        label: str,
    ) -> dict[str, object]:
        if exact_type(value) is not expected_type:
            raise expected_error(f"{label} has noncanonical type")
        require_surfaces()
        state = exact_dict()
        for name, descriptor in descriptors:
            if getattr_static(expected_type, name) is not descriptor:
                raise expected_error(
                    "campaign forward admission authority surface changed"
                )
            state[name] = descriptor.__get__(value, expected_type)
        require_surfaces()
        return state

    def sha(value: object, name: str) -> str:
        if (
            exact_type(value) is not exact_str
            or exact_len(value) != 64
            or exact_any(character not in hex_chars for character in value)
        ):
            raise expected_error(f"{name} must be lowercase SHA-256")
        return value

    def receipt_payload(receipt: object, *, cycle_receipt: object) -> dict[str, object]:
        cycle_state = read_stored_fields(
            cycle_receipt,
            expected_type=cycle_receipt_type,
            descriptors=cycle_identity_field_descriptors,
            label="campaign cycle receipt",
        )
        receipt_state = read_stored_fields(
            receipt,
            expected_type=receipt_type,
            descriptors=receipt_field_descriptors,
            label="forward verifier receipt",
        )

        if (
            exact_type(receipt_state["schema_version"]) is not exact_int
            or receipt_state["schema_version"] != 1
        ):
            raise expected_error(
                "forward verification schema version is not canonical"
            )
        campaign_id = cycle_state["campaign_id"]
        if exact_type(campaign_id) is not exact_str:
            raise expected_error(
                "campaign cycle receipt has no canonical campaign identity"
            )
        source_id = cycle_state["source_id"]
        campaign_receipt_sha256 = cycle_state["campaign_receipt_sha256"]
        if receipt_state["campaign_id"] != campaign_id:
            raise expected_error(
                "forward verification campaign differs from admission campaign"
            )
        if (
            exact_type(source_id) is not exact_str
            or receipt_state["source_id"] != source_id
        ):
            raise expected_error(
                "forward verification source differs from admission campaign cycle"
            )
        if (
            exact_type(campaign_receipt_sha256) is not exact_str
            or receipt_state["campaign_receipt_sha256"]
            != campaign_receipt_sha256
        ):
            raise expected_error(
                "forward verification inception receipt differs from campaign cycle"
            )
        if (
            receipt_state["verification_scope"] != expected_scope
            or receipt_state["provider_universe_authority_resolved"] is not True
            or receipt_state["promotion_ready"] is not False
            or receipt_state["real_money_ready"] is not False
        ):
            raise expected_error(
                "forward verification truth boundary is not canonical"
            )
        if (
            receipt_state["structural_ok"] is not True
            or exact_type(receipt_state["structural_codes"]) is not exact_tuple
            or receipt_state["structural_codes"] != ("PASS",)
        ):
            raise expected_error(
                "forward structural verification did not pass"
            )
        if (
            exact_type(receipt_state["candidate_count"]) is not exact_int
            or receipt_state["candidate_count"] < 0
        ):
            raise expected_error(
                "forward verification candidate count is invalid"
            )

        terminal_root_sha256 = receipt_state["terminal_root_sha256"]
        return {
            "schema": "autosport.paper_campaign_forward_verification",
            "schema_version": 1,
            "campaign_id": receipt_state["campaign_id"],
            "source_id": receipt_state["source_id"],
            "campaign_receipt_sha256": sha(
                receipt_state["campaign_receipt_sha256"],
                "campaign_receipt_sha256",
            ),
            "protocol_sha256": sha(
                receipt_state["protocol_sha256"],
                "protocol_sha256",
            ),
            "structural_result_sha256": sha(
                receipt_state["structural_result_sha256"],
                "structural_result_sha256",
            ),
            "terminal_root_sha256": (
                None
                if terminal_root_sha256 is None
                else sha(terminal_root_sha256, "terminal_root_sha256")
            ),
            "candidate_count": receipt_state["candidate_count"],
            "campaign_cycle_authority_sha256": sha(
                receipt_state["campaign_cycle_authority_sha256"],
                "campaign_cycle_authority_sha256",
            ),
            "prospective_evaluation_plan_sha256": sha(
                receipt_state["prospective_evaluation_plan_sha256"],
                "prospective_evaluation_plan_sha256",
            ),
            "universe_sha256": sha(
                receipt_state["universe_sha256"],
                "universe_sha256",
            ),
            "membership_sha256": sha(
                receipt_state["membership_sha256"],
                "membership_sha256",
            ),
            "verification_scope": receipt_state["verification_scope"],
            "provider_universe_authority_resolved": True,
            "structural_ok": True,
            "structural_codes": ["PASS"],
            "promotion_ready": False,
            "real_money_ready": False,
            "receipt_sha256": sha(
                receipt_state["receipt_sha256"],
                "receipt_sha256",
            ),
        }

    def resolve_payload(
        *,
        precommit_locator: object,
        collector_store: object,
        source_spec: object,
        cycle_receipt: object,
        provider_evidence_store: object,
        universe_store: object,
        event_lifecycle: object,
        evidence: object,
    ) -> dict[str, object]:
        require_surfaces()
        receipt = verify(
            precommit_locator=precommit_locator,
            collector_store=collector_store,
            source_spec=source_spec,
            cycle_receipt=cycle_receipt,
            provider_evidence_store=provider_evidence_store,
            universe_store=universe_store,
            event_lifecycle=event_lifecycle,
            evidence=evidence,
        )
        require_surfaces()
        return receipt_payload(receipt, cycle_receipt=cycle_receipt)

    def resolve_request(request: object) -> dict[str, object]:
        if exact_type(request) is not exact_tuple or exact_len(request) != 8:
            raise expected_error(
                "campaign forward admission request must be exact authority tuple"
            )
        return resolve_payload(
            precommit_locator=request[0],
            collector_store=request[1],
            source_spec=request[2],
            cycle_receipt=request[3],
            provider_evidence_store=request[4],
            universe_store=request[5],
            event_lifecycle=request[6],
            evidence=request[7],
        )

    resolve_request_code = exact_getattr(resolve_request, "__code__", None)
    run_with_forward = installer(resolve_request)
    run_with_forward_code = exact_getattr(run_with_forward, "__code__", None)
    delattr(admission_module, "_install_forward_verification_runner")

    def raw_admit_forward_verified(
        coordinator: object,
        *,
        precommit_locator: object,
        collector_store: object,
        source_spec: object,
        cycle_receipt: object,
        provider_evidence_store: object,
        universe_store: object,
        event_lifecycle: object,
        evidence: object,
        admission_id: str,
        observation: object,
        action_type: str,
        decision_action: str,
        decision_at: str,
        at: str,
        replay_run_id: str,
        agent: str,
        execution_decision_id: str,
        execution_run_id: str,
        execution_attempt_id: str,
        execution_ticket_id: str,
        strategy_reason: str = "",
        decision_payload: Mapping[str, object] | None = None,
        action_parameters: tuple[tuple[str, str], ...] = (),
    ):
        require_surfaces()
        if exact_type(coordinator) is not coordinator_type:
            raise exact_type_error(
                "coordinator must be exact PaperCampaignAdmissionCoordinator"
            )

        request = (
            precommit_locator,
            collector_store,
            source_spec,
            cycle_receipt,
            provider_evidence_store,
            universe_store,
            event_lifecycle,
            evidence,
        )

        def invoke_admission():
            return legacy_admit(
                coordinator,
                admission_id=admission_id,
                observation=observation,
                action_type=action_type,
                decision_action=decision_action,
                decision_at=decision_at,
                at=at,
                replay_run_id=replay_run_id,
                agent=agent,
                execution_decision_id=execution_decision_id,
                execution_run_id=execution_run_id,
                execution_attempt_id=execution_attempt_id,
                execution_ticket_id=execution_ticket_id,
                strategy_reason=strategy_reason,
                decision_payload=decision_payload,
                action_parameters=action_parameters,
            )

        return run_with_forward(request, invoke_admission)

    def sealed_admit_forward_verified(*args, **kwargs):
        sealed = holder["sealed"]
        if module_globals.get("admit_forward_verified") is not sealed:
            raise expected_error(
                "campaign forward admission public surface changed"
            )
        require_surfaces()
        result = raw_admit_forward_verified(*args, **kwargs)
        require_surfaces()
        return result

    holder["sealed"] = sealed_admit_forward_verified
    return sealed_admit_forward_verified


admit_forward_verified = _build_sealed_forward_admission()
del _build_sealed_forward_admission


__all__ = [
    "PaperCampaignForwardAdmissionError",
    "admit_forward_verified",
]
