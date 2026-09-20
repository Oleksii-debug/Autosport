from __future__ import annotations

"""Fail-closed prospective monetary authority for model-compute routing.

The existing model-compute router owns compute request/route identity and measured
compute-unit cost, but current product truth does not expose a product-owned,
pre-decision monetary tariff (amount + currency + tariff identity) for that route.
This adapter therefore binds an exact live OpportunityIntent to the canonical
router decision and emits explicit UNKNOWN_UNPROVEN monetary truth. It never
converts credits/tokens/dimensionless compute cost into money and never accepts a
caller-authored monetary amount, currency, tariff, zero, or applicability claim.

Schema v1 deliberately cannot represent positive monetary truth. A future positive
adapter must introduce a product-owned re-resolving billing authority instead of
trusting a caller-constructible Python object. Until then UNKNOWN_UNPROVEN is the
only representable state.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum, StrEnum
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .model_compute_router import ComputeRouteDecision, ModelComputeRouterStore
from .portfolio_plan import OpportunityIntent


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = 1


class ProspectiveModelComputeMoneyError(ValueError):
    """Raised when canonical prospective model-compute evidence is invalid."""


class ProspectiveModelComputeMoneyStatus(StrEnum):
    """Schema-v1 status. Positive money is intentionally not representable."""

    UNKNOWN_UNPROVEN = "UNKNOWN_UNPROVEN"


class ProspectiveModelComputeMoneyReason(StrEnum):
    NO_PREDECISION_MONETARY_TARIFF_AUTHORITY = (
        "NO_PREDECISION_MONETARY_TARIFF_AUTHORITY"
    )


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ProspectiveModelComputeMoneyError(
            f"{field} must be a non-empty canonical string"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProspectiveModelComputeMoneyError(f"{field} must be valid UTF-8") from exc
    return value


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if _SHA256_RE.fullmatch(digest) is None:
        raise ProspectiveModelComputeMoneyError(
            f"{field} must be canonical lowercase SHA-256"
        )
    return digest


def _instant(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        raw = _text(value, field)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProspectiveModelComputeMoneyError(
                f"{field} must be valid ISO-8601"
            ) from exc
    else:
        raise ProspectiveModelComputeMoneyError(
            f"{field} must be a timezone-aware datetime/ISO-8601 string"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProspectiveModelComputeMoneyError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_ready(value: object, field: str = "payload") -> Any:
    if value is None or type(value) in {str, int, bool}:
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ProspectiveModelComputeMoneyError(f"{field} Decimal must be finite")
        return str(value)
    if isinstance(value, datetime):
        return _time_text(_instant(value, field))
    if isinstance(value, Enum):
        return _json_ready(value.value, field)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ProspectiveModelComputeMoneyError(
                    f"{field} mapping keys must be strings"
                )
            normalized[key] = _json_ready(item, f"{field}.{key}")
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_ready(item, f"{field}[]") for item in value]
    raise ProspectiveModelComputeMoneyError(
        f"{field} contains unsupported value type {type(value).__name__}"
    )


def _digest(value: object) -> str:
    encoded = json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class ProspectiveModelComputeMoneyEvidence:
    """Fail-closed prospective evidence for one canonical compute route.

    Schema v1 intentionally exposes monetary fields only as ``None``. Keeping those
    fields in the serialized shape makes the missing authority explicit while
    preventing a caller from minting a positive exact-type result.
    """

    intent_sha256: str
    opportunity_id: str
    request_id: str
    decision_at: datetime
    router_decided_at: datetime
    router_request_sha256: str
    router_decision_sha256: str
    status: ProspectiveModelComputeMoneyStatus
    reason: ProspectiveModelComputeMoneyReason
    amount: None
    currency: None
    tariff_sha256: None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ProspectiveModelComputeMoneyError(
            "ProspectiveModelComputeMoneyEvidence is created only by the canonical resolver"
        )

    def _validate(self) -> None:
        _sha256(self.intent_sha256, "intent_sha256")
        _text(self.opportunity_id, "opportunity_id")
        _text(self.request_id, "request_id")
        _instant(self.decision_at, "decision_at")
        _instant(self.router_decided_at, "router_decided_at")
        if self.router_decided_at > self.decision_at:
            raise ProspectiveModelComputeMoneyError(
                "canonical router decision cannot be from after the live decision cutoff"
            )
        _sha256(self.router_request_sha256, "router_request_sha256")
        _sha256(self.router_decision_sha256, "router_decision_sha256")
        if (
            type(self.status) is not ProspectiveModelComputeMoneyStatus
            or self.status is not ProspectiveModelComputeMoneyStatus.UNKNOWN_UNPROVEN
        ):
            raise ProspectiveModelComputeMoneyError(
                "schema v1 cannot represent positive monetary authority"
            )
        if (
            type(self.reason) is not ProspectiveModelComputeMoneyReason
            or self.reason
            is not ProspectiveModelComputeMoneyReason.NO_PREDECISION_MONETARY_TARIFF_AUTHORITY
        ):
            raise ProspectiveModelComputeMoneyError(
                "schema v1 UNKNOWN_UNPROVEN requires the canonical missing-tariff reason"
            )
        if any(value is not None for value in (self.amount, self.currency, self.tariff_sha256)):
            raise ProspectiveModelComputeMoneyError(
                "schema v1 cannot expose an authoritative monetary value"
            )

    @property
    def evidence_id(self) -> str:
        self._validate()
        return _digest(self.to_dict(include_evidence_id=False))

    def to_dict(self, *, include_evidence_id: bool = True) -> dict[str, Any]:
        self._validate()
        payload: dict[str, Any] = {
            "schema": "autosport.prospective_model_compute_money",
            "schema_version": _SCHEMA_VERSION,
            "intent_sha256": self.intent_sha256,
            "opportunity_id": self.opportunity_id,
            "request_id": self.request_id,
            "decision_at": _time_text(self.decision_at),
            "router_decided_at": _time_text(self.router_decided_at),
            "router_request_sha256": self.router_request_sha256,
            "router_decision_sha256": self.router_decision_sha256,
            "status": self.status.value,
            "reason": self.reason.value,
            "amount": None,
            "currency": None,
            "tariff_sha256": None,
        }
        if include_evidence_id:
            payload["evidence_id"] = _digest(payload)
        return payload


def _make_unknown_evidence(
    *,
    intent_sha256: str,
    opportunity_id: str,
    request_id: str,
    decision_at: datetime,
    router_decided_at: datetime,
    router_request_sha256: str,
    router_decision_sha256: str,
) -> ProspectiveModelComputeMoneyEvidence:
    """Create the sole schema-v1 state.

    The helper has no monetary/status parameters, so importing this private helper
    does not create a positive authority surface. Product consumers must still
    treat the returned object as fail-closed UNKNOWN evidence, never as a future
    positive billing capability.
    """

    item = object.__new__(ProspectiveModelComputeMoneyEvidence)
    object.__setattr__(item, "intent_sha256", intent_sha256)
    object.__setattr__(item, "opportunity_id", opportunity_id)
    object.__setattr__(item, "request_id", request_id)
    object.__setattr__(item, "decision_at", decision_at)
    object.__setattr__(item, "router_decided_at", router_decided_at)
    object.__setattr__(item, "router_request_sha256", router_request_sha256)
    object.__setattr__(item, "router_decision_sha256", router_decision_sha256)
    object.__setattr__(
        item,
        "status",
        ProspectiveModelComputeMoneyStatus.UNKNOWN_UNPROVEN,
    )
    object.__setattr__(
        item,
        "reason",
        ProspectiveModelComputeMoneyReason.NO_PREDECISION_MONETARY_TARIFF_AUTHORITY,
    )
    object.__setattr__(item, "amount", None)
    object.__setattr__(item, "currency", None)
    object.__setattr__(item, "tariff_sha256", None)
    item._validate()
    return item


def resolve_prospective_model_compute_money(
    *,
    intent: OpportunityIntent,
    router_store: ModelComputeRouterStore,
    request_id: str,
    decision_at: datetime,
) -> ProspectiveModelComputeMoneyEvidence:
    """Bind a live intent to canonical compute-route truth without inventing money.

    The current router's ``max_cost`` / measured compute-cost values are compute
    economics used by routing/VOC. They do not carry a monetary currency or a
    product-owned billing/tariff identity. Consequently schema v1 can return only
    ``UNKNOWN_UNPROVEN``.
    """

    # Capability checks happen before any authority-bearing property/method read.
    if type(intent) is not OpportunityIntent:
        raise ProspectiveModelComputeMoneyError(
            "intent must be the exact canonical OpportunityIntent type"
        )
    if type(router_store) is not ModelComputeRouterStore:
        raise ProspectiveModelComputeMoneyError(
            "router_store must be the exact canonical ModelComputeRouterStore type"
        )

    canonical_request_id = _text(request_id, "request_id")
    cutoff = _instant(decision_at, "decision_at")
    decision = router_store.get_decision(canonical_request_id)
    if decision is None:
        raise ProspectiveModelComputeMoneyError(
            "canonical model-compute route decision is missing"
        )
    if type(decision) is not ComputeRouteDecision:
        raise ProspectiveModelComputeMoneyError(
            "canonical router returned a non-canonical ComputeRouteDecision"
        )

    router_decided_at = _instant(decision.decided_at, "router decision decided_at")
    if router_decided_at > cutoff:
        raise ProspectiveModelComputeMoneyError(
            "canonical model-compute route decision is from the future"
        )
    decision_payload = decision.payload()
    if not isinstance(decision_payload, Mapping):
        raise ProspectiveModelComputeMoneyError(
            "canonical model-compute route payload is invalid"
        )
    request_payload = decision_payload.get("request")
    if not isinstance(request_payload, Mapping):
        raise ProspectiveModelComputeMoneyError(
            "canonical model-compute request payload is missing"
        )
    if request_payload.get("request_id") != canonical_request_id:
        raise ProspectiveModelComputeMoneyError(
            "canonical model-compute request identity mismatch"
        )

    intent_sha256 = _sha256(intent.intent_sha256, "intent.intent_sha256")
    opportunity = intent.opportunity
    opportunity_id = _text(
        getattr(opportunity, "opportunity_id", None),
        "intent opportunity_id",
    )

    # Seal the complete canonical request/route identity into fail-closed evidence.
    # A future monetary implementation must be a new product-owned re-resolution
    # authority; it must not expand this caller-visible object into positive truth.
    return _make_unknown_evidence(
        intent_sha256=intent_sha256,
        opportunity_id=opportunity_id,
        request_id=canonical_request_id,
        decision_at=cutoff,
        router_decided_at=router_decided_at,
        router_request_sha256=_digest(request_payload),
        router_decision_sha256=_digest(decision_payload),
    )
