from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .agents import AgentContext
from .candidate_search import CandidateLeg, ParlayCandidate
from .domain import MarketEvent
from .forecasting import ForecastRecord, parse_iso_timestamp
from .research_pipeline import (
    ResearchDecisionAlreadyCommitted,
    ResearchDecisionPipeline,
    ResearchEvidence,
)
from .risk import RiskOfRuinEvidence
from .scenario_search import ScenarioGroup, ScenarioOutcome


RESEARCH_STRATEGY_ID = "research-replay-v1"
_MAX_RESEARCH_PLAN_JSON_DEPTH = 64


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"research strategy plan contains duplicate JSON key {key!r}")
        payload[key] = value
    return payload


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"research strategy plan contains non-finite JSON value {value!r}")


def _validate_json_text(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("research strategy plan contains non-UTF-8 JSON text") from exc


def _validate_strict_json_domain(raw: Any) -> None:
    """Fail closed on decoded values that cannot represent bounded strict JSON."""

    pending: list[tuple[Any, int]] = [(raw, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > _MAX_RESEARCH_PLAN_JSON_DEPTH:
            raise ValueError("research strategy plan JSON nesting exceeds supported depth")
        if value is None or type(value) in (bool, int):
            continue
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("research strategy plan contains non-finite JSON number")
            continue
        if type(value) is str:
            _validate_json_text(value)
            continue
        if type(value) is list:
            pending.extend((item, depth + 1) for item in value)
            continue
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("research strategy plan JSON object keys must be strings")
                _validate_json_text(key)
                pending.append((item, depth + 1))
            continue
        raise ValueError("research strategy plan contains unsupported JSON value")


def _canonical_plan_json(raw: Any) -> str:
    _validate_strict_json_domain(raw)
    try:
        return json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ValueError("research strategy plan must be canonical strict JSON") from exc


def _stable_event_projection(event: MarketEvent) -> dict[str, Any]:
    """Stable causal quote projection used to bind offline research evidence to replay state."""

    projection: dict[str, Any] = {
        "event_id": event.event_id,
        "market_id": event.market_id,
        "selection_id": event.selection_id,
        "decimal_odds": str(event.decimal_odds),
        "observed_ts": event.observed_ts,
        "source_id": event.source_id,
        "sport": event.sport,
        "sequence": event.sequence,
        "market_type": event.market_type.value,
        "status": event.status,
        "source_ts": event.source_ts,
        "score_state": event.score_state,
        "metadata": event.metadata,
    }
    # Preserve the exact legacy/None research projection while making concrete
    # canonical market/provenance semantics identity-bearing.
    for field_name in (
        "competition_id",
        "market_semantics_id",
        "provider_source_class",
        "exchange_side",
    ):
        value = getattr(event, field_name)
        if value is not None:
            projection[field_name] = value
    return projection


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
    risk_of_ruin_evidence: RiskOfRuinEvidence | None = None

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
        if self.risk_of_ruin_evidence is not None:
            if not isinstance(self.risk_of_ruin_evidence, RiskOfRuinEvidence):
                raise TypeError(
                    "research decision risk_of_ruin_evidence must be RiskOfRuinEvidence or None"
                )
            decision_time = parse_iso_timestamp(self.decision_ts)
            causal_cutoff = parse_iso_timestamp(self.risk_of_ruin_evidence.causal_cutoff)
            evaluated_at = parse_iso_timestamp(self.risk_of_ruin_evidence.evaluated_at)
            if causal_cutoff > decision_time or evaluated_at > decision_time:
                raise ValueError(
                    "research decision risk-of-ruin evidence uses future information"
                )

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
            raw = json.loads(
                raw_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_non_finite_json,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("research strategy plan must be valid UTF-8 JSON") from exc
        canonical = _canonical_plan_json(raw)
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
        _validate_strict_json_domain(raw)
        schema_version = raw.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
            raise ValueError("research strategy plan schema_version must be integer 1")
        if raw.get("strategy_id") != RESEARCH_STRATEGY_ID:
            raise ValueError(f"research strategy plan strategy_id must be {RESEARCH_STRATEGY_ID}")
        decisions = raw.get("decisions")
        if not isinstance(decisions, list) or not decisions:
            raise ValueError("research strategy plan decisions must be a non-empty list")
        instructions = tuple(_instruction_from_dict(item) for item in decisions)
        if source_sha256 is None:
            canonical = _canonical_plan_json(raw)
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
        first_observed_quote_times: dict[str, Any] = {}
        first_observed_event_times: dict[str, Any] = {}
        first_observed_market_times: dict[tuple[str, str], Any] = {}
        for event in ordered:
            observed_time = parse_iso_timestamp(event.observed_ts)
            first_observed_quote_times.setdefault(event.quote_key, observed_time)
            first_observed_event_times.setdefault(event.event_id, observed_time)
            first_observed_market_times.setdefault(
                (event.event_id, event.market_id),
                observed_time,
            )
        for event in ordered:
            latest[event.quote_key] = event
            instruction = by_trigger.get((event.observed_ts, event.quote_key))
            if instruction is None:
                continue
            _validate_scenario_future_identity(
                instruction.groups,
                first_observed_quote_times,
                first_observed_event_times,
                first_observed_market_times,
                parse_iso_timestamp(instruction.decision_ts),
            )
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
        try:
            self.pipeline.decide_and_open(
                book=context.paper_book,
                candidate=instruction.candidate,
                groups=list(instruction.groups),
                forecasts=instruction.forecasts_by_quote,
                evidence=instruction.evidence,
                # Compatibility input only; active EconomicGoal makes it non-authoritative.
                stake=instruction.stake,
                decision_ts=instruction.decision_ts,
                market_quotes=tuple(
                    context.latest_quotes[leg.quote_key]
                    for leg in instruction.candidate.legs
                ),
                risk_of_ruin_evidence=instruction.risk_of_ruin_evidence,
                decision_ledger=context.decision_ledger,
                replay_run_id=context.replay_run_id,
                material_action_id=instruction.decision_id,
            )
        except ResearchDecisionAlreadyCommitted:
            self._processed.add(instruction.decision_id)
            return
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
    _validate_scenario_space_binding(instruction.groups, latest_quotes, decision_time)
    candidate_keys = tuple(leg.quote_key for leg in instruction.candidate.legs)
    snapshot_hash = research_market_snapshot_hash(latest_quotes, candidate_keys)
    forecasts = instruction.forecasts_by_quote

    for leg in instruction.candidate.legs:
        event = latest_quotes.get(leg.quote_key)
        if event is None:
            raise ValueError(f"research candidate quote absent from replay state: {leg.quote_key}")
        if leg.ticket_identity() != (event.event_id, event.market_id, event.selection_id):
            raise ValueError(
                f"research candidate structured identity does not match replay state: {leg.quote_key}"
            )
        if event.status != "open":
            raise ValueError(
                f"research candidate quote is not open market state: {leg.quote_key}"
            )
        if event.metadata.get("execution_quote_verified") is False:
            raise ValueError(
                f"research candidate quote is not verified executable price evidence: {leg.quote_key}"
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
        if forecast.market_semantics_id != event.market_semantics_id:
            raise ValueError(
                "ForecastRecord market_semantics_id does not match replay state: "
                + leg.quote_key
            )


def _validate_scenario_future_identity(
    groups: tuple[ScenarioGroup, ...],
    first_observed_quote_times: dict[str, Any],
    first_observed_event_times: dict[str, Any],
    first_observed_market_times: dict[tuple[str, str], Any],
    decision_time,
) -> None:
    """Reject replay quote, event, or market identities not yet knowable at decision time."""

    for group in groups:
        for outcome in group.outcomes:
            first_observed = first_observed_quote_times.get(outcome.quote_key)
            if first_observed is not None and first_observed > decision_time:
                raise ValueError(
                    "research scenario outcome identity first appears after decision: "
                    f"{outcome.quote_key}"
                )
            future_event_matches = sorted(
                event_id
                for event_id, first_event_observed in first_observed_event_times.items()
                if first_event_observed > decision_time
                and outcome.quote_key.startswith(f"{event_id}|")
            )
            if future_event_matches:
                event_id = future_event_matches[0]
                raise ValueError(
                    "research scenario event identity first appears after decision: "
                    f"{event_id}"
                )
            future_market_matches = sorted(
                (
                    (event_id, market_id)
                    for (event_id, market_id), first_market_observed in first_observed_market_times.items()
                    if first_market_observed > decision_time
                    and outcome.quote_key.startswith(f"{event_id}|{market_id}|")
                ),
                key=lambda item: (item[0], item[1]),
            )
            if future_market_matches:
                event_id, market_id = future_market_matches[0]
                raise ValueError(
                    "research scenario market identity first appears after decision: "
                    f"{event_id}|{market_id}"
                )


def _validate_scenario_space_binding(
    groups: tuple[ScenarioGroup, ...],
    latest_quotes: dict[str, MarketEvent],
    decision_time,
) -> None:
    """Bind observed replay-market outcomes without banning abstract complement scenarios."""

    for group in groups:
        outcome_keys = {outcome.quote_key for outcome in group.outcomes}
        observed_outcomes: list[MarketEvent] = []
        for quote_key in sorted(outcome_keys):
            event = latest_quotes.get(quote_key)
            if event is None:
                continue
            if parse_iso_timestamp(event.observed_ts) > decision_time:
                raise ValueError(
                    f"research scenario outcome is from the future: {quote_key}"
                )
            observed_outcomes.append(event)

        if not observed_outcomes:
            continue

        market_identities = {
            (event.event_id, event.market_id) for event in observed_outcomes
        }
        if len(market_identities) != 1:
            raise ValueError(
                f"research scenario group must bind one replay event/market: {group.group_id}"
            )
        event_id, market_id = next(iter(market_identities))
        replay_market_keys = {
            event.quote_key
            for event in latest_quotes.values()
            if event.event_id == event_id
            and event.market_id == market_id
            and parse_iso_timestamp(event.observed_ts) <= decision_time
        }
        missing = sorted(replay_market_keys - outcome_keys)
        if missing:
            raise ValueError(
                "research scenario group is not complete for replay market "
                f"{event_id}|{market_id}: missing={','.join(missing)}"
            )

        market_prefix = f"{event_id}|{market_id}|"
        for quote_key in sorted(outcome_keys - replay_market_keys):
            if quote_key.startswith(market_prefix) and quote_key[len(market_prefix) :]:
                raise ValueError(
                    f"research scenario outcome absent from replay state: {quote_key}"
                )


def _canonical_identity_field(raw: dict[str, Any], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(
            f"research candidate {field_name} must be a non-empty canonical string"
        )
    return value


def _candidate_leg_from_dict(raw: Any) -> CandidateLeg:
    if not isinstance(raw, dict):
        raise ValueError("research candidate leg must be an object")
    quote_key = raw.get("quote_key")
    if not isinstance(quote_key, str) or not quote_key or quote_key.strip() != quote_key:
        raise ValueError("research candidate quote_key must be a non-empty canonical string")

    present = [field in raw for field in ("event_id", "market_id", "selection_id")]
    if any(present) and not all(present):
        raise ValueError(
            "research candidate event_id, market_id, and selection_id must be provided together"
        )
    if all(present):
        event_id = _canonical_identity_field(raw, "event_id")
        market_id = _canonical_identity_field(raw, "market_id")
        selection_id = _canonical_identity_field(raw, "selection_id")
        if quote_key != f"{event_id}|{market_id}|{selection_id}":
            raise ValueError(
                "research candidate structured event/market/selection identity does not match quote_key"
            )
    else:
        parts = quote_key.split("|")
        if len(parts) != 3 or not all(parts):
            raise ValueError(
                "research candidate with ambiguous quote_key requires structured event_id, market_id, and selection_id"
            )
        event_id, market_id, selection_id = parts

    odds = Decimal(str(raw["decimal_odds"]))
    probability = Decimal(str(raw["probability"]))
    return CandidateLeg(
        quote_key,
        event_id,
        odds,
        probability,
        market_id,
        selection_id,
    )


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
        leg = _candidate_leg_from_dict(item)
        legs.append(leg)
        combined_odds *= leg.decimal_odds
        combined_probability *= leg.probability
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
    risk_of_ruin_raw = raw.get("risk_of_ruin_evidence")
    risk_of_ruin_evidence = (
        None
        if risk_of_ruin_raw is None
        else _risk_of_ruin_evidence_from_dict(risk_of_ruin_raw)
    )
    return ResearchReplayInstruction(
        decision_id=str(raw["decision_id"]),
        trigger_quote_key=str(raw["trigger_quote_key"]),
        decision_ts=str(raw["decision_ts"]),
        stake=Decimal(str(raw["stake"])),
        candidate=candidate,
        groups=tuple(groups),
        forecasts=forecasts,
        evidence=evidence,
        risk_of_ruin_evidence=risk_of_ruin_evidence,
    )


def _risk_of_ruin_evidence_from_dict(raw: Any) -> RiskOfRuinEvidence:
    if not isinstance(raw, dict):
        raise ValueError("research risk_of_ruin_evidence must be an object")
    try:
        return RiskOfRuinEvidence(
            evidence_id=str(raw["evidence_id"]),
            research_protocol_sha256=str(raw["research_protocol_sha256"]),
            reproducibility_bundle_sha256=str(raw["reproducibility_bundle_sha256"]),
            producer_identity=str(raw["producer_identity"]),
            causal_cutoff=str(raw["causal_cutoff"]),
            evaluated_at=str(raw["evaluated_at"]),
            bankroll_id=str(raw["bankroll_id"]),
            currency=str(raw["currency"]),
            base_portfolio_sha256=str(raw["base_portfolio_sha256"]),
            candidate_sha256=str(raw["candidate_sha256"]),
            evaluated_stake=Decimal(str(raw["evaluated_stake"])),
            upper_bound=Decimal(str(raw["upper_bound"])),
        )
    except KeyError as exc:
        raise ValueError(
            "research risk_of_ruin_evidence is missing required field "
            + str(exc.args[0])
        ) from exc


def _forecast_from_dict(raw: Any) -> ForecastRecord:
    if not isinstance(raw, dict):
        raise ValueError("ForecastRecord entry must be an object")
    evidence_hashes_raw = raw.get("evidence_hashes", [])
    if not isinstance(evidence_hashes_raw, list):
        raise ValueError("ForecastRecord evidence_hashes must be a JSON array")
    if any(not isinstance(item, str) for item in evidence_hashes_raw):
        raise ValueError("ForecastRecord evidence_hashes must contain strings")
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
        evidence_hashes=tuple(evidence_hashes_raw),
        market_snapshot_hash=(
            raw["market_snapshot_hash"]
            if raw.get("market_snapshot_hash") is not None
            else None
        ),
        provenance=dict(raw.get("provenance", {})),
        forecast_id=str(raw["forecast_id"]),
        market_semantics_id=raw.get("market_semantics_id"),
    )


def _evidence_from_dict(raw: Any) -> ResearchEvidence:
    if not isinstance(raw, dict):
        raise ValueError("ResearchEvidence entry must be an object")
    quality_flags_raw = raw.get("quality_flags", [])
    if not isinstance(quality_flags_raw, list):
        raise ValueError("ResearchEvidence quality_flags must be a JSON array")
    if any(
        not isinstance(item, str) or not item.strip() or item != item.strip()
        for item in quality_flags_raw
    ):
        raise ValueError(
            "ResearchEvidence quality_flags must contain non-empty canonical strings"
        )
    return ResearchEvidence(
        evidence_id=str(raw["evidence_id"]),
        quote_key=str(raw["quote_key"]),
        source_id=str(raw["source_id"]),
        observed_at=str(raw["observed_at"]),
        available_at=str(raw["available_at"]),
        decimal_odds=Decimal(str(raw["decimal_odds"])),
        content_sha256=str(raw["content_sha256"]),
        quality_flags=tuple(quality_flags_raw),
        market_snapshot_hash=(
            str(raw["market_snapshot_hash"])
            if raw.get("market_snapshot_hash") is not None
            else None
        ),
    )