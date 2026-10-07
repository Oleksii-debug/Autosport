from __future__ import annotations

import argparse
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence

from .calculation_service import CalculationEvidence, CalculationService
from .dataset import ReplayDataset, load_dataset
from .domain import MarketEvent


SUPPORTED_OPERATIONS = (
    "odds-conversion",
    "implied-probability",
    "expected-return",
    "paper-payout",
    "fractional-kelly",
)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "calculate-dataset-quote",
        help="calculate one exact selected quote from a verified sealed dataset",
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--market-id", required=True)
    parser.add_argument("--selection-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--cutoff", required=True)
    parser.add_argument("--operation", choices=SUPPORTED_OPERATIONS, required=True)
    parser.add_argument("--probability")
    parser.add_argument("--stake", default="1")
    parser.add_argument("--fraction", default="1")
    parser.add_argument("--cap", default="1")
    parser.add_argument("--format", choices=("text", "json"), default="text")


def _timestamp(value: str, field: str) -> datetime:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field} must be a non-empty trimmed timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    return parsed


def _decimal(value: str, field: str) -> Decimal:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field} must be a canonical decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a valid Decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    return result


def _select_exact(
    events: Sequence[MarketEvent],
    *,
    event_id: str,
    market_id: str,
    selection_id: str,
    source_id: str,
    sequence: int,
) -> MarketEvent:
    matches = [
        event
        for event in events
        if (
            event.event_id == event_id
            and event.market_id == market_id
            and event.selection_id == selection_id
            and event.source_id == source_id
            and event.sequence == sequence
        )
    ]
    if not matches:
        raise ValueError("exact selected quote was not found in the verified market dataset")
    if len(matches) != 1:
        raise ValueError("exact selected quote identity is ambiguous")
    event = matches[0]
    if event.status != "open":
        raise ValueError("selected quote is closed or non-actionable")
    return event


def _assert_available_through_cutoff(event: MarketEvent, cutoff: datetime) -> None:
    observed = _timestamp(event.observed_ts, "observed_ts")
    source = None if event.source_ts is None else _timestamp(event.source_ts, "source_ts")
    ingest = _timestamp(event.ingest_ts, "ingest_ts")
    if source is None:
        raise ValueError("selected quote lacks source availability timestamp")
    if observed > cutoff:
        raise ValueError("selected quote observed_ts is after the causal cutoff")
    if source > cutoff:
        raise ValueError("selected quote source_ts is after the causal cutoff")
    if ingest > cutoff:
        raise ValueError("selected quote ingest_ts is after the causal cutoff")


def _calculate(
    service: CalculationService,
    event: MarketEvent,
    *,
    operation: str,
    cutoff: str,
    probability: str | None,
    stake: str,
    fraction: str,
    cap: str,
) -> CalculationEvidence:
    if operation == "odds-conversion":
        return service.odds_conversion_for_event(event, causal_cutoff_ts=cutoff)
    if operation == "implied-probability":
        return service.implied_probability_for_event(event, causal_cutoff_ts=cutoff)
    if operation == "expected-return":
        if probability is None:
            raise ValueError("--probability is required for expected-return")
        return service.expected_return_for_event(
            event,
            _decimal(probability, "probability"),
            stake=_decimal(stake, "stake"),
            causal_cutoff_ts=cutoff,
        )
    if operation == "paper-payout":
        return service.paper_payout_for_event(
            event,
            _decimal(stake, "stake"),
            causal_cutoff_ts=cutoff,
        )
    if operation == "fractional-kelly":
        if probability is None:
            raise ValueError("--probability is required for fractional-kelly")
        return service.fractional_kelly_for_event(
            event,
            _decimal(probability, "probability"),
            fraction=_decimal(fraction, "fraction"),
            cap=_decimal(cap, "cap"),
            causal_cutoff_ts=cutoff,
        )
    raise AssertionError(operation)


def calculate_dataset_quote(args: argparse.Namespace) -> dict[str, object]:
    cutoff_dt = _timestamp(args.cutoff, "cutoff")
    dataset: ReplayDataset = load_dataset(args.path)

    # load_market_events verifies the sealed market bytes and never opens results.
    events = dataset.load_market_events()
    event = _select_exact(
        events,
        event_id=args.event_id,
        market_id=args.market_id,
        selection_id=args.selection_id,
        source_id=args.source_id,
        sequence=args.sequence,
    )
    _assert_available_through_cutoff(event, cutoff_dt)

    evidence = _calculate(
        CalculationService(),
        event,
        operation=args.operation,
        cutoff=args.cutoff,
        probability=args.probability,
        stake=args.stake,
        fraction=args.fraction,
        cap=args.cap,
    )
    result = evidence.as_dict()
    result["dataset"] = {
        "name": dataset.name,
        "sport": dataset.sport,
        "schema_version": dataset.schema_version,
        "market_sha256": dataset.market_sha256,
        "import_identity": dataset.import_identity,
    }
    result["selected_quote_identity"] = {
        "event_id": event.event_id,
        "market_id": event.market_id,
        "selection_id": event.selection_id,
        "source_id": event.source_id,
        "sequence": event.sequence,
        "market_type": event.market_type.value,
    }
    result["causal_available_through_cutoff"] = True
    result["outcomes_accessed"] = False
    result["paper_only"] = True
    return result


def render_result(result: dict[str, object], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    return json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def run(args: argparse.Namespace) -> int:
    try:
        result = calculate_dataset_quote(args)
    except (OSError, TypeError, ValueError) as exc:
        print(f"calculation=FAIL_CLOSED error={exc}")
        return 3
    print(render_result(result, args.format))
    return 0
