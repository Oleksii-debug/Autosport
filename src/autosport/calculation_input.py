from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_DECIMAL_TEXT = re.compile(
    r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?)|(?:\.[0-9]+))(?:[eE][+-]?[0-9]+)?",
    re.ASCII,
)


@dataclass(frozen=True, slots=True)
class CalculationInputLimits:
    """Complexity limits for user-authored calculation input."""

    numeric_text_chars: int = 128
    identifier_chars: int = 256
    identifier_utf8_bytes: int = 1024
    collection_items: int = 10_000

    def __post_init__(self) -> None:
        for field in (
            "numeric_text_chars",
            "identifier_chars",
            "identifier_utf8_bytes",
            "collection_items",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field} must be a positive non-boolean integer")


class CalculationInputBoundary:
    """Fail-closed ingress for manual/user-authored calculation values.

    CalculationEngine accepts typed programmatic values. Human-facing surfaces
    should validate raw text here first so pathological strings cannot reach
    Decimal construction, evidence hashing, sorting, or calculator invocation.
    This boundary deliberately preserves accepted text verbatim and therefore
    does not change the calculation engine's mathematical/result-hash contract.
    """

    def __init__(self, limits: CalculationInputLimits | None = None) -> None:
        self.limits = limits or CalculationInputLimits()

    def decimal_text(self, value: object, *, field: str) -> str:
        """Return bounded canonical ASCII decimal text without parsing it."""

        field_name = self._field_name(field)
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be decimal text")
        if not value or value != value.strip():
            raise ValueError(f"{field_name} must be non-empty trimmed decimal text")
        if len(value) > self.limits.numeric_text_chars:
            raise ValueError(f"{field_name} exceeds the manual-input text limit")
        if _DECIMAL_TEXT.fullmatch(value) is None:
            raise ValueError(f"{field_name} must use canonical ASCII decimal syntax")
        return value

    def identifier(self, value: object, *, field: str) -> str:
        """Return a bounded, trimmed, UTF-8 encodable user identifier."""

        field_name = self._field_name(field)
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{field_name} must be a non-empty trimmed string")
        if len(value) > self.limits.identifier_chars:
            raise ValueError(f"{field_name} exceeds the manual-input character limit")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(f"{field_name} must be UTF-8 encodable") from exc
        if len(encoded) > self.limits.identifier_utf8_bytes:
            raise ValueError(f"{field_name} exceeds the manual-input UTF-8 byte limit")
        return value

    def decimal_sequence(
        self,
        values: object,
        *,
        field: str,
        maximum_items: int | None = None,
    ) -> tuple[str, ...]:
        """Validate a bounded sequence before Decimal construction."""

        field_name = self._field_name(field)
        limit = self._collection_limit(maximum_items)
        if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
            raise ValueError(f"{field_name} must be a sequence")
        if len(values) > limit:
            raise ValueError(f"{field_name} exceeds the manual-input item limit")
        return tuple(
            self.decimal_text(value, field=f"{field_name}[{index}]")
            for index, value in enumerate(values)
        )

    def decimal_mapping(
        self,
        values: object,
        *,
        field: str,
        maximum_items: int | None = None,
    ) -> dict[str, str]:
        """Validate identifier -> decimal text input without sorting or hashing."""

        field_name = self._field_name(field)
        limit = self._collection_limit(maximum_items)
        if not isinstance(values, Mapping):
            raise ValueError(f"{field_name} must be a mapping")
        if len(values) > limit:
            raise ValueError(f"{field_name} exceeds the manual-input item limit")

        validated: dict[str, str] = {}
        for raw_key, raw_value in values.items():
            key = self.identifier(raw_key, field=f"{field_name} key")
            validated[key] = self.decimal_text(
                raw_value,
                field=f"{field_name}[{key}]",
            )
        return validated

    def _collection_limit(self, maximum_items: int | None) -> int:
        if maximum_items is None:
            return self.limits.collection_items
        if (
            isinstance(maximum_items, bool)
            or not isinstance(maximum_items, int)
            or maximum_items <= 0
        ):
            raise ValueError("maximum_items must be a positive non-boolean integer")
        return min(maximum_items, self.limits.collection_items)

    @staticmethod
    def _field_name(field: object) -> str:
        if not isinstance(field, str) or not field or field != field.strip():
            raise ValueError("field must be a non-empty trimmed string")
        return field


MANUAL_CALCULATION_INPUT = CalculationInputBoundary()
