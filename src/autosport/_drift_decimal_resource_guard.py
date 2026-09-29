"""Bound drift Decimal canonicalization before fixed-point materialization.

The owning drift implementation already defines canonical decimals as fixed-point
text: exponent notation is parsed only to be rejected after rendering the equivalent
fixed-point value.  Reject exponent notation before ``Decimal`` construction and cap
the already-fixed textual representation so syntactically tiny exponent inputs cannot
amplify into attacker-sized strings inside scientific evidence validation/replay.

This guard changes no drift arithmetic, threshold semantics, evidence identity, or
scientific authority.  It only pre-validates the input domain before delegating to the
existing canonical implementation.
"""
from __future__ import annotations

from . import drift_control as _drift


# ScientificRegistry and persisted drift witnesses are product metadata, not arbitrary
# bulk numeric corpora.  This ceiling is intentionally far above normal metric values
# while making fixed-point materialization explicitly finite.
_MAX_DRIFT_DECIMAL_TEXT_LENGTH = 8192
_ORIGINAL_CANONICAL_DECIMAL = _drift._canonical_decimal


def _bounded_canonical_decimal(value: object, name: str) -> str:
    if type(value) is str:
        if len(value) > _MAX_DRIFT_DECIMAL_TEXT_LENGTH:
            raise ValueError(f"{name} canonical decimal text exceeds resource limit")
        # The owner already requires exact fixed-point canonical text.  Exponent
        # notation can never survive its text == canonical comparison, so rejecting
        # it here preserves the valid domain while avoiding expansion such as
        # Decimal('1e1000000') -> a million-character fixed-point string.
        if "e" in value or "E" in value:
            raise ValueError(f"{name} must use fixed-point canonical decimal text")
    return _ORIGINAL_CANONICAL_DECIMAL(value, name)


_bounded_canonical_decimal._autosport_drift_decimal_resource_bounded = True  # type: ignore[attr-defined]
_drift._canonical_decimal = _bounded_canonical_decimal
