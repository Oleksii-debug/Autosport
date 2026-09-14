from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


_FORBIDDEN_FUTURE_KEYS = {"final_result", "result", "winner", "settled_outcome", "future_quote"}


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    as_of_ts: str
    source: str
    kind: str
    payload: dict[str, Any]
    source_hash: str | None = None

    def __post_init__(self) -> None:
        if _contains_forbidden_key(self.payload):
            raise ValueError("strategy/research evidence must not contain future-result fields")

    @property
    def canonical_hash(self) -> str:
        raw = {
            "evidence_id": self.evidence_id,
            "as_of_ts": self.as_of_ts,
            "source": self.source,
            "kind": self.kind,
            "payload": self.payload,
            "source_hash": self.source_hash,
        }
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchPacket:
    event_id: str
    generated_at: str
    evidence: tuple[EvidenceItem, ...] = field(default_factory=tuple)


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_FUTURE_KEYS:
                return True
            if _contains_forbidden_key(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_key(child) for child in value)
    return False
