from __future__ import annotations

from threading import local

from .real_execution_ledger import (
    EventType,
    ExecutionLedgerIntegrityError,
    ExecutionStateError,
    RealExecutionLedger,
    VerifiedProviderEffectBindingView,
)
from .supervised_provider_evidence import (
    ProviderEvidenceError,
    VerifiedProviderEffectEvidence,
    assert_verified_provider_evidence_authoritative,
)


def _install_verified_provider_effect_ledger_mint_fence() -> None:
    """Allow VERIFIED_PROVIDER_EFFECT_BOUND only through the canonical verifier writer.

    The ledger intentionally exposes low-level append machinery to its own composition
    layers. That machinery must not become a second mint for provider-origin accepted
    economics: a raw, schema-valid event is durable evidence bytes, not proof that the
    authenticated Betfair readback verifier issued those economics.
    """

    ledger_type = RealExecutionLedger
    raw_append = ledger_type._append
    raw_writer = ledger_type._bind_verified_provider_effect_evidence
    evidence_type = VerifiedProviderEffectEvidence
    binding_type = VerifiedProviderEffectBindingView
    verify = assert_verified_provider_evidence_authoritative
    provider_error = ProviderEvidenceError
    integrity_error = ExecutionLedgerIntegrityError
    state_error = ExecutionStateError
    verified_kind = EventType.VERIFIED_PROVIDER_EFFECT_BOUND
    gate = local()

    def _expected_payload(evidence: VerifiedProviderEffectEvidence) -> dict[str, object]:
        return binding_type(
            evidence_id=evidence.evidence_id,
            observed_at=evidence.observed_at,
            source_payload_sha256=evidence.source_payload_sha256,
            external_receipt_id=evidence.external_receipt_id,
            status=evidence.status,
            accepted_odds=evidence.accepted_odds,
            accepted_stake=evidence.accepted_stake,
            provider_order_ref=evidence.provider_order_ref,
        ).to_dict()

    def guarded_append(
        self: RealExecutionLedger,
        kind: EventType,
        plan_id: str,
        action_id: str | None,
        attempt_id: str | None,
        payload: dict[str, object],
    ) -> None:
        if kind is verified_kind:
            active = getattr(gate, "active", None)
            if active is None:
                raise integrity_error(
                    "verified provider effect requires canonical origin-verifying ledger writer"
                )
            active_ledger, active_attempt_id, evidence, expected_payload = active
            if (
                self is not active_ledger
                or attempt_id != active_attempt_id
                or type(evidence) is not evidence_type
                or type(payload) is not dict
                or payload != expected_payload
            ):
                raise integrity_error(
                    "verified provider effect raw append conflicts with canonical writer grant"
                )
            try:
                verify(evidence)
            except provider_error as exc:
                raise integrity_error(
                    "verified provider effect lost canonical origin authority before append"
                ) from exc
        raw_append(self, kind, plan_id, action_id, attempt_id, payload)

    def guarded_writer(
        self: RealExecutionLedger,
        *,
        attempt_id: str,
        evidence: object,
    ) -> None:
        if type(self) is not ledger_type:
            raise TypeError("verified provider effect writer requires canonical RealExecutionLedger")
        if type(evidence) is not evidence_type:
            raise TypeError("evidence must be exact VerifiedProviderEffectEvidence")
        try:
            verify(evidence)
        except provider_error as exc:
            raise state_error(
                "verified provider effect evidence is not authoritative"
            ) from exc
        if getattr(gate, "active", None) is not None:
            raise integrity_error("verified provider effect writer grant is already active")

        expected_payload = _expected_payload(evidence)
        gate.active = (self, attempt_id, evidence, expected_payload)
        try:
            raw_writer(self, attempt_id=attempt_id, evidence=evidence)
            try:
                verify(evidence)
            except provider_error as exc:
                raise integrity_error(
                    "verified provider effect lost canonical origin authority during write"
                ) from exc
        finally:
            gate.active = None

    # Install both ends together. The original writer dispatches through self._append,
    # so every legitimate provider-effect write crosses the closure-local grant while
    # a direct caller of _append has no reachable object with which to mint that grant.
    ledger_type._append = guarded_append
    ledger_type._bind_verified_provider_effect_evidence = guarded_writer


_install_verified_provider_effect_ledger_mint_fence()
del _install_verified_provider_effect_ledger_mint_fence
