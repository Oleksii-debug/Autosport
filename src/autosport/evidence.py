from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .causal_integrity import (
    contains_forbidden_future_key,
    freeze_canonical_json_object,
)


_SHA256_HEX = frozenset("0123456789abcdef")


def _canonical_text(value: object, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field_name} must be a non-empty canonical string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8 text") from exc
    return value


def _aware_timestamp(value: object, field_name: str) -> str:
    text = _canonical_text(value, field_name)
    for match in re.finditer(r"[.,]([0-9]+)", text):
        fractional_digits = match.group(1)
        if len(fractional_digits) > 6 and any(
            digit != "0" for digit in fractional_digits[6:]
        ):
            raise ValueError(
                f"{field_name} precision finer than microseconds is unsupported"
            )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit timezone")
    return text


def _instant(value: object, field_name: str) -> datetime:
    text = _aware_timestamp(value, field_name)
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _json_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_payload(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_json_payload(child) for child in value]
    return value


def _validate_evidence_identity_fields(
    *,
    evidence_id: object,
    as_of_ts: object,
    source: object,
    kind: object,
    source_hash: object,
    available_at: object | None = None,
) -> None:
    _canonical_text(evidence_id, "evidence_id")
    _aware_timestamp(as_of_ts, "as_of_ts")
    _canonical_text(source, "source")
    _canonical_text(kind, "kind")
    if available_at is not None:
        _aware_timestamp(available_at, "available_at")
    if source_hash is not None:
        digest = _canonical_text(source_hash, "source_hash")
        if len(digest) != 64 or any(character not in _SHA256_HEX for character in digest):
            raise ValueError(
                "source_hash must be a canonical lowercase SHA-256 digest"
            )


def _validated_evidence_payload(value: object) -> Mapping[str, Any]:
    payload = freeze_canonical_json_object(
        value,
        field_name="strategy/research evidence payload",
    )
    if contains_forbidden_future_key(payload):
        raise ValueError(
            "strategy/research evidence must not contain future-result fields"
        )
    return payload


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    as_of_ts: str
    source: str
    kind: str
    payload: dict[str, Any]
    source_hash: str | None = None
    available_at: str | None = None

    def __post_init__(self) -> None:
        _validate_evidence_identity_fields(
            evidence_id=self.evidence_id,
            as_of_ts=self.as_of_ts,
            source=self.source,
            kind=self.kind,
            source_hash=self.source_hash,
            available_at=self.available_at,
        )
        payload = _validated_evidence_payload(self.payload)
        object.__setattr__(self, "payload", payload)

    @property
    def canonical_hash(self) -> str:
        _validate_evidence_identity_fields(
            evidence_id=self.evidence_id,
            as_of_ts=self.as_of_ts,
            source=self.source,
            kind=self.kind,
            source_hash=self.source_hash,
            available_at=self.available_at,
        )
        payload = _validated_evidence_payload(self.payload)
        raw = {
            "evidence_id": self.evidence_id,
            "as_of_ts": self.as_of_ts,
            "source": self.source,
            "kind": self.kind,
            "payload": _json_payload(payload),
            "source_hash": self.source_hash,
        }
        # Preserve legacy standalone EvidenceItem hashes when no availability
        # witness exists, while binding causal availability whenever it is present.
        if self.available_at is not None:
            raw["available_at"] = self.available_at
        canonical = json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchPacket:
    event_id: str
    generated_at: str
    evidence: tuple[EvidenceItem, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _canonical_text(self.event_id, "event_id")
        generated_at = _instant(self.generated_at, "generated_at")
        if type(self.evidence) is not tuple:
            raise ValueError("research packet evidence must be a tuple")
        seen_evidence_ids: set[str] = set()
        for item in self.evidence:
            if type(item) is not EvidenceItem:
                raise ValueError("research packet evidence must contain EvidenceItem values")
            _validate_evidence_identity_fields(
                evidence_id=item.evidence_id,
                as_of_ts=item.as_of_ts,
                source=item.source,
                kind=item.kind,
                source_hash=item.source_hash,
                available_at=item.available_at,
            )
            _validated_evidence_payload(item.payload)
            if item.available_at is None:
                raise ValueError(
                    "research packet evidence requires explicit available_at"
                )
            evidence_as_of = _instant(item.as_of_ts, "evidence.as_of_ts")
            evidence_available = _instant(
                item.available_at, "evidence.available_at"
            )
            if evidence_as_of > evidence_available:
                raise ValueError(
                    "research packet evidence as_of_ts cannot be after available_at"
                )
            if evidence_available > generated_at:
                raise ValueError(
                    "research packet contains evidence unavailable at generated_at"
                )
            if item.evidence_id in seen_evidence_ids:
                raise ValueError("research packet contains duplicate evidence identity")
            seen_evidence_ids.add(item.evidence_id)
