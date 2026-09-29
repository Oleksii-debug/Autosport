"""Bound Betfair settlement Decimal materialization before fixed-point formatting.

Provider-cleared Decimal values are already finite and semantically validated upstream,
but a compact coefficient/exponent tuple can still expand into attacker-sized text when
#1272 canonicalizes it with ``format(value, 'f')`` for revision identity. Reuse the
settlement resource envelope already established in the repository and install it over
the existing #1272 revision authority; no parallel settlement or money authority is
introduced.
"""
from __future__ import annotations

from decimal import Decimal

from . import betfair_settlement_revisions as _settlement


_MAX_COEFFICIENT_DIGITS = 4096
_MAX_ABS_EXPONENT = 4096
_MAX_FIXED_TEXT_LENGTH = 8192
_DECIMAL_FIELDS = (
    "price_requested",
    "price_matched",
    "size_settled",
    "provider_profit",
)
_ERROR = _settlement.BetfairSettlementRevisionError
_REVISION_TYPE = _settlement.BetfairSettlementRevision
_ORIGINAL_DEC = _settlement._dec
_ORIGINAL_REVISION_SEMANTIC_PAYLOAD = _REVISION_TYPE.semantic_payload
_ORIGINAL_INGEST_SEMANTIC_PAYLOAD = _settlement._semantic_payload
_ORIGINAL_DEC_CODE = getattr(_ORIGINAL_DEC, "__code__", None)
_ORIGINAL_REVISION_SEMANTIC_CODE = getattr(
    _ORIGINAL_REVISION_SEMANTIC_PAYLOAD,
    "__code__",
    None,
)
_ORIGINAL_INGEST_SEMANTIC_CODE = getattr(
    _ORIGINAL_INGEST_SEMANTIC_PAYLOAD,
    "__code__",
    None,
)

if (
    _ORIGINAL_DEC_CODE is None
    or _ORIGINAL_REVISION_SEMANTIC_CODE is None
    or _ORIGINAL_INGEST_SEMANTIC_CODE is None
):
    raise RuntimeError("Betfair settlement Decimal materialization surface is unavailable")


def _require_bounded_decimal(value: Decimal, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise _ERROR(f"{field} must be finite Decimal")

    parts = value.as_tuple()
    exponent = parts.exponent
    digits = parts.digits
    if type(exponent) is not int:
        raise _ERROR(f"{field} Decimal exponent is invalid")
    if len(digits) > _MAX_COEFFICIENT_DIGITS:
        raise _ERROR(f"{field} Decimal coefficient exceeds resource limit")
    if abs(exponent) > _MAX_ABS_EXPONENT:
        raise _ERROR(f"{field} Decimal exponent exceeds resource limit")

    coefficient_length = len(digits)
    if exponent >= 0:
        rendered_length = coefficient_length + exponent
    else:
        point = coefficient_length + exponent
        rendered_length = (
            coefficient_length + 1
            if point > 0
            else 2 + (-point) + coefficient_length
        )
    if parts.sign:
        rendered_length += 1
    if rendered_length > _MAX_FIXED_TEXT_LENGTH:
        raise _ERROR(f"{field} Decimal canonical text exceeds resource limit")
    return value


def _require_original_surfaces() -> None:
    if (
        getattr(_ORIGINAL_DEC, "__code__", None) is not _ORIGINAL_DEC_CODE
        or getattr(_ORIGINAL_REVISION_SEMANTIC_PAYLOAD, "__code__", None)
        is not _ORIGINAL_REVISION_SEMANTIC_CODE
        or getattr(_ORIGINAL_INGEST_SEMANTIC_PAYLOAD, "__code__", None)
        is not _ORIGINAL_INGEST_SEMANTIC_CODE
    ):
        raise _ERROR("settlement Decimal materialization dispatch changed")


def _bounded_dec(value: object, field: str) -> Decimal:
    _require_original_surfaces()
    parsed = _ORIGINAL_DEC(value, field)
    return _require_bounded_decimal(parsed, field)


def _bounded_revision_semantic_payload(self) -> dict[str, str]:
    _require_original_surfaces()
    for field in _DECIMAL_FIELDS:
        _require_bounded_decimal(getattr(self, field), field)
    return _ORIGINAL_REVISION_SEMANTIC_PAYLOAD(self)


def _bounded_ingest_semantic_payload(
    action,
    capture,
    order,
    plan_id: str,
    attempt_id: str,
):
    _require_original_surfaces()
    for attribute, field in (
        ("price_requested", "price_requested"),
        ("price_matched", "price_matched"),
        ("size_settled", "size_settled"),
        ("profit", "provider_profit"),
    ):
        _require_bounded_decimal(getattr(order, attribute), field)
    return _ORIGINAL_INGEST_SEMANTIC_PAYLOAD(
        action,
        capture,
        order,
        plan_id,
        attempt_id,
    )


if _settlement._dec is not _ORIGINAL_DEC:
    raise RuntimeError("Betfair settlement Decimal parser changed before resource guard")
if _REVISION_TYPE.semantic_payload is not _ORIGINAL_REVISION_SEMANTIC_PAYLOAD:
    raise RuntimeError("Betfair settlement revision payload changed before resource guard")
if _settlement._semantic_payload is not _ORIGINAL_INGEST_SEMANTIC_PAYLOAD:
    raise RuntimeError("Betfair settlement ingest payload changed before resource guard")

_settlement._dec = _bounded_dec
_REVISION_TYPE.semantic_payload = _bounded_revision_semantic_payload
_settlement._semantic_payload = _bounded_ingest_semantic_payload

__all__ = ["_require_bounded_decimal"]
