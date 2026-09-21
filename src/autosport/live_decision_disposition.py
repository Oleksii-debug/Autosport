"""Fail-closed live decision disposition evidence.

This module deliberately separates evidence eligibility from execution authority.
A disposition may say that a decision is ACTIONABLE, but that means only that the
strategy's declared evidence predicates are positively proven for the exact bound
market/evidence bundle before expiry.  It never authorizes a provider write,
real-money execution, settlement, payout, or learning outcome.

The contract is persistence-ready: canonical JSON, duplicate-key rejection,
strict schema reconstruction, and a deterministic SHA-256 disposition identity
allow a durable store to reopen exactly the same evidence after restart without
reinterpreting UNKNOWN as NO_BET.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final


SCHEMA: Final = "autosport.live_decision_disposition"
SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class LiveDecisionDispositionError(ValueError):
    """Live decision disposition evidence is malformed or semantically unsafe."""


class Disposition(StrEnum):
    ACTIONABLE = "ACTIONABLE"
    WAIT_EVIDENCE = "WAIT_EVIDENCE"
    NO_BET_POLICY = "NO_BET_POLICY"
    EXPIRED = "EXPIRED"
    HALT_SAFETY = "HALT_SAFETY"


class PredicateTruth(StrEnum):
    PROVEN = "PROVEN"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


class ReevaluationTrigger(StrEnum):
    NEW_EVIDENCE = "NEW_EVIDENCE"
    POLICY_REEVALUATION = "POLICY_REEVALUATION"
    EXPIRY_RECOMPUTE = "EXPIRY_RECOMPUTE"
    SAFETY_REVALIDATION = "SAFETY_REVALIDATION"


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise LiveDecisionDispositionError(f"{field} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise LiveDecisionDispositionError(f"{field} must be valid UTF-8") from exc
    return value


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise LiveDecisionDispositionError(f"{field} must be lowercase SHA-256 hex")
    return text


def _canonical_utc(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveDecisionDispositionError(
            f"{field} must be canonical UTC ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LiveDecisionDispositionError(
            f"{field} must be canonical UTC ISO-8601"
        )
    utc = parsed.astimezone(timezone.utc)
    canonical = utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if text != canonical:
        raise LiveDecisionDispositionError(
            f"{field} must use canonical UTC microsecond Z form"
        )
    return utc


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise LiveDecisionDispositionError("value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LiveDecisionDispositionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class MarketDecisionIdentity:
    sport: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    line: str | None = None

    def __post_init__(self) -> None:
        for field in ("sport", "event_id", "market_id", "selection_id", "side"):
            _text(getattr(self, field), field)
        if self.line is not None:
            _text(self.line, "line")

    def to_dict(self) -> dict[str, object]:
        return {
            "sport": self.sport,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "line": self.line,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "MarketDecisionIdentity":
        fields = {"sport", "event_id", "market_id", "selection_id", "side", "line"}
        if type(raw) is not dict or set(raw) != fields:
            raise LiveDecisionDispositionError("market identity fields mismatch")
        try:
            value = cls(
                sport=raw["sport"],
                event_id=raw["event_id"],
                market_id=raw["market_id"],
                selection_id=raw["selection_id"],
                side=raw["side"],
                line=raw["line"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveDecisionDispositionError("market identity is invalid") from exc
        if value.to_dict() != raw:
            raise LiveDecisionDispositionError("market identity is not canonical")
        return value


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    authority_kind: str
    evidence_id: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.authority_kind, "authority_kind")
        _text(self.evidence_id, "evidence_id")
        _sha256(self.evidence_sha256, "evidence_sha256")

    def to_dict(self) -> dict[str, str]:
        return {
            "authority_kind": self.authority_kind,
            "evidence_id": self.evidence_id,
            "evidence_sha256": self.evidence_sha256,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "EvidenceReference":
        fields = {"authority_kind", "evidence_id", "evidence_sha256"}
        if type(raw) is not dict or set(raw) != fields:
            raise LiveDecisionDispositionError("evidence reference fields mismatch")
        try:
            value = cls(
                authority_kind=raw["authority_kind"],
                evidence_id=raw["evidence_id"],
                evidence_sha256=raw["evidence_sha256"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveDecisionDispositionError("evidence reference is invalid") from exc
        if value.to_dict() != raw:
            raise LiveDecisionDispositionError("evidence reference is not canonical")
        return value


@dataclass(frozen=True, slots=True)
class PredicateEvidence:
    predicate_id: str
    truth: PredicateTruth
    reason_code: str
    evidence: tuple[EvidenceReference, ...]

    def __post_init__(self) -> None:
        _text(self.predicate_id, "predicate_id")
        if type(self.truth) is not PredicateTruth:
            raise LiveDecisionDispositionError("truth must be PredicateTruth")
        _text(self.reason_code, "reason_code")
        values = tuple(self.evidence)
        if not values or not all(type(item) is EvidenceReference for item in values):
            raise LiveDecisionDispositionError(
                "predicate evidence must contain EvidenceReference items"
            )
        identities = [
            (item.authority_kind, item.evidence_id, item.evidence_sha256)
            for item in values
        ]
        if len(set(identities)) != len(identities):
            raise LiveDecisionDispositionError("predicate evidence contains duplicates")
        object.__setattr__(self, "evidence", values)

    def to_dict(self) -> dict[str, object]:
        return {
            "predicate_id": self.predicate_id,
            "truth": self.truth.value,
            "reason_code": self.reason_code,
            "evidence": [item.to_dict() for item in self.evidence],
        }

    @classmethod
    def from_dict(cls, raw: object) -> "PredicateEvidence":
        fields = {"predicate_id", "truth", "reason_code", "evidence"}
        if type(raw) is not dict or set(raw) != fields or type(raw["evidence"]) is not list:
            raise LiveDecisionDispositionError("predicate evidence fields mismatch")
        try:
            value = cls(
                predicate_id=raw["predicate_id"],
                truth=PredicateTruth(raw["truth"]),
                reason_code=raw["reason_code"],
                evidence=tuple(EvidenceReference.from_dict(item) for item in raw["evidence"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveDecisionDispositionError("predicate evidence is invalid") from exc
        if value.to_dict() != raw:
            raise LiveDecisionDispositionError("predicate evidence is not canonical")
        return value


@dataclass(frozen=True, slots=True)
class LiveDecisionDisposition:
    decision_id: str
    strategy_id: str
    strategy_version: str
    market: MarketDecisionIdentity
    required_evidence_policy_sha256: str
    decision_at: str
    expires_at: str
    evaluated_at: str
    disposition: Disposition
    reason_code: str
    predicates: tuple[PredicateEvidence, ...]
    predecessor_disposition_id: str | None = None
    reevaluation_trigger: ReevaluationTrigger | None = None
    schema_version: int = SCHEMA_VERSION
    execution_authorized: bool = False
    provider_write_authorized: bool = False
    learning_outcome_authorized: bool = False
    real_money_execution: bool = False

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise LiveDecisionDispositionError("unsupported disposition schema")
        for field in ("decision_id", "strategy_id", "strategy_version", "reason_code"):
            _text(getattr(self, field), field)
        if type(self.market) is not MarketDecisionIdentity:
            raise LiveDecisionDispositionError("market must be MarketDecisionIdentity")
        _sha256(self.required_evidence_policy_sha256, "required_evidence_policy_sha256")
        if type(self.disposition) is not Disposition:
            raise LiveDecisionDispositionError("disposition must be Disposition")

        decision_at = _canonical_utc(self.decision_at, "decision_at")
        expires_at = _canonical_utc(self.expires_at, "expires_at")
        evaluated_at = _canonical_utc(self.evaluated_at, "evaluated_at")
        if expires_at <= decision_at:
            raise LiveDecisionDispositionError("expires_at must be after decision_at")
        if evaluated_at < decision_at:
            raise LiveDecisionDispositionError("evaluated_at cannot predate decision_at")

        values = tuple(self.predicates)
        if not values or not all(type(item) is PredicateEvidence for item in values):
            raise LiveDecisionDispositionError(
                "predicates must contain PredicateEvidence items"
            )
        predicate_ids = [item.predicate_id for item in values]
        if len(set(predicate_ids)) != len(predicate_ids):
            raise LiveDecisionDispositionError("predicate_id values must be unique")
        object.__setattr__(self, "predicates", values)

        unknown = sum(item.truth is PredicateTruth.UNKNOWN for item in values)
        failed = sum(item.truth is PredicateTruth.FAILED for item in values)
        all_proven = all(item.truth is PredicateTruth.PROVEN for item in values)

        if self.disposition is Disposition.ACTIONABLE:
            if evaluated_at >= expires_at:
                raise LiveDecisionDispositionError("ACTIONABLE disposition is expired")
            if not all_proven:
                raise LiveDecisionDispositionError(
                    "ACTIONABLE requires every required predicate to be PROVEN"
                )
        elif self.disposition is Disposition.NO_BET_POLICY:
            if evaluated_at >= expires_at:
                raise LiveDecisionDispositionError("NO_BET_POLICY disposition is expired")
            if not all_proven:
                raise LiveDecisionDispositionError(
                    "NO_BET_POLICY requires every required predicate to be PROVEN"
                )
        elif self.disposition is Disposition.WAIT_EVIDENCE:
            if evaluated_at >= expires_at:
                raise LiveDecisionDispositionError("WAIT_EVIDENCE disposition is expired")
            if unknown < 1 or failed:
                raise LiveDecisionDispositionError(
                    "WAIT_EVIDENCE requires UNKNOWN evidence and no FAILED predicate"
                )
        elif self.disposition is Disposition.HALT_SAFETY:
            if failed < 1:
                raise LiveDecisionDispositionError(
                    "HALT_SAFETY requires at least one FAILED predicate"
                )
        elif self.disposition is Disposition.EXPIRED:
            if evaluated_at < expires_at:
                raise LiveDecisionDispositionError(
                    "EXPIRED requires evaluation at or after expires_at"
                )
            if failed:
                raise LiveDecisionDispositionError(
                    "FAILED safety evidence must remain HALT_SAFETY, not EXPIRED"
                )

        if (self.predecessor_disposition_id is None) != (self.reevaluation_trigger is None):
            raise LiveDecisionDispositionError(
                "reevaluation predecessor and trigger must be present together"
            )
        if self.predecessor_disposition_id is not None:
            _sha256(self.predecessor_disposition_id, "predecessor_disposition_id")
            if type(self.reevaluation_trigger) is not ReevaluationTrigger:
                raise LiveDecisionDispositionError(
                    "reevaluation_trigger must be ReevaluationTrigger"
                )

        for field in (
            "execution_authorized",
            "provider_write_authorized",
            "learning_outcome_authorized",
            "real_money_execution",
        ):
            value = getattr(self, field)
            if type(value) is not bool or value:
                raise LiveDecisionDispositionError(f"{field} must remain exactly false")

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "market": self.market.to_dict(),
            "required_evidence_policy_sha256": self.required_evidence_policy_sha256,
            "decision_at": self.decision_at,
            "expires_at": self.expires_at,
            "evaluated_at": self.evaluated_at,
            "disposition": self.disposition.value,
            "reason_code": self.reason_code,
            "predicates": [item.to_dict() for item in self.predicates],
            "predecessor_disposition_id": self.predecessor_disposition_id,
            "reevaluation_trigger": (
                self.reevaluation_trigger.value
                if self.reevaluation_trigger is not None
                else None
            ),
            "execution_authorized": False,
            "provider_write_authorized": False,
            "learning_outcome_authorized": False,
            "real_money_execution": False,
        }

    @property
    def disposition_id(self) -> str:
        return _digest(self._identity_payload())

    def to_dict(self) -> dict[str, object]:
        return {**self._identity_payload(), "disposition_id": self.disposition_id}

    def to_json(self) -> str:
        return _canonical_json(self.to_dict()) + "\n"

    @classmethod
    def from_dict(cls, raw: object) -> "LiveDecisionDisposition":
        fields = {
            "schema",
            "schema_version",
            "disposition_id",
            "decision_id",
            "strategy_id",
            "strategy_version",
            "market",
            "required_evidence_policy_sha256",
            "decision_at",
            "expires_at",
            "evaluated_at",
            "disposition",
            "reason_code",
            "predicates",
            "predecessor_disposition_id",
            "reevaluation_trigger",
            "execution_authorized",
            "provider_write_authorized",
            "learning_outcome_authorized",
            "real_money_execution",
        }
        if type(raw) is not dict or set(raw) != fields:
            raise LiveDecisionDispositionError("disposition fields mismatch")
        if raw["schema"] != SCHEMA:
            raise LiveDecisionDispositionError("disposition schema mismatch")
        if type(raw["predicates"]) is not list:
            raise LiveDecisionDispositionError("predicates must be a list")
        trigger_raw = raw["reevaluation_trigger"]
        try:
            value = cls(
                decision_id=raw["decision_id"],
                strategy_id=raw["strategy_id"],
                strategy_version=raw["strategy_version"],
                market=MarketDecisionIdentity.from_dict(raw["market"]),
                required_evidence_policy_sha256=raw["required_evidence_policy_sha256"],
                decision_at=raw["decision_at"],
                expires_at=raw["expires_at"],
                evaluated_at=raw["evaluated_at"],
                disposition=Disposition(raw["disposition"]),
                reason_code=raw["reason_code"],
                predicates=tuple(PredicateEvidence.from_dict(item) for item in raw["predicates"]),
                predecessor_disposition_id=raw["predecessor_disposition_id"],
                reevaluation_trigger=(
                    ReevaluationTrigger(trigger_raw) if trigger_raw is not None else None
                ),
                schema_version=raw["schema_version"],
                execution_authorized=raw["execution_authorized"],
                provider_write_authorized=raw["provider_write_authorized"],
                learning_outcome_authorized=raw["learning_outcome_authorized"],
                real_money_execution=raw["real_money_execution"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, LiveDecisionDispositionError):
                raise
            raise LiveDecisionDispositionError("disposition values are invalid") from exc
        if raw["disposition_id"] != value.disposition_id:
            raise LiveDecisionDispositionError("disposition_id digest mismatch")
        if value.to_dict() != raw:
            raise LiveDecisionDispositionError("disposition payload is not canonical")
        return value

    @classmethod
    def from_json(cls, raw: str) -> "LiveDecisionDisposition":
        if type(raw) is not str:
            raise TypeError("raw must be str")
        try:
            parsed = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    LiveDecisionDispositionError(
                        f"non-finite JSON value is forbidden: {token}"
                    )
                ),
            )
        except json.JSONDecodeError as exc:
            raise LiveDecisionDispositionError("disposition JSON is invalid") from exc
        return cls.from_dict(parsed)
