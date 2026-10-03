"""Compose canonical cycle-bound forward verification into PAPER admission.

This module adds no store, scheduler, observation ledger, execution authority, or
promotion authority.  It re-resolves the existing #1639/#2050 composition immediately
before the existing #708 PREPARED boundary and binds that exact resolver-issued receipt
into #708's already-durable intent and DecisionLedger payload.
"""

from __future__ import annotations

import inspect
from typing import Mapping

from . import campaign_forward_universe_cycle_binding as _forward
from . import paper_campaign_admission as _admission

_GETATTR_STATIC = inspect.getattr_static
_EXPECTED_COORDINATOR = _admission.PaperCampaignAdmissionCoordinator
_EXPECTED_LEGACY_ADMIT = inspect.getattr_static(_EXPECTED_COORDINATOR, "admit")
_EXPECTED_LEGACY_ADMIT_CODE = getattr(_EXPECTED_LEGACY_ADMIT, "__code__", None)
_EXPECTED_CONTEXT = _admission._ACTIVE_FORWARD_VERIFICATION
_EXPECTED_DECISION_FIELD = _admission._FORWARD_VERIFICATION_DECISION_FIELD
_EXPECTED_ACTION_PARAMETER = _admission._FORWARD_VERIFICATION_ACTION_PARAMETER
_EXPECTED_VERIFY = _forward.verify_campaign_forward_evidence
_EXPECTED_VERIFY_CODE = getattr(_EXPECTED_VERIFY, "__code__", None)
_EXPECTED_RECEIPT_TYPE = _forward.CampaignForwardEvidenceVerification
_EXPECTED_SCOPE = "CYCLE_BOUND_PROVIDER_UNIVERSE_STRUCTURAL_ONLY"
_HEX = frozenset("0123456789abcdef")


class PaperCampaignForwardAdmissionError(RuntimeError):
    """Raised when forward verification cannot authorize PAPER campaign admission."""


def _require_surfaces() -> None:
    if (
        inspect.getattr_static is not _GETATTR_STATIC
        or _admission.PaperCampaignAdmissionCoordinator is not _EXPECTED_COORDINATOR
        or _GETATTR_STATIC(_EXPECTED_COORDINATOR, "admit")
        is not _EXPECTED_LEGACY_ADMIT
        or getattr(_EXPECTED_LEGACY_ADMIT, "__code__", None)
        is not _EXPECTED_LEGACY_ADMIT_CODE
        or _admission._ACTIVE_FORWARD_VERIFICATION is not _EXPECTED_CONTEXT
        or _admission._FORWARD_VERIFICATION_DECISION_FIELD
        != _EXPECTED_DECISION_FIELD
        or _admission._FORWARD_VERIFICATION_ACTION_PARAMETER
        != _EXPECTED_ACTION_PARAMETER
        or _forward.verify_campaign_forward_evidence is not _EXPECTED_VERIFY
        or getattr(_EXPECTED_VERIFY, "__code__", None) is not _EXPECTED_VERIFY_CODE
        or _forward.CampaignForwardEvidenceVerification is not _EXPECTED_RECEIPT_TYPE
    ):
        raise PaperCampaignForwardAdmissionError(
            "campaign forward admission authority surface changed"
        )


def _sha(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise PaperCampaignForwardAdmissionError(f"{name} must be lowercase SHA-256")
    return value


def _receipt_payload(receipt: object, *, cycle_receipt: object) -> dict[str, object]:
    if type(receipt) is not _EXPECTED_RECEIPT_TYPE:
        raise PaperCampaignForwardAdmissionError(
            "forward verifier returned noncanonical receipt"
        )
    if type(getattr(cycle_receipt, "campaign_id", None)) is not str:
        raise PaperCampaignForwardAdmissionError(
            "campaign cycle receipt has no canonical campaign identity"
        )
    if receipt.campaign_id != cycle_receipt.campaign_id:
        raise PaperCampaignForwardAdmissionError(
            "forward verification campaign differs from admission campaign"
        )
    if (
        receipt.verification_scope != _EXPECTED_SCOPE
        or receipt.provider_universe_authority_resolved is not True
        or receipt.promotion_ready is not False
        or receipt.real_money_ready is not False
    ):
        raise PaperCampaignForwardAdmissionError(
            "forward verification truth boundary is not canonical"
        )
    if receipt.structural_ok is not True or receipt.structural_codes != ("PASS",):
        raise PaperCampaignForwardAdmissionError(
            "forward structural verification did not pass"
        )
    if type(receipt.candidate_count) is not int or receipt.candidate_count < 0:
        raise PaperCampaignForwardAdmissionError(
            "forward verification candidate count is invalid"
        )
    return {
        "schema": "autosport.paper_campaign_forward_verification",
        "schema_version": 1,
        "campaign_id": receipt.campaign_id,
        "protocol_sha256": _sha(receipt.protocol_sha256, "protocol_sha256"),
        "structural_result_sha256": _sha(
            receipt.structural_result_sha256,
            "structural_result_sha256",
        ),
        "terminal_root_sha256": (
            None
            if receipt.terminal_root_sha256 is None
            else _sha(receipt.terminal_root_sha256, "terminal_root_sha256")
        ),
        "candidate_count": receipt.candidate_count,
        "campaign_cycle_authority_sha256": _sha(
            receipt.campaign_cycle_authority_sha256,
            "campaign_cycle_authority_sha256",
        ),
        "prospective_evaluation_plan_sha256": _sha(
            receipt.prospective_evaluation_plan_sha256,
            "prospective_evaluation_plan_sha256",
        ),
        "universe_sha256": _sha(receipt.universe_sha256, "universe_sha256"),
        "membership_sha256": _sha(receipt.membership_sha256, "membership_sha256"),
        "verification_scope": receipt.verification_scope,
        "provider_universe_authority_resolved": True,
        "structural_ok": True,
        "structural_codes": ["PASS"],
        "promotion_ready": False,
        "real_money_ready": False,
        "receipt_sha256": _sha(receipt.receipt_sha256, "receipt_sha256"),
    }


def _resolve_payload(
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
    _require_surfaces()
    receipt = _EXPECTED_VERIFY(
        precommit_locator=precommit_locator,
        collector_store=collector_store,
        source_spec=source_spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_evidence_store,
        universe_store=universe_store,
        event_lifecycle=event_lifecycle,
        evidence=evidence,
    )
    _require_surfaces()
    return _receipt_payload(receipt, cycle_receipt=cycle_receipt)


def admit_forward_verified(
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
    """Admit only after exact cycle-bound structural authority is re-resolved.

    Legacy PaperCampaignAdmissionCoordinator.admit remains a non-forward PAPER
    composition.  It rejects caller attempts to inject the reserved forward fields.
    """

    _require_surfaces()
    if type(coordinator) is not _EXPECTED_COORDINATOR:
        raise TypeError(
            "coordinator must be exact PaperCampaignAdmissionCoordinator"
        )
    before = _resolve_payload(
        precommit_locator=precommit_locator,
        collector_store=collector_store,
        source_spec=source_spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_evidence_store,
        universe_store=universe_store,
        event_lifecycle=event_lifecycle,
        evidence=evidence,
    )
    # A second pre-PREPARED resolution rejects unstable authority before any #708
    # journal mutation or economic/learning admission side effect can begin.
    stable = _resolve_payload(
        precommit_locator=precommit_locator,
        collector_store=collector_store,
        source_spec=source_spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_evidence_store,
        universe_store=universe_store,
        event_lifecycle=event_lifecycle,
        evidence=evidence,
    )
    if stable != before:
        raise PaperCampaignForwardAdmissionError(
            "campaign forward verification changed before admission"
        )

    context = _EXPECTED_CONTEXT
    legacy_admit = _EXPECTED_LEGACY_ADMIT
    token = context.set(dict(before))
    try:
        result = legacy_admit(
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
    finally:
        context.reset(token)

    after = _resolve_payload(
        precommit_locator=precommit_locator,
        collector_store=collector_store,
        source_spec=source_spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_evidence_store,
        universe_store=universe_store,
        event_lifecycle=event_lifecycle,
        evidence=evidence,
    )
    if after != before:
        raise PaperCampaignForwardAdmissionError(
            "campaign forward verification changed during admission"
        )
    return result


def _seal_public_forward_admission():
    module_globals = globals()
    raw_admit = admit_forward_verified
    expected_error = PaperCampaignForwardAdmissionError
    expected_globals = (
        ("inspect", inspect),
        ("_GETATTR_STATIC", _GETATTR_STATIC),
        ("_EXPECTED_COORDINATOR", _EXPECTED_COORDINATOR),
        ("_EXPECTED_LEGACY_ADMIT", _EXPECTED_LEGACY_ADMIT),
        ("_EXPECTED_LEGACY_ADMIT_CODE", _EXPECTED_LEGACY_ADMIT_CODE),
        ("_EXPECTED_CONTEXT", _EXPECTED_CONTEXT),
        ("_EXPECTED_DECISION_FIELD", _EXPECTED_DECISION_FIELD),
        ("_EXPECTED_ACTION_PARAMETER", _EXPECTED_ACTION_PARAMETER),
        ("_EXPECTED_VERIFY", _EXPECTED_VERIFY),
        ("_EXPECTED_VERIFY_CODE", _EXPECTED_VERIFY_CODE),
        ("_EXPECTED_RECEIPT_TYPE", _EXPECTED_RECEIPT_TYPE),
        ("_EXPECTED_SCOPE", _EXPECTED_SCOPE),
        ("_HEX", _HEX),
        ("_require_surfaces", _require_surfaces),
        ("_sha", _sha),
        ("_receipt_payload", _receipt_payload),
        ("_resolve_payload", _resolve_payload),
    )
    expected_codes = tuple(
        (
            name,
            target,
            getattr(target, "__code__", None),
        )
        for name, target in (
            ("raw_admit", raw_admit),
            ("_require_surfaces", _require_surfaces),
            ("_sha", _sha),
            ("_receipt_payload", _receipt_payload),
            ("_resolve_payload", _resolve_payload),
        )
    )
    holder: dict[str, object] = {}

    def require_guard_integrity() -> None:
        sealed = holder["sealed"]
        if module_globals.get("admit_forward_verified") is not sealed:
            raise expected_error(
                "campaign forward admission public surface changed"
            )
        for name, expected in expected_globals:
            if module_globals.get(name) is not expected:
                raise expected_error(
                    "campaign forward admission guard internals changed"
                )
        for _name, target, code in expected_codes:
            if getattr(target, "__code__", None) is not code:
                raise expected_error(
                    "campaign forward admission guard code changed"
                )

    def sealed(*args, **kwargs):
        require_guard_integrity()
        result = raw_admit(*args, **kwargs)
        require_guard_integrity()
        return result

    holder["sealed"] = sealed
    return sealed


admit_forward_verified = _seal_public_forward_admission()
del _seal_public_forward_admission


__all__ = [
    "PaperCampaignForwardAdmissionError",
    "admit_forward_verified",
]
