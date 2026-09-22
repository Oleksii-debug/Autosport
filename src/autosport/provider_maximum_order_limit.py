"""Fail-closed provider maximum order-size / liability evidence.

This module owns only the provider-origin maximum monetary order constraint for one
exact scope. It does not own balances, owner RiskPolicy, market liquidity,
minimum-stake rules, payout caps, capital reservation, provider writes, or
execution/reconciliation.

Positive evidence is an in-process capability: the canonical verifier seals exact
object identity plus an immutable payload fingerprint. Copying/reconstructing a
record cannot preserve authority. This is a trusted-process API-misuse fence, not
cryptographic proof of a remote provider; provider-specific acquisition must still
bind the source bytes/receipt supplied to the verifier.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json


class ProviderMaximumOrderLimitError(ValueError):
    """Malformed, mismatched, stale, or non-authoritative provider limit evidence."""


class ProviderMaximumOrderLimitKind(str, Enum):
    BACK_STAKE_PER_ORDER = "BACK_STAKE_PER_ORDER"
    LAY_STAKE_PER_ORDER = "LAY_STAKE_PER_ORDER"
    LAY_LIABILITY_PER_ORDER = "LAY_LIABILITY_PER_ORDER"
    ORDER_NOTIONAL_PER_ORDER = "ORDER_NOTIONAL_PER_ORDER"


class ProviderMaximumOrderLimitSourceKind(str, Enum):
    AUTHENTICATED_PROVIDER_READBACK = "AUTHENTICATED_PROVIDER_READBACK"
    AUTHENTICATED_ACTION_QUOTE = "AUTHENTICATED_ACTION_QUOTE"
    VERSIONED_PROVIDER_RULE = "VERSIONED_PROVIDER_RULE"


class ProviderMaximumOrderLimitState(str, Enum):
    WITHIN_LIMIT = "WITHIN_LIMIT"
    EXCEEDS_LIMIT = "EXCEEDS_LIMIT"


@dataclass(frozen=True, slots=True)
class ProviderMaximumOrderLimitEvidence:
    provider_id: str
    account_id: str
    adapter_id: str
    jurisdiction: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    order_family: str
    currency: str
    limit_kind: ProviderMaximumOrderLimitKind
    maximum_amount: Decimal
    observed_at: datetime
    valid_until: datetime
    source_kind: ProviderMaximumOrderLimitSourceKind
    source_ref: str
    source_payload_sha256: str
    action_binding_sha256: str | None = None
    execution_authority: bool = False

    def __post_init__(self) -> None:
        for name in (
            "provider_id",
            "account_id",
            "adapter_id",
            "jurisdiction",
            "event_id",
            "market_id",
            "selection_id",
            "side",
            "order_family",
            "currency",
            "source_ref",
        ):
            _text(getattr(self, name), name)
        if self.side not in {"BACK", "LAY"}:
            raise ProviderMaximumOrderLimitError("side must be BACK or LAY")
        if type(self.limit_kind) is not ProviderMaximumOrderLimitKind:
            raise ProviderMaximumOrderLimitError(
                "limit_kind must be exact ProviderMaximumOrderLimitKind"
            )
        _positive_decimal(self.maximum_amount, "maximum_amount")
        observed = _utc(self.observed_at, "observed_at")
        valid_until = _utc(self.valid_until, "valid_until")
        if valid_until <= observed:
            raise ProviderMaximumOrderLimitError(
                "valid_until must be strictly after observed_at"
            )
        if type(self.source_kind) is not ProviderMaximumOrderLimitSourceKind:
            raise ProviderMaximumOrderLimitError(
                "source_kind must be exact ProviderMaximumOrderLimitSourceKind"
            )
        _sha256(self.source_payload_sha256, "source_payload_sha256")
        if self.action_binding_sha256 is not None:
            _sha256(self.action_binding_sha256, "action_binding_sha256")
        if (
            self.source_kind is ProviderMaximumOrderLimitSourceKind.AUTHENTICATED_ACTION_QUOTE
            and self.action_binding_sha256 is None
        ):
            raise ProviderMaximumOrderLimitError(
                "authenticated action quote requires action_binding_sha256"
            )
        if self.execution_authority is not False:
            raise ProviderMaximumOrderLimitError(
                "maximum-order evidence never grants execution authority"
            )

    @property
    def evidence_sha256(self) -> str:
        return _fingerprint(self)


@dataclass(frozen=True, slots=True)
class ProviderMaximumOrderLimitAssessment:
    state: ProviderMaximumOrderLimitState
    requested_amount: Decimal
    maximum_amount: Decimal
    evidence_sha256: str
    reason: str
    execution_authority: bool = False

    def __post_init__(self) -> None:
        if type(self.state) is not ProviderMaximumOrderLimitState:
            raise ProviderMaximumOrderLimitError(
                "state must be exact ProviderMaximumOrderLimitState"
            )
        _positive_decimal(self.requested_amount, "requested_amount")
        _positive_decimal(self.maximum_amount, "maximum_amount")
        _sha256(self.evidence_sha256, "evidence_sha256")
        _text(self.reason, "reason")
        if self.execution_authority is not False:
            raise ProviderMaximumOrderLimitError(
                "maximum-order assessment never grants execution authority"
            )


def _install_authority() -> None:
    issued: dict[int, tuple[ProviderMaximumOrderLimitEvidence, str]] = {}

    def verify_provider_maximum_order_limit_evidence(
        evidence: ProviderMaximumOrderLimitEvidence,
        *,
        as_of: datetime,
        provider_id: str,
        account_id: str,
        adapter_id: str,
        jurisdiction: str,
        event_id: str,
        market_id: str,
        selection_id: str,
        side: str,
        order_family: str,
        currency: str,
        limit_kind: ProviderMaximumOrderLimitKind,
        action_binding_sha256: str | None = None,
    ) -> ProviderMaximumOrderLimitEvidence:
        if type(evidence) is not ProviderMaximumOrderLimitEvidence:
            raise ProviderMaximumOrderLimitError(
                "evidence must be exact ProviderMaximumOrderLimitEvidence"
            )
        current = _utc(as_of, "as_of")
        expected_text = {
            "provider_id": provider_id,
            "account_id": account_id,
            "adapter_id": adapter_id,
            "jurisdiction": jurisdiction,
            "event_id": event_id,
            "market_id": market_id,
            "selection_id": selection_id,
            "side": side,
            "order_family": order_family,
            "currency": currency,
        }
        for name, expected in expected_text.items():
            _text(expected, f"expected {name}")
            if getattr(evidence, name) != expected:
                raise ProviderMaximumOrderLimitError(f"{name} scope mismatch")
        if type(limit_kind) is not ProviderMaximumOrderLimitKind:
            raise ProviderMaximumOrderLimitError(
                "expected limit_kind must be exact ProviderMaximumOrderLimitKind"
            )
        if evidence.limit_kind is not limit_kind:
            raise ProviderMaximumOrderLimitError("limit_kind scope mismatch")
        if action_binding_sha256 is not None:
            _sha256(action_binding_sha256, "expected action_binding_sha256")
        if evidence.action_binding_sha256 != action_binding_sha256:
            raise ProviderMaximumOrderLimitError("action binding scope mismatch")
        observed = _utc(evidence.observed_at, "observed_at")
        valid_until = _utc(evidence.valid_until, "valid_until")
        if observed > current:
            raise ProviderMaximumOrderLimitError("provider limit evidence is future")
        if current >= valid_until:
            raise ProviderMaximumOrderLimitError("provider limit evidence is expired")
        fingerprint = _fingerprint(evidence)
        issued[id(evidence)] = (evidence, fingerprint)
        return evidence

    def assert_provider_maximum_order_limit_evidence_authoritative(
        evidence: ProviderMaximumOrderLimitEvidence,
    ) -> None:
        if type(evidence) is not ProviderMaximumOrderLimitEvidence:
            raise ProviderMaximumOrderLimitError(
                "evidence must be exact ProviderMaximumOrderLimitEvidence"
            )
        record = issued.get(id(evidence))
        if record is None or record[0] is not evidence:
            raise ProviderMaximumOrderLimitError(
                "provider maximum-order evidence lacks canonical verification authority"
            )
        if record[1] != _fingerprint(evidence):
            raise ProviderMaximumOrderLimitError(
                "provider maximum-order evidence changed after verification"
            )

    globals()[
        "verify_provider_maximum_order_limit_evidence"
    ] = verify_provider_maximum_order_limit_evidence
    globals()[
        "assert_provider_maximum_order_limit_evidence_authoritative"
    ] = assert_provider_maximum_order_limit_evidence_authoritative


_install_authority()
del _install_authority


def assess_provider_maximum_order_limit(
    *,
    evidence: ProviderMaximumOrderLimitEvidence,
    requested_amount: Decimal,
) -> ProviderMaximumOrderLimitAssessment:
    """Compare one exact requested quantity to one canonically verified provider maximum.

    This result proves only the provider-maximum axis. It says nothing about owner
    limits, funds, liquidity, minimum size, payout caps, fill, acceptance, or execution.
    """

    assert_provider_maximum_order_limit_evidence_authoritative(evidence)
    requested = _positive_decimal(requested_amount, "requested_amount")
    maximum = evidence.maximum_amount
    if requested > maximum:
        return ProviderMaximumOrderLimitAssessment(
            state=ProviderMaximumOrderLimitState.EXCEEDS_LIMIT,
            requested_amount=requested,
            maximum_amount=maximum,
            evidence_sha256=evidence.evidence_sha256,
            reason="requested_amount_exceeds_verified_provider_maximum",
        )
    return ProviderMaximumOrderLimitAssessment(
        state=ProviderMaximumOrderLimitState.WITHIN_LIMIT,
        requested_amount=requested,
        maximum_amount=maximum,
        evidence_sha256=evidence.evidence_sha256,
        reason="requested_amount_within_verified_provider_maximum",
    )


def _fingerprint(evidence: ProviderMaximumOrderLimitEvidence) -> str:
    payload = {
        "schema": "autosport.provider-maximum-order-limit-evidence-v1",
        "provider_id": evidence.provider_id,
        "account_id": evidence.account_id,
        "adapter_id": evidence.adapter_id,
        "jurisdiction": evidence.jurisdiction,
        "event_id": evidence.event_id,
        "market_id": evidence.market_id,
        "selection_id": evidence.selection_id,
        "side": evidence.side,
        "order_family": evidence.order_family,
        "currency": evidence.currency,
        "limit_kind": evidence.limit_kind.value,
        "maximum_amount": _decimal_text(evidence.maximum_amount),
        "observed_at": _utc_text(evidence.observed_at),
        "valid_until": _utc_text(evidence.valid_until),
        "source_kind": evidence.source_kind.value,
        "source_ref": evidence.source_ref,
        "source_payload_sha256": evidence.source_payload_sha256,
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
    _positive_decimal(value, "decimal")
    sign, digits, exponent = value.as_tuple()
    coefficient = list(digits)
    while len(coefficient) > 1 and coefficient[-1] == 0:
        coefficient.pop()
        exponent += 1
    raw = "".join(str(digit) for digit in coefficient)
    if exponent >= 0:
        text = raw + ("0" * exponent)
    else:
        point = len(raw) + exponent
        text = f"{raw[:point]}.{raw[point:]}" if point > 0 else f"0.{('0' * -point)}{raw}"
    return f"-{text}" if sign else text


def _positive_decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ProviderMaximumOrderLimitError(
            f"{name} must be exact finite positive Decimal"
        )
    return value


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderMaximumOrderLimitError(f"{name} must be timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value, "time").isoformat().replace("+00:00", "Z")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ProviderMaximumOrderLimitError(f"{name} must be canonical non-empty text")
    return value


def _sha256(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise ProviderMaximumOrderLimitError(f"{name} must be lowercase SHA-256 hex")
    return raw
