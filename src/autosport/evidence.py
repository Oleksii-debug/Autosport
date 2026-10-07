from __future__ import annotations

import hashlib
import json
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
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8") from exc
    return value


def _canonical_timestamp(value: object, field_name: str) -> str:
    text = _canonical_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware ISO-8601")
    return text


def _instant(value: object, field_name: str) -> datetime:
    text = _canonical_timestamp(value, field_name)
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _optional_sha256(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    text = _canonical_text(value, field_name)
    if len(text) != 64 or any(char not in _SHA256_HEX for char in text):
        raise ValueError(f"{field_name} must be a canonical lowercase SHA-256 digest")
    return text


def _json_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_payload(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_json_payload(child) for child in value]
    return value


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    as_of_ts: str
    source: str
    kind: str
    payload: dict[str, Any]
    source_hash: str | None = None

    def __post_init__(self) -> None:
        _canonical_text(self.evidence_id, "evidence_id")
        _canonical_timestamp(self.as_of_ts, "as_of_ts")
        _canonical_text(self.source, "source")
        _canonical_text(self.kind, "kind")
        _optional_sha256(self.source_hash, "source_hash")
        payload = freeze_canonical_json_object(
            self.payload, field_name="strategy/research evidence payload"
        )
        if contains_forbidden_future_key(payload):
            raise ValueError("strategy/research evidence must not contain future-result fields")
        object.__setattr__(self, "payload", payload)

    @property
    def canonical_hash(self) -> str:
        raw = {
            "evidence_id": self.evidence_id,
            "as_of_ts": self.as_of_ts,
            "source": self.source,
            "kind": self.kind,
            "payload": _json_payload(self.payload),
            "source_hash": self.source_hash,
        }
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
            raise ValueError("evidence must be an exact tuple")
        if any(type(item) is not EvidenceItem for item in self.evidence):
            raise ValueError("evidence must contain exact EvidenceItem values")
        for item in self.evidence:
            if _instant(item.as_of_ts, "evidence.as_of_ts") > generated_at:
                raise ValueError("research packet contains evidence from after generated_at")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("research packet contains duplicate evidence_id")
