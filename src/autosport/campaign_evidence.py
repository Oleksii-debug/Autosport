from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .integrity import atomic_write_json
from .scientific_registry import ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock


SCHEMA = "autosport.paper_campaign"
SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")
_DECIMAL_CONTEXT = Context(
    prec=40,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
)
_DECIMAL_CONTEXT.traps[InvalidOperation] = True


class CampaignError(ValueError):
    """Base error for invalid paper/live-observation campaign evidence."""


class CampaignFinalizedError(CampaignError):
    """Raised when a finalized campaign is mutated."""


class CampaignIntegrityError(CampaignError):
    """Raised when durable campaign evidence fails integrity validation."""


class CampaignOutcome(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NULL = "NULL"
    HARMFUL = "HARMFUL"
    INCONCLUSIVE = "INCONCLUSIVE"


class CampaignReadiness(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    INCONCLUSIVE = "INCONCLUSIVE"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise CampaignError(f"{name} must be a non-empty trimmed string")
    if "\x00" in value:
        raise CampaignError(f"{name} must not contain NUL")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise CampaignError(f"{name} must be canonical SHA-256 hex")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_decimal(value: object, name: str, *, non_negative: bool = False) -> str | None:
    if value is None:
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CampaignError(f"{name} must be a Decimal-compatible value") from exc
    if not parsed.is_finite():
        raise CampaignError(f"{name} must be finite")
    if non_negative and parsed < 0:
        raise CampaignError(f"{name} must be non-negative")
    # Do not normalize through a Decimal context: normalize() is arithmetic and can
    # round a high-precision input. Canonicalization here must be representation-only.
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text


def _decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CampaignIntegrityError(f"{name} is not a Decimal") from exc
    if not parsed.is_finite():
        raise CampaignIntegrityError(f"{name} is non-finite")
    return parsed



def _ordered_unique(values: object, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or values is None:
        raise CampaignError(f"{name} must be a collection of strings")
    try:
        items = tuple(_text(item, name) for item in values)
    except TypeError as exc:
        raise CampaignError(f"{name} must be a collection of strings") from exc
    if not allow_empty and not items:
        raise CampaignError(f"{name} must not be empty")
    if items != tuple(sorted(items)) or len(items) != len(set(items)):
        raise CampaignError(f"{name} must be sorted and unique")
    return items


@dataclass(frozen=True, slots=True)
class SessionEvidence:
    session_id: str
    run_id: str
    evidence_id: str
    evidence_sha256: str
    source_sha256: str
    research_protocol_id: str
    protocol_sha256: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    strategy_version_id: str
    model_version_id: str | None
    config_sha256: str
    evaluation_window_start: str
    evaluation_window_end: str
    as_of: str
    available_at: str
    outcome_reveal_after: str | None
    observation_timestamps: tuple[str, ...]
    starting_bankroll: Decimal
    ending_bankroll: Decimal
    net_profit: Decimal
    turnover: Decimal
    bets: int
    wins: int
    losses: int
    voids: int
    brier_sum: Decimal | None = None
    log_loss_sum: Decimal | None = None
    prediction_count: int = 0
    max_drawdown: Decimal | None = None
    peak_exposure: Decimal | None = None
    risk_of_ruin: Decimal | None = None
    volatility: Decimal | None = None
    outcome: CampaignOutcome = CampaignOutcome.INCONCLUSIVE

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "run_id",
            "evidence_id",
            "research_protocol_id",
            "dataset_snapshot_id",
            "strategy_version_id",
        ):
            _text(getattr(self, name), name)
        for name in (
            "evidence_sha256",
            "source_sha256",
            "protocol_sha256",
            "dataset_manifest_sha256",
            "config_sha256",
        ):
            _sha256(getattr(self, name), name)
        if self.model_version_id is not None:
            _text(self.model_version_id, "model_version_id")
        start = _instant(self.evaluation_window_start, "evaluation_window_start")
        end = _instant(self.evaluation_window_end, "evaluation_window_end")
        as_of = _instant(self.as_of, "as_of")
        available = _instant(self.available_at, "available_at")
        if end < start:
            raise CampaignError("evaluation_window_end must not precede evaluation_window_start")
        if as_of < end:
            raise CampaignError("as_of must cover the declared evaluation window")
        if available > as_of:
            raise CampaignError("available_at must not be after as_of")
        if self.outcome_reveal_after is not None and _instant(self.outcome_reveal_after, "outcome_reveal_after") > as_of:
            raise CampaignError("outcome_reveal_after must not be after as_of")
        if type(self.observation_timestamps) is not tuple or not self.observation_timestamps:
            raise CampaignError("observation_timestamps must be a non-empty tuple")
        normalized_times = tuple(_timestamp(ts, "observation_timestamp") for ts in self.observation_timestamps)
        if normalized_times != tuple(sorted(normalized_times)):
            raise CampaignError("observation_timestamps must be sorted")
        if len(normalized_times) != len(set(normalized_times)):
            raise CampaignError("observation_timestamps must be unique")
        for ts in normalized_times:
            point = _instant(ts, "observation_timestamp")
            if point < start or point > end:
                raise CampaignError("every observation timestamp must belong to the declared evaluation window")
            if point > as_of:
                raise CampaignError("observation timestamp cannot be after as_of")
        if self.bets < 0 or self.wins < 0 or self.losses < 0 or self.voids < 0 or self.prediction_count < 0:
            raise CampaignError("session counts must be non-negative integers")
        if any(type(v) is not int for v in (self.bets, self.wins, self.losses, self.voids, self.prediction_count)):
            raise CampaignError("session counts must be integers")
        if self.wins + self.losses + self.voids > self.bets:
            raise CampaignError("wins + losses + voids cannot exceed bets")
        if _canonical_decimal(self.starting_bankroll, "starting_bankroll", non_negative=True) is None:
            raise CampaignError("starting_bankroll is required")
        if _canonical_decimal(self.ending_bankroll, "ending_bankroll", non_negative=True) is None:
            raise CampaignError("ending_bankroll is required")
        _canonical_decimal(self.net_profit, "net_profit")
        _canonical_decimal(self.turnover, "turnover", non_negative=True)
        for name in ("brier_sum", "log_loss_sum", "max_drawdown", "peak_exposure", "risk_of_ruin", "volatility"):
            _canonical_decimal(getattr(self, name), name, non_negative=(name not in {"risk_of_ruin"}))
        if self.risk_of_ruin is not None and not (Decimal("0") <= self.risk_of_ruin <= Decimal("1")):
            raise CampaignError("risk_of_ruin must be between 0 and 1")
        if not isinstance(self.outcome, CampaignOutcome):
            raise CampaignError("outcome must be a CampaignOutcome")
        if self.evidence_sha256.lower() != self.computed_evidence_sha256:
            raise CampaignError("evidence_sha256 does not bind the supplied session evidence")

    @property
    def computed_evidence_sha256(self) -> str:
        payload = self.payload_without_evidence_sha()
        return _digest(payload)

    def payload_without_evidence_sha(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "evidence_id": self.evidence_id,
            "source_sha256": self.source_sha256.lower(),
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256.lower(),
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_manifest_sha256": self.dataset_manifest_sha256.lower(),
            "strategy_version_id": self.strategy_version_id,
            "model_version_id": self.model_version_id,
            "config_sha256": self.config_sha256.lower(),
            "evaluation_window_start": _timestamp(self.evaluation_window_start, "evaluation_window_start"),
            "evaluation_window_end": _timestamp(self.evaluation_window_end, "evaluation_window_end"),
            "as_of": _timestamp(self.as_of, "as_of"),
            "available_at": _timestamp(self.available_at, "available_at"),
            "outcome_reveal_after": (
                None if self.outcome_reveal_after is None
                else _timestamp(self.outcome_reveal_after, "outcome_reveal_after")
            ),
            "observation_timestamps": [
                _timestamp(ts, "observation_timestamp") for ts in self.observation_timestamps
            ],
            "starting_bankroll": _canonical_decimal(self.starting_bankroll, "starting_bankroll", non_negative=True),
            "ending_bankroll": _canonical_decimal(self.ending_bankroll, "ending_bankroll", non_negative=True),
            "net_profit": _canonical_decimal(self.net_profit, "net_profit"),
            "turnover": _canonical_decimal(self.turnover, "turnover", non_negative=True),
            "bets": self.bets,
            "wins": self.wins,
            "losses": self.losses,
            "voids": self.voids,
            "brier_sum": _canonical_decimal(self.brier_sum, "brier_sum", non_negative=True),
            "log_loss_sum": _canonical_decimal(self.log_loss_sum, "log_loss_sum", non_negative=True),
            "prediction_count": self.prediction_count,
            "max_drawdown": _canonical_decimal(self.max_drawdown, "max_drawdown", non_negative=True),
            "peak_exposure": _canonical_decimal(self.peak_exposure, "peak_exposure", non_negative=True),
            "risk_of_ruin": _canonical_decimal(self.risk_of_ruin, "risk_of_ruin"),
            "volatility": _canonical_decimal(self.volatility, "volatility", non_negative=True),
            "outcome": self.outcome.value,
        }

    def to_payload(self) -> dict[str, object]:
        payload = self.payload_without_evidence_sha()
        payload["evidence_sha256"] = self.evidence_sha256.lower()
        return payload

    @classmethod
    def build(cls, **kwargs: object) -> "SessionEvidence":
        provisional = dict(kwargs)
        provisional["evidence_sha256"] = "0" * 64
        obj = object.__new__(cls)
        for field_def in cls.__dataclass_fields__.values():
            object.__setattr__(obj, field_def.name, provisional.get(field_def.name, field_def.default))
        digest = obj.computed_evidence_sha256
        provisional["evidence_sha256"] = digest
        return cls(**provisional)


@dataclass(frozen=True, slots=True)
class CampaignSummary:
    campaign_id: str
    campaign_version: int
    status: str
    session_ids: tuple[str, ...]
    session_count: int
    starting_bankroll_total: Decimal
    ending_bankroll_total: Decimal
    net_profit_total: Decimal
    turnover_total: Decimal
    roi: Decimal | None
    bets: int
    wins: int
    losses: int
    voids: int
    hit_rate: Decimal | None
    brier_score: Decimal | None
    log_loss: Decimal | None
    max_session_drawdown: Decimal | None
    max_peak_exposure: Decimal | None
    risk_of_ruin: Decimal | None
    volatility: Decimal | None
    outcome: CampaignOutcome | None
    readiness: CampaignReadiness | None
    campaign_sha256: str | None


@dataclass(slots=True)
class PaperCampaign:
    campaign_id: str
    campaign_version: int
    research_protocol_id: str
    protocol_sha256: str
    hypothesis_id: str
    primary_metric: str
    protective_metrics: tuple[str, ...]
    evaluation_window_start: str
    evaluation_window_end: str
    evaluation_as_of: str
    readiness_rule: str
    source_sha256: str
    created_at: str
    strategy_version_id: str
    model_version_id: str | None = None
    sessions: list[SessionEvidence] = field(default_factory=list)
    finalized: bool = False
    finalized_at: str | None = None
    outcome: CampaignOutcome | None = None
    readiness: CampaignReadiness | None = None
    campaign_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "campaign_id",
            "research_protocol_id",
            "hypothesis_id",
            "primary_metric",
            "readiness_rule",
            "strategy_version_id",
        ):
            _text(getattr(self, name), name)
        for name in ("protocol_sha256", "source_sha256"):
            _sha256(getattr(self, name), name)
        if self.model_version_id is not None:
            _text(self.model_version_id, "model_version_id")
        if type(self.campaign_version) is not int or isinstance(self.campaign_version, bool) or self.campaign_version <= 0:
            raise CampaignError("campaign_version must be a positive integer")
        self.protective_metrics = _ordered_unique(self.protective_metrics, "protective_metrics", allow_empty=True)
        start = _instant(self.evaluation_window_start, "evaluation_window_start")
        end = _instant(self.evaluation_window_end, "evaluation_window_end")
        as_of = _instant(self.evaluation_as_of, "evaluation_as_of")
        if end < start or as_of < end:
            raise CampaignError("campaign evaluation boundaries are invalid")
        _instant(self.created_at, "created_at")
        if self.finalized_at is not None:
            _instant(self.finalized_at, "finalized_at")
        if self.finalized and self.finalized_at is None:
            raise CampaignError("finalized_at is required for a finalized campaign")
        if self.finalized and self.outcome is None:
            raise CampaignError("outcome is required for a finalized campaign")
        if self.finalized and self.readiness is None:
            raise CampaignError("readiness is required for a finalized campaign")
        if not isinstance(self.sessions, list):
            raise CampaignError("sessions must be a list")
        self._validate_sessions()
        if self.finalized:
            expected = self._computed_campaign_sha256()
            if self.campaign_sha256 != expected:
                raise CampaignIntegrityError("campaign_sha256 does not match finalized campaign")
        elif self.campaign_sha256 is not None:
            raise CampaignError("draft campaign must not have campaign_sha256")

    def _validate_sessions(self) -> None:
        seen_session_ids: set[str] = set()
        seen_run_ids: set[str] = set()
        seen_evidence_ids: set[str] = set()
        for session in self.sessions:
            if not isinstance(session, SessionEvidence):
                raise CampaignError("sessions must contain SessionEvidence values")
            if session.session_id in seen_session_ids:
                raise CampaignError("duplicate session membership is forbidden")
            if session.run_id in seen_run_ids:
                raise CampaignError("duplicate run_id membership is forbidden")
            if session.evidence_id in seen_evidence_ids:
                raise CampaignError("duplicate evidence_id membership is forbidden")
            seen_session_ids.add(session.session_id)
            seen_run_ids.add(session.run_id)
            seen_evidence_ids.add(session.evidence_id)
            if session.research_protocol_id != self.research_protocol_id:
                raise CampaignError("session research_protocol_id mismatches campaign")
            if session.protocol_sha256.lower() != self.protocol_sha256.lower():
                raise CampaignError("session protocol_sha256 mismatches campaign")
            if session.strategy_version_id != self.strategy_version_id:
                raise CampaignError("session strategy_version_id mismatches campaign")
            if session.model_version_id != self.model_version_id:
                raise CampaignError("session model_version_id mismatches campaign")
            if session.source_sha256.lower() != self.source_sha256.lower():
                raise CampaignError("session source_sha256 mismatches campaign")
            start = _instant(self.evaluation_window_start, "evaluation_window_start")
            end = _instant(self.evaluation_window_end, "evaluation_window_end")
            session_start = _instant(session.evaluation_window_start, "session.evaluation_window_start")
            session_end = _instant(session.evaluation_window_end, "session.evaluation_window_end")
            if session_start < start or session_end > end:
                raise CampaignError("session evaluation window escapes campaign boundary")
            if _instant(session.as_of, "session.as_of") > _instant(self.evaluation_as_of, "evaluation_as_of"):
                raise CampaignError("session as_of is after campaign evaluation_as_of")
            if _instant(session.available_at, "session.available_at") > _instant(self.evaluation_as_of, "evaluation_as_of"):
                raise CampaignError("session evidence was not available at campaign evaluation_as_of")
            if session.outcome_reveal_after is not None and _instant(session.outcome_reveal_after, "session.outcome_reveal_after") > _instant(
                self.evaluation_as_of, "evaluation_as_of"
            ):
                raise CampaignError("session outcome was not causally revealed at campaign evaluation_as_of")

    def add_session(self, session: SessionEvidence) -> None:
        if self.finalized:
            raise CampaignFinalizedError("finalized campaign cannot accept new session evidence")
        self.sessions.append(session)
        try:
            self._validate_sessions()
        except Exception:
            self.sessions.pop()
            raise

    def remove_session(self, session_id: str) -> SessionEvidence:
        if self.finalized:
            raise CampaignFinalizedError("finalized campaign cannot remove session evidence")
        _text(session_id, "session_id")
        for index, session in enumerate(self.sessions):
            if session.session_id == session_id:
                return self.sessions.pop(index)
        raise CampaignError(f"unknown session_id {session_id}")

    def finalize(
        self,
        *,
        outcome: CampaignOutcome,
        readiness: CampaignReadiness,
        finalized_at: str,
    ) -> CampaignSummary:
        if self.finalized:
            raise CampaignFinalizedError("campaign is already finalized")
        if not isinstance(outcome, CampaignOutcome):
            raise CampaignError("outcome must be a CampaignOutcome")
        if not isinstance(readiness, CampaignReadiness):
            raise CampaignError("readiness must be a CampaignReadiness")
        when = _instant(finalized_at, "finalized_at")
        if when < _instant(self.evaluation_as_of, "evaluation_as_of"):
            raise CampaignError("finalized_at must not precede evaluation_as_of")
        if not self.sessions:
            raise CampaignError("campaign requires at least one session before finalization")
        self._validate_sessions()
        self.finalized = True
        self.finalized_at = when.isoformat().replace("+00:00", "Z")
        self.outcome = outcome
        self.readiness = readiness
        self.campaign_sha256 = self._computed_campaign_sha256()
        return self.summary()

    def fork_new_version(self, new_version: int) -> "PaperCampaign":
        if not self.finalized:
            raise CampaignError("only a finalized campaign can fork a new version")
        if type(new_version) is not int or isinstance(new_version, bool) or new_version <= self.campaign_version:
            raise CampaignError("new_version must be greater than the finalized version")
        return PaperCampaign(
            campaign_id=self.campaign_id,
            campaign_version=new_version,
            research_protocol_id=self.research_protocol_id,
            protocol_sha256=self.protocol_sha256,
            hypothesis_id=self.hypothesis_id,
            primary_metric=self.primary_metric,
            protective_metrics=self.protective_metrics,
            evaluation_window_start=self.evaluation_window_start,
            evaluation_window_end=self.evaluation_window_end,
            evaluation_as_of=self.evaluation_as_of,
            readiness_rule=self.readiness_rule,
            source_sha256=self.source_sha256,
            created_at=self.created_at,
            strategy_version_id=self.strategy_version_id,
            model_version_id=self.model_version_id,
        )

    def summary(self) -> CampaignSummary:
        sessions = tuple(self.sessions)
        starting = _sum_decimal(s.starting_bankroll for s in sessions)
        ending = _sum_decimal(s.ending_bankroll for s in sessions)
        net = _sum_decimal(s.net_profit for s in sessions)
        turnover = _sum_decimal(s.turnover for s in sessions)
        roi = _safe_ratio(net, turnover)
        bets = sum(s.bets for s in sessions)
        wins = sum(s.wins for s in sessions)
        losses = sum(s.losses for s in sessions)
        voids = sum(s.voids for s in sessions)
        total_predictions = sum(s.prediction_count for s in sessions)
        brier_sum = _sum_optional(s.brier_sum for s in sessions)
        log_loss_sum = _sum_optional(s.log_loss_sum for s in sessions)
        brier_score = _safe_ratio(brier_sum, Decimal(total_predictions)) if brier_sum is not None and total_predictions else None
        log_loss = _safe_ratio(log_loss_sum, Decimal(total_predictions)) if log_loss_sum is not None and total_predictions else None
        hit_rate = _safe_ratio(Decimal(wins), Decimal(wins + losses)) if wins + losses else None
        drawdowns = [s.max_drawdown for s in sessions if s.max_drawdown is not None]
        exposures = [s.peak_exposure for s in sessions if s.peak_exposure is not None]
        risks = [s.risk_of_ruin for s in sessions if s.risk_of_ruin is not None]
        vols = [s.volatility for s in sessions if s.volatility is not None]
        return CampaignSummary(
            campaign_id=self.campaign_id,
            campaign_version=self.campaign_version,
            status="FINALIZED" if self.finalized else "DRAFT",
            session_ids=tuple(s.session_id for s in sessions),
            session_count=len(sessions),
            starting_bankroll_total=starting,
            ending_bankroll_total=ending,
            net_profit_total=net,
            turnover_total=turnover,
            roi=roi,
            bets=bets,
            wins=wins,
            losses=losses,
            voids=voids,
            hit_rate=hit_rate,
            brier_score=brier_score,
            log_loss=log_loss,
            max_session_drawdown=max(drawdowns) if drawdowns else None,
            max_peak_exposure=max(exposures) if exposures else None,
            risk_of_ruin=max(risks) if risks else None,
            volatility=_mean_optional(vols),
            outcome=self.outcome,
            readiness=self.readiness,
            campaign_sha256=self.campaign_sha256,
        )

    def _computed_campaign_sha256(self) -> str:
        frozen = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "campaign_version": self.campaign_version,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256.lower(),
            "hypothesis_id": self.hypothesis_id,
            "primary_metric": self.primary_metric,
            "protective_metrics": list(self.protective_metrics),
            "evaluation_window_start": _timestamp(self.evaluation_window_start, "evaluation_window_start"),
            "evaluation_window_end": _timestamp(self.evaluation_window_end, "evaluation_window_end"),
            "evaluation_as_of": _timestamp(self.evaluation_as_of, "evaluation_as_of"),
            "readiness_rule": self.readiness_rule,
            "source_sha256": self.source_sha256.lower(),
            "created_at": _timestamp(self.created_at, "created_at"),
            "strategy_version_id": self.strategy_version_id,
            "model_version_id": self.model_version_id,
            "sessions": [session.to_payload() for session in self.sessions],
            "finalized_at": self.finalized_at,
            "outcome": None if self.outcome is None else self.outcome.value,
            "readiness": None if self.readiness is None else self.readiness.value,
        }
        return _digest(frozen)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "campaign_version": self.campaign_version,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256.lower(),
            "hypothesis_id": self.hypothesis_id,
            "primary_metric": self.primary_metric,
            "protective_metrics": list(self.protective_metrics),
            "evaluation_window_start": _timestamp(self.evaluation_window_start, "evaluation_window_start"),
            "evaluation_window_end": _timestamp(self.evaluation_window_end, "evaluation_window_end"),
            "evaluation_as_of": _timestamp(self.evaluation_as_of, "evaluation_as_of"),
            "readiness_rule": self.readiness_rule,
            "source_sha256": self.source_sha256.lower(),
            "created_at": _timestamp(self.created_at, "created_at"),
            "strategy_version_id": self.strategy_version_id,
            "model_version_id": self.model_version_id,
            "sessions": [session.to_payload() for session in self.sessions],
            "finalized": self.finalized,
            "finalized_at": self.finalized_at,
            "outcome": None if self.outcome is None else self.outcome.value,
            "readiness": None if self.readiness is None else self.readiness.value,
            "campaign_sha256": self.campaign_sha256,
        }

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_payload()
        state = dict(payload)
        state["state_sha256"] = _digest(payload)
        with WorkspaceEconomicLock(destination.parent):
            atomic_write_json(destination, state)

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        scientific_registry: ScientificRegistry | None = None,
    ) -> "PaperCampaign":
        destination = Path(path)
        raw_text = destination.read_text(encoding="utf-8")
        try:
            state = json.loads(
                raw_text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CampaignIntegrityError("campaign state must be valid UTF-8 JSON") from exc
        if type(state) is not dict:
            raise CampaignIntegrityError("campaign state must be an object")
        state_sha = state.pop("state_sha256", None)
        if state_sha is None or _sha256(state_sha, "state_sha256") != _digest(state):
            raise CampaignIntegrityError("campaign state digest mismatch")
        if state.get("schema") != SCHEMA or state.get("schema_version") != SCHEMA_VERSION:
            raise CampaignIntegrityError("campaign schema mismatch")
        required = {
            "schema",
            "schema_version",
            "campaign_id",
            "campaign_version",
            "research_protocol_id",
            "protocol_sha256",
            "hypothesis_id",
            "primary_metric",
            "protective_metrics",
            "evaluation_window_start",
            "evaluation_window_end",
            "evaluation_as_of",
            "readiness_rule",
            "source_sha256",
            "created_at",
            "strategy_version_id",
            "model_version_id",
            "sessions",
            "finalized",
            "finalized_at",
            "outcome",
            "readiness",
            "campaign_sha256",
        }
        if set(state) != required:
            raise CampaignIntegrityError("campaign state fields mismatch")
        sessions = [_session_from_payload(item) for item in state["sessions"]]
        campaign = cls(
            campaign_id=state["campaign_id"],
            campaign_version=state["campaign_version"],
            research_protocol_id=state["research_protocol_id"],
            protocol_sha256=state["protocol_sha256"],
            hypothesis_id=state["hypothesis_id"],
            primary_metric=state["primary_metric"],
            protective_metrics=tuple(state["protective_metrics"]),
            evaluation_window_start=state["evaluation_window_start"],
            evaluation_window_end=state["evaluation_window_end"],
            evaluation_as_of=state["evaluation_as_of"],
            readiness_rule=state["readiness_rule"],
            source_sha256=state["source_sha256"],
            created_at=state["created_at"],
            strategy_version_id=state["strategy_version_id"],
            model_version_id=state["model_version_id"],
            sessions=sessions,
            finalized=state["finalized"],
            finalized_at=state["finalized_at"],
            outcome=None if state["outcome"] is None else CampaignOutcome(state["outcome"]),
            readiness=None if state["readiness"] is None else CampaignReadiness(state["readiness"]),
            campaign_sha256=state["campaign_sha256"],
        )
        if scientific_registry is not None:
            _validate_registry_bindings(campaign, scientific_registry)
        return campaign

    def evidence_summary(self) -> str:
        summary = self.summary()
        parts = [
            f"Campaign {summary.campaign_id} v{summary.campaign_version} [{summary.status}]",
            f"sessions={summary.session_count}",
            f"net_profit={summary.net_profit_total}",
            f"turnover={summary.turnover_total}",
            f"roi={summary.roi if summary.roi is not None else 'TBD'}",
            f"hit_rate={summary.hit_rate if summary.hit_rate is not None else 'TBD'}",
            f"brier_score={summary.brier_score if summary.brier_score is not None else 'TBD'}",
            f"log_loss={summary.log_loss if summary.log_loss is not None else 'TBD'}",
            f"max_session_drawdown={summary.max_session_drawdown if summary.max_session_drawdown is not None else 'TBD'}",
            f"risk_of_ruin={summary.risk_of_ruin if summary.risk_of_ruin is not None else 'TBD'}",
            f"volatility={summary.volatility if summary.volatility is not None else 'TBD'}",
        ]
        if self.finalized:
            parts.extend([
                f"outcome={summary.outcome.value}",
                f"readiness={summary.readiness.value}",
                f"campaign_sha256={summary.campaign_sha256}",
                f"primary_metric={self.primary_metric}",
                f"readiness_rule={self.readiness_rule}",
            ])
        return "; ".join(parts)

    def export_summary(self) -> dict[str, object]:
        summary = self.summary()
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "campaign_id": summary.campaign_id,
            "campaign_version": summary.campaign_version,
            "status": summary.status,
            "session_ids": list(summary.session_ids),
            "session_count": summary.session_count,
            "metrics": {
                "starting_bankroll_total": _canonical_decimal(summary.starting_bankroll_total, "starting_bankroll_total"),
                "ending_bankroll_total": _canonical_decimal(summary.ending_bankroll_total, "ending_bankroll_total"),
                "net_profit_total": _canonical_decimal(summary.net_profit_total, "net_profit_total"),
                "turnover_total": _canonical_decimal(summary.turnover_total, "turnover_total"),
                "roi": _canonical_decimal(summary.roi, "roi"),
                "bets": summary.bets,
                "wins": summary.wins,
                "losses": summary.losses,
                "voids": summary.voids,
                "hit_rate": _canonical_decimal(summary.hit_rate, "hit_rate"),
                "brier_score": _canonical_decimal(summary.brier_score, "brier_score"),
                "log_loss": _canonical_decimal(summary.log_loss, "log_loss"),
                "max_session_drawdown": _canonical_decimal(summary.max_session_drawdown, "max_session_drawdown"),
                "max_peak_exposure": _canonical_decimal(summary.max_peak_exposure, "max_peak_exposure"),
                "risk_of_ruin": _canonical_decimal(summary.risk_of_ruin, "risk_of_ruin"),
                "volatility": _canonical_decimal(summary.volatility, "volatility"),
            },
            "outcome": None if summary.outcome is None else summary.outcome.value,
            "readiness": None if summary.readiness is None else summary.readiness.value,
            "campaign_sha256": summary.campaign_sha256,
        }


def _sum_decimal(values: object) -> Decimal:
    values_tuple = tuple(values)  # type: ignore[arg-type]
    if not values_tuple:
        return Decimal("0")
    minimum_exponent: int | None = None
    components: list[tuple[int, int]] = []
    for value in values_tuple:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise CampaignError("campaign aggregate contains non-finite Decimal")
        parts = value.as_tuple()
        if not parts.digits:
            continue
        significant_end = len(parts.digits)
        while significant_end > 1 and parts.digits[significant_end - 1] == 0:
            significant_end -= 1
        significant_digits = parts.digits[:significant_end]
        normalized_exponent = parts.exponent + (len(parts.digits) - significant_end)
        adjusted_exponent = normalized_exponent + len(significant_digits) - 1
        if normalized_exponent < _DECIMAL_CONTEXT.Etiny() or adjusted_exponent > _DECIMAL_CONTEXT.Emax:
            raise CampaignError("campaign aggregate Decimal is outside the bounded envelope")
        coefficient = 0
        for digit in significant_digits:
            coefficient = coefficient * 10 + digit
        if parts.sign:
            coefficient = -coefficient
        components.append((coefficient, normalized_exponent))
        minimum_exponent = (
            normalized_exponent
            if minimum_exponent is None
            else min(minimum_exponent, normalized_exponent)
        )
    if minimum_exponent is None:
        return Decimal("0")
    total = 0
    for coefficient, exponent in components:
        total += coefficient * (10 ** (exponent - minimum_exponent))
    if total == 0:
        return Decimal("0")
    sign = 1 if total < 0 else 0
    digits = tuple(int(ch) for ch in str(abs(total)))
    result = Decimal((sign, digits, minimum_exponent))
    result_tuple = result.as_tuple()
    if result_tuple.digits:
        result_significant_end = len(result_tuple.digits)
        while result_significant_end > 1 and result_tuple.digits[result_significant_end - 1] == 0:
            result_significant_end -= 1
        result_normalized_exponent = result_tuple.exponent + (
            len(result_tuple.digits) - result_significant_end
        )
        result_adjusted_exponent = (
            result_normalized_exponent + result_significant_end - 1
        )
        if (
            result_normalized_exponent < _DECIMAL_CONTEXT.Etiny()
            or result_adjusted_exponent > _DECIMAL_CONTEXT.Emax
        ):
            raise CampaignError("campaign aggregate Decimal result is outside the bounded envelope")
    return result


def _sum_optional(values: object) -> Decimal | None:
    values_tuple = tuple(values)  # type: ignore[arg-type]
    if any(value is None for value in values_tuple):
        return None
    return _sum_decimal(values_tuple)


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    with localcontext(_DECIMAL_CONTEXT):
        return (numerator / denominator).normalize()


def _mean_optional(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return _safe_ratio(_sum_decimal(values), Decimal(len(values)))


def _session_from_payload(raw: object) -> SessionEvidence:
    if type(raw) is not dict:
        raise CampaignIntegrityError("session payload must be an object")
    required = set(SessionEvidence.__dataclass_fields__)
    if set(raw) != required:
        raise CampaignIntegrityError("session payload fields mismatch")
    try:
        return SessionEvidence(
            **{
                **raw,
                "model_version_id": raw["model_version_id"],
                "observation_timestamps": tuple(raw["observation_timestamps"]),
                "starting_bankroll": _decimal(raw["starting_bankroll"], "starting_bankroll"),
                "ending_bankroll": _decimal(raw["ending_bankroll"], "ending_bankroll"),
                "net_profit": _decimal(raw["net_profit"], "net_profit"),
                "turnover": _decimal(raw["turnover"], "turnover"),
                "brier_sum": None if raw["brier_sum"] is None else _decimal(raw["brier_sum"], "brier_sum"),
                "log_loss_sum": None if raw["log_loss_sum"] is None else _decimal(raw["log_loss_sum"], "log_loss_sum"),
                "max_drawdown": None if raw["max_drawdown"] is None else _decimal(raw["max_drawdown"], "max_drawdown"),
                "peak_exposure": None if raw["peak_exposure"] is None else _decimal(raw["peak_exposure"], "peak_exposure"),
                "risk_of_ruin": None if raw["risk_of_ruin"] is None else _decimal(raw["risk_of_ruin"], "risk_of_ruin"),
                "volatility": None if raw["volatility"] is None else _decimal(raw["volatility"], "volatility"),
                "outcome": CampaignOutcome(raw["outcome"]),
            }
        )
    except (KeyError, TypeError, CampaignError) as exc:
        raise CampaignIntegrityError("invalid session evidence payload") from exc


def _validate_registry_bindings(campaign: PaperCampaign, registry: ScientificRegistry) -> None:
    if not isinstance(registry, ScientificRegistry):
        raise CampaignError("scientific_registry must be ScientificRegistry")
    bindings = (
        ("ResearchProtocol", campaign.research_protocol_id, campaign.protocol_sha256),
        ("Hypothesis", campaign.hypothesis_id, None),
    )
    for kind, identity, expected_sha in bindings:
        entry = registry.get(kind, identity)
        if entry is None:
            raise CampaignIntegrityError(f"missing scientific registry binding {kind}:{identity}")
        if expected_sha is not None and entry.payload.get("protocol_sha256") != expected_sha.lower():
            raise CampaignIntegrityError(f"{kind}:{identity} hash binding mismatch")
    for session in campaign.sessions:
        for kind, identity in (
            ("StrategyVersion", session.strategy_version_id),
            ("DatasetSnapshot", session.dataset_snapshot_id),
        ):
            entry = registry.get(kind, identity)
            if entry is None:
                raise CampaignIntegrityError(f"missing scientific registry binding {kind}:{identity}")
        if session.model_version_id is not None and registry.get("ModelVersion", session.model_version_id) is None:
            raise CampaignIntegrityError(f"missing scientific registry binding ModelVersion:{session.model_version_id}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CampaignIntegrityError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise CampaignIntegrityError(f"non-finite JSON constant: {value}")
