"""Product-owned durable pre-evaluation slot/session evidence authority.

This module deliberately accepts only canonical *facts* from an upstream resolver.
Decision, attrition, freshness, configuration, risk and cost semantics are derived
inside the authority and therefore cannot be supplied by provider members or other
callers as outcome fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, Iterable, Mapping


SCHEMA_VERSION = 1
AUTHORITY_FAMILY = "research.pre-evaluation-slot-decision-session-authority-v1"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical_json(value)).hexdigest()


def _require_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


class DecisionCode(str, Enum):
    """Bounded product-owned pre-evaluation decisions."""

    ELIGIBLE = "eligible"
    SAFE_DENY_MISSING_STATE = "safe_deny_missing_state"
    SAFE_DENY_INVALID_TIME = "safe_deny_invalid_time"
    CONFIG_VETO = "config_veto"
    FRESHNESS_VETO = "freshness_veto"
    RISK_VETO = "risk_veto"
    COST_VETO = "cost_veto"


@dataclass(frozen=True, slots=True)
class PreEvaluationPolicy:
    """Constructor-owned policy used to derive pre-evaluation outcomes."""

    max_age_ns: int

    def __post_init__(self) -> None:
        _require_nonnegative_int("max_age_ns", self.max_age_ns)

    @property
    def digest(self) -> str:
        return _digest(
            {
                "authority_family": AUTHORITY_FAMILY,
                "schema_version": SCHEMA_VERSION,
                "max_age_ns": self.max_age_ns,
            }
        )


@dataclass(frozen=True, slots=True)
class CanonicalCandidateFacts:
    """Raw canonical facts. No outcome/decision fields are accepted here."""

    candidate_id: str
    observed_at_ns: int
    config_enabled: bool
    risk_required_micros: int
    risk_available_micros: int
    cost_estimate_micros: int
    cost_limit_micros: int
    source_authority_id: str
    source_revision: str

    def __post_init__(self) -> None:
        _require_text("candidate_id", self.candidate_id)
        _require_nonnegative_int("observed_at_ns", self.observed_at_ns)
        if not isinstance(self.config_enabled, bool):
            raise ValueError("config_enabled must be bool")
        _require_nonnegative_int("risk_required_micros", self.risk_required_micros)
        _require_nonnegative_int("risk_available_micros", self.risk_available_micros)
        _require_nonnegative_int("cost_estimate_micros", self.cost_estimate_micros)
        _require_nonnegative_int("cost_limit_micros", self.cost_limit_micros)
        _require_text("source_authority_id", self.source_authority_id)
        _require_text("source_revision", self.source_revision)

    def to_payload(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "observed_at_ns": self.observed_at_ns,
            "config_enabled": self.config_enabled,
            "risk_required_micros": self.risk_required_micros,
            "risk_available_micros": self.risk_available_micros,
            "cost_estimate_micros": self.cost_estimate_micros,
            "cost_limit_micros": self.cost_limit_micros,
            "source_authority_id": self.source_authority_id,
            "source_revision": self.source_revision,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "CanonicalCandidateFacts":
        expected = {
            "candidate_id",
            "observed_at_ns",
            "config_enabled",
            "risk_required_micros",
            "risk_available_micros",
            "cost_estimate_micros",
            "cost_limit_micros",
            "source_authority_id",
            "source_revision",
        }
        if set(payload) != expected:
            raise ValueError("candidate facts payload has unexpected fields")
        return cls(
            candidate_id=str(payload["candidate_id"]),
            observed_at_ns=_as_int("observed_at_ns", payload["observed_at_ns"]),
            config_enabled=_as_bool("config_enabled", payload["config_enabled"]),
            risk_required_micros=_as_int(
                "risk_required_micros", payload["risk_required_micros"]
            ),
            risk_available_micros=_as_int(
                "risk_available_micros", payload["risk_available_micros"]
            ),
            cost_estimate_micros=_as_int(
                "cost_estimate_micros", payload["cost_estimate_micros"]
            ),
            cost_limit_micros=_as_int("cost_limit_micros", payload["cost_limit_micros"]),
            source_authority_id=str(payload["source_authority_id"]),
            source_revision=str(payload["source_revision"]),
        )

    @property
    def digest(self) -> str:
        return _digest(self.to_payload())


def _as_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _as_bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


@dataclass(frozen=True, slots=True)
class PreEvaluationSlotEvidence:
    """Immutable slot evidence whose outcome fields are always derived."""

    session_id: str
    candidate_id: str
    evaluated_at_ns: int
    policy_max_age_ns: int
    policy_digest: str
    facts: CanonicalCandidateFacts | None

    def __post_init__(self) -> None:
        _require_text("session_id", self.session_id)
        _require_text("candidate_id", self.candidate_id)
        _require_nonnegative_int("evaluated_at_ns", self.evaluated_at_ns)
        _require_nonnegative_int("policy_max_age_ns", self.policy_max_age_ns)
        _require_text("policy_digest", self.policy_digest)
        expected_policy = PreEvaluationPolicy(self.policy_max_age_ns).digest
        if self.policy_digest != expected_policy:
            raise ValueError("policy_digest does not match policy_max_age_ns")
        if self.facts is not None and self.facts.candidate_id != self.candidate_id:
            raise ValueError("facts candidate_id does not match slot candidate_id")

    @property
    def invalid_time(self) -> bool:
        return self.facts is not None and self.facts.observed_at_ns > self.evaluated_at_ns

    @property
    def age_ns(self) -> int | None:
        if self.facts is None or self.invalid_time:
            return None
        return self.evaluated_at_ns - self.facts.observed_at_ns

    @property
    def config_veto(self) -> bool:
        return self.facts is not None and not self.facts.config_enabled

    @property
    def freshness_veto(self) -> bool:
        return self.age_ns is not None and self.age_ns > self.policy_max_age_ns

    @property
    def risk_shortfall_micros(self) -> int:
        if self.facts is None:
            return 0
        return max(self.facts.risk_required_micros - self.facts.risk_available_micros, 0)

    @property
    def risk_veto(self) -> bool:
        return self.risk_shortfall_micros > 0

    @property
    def cost_overage_micros(self) -> int:
        if self.facts is None:
            return 0
        return max(self.facts.cost_estimate_micros - self.facts.cost_limit_micros, 0)

    @property
    def cost_veto(self) -> bool:
        return self.cost_overage_micros > 0

    @property
    def decision_code(self) -> DecisionCode:
        if self.facts is None:
            return DecisionCode.SAFE_DENY_MISSING_STATE
        if self.invalid_time:
            return DecisionCode.SAFE_DENY_INVALID_TIME
        if self.config_veto:
            return DecisionCode.CONFIG_VETO
        if self.freshness_veto:
            return DecisionCode.FRESHNESS_VETO
        if self.risk_veto:
            return DecisionCode.RISK_VETO
        if self.cost_veto:
            return DecisionCode.COST_VETO
        return DecisionCode.ELIGIBLE

    @property
    def eligible_for_evaluation(self) -> bool:
        return self.decision_code is DecisionCode.ELIGIBLE

    @property
    def dropped(self) -> bool:
        return not self.eligible_for_evaluation

    @property
    def source_digest(self) -> str | None:
        return None if self.facts is None else self.facts.digest

    def to_payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "candidate_id": self.candidate_id,
            "evaluated_at_ns": self.evaluated_at_ns,
            "policy_max_age_ns": self.policy_max_age_ns,
            "policy_digest": self.policy_digest,
            "facts": None if self.facts is None else self.facts.to_payload(),
            "source_digest": self.source_digest,
            "decision_code": self.decision_code.value,
            "eligible_for_evaluation": self.eligible_for_evaluation,
            "dropped": self.dropped,
            "age_ns": self.age_ns,
            "config_veto": self.config_veto,
            "freshness_veto": self.freshness_veto,
            "risk_veto": self.risk_veto,
            "cost_veto": self.cost_veto,
            "risk_shortfall_micros": self.risk_shortfall_micros,
            "cost_overage_micros": self.cost_overage_micros,
        }

    @property
    def evidence_digest(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class PreEvaluationSessionEvidence:
    """Canonical session evidence and additive funnel inputs."""

    session_id: str
    evaluated_at_ns: int
    policy_max_age_ns: int
    policy_digest: str
    slots: tuple[PreEvaluationSlotEvidence, ...]

    def __post_init__(self) -> None:
        _require_text("session_id", self.session_id)
        _require_nonnegative_int("evaluated_at_ns", self.evaluated_at_ns)
        _require_nonnegative_int("policy_max_age_ns", self.policy_max_age_ns)
        expected_policy = PreEvaluationPolicy(self.policy_max_age_ns).digest
        if self.policy_digest != expected_policy:
            raise ValueError("policy_digest does not match policy_max_age_ns")
        ids: list[str] = []
        for slot in self.slots:
            if slot.session_id != self.session_id:
                raise ValueError("slot session_id mismatch")
            if slot.evaluated_at_ns != self.evaluated_at_ns:
                raise ValueError("slot evaluated_at_ns mismatch")
            if slot.policy_digest != self.policy_digest:
                raise ValueError("slot policy_digest mismatch")
            if slot.policy_max_age_ns != self.policy_max_age_ns:
                raise ValueError("slot policy_max_age_ns mismatch")
            ids.append(slot.candidate_id)
        if ids != sorted(ids):
            raise ValueError("slots must be sorted by candidate_id")
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate candidate_id in session evidence")

    @property
    def candidate_count(self) -> int:
        return len(self.slots)

    @property
    def eligible_count(self) -> int:
        return sum(slot.eligible_for_evaluation for slot in self.slots)

    @property
    def dropped_count(self) -> int:
        return sum(slot.dropped for slot in self.slots)

    @property
    def dropped_candidate_ids(self) -> tuple[str, ...]:
        return tuple(slot.candidate_id for slot in self.slots if slot.dropped)

    @property
    def config_veto_count(self) -> int:
        return sum(slot.config_veto for slot in self.slots)

    @property
    def freshness_veto_count(self) -> int:
        return sum(slot.freshness_veto for slot in self.slots)

    @property
    def risk_veto_count(self) -> int:
        return sum(slot.risk_veto for slot in self.slots)

    @property
    def cost_veto_count(self) -> int:
        return sum(slot.cost_veto for slot in self.slots)

    @property
    def total_risk_shortfall_micros(self) -> int:
        return sum(slot.risk_shortfall_micros for slot in self.slots)

    @property
    def total_cost_overage_micros(self) -> int:
        return sum(slot.cost_overage_micros for slot in self.slots)

    def _summary(self) -> dict[str, object]:
        return {
            "candidate_count": self.candidate_count,
            "eligible_count": self.eligible_count,
            "dropped_count": self.dropped_count,
            "dropped_candidate_ids": list(self.dropped_candidate_ids),
            "config_veto_count": self.config_veto_count,
            "freshness_veto_count": self.freshness_veto_count,
            "risk_veto_count": self.risk_veto_count,
            "cost_veto_count": self.cost_veto_count,
            "total_risk_shortfall_micros": self.total_risk_shortfall_micros,
            "total_cost_overage_micros": self.total_cost_overage_micros,
        }

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "authority_family": AUTHORITY_FAMILY,
            "session_id": self.session_id,
            "evaluated_at_ns": self.evaluated_at_ns,
            "policy_max_age_ns": self.policy_max_age_ns,
            "policy_digest": self.policy_digest,
            "slots": [
                {**slot.to_payload(), "evidence_digest": slot.evidence_digest}
                for slot in self.slots
            ],
            "summary": self._summary(),
        }

    @property
    def authority_digest(self) -> str:
        return _digest(self.to_payload())

    @property
    def authority_id(self) -> str:
        return f"pre-evaluation:{self.session_id}:{self.authority_digest[:24]}"

    def combine(self, other: "PreEvaluationSessionEvidence") -> "PreEvaluationSessionEvidence":
        if self.session_id != other.session_id:
            raise ValueError("cannot combine different sessions")
        if self.evaluated_at_ns != other.evaluated_at_ns:
            raise ValueError("cannot combine evidence evaluated at different times")
        if self.policy_digest != other.policy_digest:
            raise ValueError("cannot combine evidence with different policies")
        by_id = {slot.candidate_id: slot for slot in self.slots}
        for slot in other.slots:
            if slot.candidate_id in by_id:
                raise ValueError(f"duplicate candidate_id across shards: {slot.candidate_id}")
            by_id[slot.candidate_id] = slot
        return PreEvaluationSessionEvidence(
            session_id=self.session_id,
            evaluated_at_ns=self.evaluated_at_ns,
            policy_max_age_ns=self.policy_max_age_ns,
            policy_digest=self.policy_digest,
            slots=tuple(by_id[candidate_id] for candidate_id in sorted(by_id)),
        )


CandidateFactsResolver = Callable[[str], CanonicalCandidateFacts | None]


class PreEvaluationEvidenceAuthority:
    """The only constructor that turns canonical candidate facts into outcomes."""

    def __init__(self, policy: PreEvaluationPolicy):
        self._policy = policy

    @property
    def policy(self) -> PreEvaluationPolicy:
        return self._policy

    def evaluate_session(
        self,
        *,
        session_id: str,
        candidate_ids: Iterable[str],
        resolver: CandidateFactsResolver,
        evaluated_at_ns: int,
    ) -> PreEvaluationSessionEvidence:
        _require_text("session_id", session_id)
        _require_nonnegative_int("evaluated_at_ns", evaluated_at_ns)
        if not callable(resolver):
            raise TypeError("resolver must be callable")

        requested: list[str] = []
        seen: set[str] = set()
        for candidate_id in candidate_ids:
            _require_text("candidate_id", candidate_id)
            if candidate_id in seen:
                raise ValueError(f"duplicate candidate_id: {candidate_id}")
            seen.add(candidate_id)
            requested.append(candidate_id)

        slots: list[PreEvaluationSlotEvidence] = []
        for candidate_id in sorted(requested):
            facts = resolver(candidate_id)
            if facts is not None and not isinstance(facts, CanonicalCandidateFacts):
                raise TypeError("resolver must return CanonicalCandidateFacts or None")
            if facts is not None and facts.candidate_id != candidate_id:
                raise ValueError("resolver returned facts for a different candidate")
            slots.append(
                PreEvaluationSlotEvidence(
                    session_id=session_id,
                    candidate_id=candidate_id,
                    evaluated_at_ns=evaluated_at_ns,
                    policy_max_age_ns=self._policy.max_age_ns,
                    policy_digest=self._policy.digest,
                    facts=facts,
                )
            )

        return PreEvaluationSessionEvidence(
            session_id=session_id,
            evaluated_at_ns=evaluated_at_ns,
            policy_max_age_ns=self._policy.max_age_ns,
            policy_digest=self._policy.digest,
            slots=tuple(slots),
        )


class PreEvaluationEvidenceStore:
    """Atomic durable storage with exact semantic replay validation."""

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def save(self, evidence: PreEvaluationSessionEvidence) -> None:
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "authority_family": AUTHORITY_FAMILY,
            "authority_id": evidence.authority_id,
            "authority_digest": evidence.authority_digest,
            "payload": evidence.to_payload(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = _canonical_json(envelope) + b"\n"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def load(self) -> PreEvaluationSessionEvidence:
        try:
            envelope = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid pre-evaluation evidence file") from exc
        if not isinstance(envelope, dict):
            raise ValueError("pre-evaluation evidence envelope must be an object")
        expected = {
            "schema_version",
            "authority_family",
            "authority_id",
            "authority_digest",
            "payload",
        }
        if set(envelope) != expected:
            raise ValueError("pre-evaluation evidence envelope has unexpected fields")
        if envelope["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported pre-evaluation evidence schema")
        if envelope["authority_family"] != AUTHORITY_FAMILY:
            raise ValueError("unexpected pre-evaluation authority family")
        payload = envelope["payload"]
        if not isinstance(payload, dict):
            raise ValueError("pre-evaluation evidence payload must be an object")
        evidence = self._from_payload(payload)
        if evidence.to_payload() != payload:
            raise ValueError("pre-evaluation evidence semantic replay mismatch")
        if envelope["authority_digest"] != evidence.authority_digest:
            raise ValueError("pre-evaluation authority digest mismatch")
        if envelope["authority_id"] != evidence.authority_id:
            raise ValueError("pre-evaluation authority id mismatch")
        return evidence

    @staticmethod
    def _from_payload(payload: Mapping[str, object]) -> PreEvaluationSessionEvidence:
        expected = {
            "schema_version",
            "authority_family",
            "session_id",
            "evaluated_at_ns",
            "policy_max_age_ns",
            "policy_digest",
            "slots",
            "summary",
        }
        if set(payload) != expected:
            raise ValueError("pre-evaluation session payload has unexpected fields")
        if payload["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported pre-evaluation session schema")
        if payload["authority_family"] != AUTHORITY_FAMILY:
            raise ValueError("unexpected pre-evaluation session authority family")
        session_id = str(payload["session_id"])
        evaluated_at_ns = _as_int("evaluated_at_ns", payload["evaluated_at_ns"])
        policy_max_age_ns = _as_int("policy_max_age_ns", payload["policy_max_age_ns"])
        policy_digest = str(payload["policy_digest"])
        raw_slots = payload["slots"]
        if not isinstance(raw_slots, list):
            raise ValueError("slots must be a list")

        slots: list[PreEvaluationSlotEvidence] = []
        for raw_slot in raw_slots:
            if not isinstance(raw_slot, dict):
                raise ValueError("slot must be an object")
            stored = dict(raw_slot)
            stored_digest = stored.pop("evidence_digest", None)
            facts_payload = stored.get("facts")
            facts: CanonicalCandidateFacts | None
            if facts_payload is None:
                facts = None
            elif isinstance(facts_payload, dict):
                facts = CanonicalCandidateFacts.from_payload(facts_payload)
            else:
                raise ValueError("slot facts must be an object or null")
            slot = PreEvaluationSlotEvidence(
                session_id=str(stored.get("session_id")),
                candidate_id=str(stored.get("candidate_id")),
                evaluated_at_ns=_as_int("evaluated_at_ns", stored.get("evaluated_at_ns")),
                policy_max_age_ns=_as_int(
                    "policy_max_age_ns", stored.get("policy_max_age_ns")
                ),
                policy_digest=str(stored.get("policy_digest")),
                facts=facts,
            )
            if slot.to_payload() != stored:
                raise ValueError("slot semantic replay mismatch")
            if stored_digest != slot.evidence_digest:
                raise ValueError("slot evidence digest mismatch")
            slots.append(slot)

        evidence = PreEvaluationSessionEvidence(
            session_id=session_id,
            evaluated_at_ns=evaluated_at_ns,
            policy_max_age_ns=policy_max_age_ns,
            policy_digest=policy_digest,
            slots=tuple(slots),
        )
        if payload["summary"] != evidence._summary():
            raise ValueError("session summary does not replay exactly")
        return evidence
