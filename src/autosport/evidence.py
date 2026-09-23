from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .causal_integrity import (
    contains_forbidden_future_key,
    freeze_canonical_json_object,
)


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
