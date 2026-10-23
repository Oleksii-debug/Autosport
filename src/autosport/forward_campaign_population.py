"""Immutable population firewall for provider-free forward campaigns.

This module is an admission/evidence layer over Autosport's existing authorities.
It does not collect data, settle tickets, evaluate policies, persist a campaign
journal, promote a model, or authorize execution.  Those responsibilities remain
with ``product_source``, replay/outcome/PAPER authorities, ``drift_control``, the
scientific registry, and the Strategy/Model Factory.

The firewall closes one cross-authority gap: a multi-session campaign must prove
that its new, replay, retention, and untouched-holdout populations are disjoint at
sample, event, and source-evidence level, while preserving immutable causal
cutoffs, outcome-after-decision chronology, negative evidence, per-regime
receipts, and deterministic restart verification.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, Mapping

from .forward_evidence_completeness import DecisionState


SCHEMA: Final = "autosport.forward_campaign_population"
SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")
_GENESIS: Final = "GENESIS"


class ForwardCampaignPopulationError(ValueError):
    """Forward campaign population evidence is incomplete or contradictory."""


class PopulationRole(StrEnum):
    NEW = "NEW"
    REPLAY = "REPLAY"
    RETENTION = "RETENTION"
    HOLDOUT = "HOLDOUT"


class TerminalEvidenceState(StrEnum):
    SETTLED = "SETTLED"
    VOID = "VOID"
    NEGATIVE = "NEGATIVE"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ForwardCampaignPopulationError(
            f"{name} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ForwardCampaignPopulationError(f"{name} must be valid UTF-8") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ForwardCampaignPopulationError(f"{name} must be lowercase SHA-256")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardCampaignPopulationError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardCampaignPopulationError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ForwardCampaignPopulationError(f"{name} must be a finite exact Decimal")
    return value


def _decimal_text(value: Decimal) -> str:
    value = _decimal(value, "decimal")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _decimal_from_text(value: object, name: str) -> Decimal:
    text = _text(value, name)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ForwardCampaignPopulationError(f"{name} must be Decimal text") from exc
    if not parsed.is_finite() or _decimal_text(parsed) != text:
        raise ForwardCampaignPopulationError(f"{name} must be canonical Decimal text")
    return parsed


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ForwardCampaignPopulationError(
            "forward campaign evidence is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _strict_mapping(value: object, fields: set[str], name: str) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ForwardCampaignPopulationError(f"{name} fields mismatch")
    return value


@dataclass(frozen=True, slots=True)
class ForwardPopulationSample:
    """One settled or negative-evidence member of exactly one frozen population."""

    sample_id: str
    session_id: str
    role: PopulationRole
    source_id: str
    event_id: str
    regime_id: str
    source_evidence_sha256: str
    source_rights_manifest_sha256: str
    observed_at: str
    available_at: str
    decision_cutoff: str
    decided_at: str
    outcome_available_at: str
    settled_at: str
    decision_state: DecisionState
    terminal_state: TerminalEvidenceState
    terminal_evidence_sha256: str
    observed_reward: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("sample_id", "session_id", "source_id", "event_id", "regime_id"):
            _text(getattr(self, name), name)
        if type(self.role) is not PopulationRole:
            raise ForwardCampaignPopulationError("role must be exact PopulationRole")
        if type(self.decision_state) is not DecisionState:
            raise ForwardCampaignPopulationError(
                "decision_state must be exact canonical DecisionState"
            )
        if type(self.terminal_state) is not TerminalEvidenceState:
            raise ForwardCampaignPopulationError(
                "terminal_state must be exact TerminalEvidenceState"
            )
        for name in (
            "source_evidence_sha256",
            "source_rights_manifest_sha256",
            "terminal_evidence_sha256",
        ):
            _sha256(getattr(self, name), name)

        observed = _instant(self.observed_at, "observed_at")
        available = _instant(self.available_at, "available_at")
        cutoff = _instant(self.decision_cutoff, "decision_cutoff")
        decided = _instant(self.decided_at, "decided_at")
        outcome_available = _instant(self.outcome_available_at, "outcome_available_at")
        settled = _instant(self.settled_at, "settled_at")
        if not observed <= available <= cutoff <= decided:
            raise ForwardCampaignPopulationError(
                "sample violates observed/available/cutoff/decision causal order"
            )
        if outcome_available <= decided:
            raise ForwardCampaignPopulationError(
                "outcome evidence must become available strictly after decision"
            )
        if settled < outcome_available:
            raise ForwardCampaignPopulationError(
                "settlement cannot precede outcome availability"
            )

        if self.decision_state is DecisionState.ACTION:
            if self.terminal_state not in {
                TerminalEvidenceState.SETTLED,
                TerminalEvidenceState.VOID,
            }:
                raise ForwardCampaignPopulationError(
                    "ACTION requires settled or void terminal evidence"
                )
            if self.observed_reward is None:
                raise ForwardCampaignPopulationError(
                    "ACTION requires an observed exact reward"
                )
            _decimal(self.observed_reward, "observed_reward")
        else:
            if self.terminal_state is not TerminalEvidenceState.NEGATIVE:
                raise ForwardCampaignPopulationError(
                    "non-action decisions must remain explicit negative evidence"
                )
            if self.observed_reward is not None:
                raise ForwardCampaignPopulationError(
                    "negative evidence cannot invent an observed reward"
                )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "session_id": self.session_id,
            "role": self.role.value,
            "source_id": self.source_id,
            "event_id": self.event_id,
            "regime_id": self.regime_id,
            "source_evidence_sha256": self.source_evidence_sha256,
            "source_rights_manifest_sha256": self.source_rights_manifest_sha256,
            "observed_at": _timestamp(self.observed_at, "observed_at"),
            "available_at": _timestamp(self.available_at, "available_at"),
            "decision_cutoff": _timestamp(self.decision_cutoff, "decision_cutoff"),
            "decided_at": _timestamp(self.decided_at, "decided_at"),
            "outcome_available_at": _timestamp(
                self.outcome_available_at, "outcome_available_at"
            ),
            "settled_at": _timestamp(self.settled_at, "settled_at"),
            "decision_state": self.decision_state.value,
            "terminal_state": self.terminal_state.value,
            "terminal_evidence_sha256": self.terminal_evidence_sha256,
            "observed_reward": (
                None
                if self.observed_reward is None
                else _decimal_text(self.observed_reward)
            ),
        }

    @property
    def sample_sha256(self) -> str:
        return _digest(
            {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "kind": "population_sample",
                "payload": self.canonical_payload(),
            }
        )

    @classmethod
    def from_payload(cls, payload: object) -> "ForwardPopulationSample":
        fields = {
            "sample_id", "session_id", "role", "source_id", "event_id",
            "regime_id", "source_evidence_sha256", "source_rights_manifest_sha256",
            "observed_at", "available_at", "decision_cutoff", "decided_at",
            "outcome_available_at", "settled_at", "decision_state", "terminal_state",
            "terminal_evidence_sha256", "observed_reward",
        }
        raw = _strict_mapping(payload, fields, "population sample")
        try:
            role = PopulationRole(raw["role"])
            decision = DecisionState(raw["decision_state"])
            terminal = TerminalEvidenceState(raw["terminal_state"])
        except (TypeError, ValueError) as exc:
            raise ForwardCampaignPopulationError(
                "population sample enum value is invalid"
            ) from exc
        reward_raw = raw["observed_reward"]
        reward = None if reward_raw is None else _decimal_from_text(
            reward_raw, "observed_reward"
        )
        return cls(
            sample_id=raw["sample_id"], session_id=raw["session_id"], role=role,
            source_id=raw["source_id"], event_id=raw["event_id"],
            regime_id=raw["regime_id"],
            source_evidence_sha256=raw["source_evidence_sha256"],
            source_rights_manifest_sha256=raw["source_rights_manifest_sha256"],
            observed_at=raw["observed_at"], available_at=raw["available_at"],
            decision_cutoff=raw["decision_cutoff"], decided_at=raw["decided_at"],
            outcome_available_at=raw["outcome_available_at"],
            settled_at=raw["settled_at"], decision_state=decision,
            terminal_state=terminal,
            terminal_evidence_sha256=raw["terminal_evidence_sha256"],
            observed_reward=reward,
        )


@dataclass(frozen=True, slots=True)
class RegimeMetricEvidence:
    """Exact per-session/per-regime counts; descriptive evidence, not an edge claim."""

    regime_id: str
    sample_count: int
    action_count: int
    negative_count: int
    settled_count: int
    observed_reward_sum: Decimal
    evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.regime_id, "regime_id")
        counts = (
            self.sample_count,
            self.action_count,
            self.negative_count,
            self.settled_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ForwardCampaignPopulationError(
                "regime metric counts must be non-negative exact integers"
            )
        if self.action_count + self.negative_count != self.sample_count:
            raise ForwardCampaignPopulationError(
                "regime action plus negative count must equal sample count"
            )
        if self.settled_count > self.action_count:
            raise ForwardCampaignPopulationError(
                "regime settled count cannot exceed action count"
            )
        _decimal(self.observed_reward_sum, "observed_reward_sum")
        _sha256(self.evidence_sha256, "evidence_sha256")

    def payload_without_sha(self) -> dict[str, object]:
        return {
            "regime_id": self.regime_id,
            "sample_count": self.sample_count,
            "action_count": self.action_count,
            "negative_count": self.negative_count,
            "settled_count": self.settled_count,
            "observed_reward_sum": _decimal_text(self.observed_reward_sum),
        }

    def canonical_payload(self) -> dict[str, object]:
        return {**self.payload_without_sha(), "evidence_sha256": self.evidence_sha256}

    @classmethod
    def build(
        cls, regime_id: str, samples: tuple[ForwardPopulationSample, ...]
    ) -> "RegimeMetricEvidence":
        selected = tuple(sample for sample in samples if sample.regime_id == regime_id)
        if not selected:
            raise ForwardCampaignPopulationError("regime metric population is empty")
        actions = tuple(
            sample for sample in selected if sample.decision_state is DecisionState.ACTION
        )
        settled = tuple(
            sample for sample in actions
            if sample.terminal_state is TerminalEvidenceState.SETTLED
        )
        reward_sum = sum(
            (sample.observed_reward for sample in actions if sample.observed_reward is not None),
            Decimal("0"),
        )
        payload = {
            "regime_id": regime_id,
            "sample_count": len(selected),
            "action_count": len(actions),
            "negative_count": len(selected) - len(actions),
            "settled_count": len(settled),
            "observed_reward_sum": _decimal_text(reward_sum),
        }
        return cls(
            regime_id=regime_id,
            sample_count=len(selected),
            action_count=len(actions),
            negative_count=len(selected) - len(actions),
            settled_count=len(settled),
            observed_reward_sum=reward_sum,
            evidence_sha256=_digest(payload),
        )

    @classmethod
    def from_payload(cls, payload: object) -> "RegimeMetricEvidence":
        fields = {
            "regime_id", "sample_count", "action_count", "negative_count",
            "settled_count", "observed_reward_sum", "evidence_sha256",
        }
        raw = _strict_mapping(payload, fields, "regime metric")
        value = cls(
            regime_id=raw["regime_id"], sample_count=raw["sample_count"],
            action_count=raw["action_count"], negative_count=raw["negative_count"],
            settled_count=raw["settled_count"],
            observed_reward_sum=_decimal_from_text(
                raw["observed_reward_sum"], "observed_reward_sum"
            ),
            evidence_sha256=raw["evidence_sha256"],
        )
        if value.evidence_sha256 != _digest(value.payload_without_sha()):
            raise ForwardCampaignPopulationError("regime metric digest mismatch")
        return value


@dataclass(frozen=True, slots=True)
class ForwardCampaignSession:
    """One closed session linked to canonical negative and drift evidence."""

    session_id: str
    session_index: int
    opened_at: str
    closed_at: str
    sample_sha256s: tuple[str, ...]
    regime_metrics: tuple[RegimeMetricEvidence, ...]
    negative_evidence_sha256: str
    drift_evidence_sha256: str
    predecessor_session_sha256: str

    def __post_init__(self) -> None:
        _text(self.session_id, "session_id")
        if type(self.session_index) is not int or self.session_index <= 0:
            raise ForwardCampaignPopulationError(
                "session_index must be a positive exact integer"
            )
        if _instant(self.closed_at, "closed_at") < _instant(self.opened_at, "opened_at"):
            raise ForwardCampaignPopulationError("session cannot close before it opens")
        if type(self.sample_sha256s) is not tuple or not self.sample_sha256s:
            raise ForwardCampaignPopulationError("session samples must be non-empty tuple")
        if self.sample_sha256s != tuple(sorted(self.sample_sha256s)):
            raise ForwardCampaignPopulationError("session sample digests must be sorted")
        if len(self.sample_sha256s) != len(set(self.sample_sha256s)):
            raise ForwardCampaignPopulationError("session sample digests must be unique")
        for value in self.sample_sha256s:
            _sha256(value, "sample_sha256")
        if type(self.regime_metrics) is not tuple or not self.regime_metrics:
            raise ForwardCampaignPopulationError("session regime metrics are required")
        if any(type(value) is not RegimeMetricEvidence for value in self.regime_metrics):
            raise ForwardCampaignPopulationError(
                "session metrics must be exact RegimeMetricEvidence"
            )
        regimes = tuple(value.regime_id for value in self.regime_metrics)
        if regimes != tuple(sorted(regimes)) or len(regimes) != len(set(regimes)):
            raise ForwardCampaignPopulationError(
                "session regime metrics must be sorted and unique"
            )
        _sha256(self.negative_evidence_sha256, "negative_evidence_sha256")
        _sha256(self.drift_evidence_sha256, "drift_evidence_sha256")
        if self.predecessor_session_sha256 != _GENESIS:
            _sha256(self.predecessor_session_sha256, "predecessor_session_sha256")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "session_index": self.session_index,
            "opened_at": _timestamp(self.opened_at, "opened_at"),
            "closed_at": _timestamp(self.closed_at, "closed_at"),
            "sample_sha256s": list(self.sample_sha256s),
            "regime_metrics": [metric.canonical_payload() for metric in self.regime_metrics],
            "negative_evidence_sha256": self.negative_evidence_sha256,
            "drift_evidence_sha256": self.drift_evidence_sha256,
            "predecessor_session_sha256": self.predecessor_session_sha256,
        }

    @property
    def session_sha256(self) -> str:
        return _digest(
            {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "kind": "campaign_session",
                "payload": self.canonical_payload(),
            }
        )

    @classmethod
    def from_payload(cls, payload: object) -> "ForwardCampaignSession":
        fields = {
            "session_id", "session_index", "opened_at", "closed_at",
            "sample_sha256s", "regime_metrics", "negative_evidence_sha256",
            "drift_evidence_sha256", "predecessor_session_sha256",
        }
        raw = _strict_mapping(payload, fields, "campaign session")
        if type(raw["sample_sha256s"]) is not list or type(raw["regime_metrics"]) is not list:
            raise ForwardCampaignPopulationError("session arrays are malformed")
        return cls(
            session_id=raw["session_id"], session_index=raw["session_index"],
            opened_at=raw["opened_at"], closed_at=raw["closed_at"],
            sample_sha256s=tuple(raw["sample_sha256s"]),
            regime_metrics=tuple(
                RegimeMetricEvidence.from_payload(value)
                for value in raw["regime_metrics"]
            ),
            negative_evidence_sha256=raw["negative_evidence_sha256"],
            drift_evidence_sha256=raw["drift_evidence_sha256"],
            predecessor_session_sha256=raw["predecessor_session_sha256"],
        )


def build_forward_campaign_session(
    *,
    session_id: str,
    session_index: int,
    opened_at: str,
    closed_at: str,
    samples: tuple[ForwardPopulationSample, ...],
    drift_evidence_sha256: str,
    predecessor_session_sha256: str = _GENESIS,
) -> ForwardCampaignSession:
    if type(samples) is not tuple or not samples:
        raise ForwardCampaignPopulationError("session samples must be non-empty tuple")
    if any(type(sample) is not ForwardPopulationSample for sample in samples):
        raise ForwardCampaignPopulationError(
            "session samples must be exact ForwardPopulationSample values"
        )
    if any(sample.session_id != session_id for sample in samples):
        raise ForwardCampaignPopulationError("sample belongs to another session")
    opened = _instant(opened_at, "opened_at")
    closed = _instant(closed_at, "closed_at")
    if any(
        _instant(sample.observed_at, "observed_at") < opened
        or _instant(sample.settled_at, "settled_at") > closed
        for sample in samples
    ):
        raise ForwardCampaignPopulationError(
            "session sample chronology falls outside its closed interval"
        )
    regimes = tuple(sorted({sample.regime_id for sample in samples}))
    metrics = tuple(RegimeMetricEvidence.build(regime, samples) for regime in regimes)
    negative_receipt = _digest(
        {
            "session_id": session_id,
            "negative_terminal_evidence": sorted(
                sample.terminal_evidence_sha256
                for sample in samples
                if sample.decision_state is not DecisionState.ACTION
            ),
            "negative_count": sum(
                sample.decision_state is not DecisionState.ACTION for sample in samples
            ),
            "denominator_count": len(samples),
        }
    )
    return ForwardCampaignSession(
        session_id=session_id,
        session_index=session_index,
        opened_at=opened_at,
        closed_at=closed_at,
        sample_sha256s=tuple(sorted(sample.sample_sha256 for sample in samples)),
        regime_metrics=metrics,
        negative_evidence_sha256=negative_receipt,
        drift_evidence_sha256=drift_evidence_sha256,
        predecessor_session_sha256=predecessor_session_sha256,
    )


@dataclass(frozen=True, slots=True)
class ForwardCampaignPopulationManifest:
    """Restart-verifiable campaign population receipt with no promotion authority."""

    campaign_id: str
    scientific_protocol_sha256: str
    created_at: str
    samples: tuple[ForwardPopulationSample, ...]
    sessions: tuple[ForwardCampaignSession, ...]
    role_population_sha256s: tuple[tuple[str, str], ...]
    profitability_claim: bool = False
    promotion_authorized: bool = False
    real_money_execution: bool = False

    def __post_init__(self) -> None:
        _text(self.campaign_id, "campaign_id")
        _sha256(self.scientific_protocol_sha256, "scientific_protocol_sha256")
        _instant(self.created_at, "created_at")
        if type(self.samples) is not tuple or not self.samples:
            raise ForwardCampaignPopulationError("manifest samples must be non-empty tuple")
        if any(type(value) is not ForwardPopulationSample for value in self.samples):
            raise ForwardCampaignPopulationError(
                "manifest samples must be exact ForwardPopulationSample"
            )
        if type(self.sessions) is not tuple or len(self.sessions) < 2:
            raise ForwardCampaignPopulationError(
                "forward campaign requires at least two closed sessions"
            )
        if any(type(value) is not ForwardCampaignSession for value in self.sessions):
            raise ForwardCampaignPopulationError(
                "manifest sessions must be exact ForwardCampaignSession"
            )
        for name in (
            "profitability_claim", "promotion_authorized", "real_money_execution"
        ):
            if getattr(self, name) is not False:
                raise ForwardCampaignPopulationError(
                    f"{name} must remain false for PAPER/SHADOW evidence"
                )
        expected_roles = tuple((role.value, self._role_digest(role)) for role in PopulationRole)
        if self.role_population_sha256s != expected_roles:
            raise ForwardCampaignPopulationError("role population digest mismatch")
        self._validate_population_firewall()
        self._validate_sessions()

    def _role_digest(self, role: PopulationRole) -> str:
        return _digest(
            {
                "role": role.value,
                "sample_sha256s": sorted(
                    sample.sample_sha256 for sample in self.samples if sample.role is role
                ),
            }
        )

    def _validate_population_firewall(self) -> None:
        sample_ids = [sample.sample_id for sample in self.samples]
        event_ids = [sample.event_id for sample in self.samples]
        evidence_ids = [sample.source_evidence_sha256 for sample in self.samples]
        for values, name in (
            (sample_ids, "sample"),
            (event_ids, "event"),
            (evidence_ids, "source evidence"),
        ):
            if len(values) != len(set(values)):
                raise ForwardCampaignPopulationError(
                    f"{name} identity overlaps campaign populations"
                )
        roles = {sample.role for sample in self.samples}
        missing = set(PopulationRole) - roles
        if missing:
            raise ForwardCampaignPopulationError(
                "campaign population roles are incomplete: "
                + ", ".join(sorted(role.value for role in missing))
            )

    def _validate_sessions(self) -> None:
        ordered = tuple(sorted(self.sessions, key=lambda value: value.session_index))
        if ordered != self.sessions:
            raise ForwardCampaignPopulationError("sessions must be index ordered")
        all_sample_hashes: list[str] = []
        predecessor = _GENESIS
        prior_close: datetime | None = None
        sample_by_session: dict[str, tuple[ForwardPopulationSample, ...]] = {}
        for sample in self.samples:
            sample_by_session.setdefault(sample.session_id, ())
            sample_by_session[sample.session_id] += (sample,)
        for expected_index, session in enumerate(self.sessions, start=1):
            if session.session_index != expected_index:
                raise ForwardCampaignPopulationError("session index sequence has a gap")
            if session.predecessor_session_sha256 != predecessor:
                raise ForwardCampaignPopulationError("session hash chain is broken")
            opened = _instant(session.opened_at, "opened_at")
            closed = _instant(session.closed_at, "closed_at")
            if prior_close is not None and opened < prior_close:
                raise ForwardCampaignPopulationError("campaign sessions overlap in time")
            selected = sample_by_session.get(session.session_id, ())
            if not selected:
                raise ForwardCampaignPopulationError("session has no manifest population")
            rebuilt = build_forward_campaign_session(
                session_id=session.session_id,
                session_index=session.session_index,
                opened_at=session.opened_at,
                closed_at=session.closed_at,
                samples=selected,
                drift_evidence_sha256=session.drift_evidence_sha256,
                predecessor_session_sha256=session.predecessor_session_sha256,
            )
            if rebuilt != session:
                raise ForwardCampaignPopulationError(
                    "session metrics or negative evidence changed"
                )
            all_sample_hashes.extend(session.sample_sha256s)
            predecessor = session.session_sha256
            prior_close = closed
        if sorted(all_sample_hashes) != sorted(sample.sample_sha256 for sample in self.samples):
            raise ForwardCampaignPopulationError(
                "session membership does not exactly cover campaign samples"
            )

    def payload_without_manifest_sha(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "scientific_protocol_sha256": self.scientific_protocol_sha256,
            "created_at": _timestamp(self.created_at, "created_at"),
            "samples": [sample.canonical_payload() for sample in self.samples],
            "sessions": [session.canonical_payload() for session in self.sessions],
            "role_population_sha256s": [list(value) for value in self.role_population_sha256s],
            "profitability_claim": self.profitability_claim,
            "promotion_authorized": self.promotion_authorized,
            "real_money_execution": self.real_money_execution,
        }

    @property
    def manifest_sha256(self) -> str:
        return _digest(self.payload_without_manifest_sha())

    def canonical_payload(self) -> dict[str, object]:
        return {**self.payload_without_manifest_sha(), "manifest_sha256": self.manifest_sha256}

    @classmethod
    def from_payload(cls, payload: object) -> "ForwardCampaignPopulationManifest":
        fields = {
            "schema", "schema_version", "campaign_id", "scientific_protocol_sha256",
            "created_at", "samples", "sessions", "role_population_sha256s",
            "profitability_claim", "promotion_authorized", "real_money_execution",
            "manifest_sha256",
        }
        raw = _strict_mapping(payload, fields, "campaign population manifest")
        if raw["schema"] != SCHEMA or raw["schema_version"] != SCHEMA_VERSION:
            raise ForwardCampaignPopulationError("campaign population schema is unsupported")
        if type(raw["samples"]) is not list or type(raw["sessions"]) is not list:
            raise ForwardCampaignPopulationError("manifest arrays are malformed")
        role_pairs_raw = raw["role_population_sha256s"]
        if type(role_pairs_raw) is not list or any(
            type(value) is not list or len(value) != 2 for value in role_pairs_raw
        ):
            raise ForwardCampaignPopulationError("role population digests are malformed")
        value = cls(
            campaign_id=raw["campaign_id"],
            scientific_protocol_sha256=raw["scientific_protocol_sha256"],
            created_at=raw["created_at"],
            samples=tuple(ForwardPopulationSample.from_payload(item) for item in raw["samples"]),
            sessions=tuple(ForwardCampaignSession.from_payload(item) for item in raw["sessions"]),
            role_population_sha256s=tuple((item[0], item[1]) for item in role_pairs_raw),
            profitability_claim=raw["profitability_claim"],
            promotion_authorized=raw["promotion_authorized"],
            real_money_execution=raw["real_money_execution"],
        )
        if raw["manifest_sha256"] != value.manifest_sha256:
            raise ForwardCampaignPopulationError("campaign population manifest digest mismatch")
        return value


def build_forward_campaign_manifest(
    *,
    campaign_id: str,
    scientific_protocol_sha256: str,
    created_at: str,
    samples: tuple[ForwardPopulationSample, ...],
    sessions: tuple[ForwardCampaignSession, ...],
) -> ForwardCampaignPopulationManifest:
    if type(samples) is not tuple or any(
        type(value) is not ForwardPopulationSample for value in samples
    ):
        raise ForwardCampaignPopulationError(
            "samples must be a tuple of exact ForwardPopulationSample values"
        )
    ordered_samples = tuple(sorted(samples, key=lambda value: value.sample_id))
    role_digests = tuple(
        (
            role.value,
            _digest(
                {
                    "role": role.value,
                    "sample_sha256s": sorted(
                        sample.sample_sha256
                        for sample in ordered_samples
                        if sample.role is role
                    ),
                }
            ),
        )
        for role in PopulationRole
    )
    return ForwardCampaignPopulationManifest(
        campaign_id=campaign_id,
        scientific_protocol_sha256=scientific_protocol_sha256,
        created_at=created_at,
        samples=ordered_samples,
        sessions=sessions,
        role_population_sha256s=role_digests,
    )


def verify_forward_campaign_restart(
    manifest: ForwardCampaignPopulationManifest,
) -> bool:
    """Round-trip exact campaign evidence and reject altered restart state."""

    if type(manifest) is not ForwardCampaignPopulationManifest:
        raise TypeError("manifest must be exact ForwardCampaignPopulationManifest")
    restored = ForwardCampaignPopulationManifest.from_payload(
        json.loads(_canonical_bytes(manifest.canonical_payload()).decode("utf-8"))
    )
    if restored != manifest or restored.manifest_sha256 != manifest.manifest_sha256:
        raise ForwardCampaignPopulationError("campaign restart replay mismatch")
    return True


__all__ = [
    "ForwardCampaignPopulationError",
    "ForwardCampaignPopulationManifest",
    "ForwardCampaignSession",
    "ForwardPopulationSample",
    "PopulationRole",
    "RegimeMetricEvidence",
    "TerminalEvidenceState",
    "build_forward_campaign_manifest",
    "build_forward_campaign_session",
    "verify_forward_campaign_restart",
]
