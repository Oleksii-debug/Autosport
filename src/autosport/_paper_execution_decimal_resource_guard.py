"""Fail closed before PAPER execution Decimals amplify into fixed-point strings.

The real execution ledger already owns the product's fixed-point Decimal resource
policy.  PAPER execution evidence must reuse that same boundary rather than minting
an independent numerical cap.  The legacy PAPER implementation resolves `_decimal`
and `_decimal_text` through module globals at call time, so this pre-facade guard can
compose the existing parser/serializer without creating a second ledger or schema.
"""

from __future__ import annotations

from decimal import Decimal
from types import FunctionType

from . import _paper_execution_reality_legacy as _impl
from .real_execution_ledger import _validate_decimal_text_resource_bound


_ORIGINAL_DECIMAL = _impl._decimal
_ORIGINAL_DECIMAL_TEXT = _impl._decimal_text
_RESOURCE_VALIDATOR = _validate_decimal_text_resource_bound

_HELPER_WITNESSES = tuple(
    (
        helper,
        helper.__code__,
        helper.__defaults__,
        helper.__kwdefaults__,
        helper.__closure__,
    )
    for helper in (
        _ORIGINAL_DECIMAL,
        _ORIGINAL_DECIMAL_TEXT,
        _RESOURCE_VALIDATOR,
    )
)


def _require_helpers(
    _witnesses=_HELPER_WITNESSES,
    _function_type=FunctionType,
) -> None:
    for function, code, defaults, kwdefaults, closure in _witnesses:
        if (
            type(function) is not _function_type
            or function.__code__ is not code
            or function.__defaults__ is not defaults
            or function.__kwdefaults__ is not kwdefaults
            or function.__closure__ is not closure
        ):
            raise ValueError("PAPER execution Decimal resource authority drifted")


def _bounded_decimal(
    value: object,
    name: str,
    *,
    allow_zero: bool = False,
    _parse=_ORIGINAL_DECIMAL,
    _validate=_RESOURCE_VALIDATOR,
    _require=_require_helpers,
) -> Decimal:
    _require()
    parsed = _parse(value, name, allow_zero=allow_zero)
    if type(parsed) is not Decimal:
        raise ValueError(f"{name} must resolve to an exact Decimal")
    _validate(parsed)
    return parsed


def _bounded_decimal_text(
    value: Decimal,
    _serialize=_ORIGINAL_DECIMAL_TEXT,
    _validate=_RESOURCE_VALIDATOR,
    _require=_require_helpers,
) -> str:
    _require()
    if type(value) is not Decimal:
        raise ValueError("PAPER execution Decimal must be exact")
    _validate(value)
    return _serialize(value)


def _install() -> None:
    if getattr(_impl, "_autosport_decimal_resource_guard_installed", False):
        return
    if _impl._decimal is not _ORIGINAL_DECIMAL:
        raise RuntimeError("PAPER execution Decimal parser changed before resource guard")
    if _impl._decimal_text is not _ORIGINAL_DECIMAL_TEXT:
        raise RuntimeError("PAPER execution Decimal serializer changed before resource guard")
    _impl._decimal = _bounded_decimal
    _impl._decimal_text = _bounded_decimal_text
    _impl._autosport_decimal_resource_guard_installed = True


_install()
