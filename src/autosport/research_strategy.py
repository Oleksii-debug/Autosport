from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .agents import AgentContext
from .candidate_search import CandidateLeg, ParlayCandidate
from .domain import MarketEvent
from .forecasting import ForecastRecord, parse_iso_timestamp
from .price_truth import paper_quote_rejection_reason
from .research_pipeline import ResearchDecisionPipeline, ResearchEvidence
from .scenario_search import ScenarioGroup, ScenarioOutcome


RESEARCH_STRATEGY_ID = "research-replay-v1"


def _stable_event_projection(event: MarketEvent) -> dict[str, Any]:
    """Stable causal quote projection used to bind offline research evidence to replay state."""

    return {
        "event_id": event.event_id,
        "market_id": event.market_id,
        "selection_id": event.selection_id,
        "decimal_odds": str(event.decimal_odds),
        "observed_ts": event.observed_ts,
        "source_id": event.source_id,
        "sequence": event.sequence,
        "market_type": event.market_type.value,
        "status": event.status,
        "source_ts": event.source_ts,
        "score_state": event.score_state,
        "metadata": event.metadata,
    }


def market_event_evidence_hash(event: MarketEvent) -> str:
    canonical = json.dumps(
        _stable_event_projection(event),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def research_market_snapshot_hash(
    latest_quotes: dict[str, MarketEvent],
    quote_keys: Iterable[str],
) -> str:
    keys = tuple(sorted(set(quote_keys)))
    projection: dict[str, dict[str, Any]] = {}
    for key in keys:
        event = latest_quotes.get(key)
        if event is None:
            raise ValueError(f"research snapshot missing replay quote: {key}")
        projection[key] = _stable_event_projection(event)
    canonical = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchReplayInstruction:
    decision_id: str
    trigger_quote_key: str
    decision_ts: str
    stake: Decimal
    candidate: ParlayCandidate
    groups: tuple[ScenarioGroup, ...]
    forecasts: tuple[ForecastRecord, ...]
    evidence: tuple[ResearchEvidence, ...]

    def __post_init__(self) -> None:
        if not self.decision_id or not self.trigger_quote_key:
            raise ValueError("research decision identity fields are required")
        parse_iso_timestamp(self.decision_ts)
        amount = Decimal(str(self.stake))
        if amount <= 0:
            raise ValueError("research decision stake must be positive")
        object.__setattr__(self, "stake", amount)
        candidate_keys = {leg.quote_key for leg in self.candidate.legs}
        if not candidate_keys:
            raise ValueError("research decision candidate requires at least one leg")
        if self.trigger_quote_key not in candidate_keys:
            raise ValueError("research trigger_quote_key must be one of the candidate legs")
        forecast_keys = [record.quote_key for record in self.forecasts]
        if len(forecast_keys) != len(set(forecast_keys)):
            raise ValueError("research decision has duplicate ForecastRecord quote_key")
        missing = candidate_keys.difference(forecast_keys)
        if missing:
            raise ValueError(
                "research decision lacks ForecastRecord for candidate quote(s): "
                + ",".join(sorted(missing))
            )
        if not self.groups:
            raise ValueError("research decision scenario groups are required")

    @property
    def forecasts_by_quote(self) -> dict[str, ForecastRecord]:
        return {record.quote_key: record for record in self.forecasts}


@dataclass(frozen=True, slots=True)
class ResearchStrategyPlan:
    instructions: tuple[ResearchReplayInstruction, ...]
    source_sha256: str

    def __post_init__(self) -> None:
        if not self.instructions:
            raise ValueError("research strategy plan must contain at least one decision")
        if len(self.source_sha256) != 64:
            raise ValueError("research strategy plan source_sha256 must be SHA-256")
        try:
            int(self.source_sha256, 16)
        except ValueError as exc:
            raise ValueError("research strategy plan source_sha256 must be hexadecimal") from exc
        ids = [item.decision_id for item in self.instructions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate research decision_id")
        triggers = [(item.decision_ts, item.trigger_quote_key) for item in self.instructions]
        if len(triggers) != len(set(triggers)):
            raise ValueError("duplicate research decision trigger")

    @property
    def experiment_strategy_id(self) -> str:
        return f"{RESEARCH_STRATEGY_ID}@{self.source_sha256}"

    @classmethod
    def from_path(cls, path: str | Path) -> "ResearchStrategyPlan":
        raw_bytes = Path(path).read_bytes()
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("research strategy plan must be valid UTF-8 JSON") from exc
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls.from_dict(raw, source_sha256=digest)

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        *,
        source_sha256: str | None = None,
    ) -> "ResearchStrategyPlan":
        if not isinstance(raw, dict):
            raise ValueError("research strategy plan root must be an object")
        if raw.get("schema_version") != 1:
            raise ValueError("research strategy plan schema_version must be 1")
        if raw.get("strategy_id") != RESEARCH_STRATEGY_ID:
            raise ValueError(f"research strategy plan strategy_id must be {RESEARCH_STRATEGY_ID}")
        decisions = raw.get("decisions")
        if not isinstance(decisions, list) or not decisions:
            raise ValueError("research strategy plan decisions must be a non-empty list")
        instructions = tuple(_instruction_from_dict(item) for item in decisions)
        if source_sha256 is None:
            canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            source_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls(instructions, source_sha256.lower())

    def preflight(self, events: Iterable[MarketEvent]) -> None:
        """Bind every planned decision to the same causal replay state before economic mutation."""

        by_trigger = {
            (instruction.decision_ts, instruction.trigger_quote_key): instruction
            for instruction in self.instructions
        }
        processed: set[str] = set()
        latest: dict[str, MarketEvent] = {}
        ordered = sorted(
            events,
            key=lambda event: (
                parse_iso_timestamp(event.observed_ts),
                event.sequence,
                event.dedupe_key,
            ),
        )
        for event in ordered:
            latest[event.quote_key] = event
            instruction = by_trigger.get((event.observed_ts, event.quote_key))
            if instruction is None:
                continue
            _validate_market_binding(instruction, latest)
            processed.add(instruction.decision_id)
        missing = [
            instruction.decision_id
            for instruction in self.instructions
            if instruction.decision_id not in processed
        ]
        if missing:
            raise ValueError(
                "research plan decision trigger not present in causal replay: "
                + ",".join(sorted(missing))
            )


class ResearchReplayAgent:
    """Executes typed, pre-outcome research decisions only at their exact replay trigger."""

    name = "research-replay-pipeline"

    def __init__(
        self,
        plan: ResearchStrategyPlan,
        pipeline: ResearchDecisionPipeline | None = None,
    ) -> None:
        self.plan = plan
        self.pipeline = pipeline or ResearchDecisionPipeline()
        self._by_trigger = {
            (instruction.decision_ts, instruction.trigger_quote_key): instruction
            for instruction in plan.instructions
        }
        self._processed: set[str] = set()

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None:
        instruction = self._by_trigger.get((event.observed_ts, event.quote_key))
        if instruction is None:
            return
        if instruction.decision_id in self._processed:
            raise RuntimeError(f"research decision executed twice: {instruction.decision_id}")
        if context.decision_ledger is None:
            raise RuntimeError("research replay strategy requires a decision ledger")
        _validate_market_binding(instruction, context.latest_quotes)
        self.pipeline.decide_and_open(
            book=context.paper_book,
            candidate=instruction.candidate,
            groups=list(instruction.groups),
            forecasts=instruction.forecasts_by_quote,
            evidence=instruction.evidence,
            stake=instruction.stake,
            decision_ts=instruction.decision_ts,
            decision_ledger=context.decision_ledger,
            replay_run_id=context.replay_run_id,
        )
        self._processed.add(instruction.decision_id)

    def finalize_replay(self, _context: AgentContext) -> None:
        missing = [
            instruction.decision_id
            for instruction in self.plan.instructions
            if instruction.decision_id not in self._processed
        ]
        if missing:
            raise RuntimeError(
                "research replay completed without executing planned decision(s): "
                + ",".join(sorted(missing))
            )


def _validate_market_binding(
    instruction: ResearchReplayInstruction,
    latest_quotes: dict[str, MarketEvent],
) -> None:
    decision_time = parse_iso_timestamp(instruction.decision_ts)
    candidate_keys = tuple(leg.quote_key for leg in instruction.candidate.legs)
    snapshot_hash = research_market_snapshot_hash(latest_quotes, candidate_keys)
    forecasts = instruction.forecasts_by_quote

    for leg in instruction.candidate.legs:
        event = latest_quotes.get(leg.quote_key)
        if event is None:
            raise ValueError(f"research candidate quote absent from replay state: {leg.quote_key}")
        if event.status != "open":
            raise ValueError(
                f"research candidate quote is not open market state: {leg.quote_key}"
            )
        if event.metadata.get("execution_quote_verified") is False:
            raise ValueError(
                f"research candidate quote is not verified executable price evidence: {leg.quote_key}"
            )
        rejection = paper_quote_rejection_reason(event, instruction.stake)
        if rejection is not None:
            raise ValueError(
                f"research candidate quote cannot support paper fill: {leg.quote_key}: {rejection}"
            )
        if parse_iso_timestamp(event.observed_ts) > decision_time:
            raise ValueError(f"research candidate quote is from the future: {leg.quote_key}")
        if event.decimal_odds != leg.decimal_odds:
            raise ValueError(f"research candidate odds do not match replay state: {leg.quote_key}")

        available = sorted(
            (
                item
                for item in instruction.evidence
                if item.quote_key == leg.quote_key
                and parse_iso_timestamp(item.available_at) <= decision_time
            ),
            key=lambda item: (parse_iso_timestamp(item.available_at), item.evidence_id),
        )
        if not available:
            raise ValueError(f"research candidate has no causal replay evidence: {leg.quote_key}")
        latest_evidence = available[-1]
        if latest_evidence.source_id != event.source_id:
            raise ValueError(f"research evidence source does not match replay state: {leg.quote_key}")
        if latest_evidence.observed_at != event.observed_ts:
            raise ValueError(f"research evidence timestamp does not match replay state: {leg.quote_key}")
        if latest_evidence.decimal_odds != event.decimal_odds:
            raise ValueError(f"research evidence odds do not match replay state: {leg.quote_key}")
        if latest_evidence.content_sha256 != market_event_evidence_hash(event):
            raise ValueError(f"research evidence hash does not match replay quote: {leg.quote_key}")
        if latest_evidence.market_snapshot_hash != snapshot_hash:
            raise ValueError(f"research evidence snapshot hash does not match replay state: {leg.quote_key}")

        forecast = forecasts[leg.quote_key]
        if forecast.market_snapshot_hash != snapshot_hash:
            raise ValueError(f"ForecastRecord snapshot hash does not match replay state: {leg.quote_key}")


def _instruction_from_dict(raw: Any) -> ResearchReplayInstruction:
    if not isinstance(raw, dict):
        raise ValueError("research decision must be an object")
    candidate_raw = raw.get("candidate")
    if not isinstance(candidate_raw, dict):
        raise ValueError("research decision candidate must be an object")
    legs_raw = candidate_raw.get("legs")
    if not isinstance(legs_raw, list) or not legs_raw:
        raise ValueError("research decision candidate legs must be a non-empty list")
    legs: list[CandidateLeg] = []
    combined_odds = Decimal("1")
    combined_probability = Decimal("1")
    for item in legs_raw:
        if not isinstance(item, dict):
            raise ValueError("research candidate leg must be an object")
        quote_key = str(item["quote_key"])
        parts = quote_key.split("|", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError("research candidate quote_key must be event|market|selection")
        odds = Decimal(str(item["decimal_odds"]))
        probability = Decimal(str(item["probability"]))
        leg = CandidateLeg(quote_key, parts[0], odds, probability)
        legs.append(leg)
        combined_odds *= odds
        combined_probability *= probability
    candidate = ParlayCandidate(
        tuple(legs),
        combined_odds,
        combined_probability,
        combined_probability * combined_odds - Decimal("1"),
    )

    groups_raw = raw.get("scenario_groups")
    if not isinstance(groups_raw, list) or not groups_raw:
        raise ValueError("research decision scenario_groups must be a non-empty list")
    groups: list[ScenarioGroup] = []
    for group_raw in groups_raw:
        if not isinstance(group_raw, dict):
            raise ValueError("research scenario group must be an object")
        outcomes_raw = group_raw.get("outcomes")
        if not isinstance(outcomes_raw, list):
            raise ValueError("research scenario group outcomes must be a list")
        outcomes = tuple(
            ScenarioOutcome(
                str(outcome["quote_key"]),
                Decimal(str(outcome["probability"]))
                if outcome.get("probability") is not None
                else None,
            )
            for outcome in outcomes_raw
        )
        groups.append(ScenarioGroup(str(group_raw["group_id"]), outcomes))

    forecasts_raw = raw.get("forecasts")
    if not isinstance(forecasts_raw, list) or not forecasts_raw:
        raise ValueError("research decision forecasts must be a non-empty list")
    forecasts = tuple(_forecast_from_dict(item) for item in forecasts_raw)

    evidence_raw = raw.get("evidence")
    if not isinstance(evidence_raw, list) or not evidence_raw:
        raise ValueError("research decision evidence must be a non-empty list")
    evidence = tuple(_evidence_from_dict(item) for item in evidence_raw)
    return ResearchReplayInstruction(
        decision_id=str(raw["decision_id"]),
        trigger_quote_key=str(raw["trigger_quote_key"]),
        decision_ts=str(raw["decision_ts"]),
        stake=Decimal(str(raw["stake"])),
        candidate=candidate,
        groups=tuple(groups),
        forecasts=forecasts,
        evidence=evidence,
    )


def _forecast_from_dict(raw: Any) -> ForecastRecord:
    if not isinstance(raw, dict):
        raise ValueError("ForecastRecord entry must be an object")
    return ForecastRecord(
        quote_key=str(raw["quote_key"]),
        probability=Decimal(str(raw["probability"])),
        model_id=str(raw["model_id"]),
        model_version=str(raw["model_version"]),
        strategy_version=str(raw["strategy_version"]),
        model_training_cutoff_ts=str(raw["model_training_cutoff_ts"]),
        input_cutoff_ts=str(raw["input_cutoff_ts"]),
        generated_at=str(raw["generated_at"]),
        uncertainty=Decimal(str(raw.get("uncertainty", "0"))),
        evidence_hashes=tuple(str(item) for item in raw.get("evidence_hashes", ())),
        market_snapshot_hash=(
            str(raw["market_snapshot_hash"])
            if raw.get("market_snapshot_hash") is not None
            else None
        ),
        provenance=dict(raw.get("provenance", {})),
        forecast_id=str(raw["forecast_id"]),
    )


def _evidence_from_dict(raw: Any) -> ResearchEvidence:
    if not isinstance(raw, dict):
        raise ValueError("ResearchEvidence entry must be an object")
    return ResearchEvidence(
        evidence_id=str(raw["evidence_id"]),
        quote_key=str(raw["quote_key"]),
        source_id=str(raw["source_id"]),
        observed_at=str(raw["observed_at"]),
        available_at=str(raw["available_at"]),
        decimal_odds=Decimal(str(raw["decimal_odds"])),
        content_sha256=str(raw["content_sha256"]),
        quality_flags=tuple(str(item) for item in raw.get("quality_flags", ())),
        market_snapshot_hash=(
            str(raw["market_snapshot_hash"])
            if raw.get("market_snapshot_hash") is not None
            else None
        ),
    )
