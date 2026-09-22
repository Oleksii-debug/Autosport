"""Fail-closed provider maximum payout / return-cap evidence.

This module owns only the typed shape and deterministic arithmetic for one scoped
maximum payout/return constraint. It deliberately does not own provider
acquisition, balances, settlement, owner risk policy, execution, or portfolio
classification.

Caller-constructible evidence may be structurally sealed in-process so exact
scope, time, Decimal, typed cap semantics, headroom, and copy/restart misuse can
be checked deterministically. That structural seal is NOT provider-origin
proof. Positive provider authority remains fail closed until an independent
product-owned authenticated acquisition or versioned-rule issuer is composed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json


class ProviderPayoutCapError(ValueError):
    """Malformed, mismatched, stale, or non-authoritative payout-cap evidence."""


class ProviderPayoutCapMeasure(str, Enum):
    """Provider-defined monetary quantity to which the cap applies."""

    GROSS_RETURN = "GROSS_RETURN"
    NET_WINNINGS = "NET_WINNINGS"


class ProviderPayoutCapScope(str, Enum):
    """Scope over which a provider-defined maximum is accumulated."""

    PER_BET = "PER_BET"
    PER_EVENT = "PER_EVENT"
    PER_MARKET = "PER_MARKET"
    PER_COMPETITION = "PER_COMPETITION"
    PER_SPORT = "PER_SPORT"
    PER_DAY = "PER_DAY"
    PER_ACCOUNT = "PER_ACCOUNT"


class ProviderPayoutCapSourceKind(str, Enum):
    """Origin class asserted by the structural record, not origin proof."""

    AUTHENTICATED_PROVIDER_READBACK = "AUTHENTICATED_PROVIDER_READBACK"
    AUTHENTICATED_ACTION_QUOTE = "AUTHENTICATED_ACTION_QUOTE"
    VERSIONED_PROVIDER_RULE = "VERSIONED_PROVIDER_RULE"


class ProviderPayoutCapAssessmentState(str, Enum):
    """Structural arithmetic result, never execution authority."""

    WITHIN_STRUCTURAL_CAP = "WITHIN_STRUCTURAL_CAP"
    EXCEEDS_STRUCTURAL_CAP = "EXCEEDS_STRUCTURAL_CAP"
    HEADROOM_UNKNOWN = "HEADROOM_UNKNOWN"


@dataclass(frozen=True, slots=True)
class ProviderPayoutCapEvidence:
    """One exact, typed payout-cap assertion.

    scope_binding_sha256 is the canonical identity of the provider-defined
    scope. This generic contract does not guess provider-specific scope ids.

    For cumulative scopes, current_headroom is optional because many providers
    do not expose enough current consumption state to prove it. Missing current
    headroom therefore remains explicitly unknown.

    settlement_rule_sha256 binds the exact settlement/cost-order semantics
    needed by downstream economics. This module does not apply those semantics.
    """

    provider_id: str
    account_id: str
    adapter_id: str
    jurisdiction: str
    currency: str
    scope_kind: ProviderPayoutCapScope
    cap_measure: ProviderPayoutCapMeasure
    maximum_amount: Decimal
    scope_binding_sha256: str
    settlement_rule_sha256: str
    rule_version: str
    observed_at: datetime
    valid_until: datetime
    source_kind: ProviderPayoutCapSourceKind
    source_ref: str
    source_payload_sha256: str
    current_headroom: Decimal | None = None
    headroom_evidence_sha256: str | None = None
    action_binding_sha256: str | None = None
    execution_authority: bool = False

    def __post_init__(self) -> None:
        for name in (
            "provider_id",
            "account_id",
            "adapter_id",
            "jurisdiction",
            "currency",
            "rule_version",
            "source_ref",
        ):
            _text(getattr(self, name), name)

        if type(self.scope_kind) is not ProviderPayoutCapScope:
            raise ProviderPayoutCapError(
                "scope_kind must be exact ProviderPayoutCapScope"
            )
        if type(self.cap_measure) is not ProviderPayoutCapMeasure:
            raise ProviderPayoutCapError(
                "cap_measure must be exact ProviderPayoutCapMeasure"
            )
        if type(self.source_kind) is not ProviderPayoutCapSourceKind:
            raise ProviderPayoutCapError(
                "source_kind must be exact ProviderPayoutCapSourceKind"
            )

        _positive_decimal(self.maximum_amount, "maximum_amount")
        _sha256(self.scope_binding_sha256, "scope_binding_sha256")
        _sha256(self.settlement_rule_sha256, "settlement_rule_sha256")
        _sha256(self.source_payload_sha256, "source_payload_sha256")

        observed = _utc(self.observed_at, "observed_at")
        valid_until = _utc(self.valid_until, "valid_until")
        if valid_until <= observed:
            raise ProviderPayoutCapError(
                "valid_until must be strictly after observed_at"
            )

        if self.current_headroom is None:
            if self.headroom_evidence_sha256 is not None:
                raise ProviderPayoutCapError(
                    "headroom_evidence_sha256 requires current_headroom"
                )
        else:
            headroom = _nonnegative_decimal(
                self.current_headroom, "current_headroom"
            )
            if headroom > self.maximum_amount:
                raise ProviderPayoutCapError(
                    "current_headroom cannot exceed maximum_amount"
                )
            if self.scope_kind is ProviderPayoutCapScope.PER_BET:
                raise ProviderPayoutCapError(
                    "PER_BET caps use maximum_amount directly and must not "
                    "carry cumulative current_headroom"
                )
            if self.headroom_evidence_sha256 is None:
                raise ProviderPayoutCapError(
                    "current_headroom requires headroom_evidence_sha256"
                )
            _sha256(
                self.headroom_evidence_sha256,
                "headroom_evidence_sha256",
            )

        if self.action_binding_sha256 is not None:
            _sha256(self.action_binding_sha256, "action_binding_sha256")
        if (
            self.source_kind
            is ProviderPayoutCapSourceKind.AUTHENTICATED_ACTION_QUOTE
            and self.action_binding_sha256 is None
        ):
            raise ProviderPayoutCapError(
                "authenticated action quote requires action_binding_sha256"
            )

        if self.execution_authority is not False:
            raise ProviderPayoutCapError(
                "payout-cap evidence never grants execution authority"
            )

    @property
    def evidence_sha256(self) -> str:
        return _fingerprint(self)


@dataclass(frozen=True, slots=True)
class ProviderPayoutCapAssessment:
    """Deterministic arithmetic over structurally sealed evidence."""

    state: ProviderPayoutCapAssessmentState
    candidate_amount: Decimal
    maximum_amount: Decimal
    effective_headroom: Decimal | None
    cap_measure: ProviderPayoutCapMeasure
    scope_kind: ProviderPayoutCapScope
    evidence_sha256: str
    reason: str
    execution_authority: bool = False

    def __post_init__(self) -> None:
        if type(self.state) is not ProviderPayoutCapAssessmentState:
            raise ProviderPayoutCapError(
                "state must be exact ProviderPayoutCapAssessmentState"
            )
        _nonnegative_decimal(self.candidate_amount, "candidate_amount")
        _positive_decimal(self.maximum_amount, "maximum_amount")
        if self.effective_headroom is not None:
            _nonnegative_decimal(
                self.effective_headroom, "effective_headroom"
            )
        if type(self.cap_measure) is not ProviderPayoutCapMeasure:
            raise ProviderPayoutCapError(
                "cap_measure must be exact ProviderPayoutCapMeasure"
            )
        if type(self.scope_kind) is not ProviderPayoutCapScope:
            raise ProviderPayoutCapError(
                "scope_kind must be exact ProviderPayoutCapScope"
            )
        _sha256(self.evidence_sha256, "evidence_sha256")
        _text(self.reason, "reason")
        if self.execution_authority is not False:
            raise ProviderPayoutCapError(
                "payout-cap assessment never grants execution authority"
            )

    @property
    def provider_origin_proven(self) -> bool:
        return False

    @property
    def supports_exact_execution(self) -> bool:
        return False

    @property
    def no_cap_proven(self) -> bool:
        """Missing or structural evidence never proves unlimited payout."""

        return False


def _install_structural_authority() -> None:
    sealed: dict[int, tuple[ProviderPayoutCapEvidence, str]] = {}

    def _validate_scope_and_time(
        evidence: ProviderPayoutCapEvidence,
        *,
        as_of: datetime,
        provider_id: str,
        account_id: str,
        adapter_id: str,
        jurisdiction: str,
        currency: str,
        scope_kind: ProviderPayoutCapScope,
        cap_measure: ProviderPayoutCapMeasure,
        scope_binding_sha256: str,
        settlement_rule_sha256: str,
        action_binding_sha256: str | None,
    ) -> str:
        if type(evidence) is not ProviderPayoutCapEvidence:
            raise ProviderPayoutCapError(
                "evidence must be exact ProviderPayoutCapEvidence"
            )

        current = _utc(as_of, "as_of")
        expected_text = {
            "provider_id": provider_id,
            "account_id": account_id,
            "adapter_id": adapter_id,
            "jurisdiction": jurisdiction,
            "currency": currency,
        }
        for name, expected in expected_text.items():
            _text(expected, f"expected {name}")
            if getattr(evidence, name) != expected:
                raise ProviderPayoutCapError(f"{name} scope mismatch")

        if type(scope_kind) is not ProviderPayoutCapScope:
            raise ProviderPayoutCapError(
                "expected scope_kind must be exact ProviderPayoutCapScope"
            )
        if evidence.scope_kind is not scope_kind:
            raise ProviderPayoutCapError("scope_kind mismatch")

        if type(cap_measure) is not ProviderPayoutCapMeasure:
            raise ProviderPayoutCapError(
                "expected cap_measure must be exact ProviderPayoutCapMeasure"
            )
        if evidence.cap_measure is not cap_measure:
            raise ProviderPayoutCapError("cap_measure mismatch")

        _sha256(scope_binding_sha256, "expected scope_binding_sha256")
        if evidence.scope_binding_sha256 != scope_binding_sha256:
            raise ProviderPayoutCapError("scope binding mismatch")

        _sha256(
            settlement_rule_sha256,
            "expected settlement_rule_sha256",
        )
        if evidence.settlement_rule_sha256 != settlement_rule_sha256:
            raise ProviderPayoutCapError("settlement rule mismatch")

        if action_binding_sha256 is not None:
            _sha256(
                action_binding_sha256,
                "expected action_binding_sha256",
            )
        if evidence.action_binding_sha256 != action_binding_sha256:
            raise ProviderPayoutCapError("action binding mismatch")

        observed = _utc(evidence.observed_at, "observed_at")
        valid_until = _utc(evidence.valid_until, "valid_until")
        if observed > current:
            raise ProviderPayoutCapError("payout-cap evidence is future")
        if current >= valid_until:
            raise ProviderPayoutCapError("payout-cap evidence is expired")

        return _fingerprint(evidence)

    def seal_provider_payout_cap_structure(
        evidence: ProviderPayoutCapEvidence,
        *,
        as_of: datetime,
        provider_id: str,
        account_id: str,
        adapter_id: str,
        jurisdiction: str,
        currency: str,
        scope_kind: ProviderPayoutCapScope,
        cap_measure: ProviderPayoutCapMeasure,
        scope_binding_sha256: str,
        settlement_rule_sha256: str,
        action_binding_sha256: str | None = None,
    ) -> ProviderPayoutCapEvidence:
        fingerprint = _validate_scope_and_time(
            evidence,
            as_of=as_of,
            provider_id=provider_id,
            account_id=account_id,
            adapter_id=adapter_id,
            jurisdiction=jurisdiction,
            currency=currency,
            scope_kind=scope_kind,
            cap_measure=cap_measure,
            scope_binding_sha256=scope_binding_sha256,
            settlement_rule_sha256=settlement_rule_sha256,
            action_binding_sha256=action_binding_sha256,
        )
        sealed[id(evidence)] = (evidence, fingerprint)
        return evidence

    def assert_provider_payout_cap_structure_sealed(
        evidence: ProviderPayoutCapEvidence,
    ) -> None:
        if type(evidence) is not ProviderPayoutCapEvidence:
            raise ProviderPayoutCapError(
                "evidence must be exact ProviderPayoutCapEvidence"
            )
        record = sealed.get(id(evidence))
        if record is None or record[0] is not evidence:
            raise ProviderPayoutCapError(
                "payout-cap evidence lacks structural seal"
            )
        if record[1] != _fingerprint(evidence):
            raise ProviderPayoutCapError(
                "payout-cap evidence changed after structural sealing"
            )

    def verify_provider_payout_cap_evidence(
        evidence: ProviderPayoutCapEvidence,
        *,
        as_of: datetime,
        provider_id: str,
        account_id: str,
        adapter_id: str,
        jurisdiction: str,
        currency: str,
        scope_kind: ProviderPayoutCapScope,
        cap_measure: ProviderPayoutCapMeasure,
        scope_binding_sha256: str,
        settlement_rule_sha256: str,
        action_binding_sha256: str | None = None,
    ) -> ProviderPayoutCapEvidence:
        _validate_scope_and_time(
            evidence,
            as_of=as_of,
            provider_id=provider_id,
            account_id=account_id,
            adapter_id=adapter_id,
            jurisdiction=jurisdiction,
            currency=currency,
            scope_kind=scope_kind,
            cap_measure=cap_measure,
            scope_binding_sha256=scope_binding_sha256,
            settlement_rule_sha256=settlement_rule_sha256,
            action_binding_sha256=action_binding_sha256,
        )
        raise ProviderPayoutCapError(
            "provider-origin verification requires independent product-owned "
            "authenticated acquisition or versioned-rule authority"
        )

    def assert_provider_payout_cap_evidence_authoritative(
        evidence: ProviderPayoutCapEvidence,
    ) -> None:
        if type(evidence) is not ProviderPayoutCapEvidence:
            raise ProviderPayoutCapError(
                "evidence must be exact ProviderPayoutCapEvidence"
            )
        raise ProviderPayoutCapError(
            "provider payout-cap origin authority is not proven"
        )

    globals()["seal_provider_payout_cap_structure"] = (
        seal_provider_payout_cap_structure
    )
    globals()["assert_provider_payout_cap_structure_sealed"] = (
        assert_provider_payout_cap_structure_sealed
    )
    globals()["verify_provider_payout_cap_evidence"] = (
        verify_provider_payout_cap_evidence
    )
    globals()["assert_provider_payout_cap_evidence_authoritative"] = (
        assert_provider_payout_cap_evidence_authoritative
    )


_install_structural_authority()
del _install_structural_authority


def assess_provider_payout_cap(
    *,
    evidence: ProviderPayoutCapEvidence,
    candidate_amount: Decimal,
) -> ProviderPayoutCapAssessment:
    """Compare one provider-semantic amount with a sealed structural cap.

    candidate_amount must already use the exact provider-defined measure named
    by cap_measure. This function does not convert gross return to winnings,
    apply commission or tax, or guess settlement ordering.

    For cumulative scopes, a nominal ceiling without current headroom is not
    enough even for structural within/exceeds arithmetic.
    """

    assert_provider_payout_cap_structure_sealed(evidence)
    candidate = _nonnegative_decimal(
        candidate_amount, "candidate_amount"
    )

    if evidence.scope_kind is ProviderPayoutCapScope.PER_BET:
        effective_headroom = evidence.maximum_amount
    elif evidence.current_headroom is None:
        return ProviderPayoutCapAssessment(
            state=ProviderPayoutCapAssessmentState.HEADROOM_UNKNOWN,
            candidate_amount=candidate,
            maximum_amount=evidence.maximum_amount,
            effective_headroom=None,
            cap_measure=evidence.cap_measure,
            scope_kind=evidence.scope_kind,
            evidence_sha256=evidence.evidence_sha256,
            reason="cumulative_scope_current_headroom_unproven",
        )
    else:
        effective_headroom = evidence.current_headroom

    if candidate > effective_headroom:
        state = ProviderPayoutCapAssessmentState.EXCEEDS_STRUCTURAL_CAP
        reason = "candidate_amount_exceeds_structural_headroom_only"
    else:
        state = ProviderPayoutCapAssessmentState.WITHIN_STRUCTURAL_CAP
        reason = "candidate_amount_within_structural_headroom_only"

    return ProviderPayoutCapAssessment(
        state=state,
        candidate_amount=candidate,
        maximum_amount=evidence.maximum_amount,
        effective_headroom=effective_headroom,
        cap_measure=evidence.cap_measure,
        scope_kind=evidence.scope_kind,
        evidence_sha256=evidence.evidence_sha256,
        reason=reason,
    )


def _fingerprint(evidence: ProviderPayoutCapEvidence) -> str:
    payload = {
        "schema": "autosport.provider-payout-cap-evidence-v1",
        "provider_id": evidence.provider_id,
        "account_id": evidence.account_id,
        "adapter_id": evidence.adapter_id,
        "jurisdiction": evidence.jurisdiction,
        "currency": evidence.currency,
        "scope_kind": evidence.scope_kind.value,
        "cap_measure": evidence.cap_measure.value,
        "maximum_amount": _decimal_text(evidence.maximum_amount),
        "scope_binding_sha256": evidence.scope_binding_sha256,
        "settlement_rule_sha256": evidence.settlement_rule_sha256,
        "rule_version": evidence.rule_version,
        "observed_at": _utc_text(evidence.observed_at),
        "valid_until": _utc_text(evidence.valid_until),
        "source_kind": evidence.source_kind.value,
        "source_ref": evidence.source_ref,
        "source_payload_sha256": evidence.source_payload_sha256,
        "current_headroom": (
            None
            if evidence.current_headroom is None
            else _decimal_text(evidence.current_headroom)
        ),
        "headroom_evidence_sha256": evidence.headroom_evidence_sha256,
        "action_binding_sha256": evidence.action_binding_sha256,
        "execution_authority": False,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _decimal_text(value: Decimal) -> str:
    _nonnegative_decimal(value, "decimal")
    if value == 0:
        return "0"
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise ProviderPayoutCapError(
            f"{field} must be non-empty canonical text"
        )
    return value


def _sha256(value: object, field: str) -> str:
    raw = _text(value, field)
    if len(raw) != 64 or any(
        char not in "0123456789abcdef" for char in raw
    ):
        raise ProviderPayoutCapError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return raw


def _utc(value: object, field: str) -> datetime:
    if type(value) is not datetime:
        raise ProviderPayoutCapError(f"{field} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderPayoutCapError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat(timespec="microseconds")


def _positive_decimal(value: object, field: str) -> Decimal:
    if (
        type(value) is not Decimal
        or not value.is_finite()
        or value <= 0
    ):
        raise ProviderPayoutCapError(
            f"{field} must be a positive finite exact Decimal"
        )
    return value


def _nonnegative_decimal(value: object, field: str) -> Decimal:
    if (
        type(value) is not Decimal
        or not value.is_finite()
        or value < 0
    ):
        raise ProviderPayoutCapError(
            f"{field} must be a non-negative finite exact Decimal"
        )
    return value
