from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

from .dataset import load_dataset
from .integrity import sha256_file
from .research_strategy import RESEARCH_STRATEGY_ID, ResearchStrategyPlan
from .session import AutosportSession


def _require_packaged_plan(dataset_root: Path, research_plan_path: Path) -> None:
    root = dataset_root.resolve()
    plan = research_plan_path.resolve()
    try:
        plan.relative_to(root)
    except ValueError as exc:
        raise ValueError("product journey research plan must be contained in the selected dataset directory") from exc


def run_product_journey_audit(
    output_path: str | Path,
    dataset_root: str | Path,
    research_plan_path: str | Path,
) -> int:
    """Exercise the shipped causal research replay -> paper -> settlement -> evaluation path.

    This is a deterministic machine release gate over the shipped sample only. It is
    deliberately not evidence of real historical market coverage, profitability, or
    physical Windows/NVDA acceptance.
    """

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        dataset_root = Path(dataset_root)
        research_plan_path = Path(research_plan_path)
        _require_packaged_plan(dataset_root, research_plan_path)

        dataset = load_dataset(dataset_root)
        plan = ResearchStrategyPlan.from_path(research_plan_path)
        events = dataset.load_market_events()
        plan.preflight(events)

        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            session = AutosportSession(
                workspace,
                "1000",
                strategy_id=RESEARCH_STRATEGY_ID,
                research_plan=plan,
            )
            try:
                result = session.run_dataset(dataset)
                if result.replay.event_count != len(events) or result.replay.event_count <= 0:
                    raise RuntimeError("product journey did not consume the complete causal replay stream")
                if len(result.settled_ticket_ids) != 1:
                    raise RuntimeError("product journey did not produce exactly one settled demo paper ticket")
                if result.balance != Decimal("990"):
                    raise RuntimeError("product journey demo balance does not match the deterministic fixture")
                if result.evaluation.net_profit != Decimal("-10"):
                    raise RuntimeError("product journey demo evaluation does not match the deterministic fixture")
                if session.registry.in_progress():
                    raise RuntimeError("product journey left an unresolved economic transaction")

                summary_path = Path(result.result_path)
                if not summary_path.is_file():
                    raise RuntimeError("product journey durable run summary is missing")
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                if not isinstance(summary, dict):
                    raise RuntimeError("product journey durable run summary is not an object")
                runtime = summary.get("strategy_runtime")
                if not isinstance(runtime, dict):
                    raise RuntimeError("product journey durable strategy runtime evidence is missing")
                if runtime.get("canonical_strategy_id") != RESEARCH_STRATEGY_ID:
                    raise RuntimeError("product journey did not execute research-replay-v1")
                if runtime.get("research_plan_sha256") != plan.source_sha256:
                    raise RuntimeError("product journey durable summary is not bound to the selected research plan")
                if summary.get("market_sha256") != dataset.market_sha256:
                    raise RuntimeError("product journey durable summary market hash mismatch")
                if summary.get("sealed_results_sha256") != dataset.results_sha256:
                    raise RuntimeError("product journey durable summary results hash mismatch")
                if summary.get("real_money_execution") is not False:
                    raise RuntimeError("product journey durable summary violated REAL_MONEY_EXECUTION=false")

                payload = {
                    "status": "PASS",
                    "evidence_scope": (
                        "packaged executable causal research replay/paper/settlement/evaluation journey "
                        "over the shipped deterministic sample; not real historical market coverage, "
                        "profitability proof, or physical human/NVDA evidence"
                    ),
                    "dataset_name": dataset.name,
                    "dataset_schema_version": dataset.schema_version,
                    "market_sha256": dataset.market_sha256,
                    "sealed_results_sha256": dataset.results_sha256,
                    "research_plan_sha256": plan.source_sha256,
                    "canonical_strategy_id": RESEARCH_STRATEGY_ID,
                    "strategy_identity": session.strategy_id,
                    "event_count": result.replay.event_count,
                    "settled_ticket_count": len(result.settled_ticket_ids),
                    "balance": str(result.balance),
                    "net_profit": str(result.evaluation.net_profit),
                    "run_summary_sha256": sha256_file(summary_path),
                    "transaction_terminal": True,
                    "governed_historical_import": dataset.governance is not None,
                    "real_historical_point_in_time_market_coverage_verified": False,
                    "profitability_claim": False,
                    "real_money_execution": False,
                    "human_tested": False,
                    "nvda_verified": False,
                }
            finally:
                session.close()

        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
            "real_historical_point_in_time_market_coverage_verified": False,
            "profitability_claim": False,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 1
