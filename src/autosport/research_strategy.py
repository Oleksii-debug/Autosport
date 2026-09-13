from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from .agents import AgentContext
from .candidate_search import CandidateLeg, ParlayCandidate
from .domain import MarketEvent, TicketStatus
from .forecasting import ForecastRecord
from .research_pipeline import ResearchDecisionPipeline, ResearchEvidence
from .scenario_search import ScenarioGroup, ScenarioOutcome


class ResearchSignalAgent:
    """Selectable paper-only adapter from explicit causal dataset signals into the typed research pipeline.

    This adapter does not estimate probabilities and makes no network/LLM call. A dataset event must
    explicitly carry a pre-outcome ``research_signal`` with model identity, probability, uncertainty,
    training cutoff and virtual stake. The signal is then evaluated by the canonical typed pipeline
    against the current replay snapshot, portfolio scenario surface and PaperRiskPolicy.
    """

    name = "research-v1"

    def __init__(self, pipeline: ResearchDecisionPipeline | None = None) -> None:
        self.pipeline = pipeline or ResearchDecisionPipeline()
        self._used_signal_ids: set[str] = set()

    def on_market_event(self, event: MarketEvent, context: AgentContext) -> None:
        raw_signal = event.metadata.get("research_signal")
        if raw_signal is None:
            return
        if not isinstance(raw_signal, dict):
            raise ValueError("research_signal must be an object")
        if context.decision_ledger is None:
            raise ValueError("research-v1 requires a staged decision ledger")

        signal_id = _required_string(raw_signal, "signal_id")
        if signal_id in self._used_signal_ids:
            return

        probability = _probability(raw_signal.get("probability"), "research_signal.probability")
        uncertainty = _probability(raw_signal.get("uncertainty", "0"), "research_signal.uncertainty")
        stake = _positive_decimal(raw_signal.get("stake"), "research_signal.stake")
        model_id = _required_string(raw_signal, "model_id")
        model_version = _required_string(raw_signal, "model_version")
        training_cutoff = _required_string(raw_signal, "model_training_cutoff_ts")

        evidence_hash = _market_evidence_hash(event)
        snapshot_hash = context.market_context_hash()
        evidence = ResearchEvidence(
            evidence_id=f"{signal_id}:{event.quote_key}",
            quote_key=event.quote_key,
            source_id=event.source_id,
            observed_at=event.observed_ts,
            available_at=event.observed_ts,
            decimal_odds=event.decimal_odds,
            content_sha256=evidence_hash,
            quality_flags=_quality_flags(event.metadata.get("quality_flags", ())),
            market_snapshot_hash=snapshot_hash,
        )
        forecast = ForecastRecord(
            forecast_id=_forecast_id(signal_id, event.quote_key, evidence_hash),
            quote_key=event.quote_key,
            probability=probability,
            model_id=model_id,
            model_version=model_version,
            strategy_version=self.name,
            model_training_cutoff_ts=training_cutoff,
            input_cutoff_ts=event.observed_ts,
            generated_at=event.observed_ts,
            uncertainty=uncertainty,
            evidence_hashes=(evidence_hash,),
            market_snapshot_hash=snapshot_hash,
            provenance={
                "adapter": self.name,
                "signal_id": signal_id,
                "source_id": event.source_id,
                "real_money_execution": False,
            },
        )
        candidate = ParlayCandidate(
            legs=(
                CandidateLeg(
                    quote_key=event.quote_key,
                    event_id=event.event_id,
                    decimal_odds=event.decimal_odds,
                    probability=probability,
                ),
            ),
            combined_odds=event.decimal_odds,
            independent_probability=probability,
            expected_profit_per_unit=probability * event.decimal_odds - Decimal("1"),
        )
        groups = _scenario_groups(context, candidate_quote_key=event.quote_key)

        self.pipeline.decide_and_open(
            book=context.paper_book,
            candidate=candidate,
            groups=groups,
            forecasts={event.quote_key: forecast},
            evidence=(evidence,),
            stake=stake,
            decision_ts=event.observed_ts,
            decision_ledger=context.decision_ledger,
            replay_run_id=context.replay_run_id,
        )
        self._used_signal_ids.add(signal_id)


def _scenario_groups(context: AgentContext, *, candidate_quote_key: str) -> list[ScenarioGroup]:
    required_markets: set[tuple[str, str]] = set()
    candidate = context.latest_quotes.get(candidate_quote_key)
    if candidate is None:
        raise ValueError("research candidate is absent from current market snapshot")
    required_markets.add((candidate.event_id, candidate.market_id))

    for ticket in context.paper_book.tickets.values():
        if ticket.status is not TicketStatus.OPEN:
            continue
        for leg in ticket.legs:
            required_markets.add((leg.event_id, leg.market_id))

    grouped: dict[tuple[str, str], list[MarketEvent]] = {key: [] for key in required_markets}
    for quote in context.latest_quotes.values():
        key = (quote.event_id, quote.market_id)
        if key in grouped:
            grouped[key].append(quote)

    groups: list[ScenarioGroup] = []
    for key in sorted(required_markets):
        quotes = sorted(grouped[key], key=lambda item: item.quote_key)
        unique = {item.quote_key: item for item in quotes}
        if len(unique) < 2:
            event_id, market_id = key
            raise ValueError(
                "research-v1 requires at least two current outcomes for scenario coverage: "
                f"{event_id}|{market_id}"
            )
        groups.append(
            ScenarioGroup(
                group_id=f"{key[0]}|{key[1]}",
                outcomes=tuple(ScenarioOutcome(quote_key) for quote_key in sorted(unique)),
            )
        )
    return groups


def _market_evidence_hash(event: MarketEvent) -> str:
    payload = event.to_dict()
    metadata = dict(payload.get("metadata", {}))
    metadata.pop("research_signal", None)
    payload["metadata"] = metadata
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _forecast_id(signal_id: str, quote_key: str, evidence_hash: str) -> str:
    material = f"{signal_id}|{quote_key}|{evidence_hash}".encode("utf-8")
    return "research-v1:" + hashlib.sha256(material).hexdigest()[:32]


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"research_signal.{key} must be a non-empty string")
    return value.strip()


def _probability(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if result < 0 or result > 1:
        raise ValueError(f"{field} must be between 0 and 1")
    return result


def _positive_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _quality_flags(value: Any) -> tuple[str, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("metadata.quality_flags must be a list")
    flags = tuple(str(item) for item in value)
    if any(not item for item in flags) or len(flags) != len(set(flags)):
        raise ValueError("metadata.quality_flags must contain unique non-empty strings")
    return flags
