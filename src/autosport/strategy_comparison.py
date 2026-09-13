from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .integrity import atomic_write_json
from .run_transaction import RunTransaction


@dataclass(frozen=True, slots=True)
class StrategyRunEvidence:
    source_path: str
    source_sha256: str
    run_id: str
    dataset_name: str
    sport: str
    dataset_schema_version: int
    market_sha256: str
    sealed_results_sha256: str
    historical_import_identity: str | None
    replay_dataset_hash: str
    event_count: int
    strategy_id: str
    canonical_strategy_id: str
    research_plan_sha256: str | None
    initial_bankroll: Decimal
    final_balance: Decimal
    settled_stake: Decimal
    net_profit: Decimal
    roi: Decimal
    won: int
    lost: int
    void: int

    @property
    def comparison_identity(self) -> tuple[Any, ...]:
        return (
            self.dataset_name,
            self.sport,
            self.dataset_schema_version,
            self.market_sha256,
            self.sealed_results_sha256,
            self.historical_import_identity,
            self.replay_dataset_hash,
            self.event_count,
            self.initial_bankroll,
        )


def load_strategy_run_summary(path: str | Path) -> StrategyRunEvidence:
    source = Path(path)
    raw_bytes = source.read_bytes()
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{source}: run summary must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{source}: run summary root must be an object")
    if payload.get("schema_version") != 2:
        raise ValueError(f"{source}: run summary schema_version must be 2")
    if payload.get("transaction_schema_version") != RunTransaction.SCHEMA_VERSION:
        raise ValueError(
            f"{source}: durable transaction evidence must use canonical transaction schema "
            f"{RunTransaction.SCHEMA_VERSION}"
        )
    if payload.get("real_money_execution") is not False:
        raise ValueError(f"{source}: real_money_execution truth boundary is invalid")

    run_id = _required_text(payload, "run_id", source)
    if payload.get("transaction_run_id") != run_id:
        raise ValueError(f"{source}: transaction_run_id does not match run_id")

    runtime = payload.get("strategy_runtime")
    if not isinstance(runtime, dict):
        raise ValueError(f"{source}: strategy_runtime must be an object")
    strategy_id = _required_text(payload, "strategy_id", source)
    if runtime.get("strategy_id") != strategy_id:
        raise ValueError(f"{source}: strategy runtime identity mismatch")
    canonical_strategy_id = _required_text(runtime, "canonical_strategy_id", source)
    research_plan_sha256 = _optional_hash(runtime.get("research_plan_sha256"), "research_plan_sha256", source)

    dataset_schema_version = _required_int(payload, "dataset_schema_version", source, minimum=1)
    historical_import_identity = _optional_hash(
        payload.get("historical_import_identity"),
        "historical_import_identity",
        source,
    )
    if dataset_schema_version >= 2 and historical_import_identity is None:
        raise ValueError(f"{source}: governed historical dataset lacks historical_import_identity")

    evaluation = payload.get("evaluation")
    if not isinstance(evaluation, dict):
        raise ValueError(f"{source}: evaluation must be an object")

    return StrategyRunEvidence(
        source_path=str(source),
        source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        run_id=run_id,
        dataset_name=_required_text(payload, "dataset_name", source),
        sport=_required_text(payload, "sport", source),
        dataset_schema_version=dataset_schema_version,
        market_sha256=_required_hash(payload, "market_sha256", source),
        sealed_results_sha256=_required_hash(payload, "sealed_results_sha256", source),
        historical_import_identity=historical_import_identity,
        replay_dataset_hash=_required_hash(payload, "replay_dataset_hash", source),
        event_count=_required_int(payload, "event_count", source, minimum=1),
        strategy_id=strategy_id,
        canonical_strategy_id=canonical_strategy_id,
        research_plan_sha256=research_plan_sha256,
        initial_bankroll=_required_decimal(evaluation, "initial_bankroll", source),
        final_balance=_required_decimal(evaluation, "final_balance", source),
        settled_stake=_required_decimal(evaluation, "settled_stake", source, minimum=Decimal("0")),
        net_profit=_required_decimal(evaluation, "net_profit", source),
        roi=_required_decimal(evaluation, "roi", source),
        won=_required_int(evaluation, "won", source, minimum=0),
        lost=_required_int(evaluation, "lost", source, minimum=0),
        void=_required_int(evaluation, "void", source, minimum=0),
    )


def compare_strategy_runs(
    runs: Iterable[StrategyRunEvidence],
    *,
    baseline_strategy_id: str | None = None,
) -> dict[str, Any]:
    evidence = tuple(runs)
    if len(evidence) < 2:
        raise ValueError("strategy comparison requires at least two run summaries")

    first = evidence[0]
    mismatched = [item.strategy_id for item in evidence[1:] if item.comparison_identity != first.comparison_identity]
    if mismatched:
        raise ValueError(
            "strategy comparison requires identical sealed dataset, replay identity, event count, and initial bankroll; "
            "mismatch=" + ",".join(sorted(mismatched))
        )

    by_strategy: dict[str, StrategyRunEvidence] = {}
    for item in evidence:
        if item.strategy_id in by_strategy:
            raise ValueError(f"duplicate strategy_id in comparison input: {item.strategy_id}")
        by_strategy[item.strategy_id] = item

    if baseline_strategy_id is None:
        baseline_strategy_id = "baseline-v1" if "baseline-v1" in by_strategy else sorted(by_strategy)[0]
    if baseline_strategy_id not in by_strategy:
        raise ValueError(f"baseline strategy is not present in comparison input: {baseline_strategy_id}")
    baseline = by_strategy[baseline_strategy_id]

    ordered = tuple(by_strategy[key] for key in sorted(by_strategy))
    observed_order = [
        item.strategy_id
        for item in sorted(ordered, key=lambda item: (-item.net_profit, item.strategy_id))
    ]

    entries = []
    for item in ordered:
        entries.append(
            {
                "strategy_id": item.strategy_id,
                "canonical_strategy_id": item.canonical_strategy_id,
                "research_plan_sha256": item.research_plan_sha256,
                "run_id": item.run_id,
                "summary_path": item.source_path,
                "summary_sha256": item.source_sha256,
                "evaluation": {
                    "initial_bankroll": str(item.initial_bankroll),
                    "final_balance": str(item.final_balance),
                    "settled_stake": str(item.settled_stake),
                    "net_profit": str(item.net_profit),
                    "roi": str(item.roi),
                    "won": item.won,
                    "lost": item.lost,
                    "void": item.void,
                },
                "observed_delta_vs_baseline": {
                    "final_balance": str(item.final_balance - baseline.final_balance),
                    "net_profit": str(item.net_profit - baseline.net_profit),
                    "roi": str(item.roi - baseline.roi),
                },
            }
        )

    return {
        "schema_version": 1,
        "kind": "autosport_strategy_comparison",
        "comparison_scope": "same_sealed_dataset_single_run_per_strategy",
        "dataset": {
            "name": first.dataset_name,
            "sport": first.sport,
            "dataset_schema_version": first.dataset_schema_version,
            "market_sha256": first.market_sha256,
            "sealed_results_sha256": first.sealed_results_sha256,
            "historical_import_identity": first.historical_import_identity,
            "replay_dataset_hash": first.replay_dataset_hash,
            "event_count": first.event_count,
            "initial_bankroll": str(first.initial_bankroll),
        },
        "baseline_strategy_id": baseline_strategy_id,
        "strategies": entries,
        "observed_paper_order_by_net_profit": observed_order,
        "truth": {
            "paper_only": True,
            "governed_historical_import": first.historical_import_identity is not None,
            "real_historical_market_coverage_verified": False,
            "licensing_retention_verified": False,
            "profitability_claim": False,
            "predictive_superiority_claim": False,
            "out_of_sample_claim": False,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m autosport.strategy_comparison",
        description="Fail-closed comparison of paper strategy run summaries on the exact same sealed dataset.",
    )
    parser.add_argument("run_summaries", nargs="+", type=Path)
    parser.add_argument("--baseline-strategy", default=None)
    parser.add_argument("--output", type=Path, default=Path("strategy-comparison.json"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runs = tuple(load_strategy_run_summary(path) for path in args.run_summaries)
        report = compare_strategy_runs(runs, baseline_strategy_id=args.baseline_strategy)
        atomic_write_json(args.output, report)
    except (OSError, ValueError) as exc:
        print(f"strategy_comparison=FAIL_CLOSED error={exc}")
        return 2
    print(
        f"strategy_comparison=OK strategies={len(report['strategies'])} "
        f"baseline={report['baseline_strategy_id']} scope={report['comparison_scope']}"
    )
    print("profitability_claim=false predictive_superiority_claim=false real_money_execution=false")
    print(f"report={args.output}")
    return 0


def _required_text(payload: dict[str, Any], field: str, source: Path) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: {field} must be a non-empty string")
    return value


def _required_hash(payload: dict[str, Any], field: str, source: Path) -> str:
    value = _required_text(payload, field, source)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise ValueError(f"{source}: {field} must be a SHA-256 hex digest")
    return value.lower()


def _optional_hash(value: Any, field: str, source: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise ValueError(f"{source}: {field} must be null or a SHA-256 hex digest")
    return value.lower()


def _required_int(payload: dict[str, Any], field: str, source: Path, *, minimum: int) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{source}: {field} must be an integer >= {minimum}")
    return value


def _required_decimal(
    payload: dict[str, Any],
    field: str,
    source: Path,
    *,
    minimum: Decimal | None = None,
) -> Decimal:
    value = payload.get(field)
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{source}: {field} must be a finite decimal value")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{source}: {field} must be a finite decimal value") from exc
    if not parsed.is_finite() or (minimum is not None and parsed < minimum):
        raise ValueError(f"{source}: {field} is outside the allowed range")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
