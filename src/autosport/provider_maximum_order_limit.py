"""Fail-closed provider maximum order-size / liability evidence.

This module owns only the shape and deterministic arithmetic for one scoped maximum
monetary order constraint. It does not own balances, owner RiskPolicy, market
liquidity, minimum-stake rules, payout caps, capital reservation, provider writes,
or execution/reconciliation.

Caller-constructible evidence may be structurally sealed in-process so scope,
expiry, typed maximum semantics, action binding, and copy/restart misuse are
checked deterministically. That structural seal is deliberately NOT provider-origin
proof. Until an independent product-owned authenticated acquisition/rule authority
is composed, this module cannot issue positive provider support.
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
    UNKNOWN_UNPROVEN = "UNKNOWN_UNPROVEN"


class ProviderMaximumOrderLimitComparison(str, Enum):
    AT_OR_BELOW_UNPROVEN_MAXIMUM = "AT_OR_BELOW_UNPROVEN_MAXIMUM"
    ABOVE_UNPROVEN_MAXIMUM = "ABOVE_UNPROVEN_MAXIMUM"


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
        allowed_limit_kinds = {
            "BACK": {
                ProviderMaximumOrderLimitKind.BACK_STAKE_PER_ORDER,
                ProviderMaximumOrderLimitKind.ORDER_NOTIONAL_PER_ORDER,
            },
            "LAY": {
                ProviderMaximumOrderLimitKind.LAY_STAKE_PER_ORDER,
                ProviderMaximumOrderLimitKind.LAY_LIABILITY_PER_ORDER,
                ProviderMaximumOrderLimitKind.ORDER_NOTIONAL_PER_ORDER,
            },
        }
        if self.limit_kind not in allowed_limit_kinds[self.side]:
            raise ProviderMaximumOrderLimitError(
                "limit_kind is incompatible with side"
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
            self.source_kind
            is ProviderMaximumOrderLimitSourceKind.AUTHENTICATED_ACTION_QUOTE
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
    """Numerical comparison over one structurally sealed caller-supplied maximum.

    state is always UNKNOWN_UNPROVEN. comparison is arithmetic-only and the
    hard-false authority properties prevent a caller-selected maximum/source hash
    from becoming provider-origin admission support.
    """

    state: ProviderMaximumOrderLimitState
    comparison: ProviderMaximumOrderLimitComparison
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
        if self.state is not ProviderMaximumOrderLimitState.UNKNOWN_UNPROVEN:
            raise ProviderMaximumOrderLimitError(
                "structural assessment cannot claim a provider limit state"
            )
        if type(self.comparison) is not ProviderMaximumOrderLimitComparison:
            raise ProviderMaximumOrderLimitError(
                "comparison must be exact ProviderMaximumOrderLimitComparison"
            )
        _positive_decimal(self.requested_amount, "requested_amount")
        _positive_decimal(self.maximum_amount, "maximum_amount")
        _sha256(self.evidence_sha256, "evidence_sha256")
        _text(self.reason, "reason")
        if self.execution_authority is not False:
            raise ProviderMaximumOrderLimitError(
                "maximum-order assessment never grants execution authority"
            )

    @property
    def provider_origin_proven(self) -> bool:
        return False

    @property
    def supports_requested_amount(self) -> bool:
        return False


def _install_structural_authority() -> None:
    sealed: dict[int, tuple[ProviderMaximumOrderLimitEvidence, str]] = {}

    def _validate_scope_and_time(
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
        action_binding_sha256: str | None,
    ) -> str:
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
        return _fingerprint(evidence)

    def seal_provider_maximum_order_limit_structure(
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
        fingerprint = _validate_scope_and_time(
            evidence,
            as_of=as_of,
            provider_id=provider_id,
            account_id=account_id,
            adapter_id=adapter_id,
            jurisdiction=jurisdiction,
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            side=side,
            order_family=order_family,
            currency=currency,
            limit_kind=limit_kind,
            action_binding_sha256=action_binding_sha256,
        )
        sealed[id(evidence)] = (evidence, fingerprint)
        return evidence

    def assert_provider_maximum_order_limit_structure_sealed(
        evidence: ProviderMaximumOrderLimitEvidence,
    ) -> None:
        if type(evidence) is not ProviderMaximumOrderLimitEvidence:
            raise ProviderMaximumOrderLimitError(
                "evidence must be exact ProviderMaximumOrderLimitEvidence"
            )
        record = sealed.get(id(evidence))
        if record is None or record[0] is not evidence:
            raise ProviderMaximumOrderLimitError(
                "provider maximum-order evidence lacks structural seal"
            )
        if record[1] != _fingerprint(evidence):
            raise ProviderMaximumOrderLimitError(
                "provider maximum-order evidence changed after structural sealing"
            )

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
        _validate_scope_and_time(
            evidence,
            as_of=as_of,
            provider_id=provider_id,
            account_id=account_id,
            adapter_id=adapter_id,
            jurisdiction=jurisdiction,
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            side=side,
            order_family=order_family,
            currency=currency,
            limit_kind=limit_kind,
            action_binding_sha256=action_binding_sha256,
        )
        raise ProviderMaximumOrderLimitError(
            "provider-origin verification requires independent product-owned "
            "acquisition or rule authority"
        )

    def assert_provider_maximum_order_limit_evidence_authoritative(
        evidence: ProviderMaximumOrderLimitEvidence,
    ) -> None:
        if type(evidence) is not ProviderMaximumOrderLimitEvidence:
            raise ProviderMaximumOrderLimitError(
                "evidence must be exact ProviderMaximumOrderLimitEvidence"
            )
        raise ProviderMaximumOrderLimitError(
            "provider maximum-order origin authority is not proven"
        )

    globals()[
        "seal_provider_maximum_order_limit_structure"
    ] = seal_provider_maximum_order_limit_structure
    globals()[
        "assert_provider_maximum_order_limit_structure_sealed"
    ] = assert_provider_maximum_order_limit_structure_sealed
    globals()[
        "verify_provider_maximum_order_limit_evidence"
    ] = verify_provider_maximum_order_limit_evidence
    globals()[
        "assert_provider_maximum_order_limit_evidence_authoritative"
    ] = assert_provider_maximum_order_limit_evidence_authoritative


_install_structural_authority()
del _install_structural_authority


def assess_provider_maximum_order_limit(
    *,
    evidence: ProviderMaximumOrderLimitEvidence,
    requested_amount: Decimal,
    as_of: datetime,
) -> ProviderMaximumOrderLimitAssessment:
    """Compare a current structurally sealed, non-authoritative maximum.

    The diagnostic comparison is deterministic, but this isolated generic
    contract cannot prove the remote provider imposed the supplied maximum.
    State therefore remains UNKNOWN_UNPROVEN and provider_origin_proven plus
    supports_requested_amount remain hard false. Validity is rechecked at this exact use-time so
    an earlier structural seal cannot keep stale evidence numerically active.
    """

    assert_provider_maximum_order_limit_structure_sealed(evidence)
    current = _utc(as_of, "as_of")
    observed = _utc(evidence.observed_at, "observed_at")
    valid_until = _utc(evidence.valid_until, "valid_until")
    if observed > current:
        raise ProviderMaximumOrderLimitError(
            "provider limit evidence is future at assessment"
        )
    if current >= valid_until:
        raise ProviderMaximumOrderLimitError(
            "provider limit evidence is expired at assessment"
        )
    requested = _positive_decimal(requested_amount, "requested_amount")
    maximum = evidence.maximum_amount
    comparison = (
        ProviderMaximumOrderLimitComparison.ABOVE_UNPROVEN_MAXIMUM
        if requested > maximum
        else ProviderMaximumOrderLimitComparison.AT_OR_BELOW_UNPROVEN_MAXIMUM
    )
    reason = (
        "requested_amount_above_unproven_structural_maximum"
        if requested > maximum
        else "requested_amount_at_or_below_unproven_structural_maximum"
    )
    return ProviderMaximumOrderLimitAssessment(
        state=ProviderMaximumOrderLimitState.UNKNOWN_UNPROVEN,
        comparison=comparison,
        requested_amount=requested,
        maximum_amount=maximum,
        evidence_sha256=evidence.evidence_sha256,
        reason=reason,
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
