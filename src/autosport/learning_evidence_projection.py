"""Read-only, source-verified research and agent-learning evidence for owner reports.

This is a projection over canonical Autosport registries. It neither trains a
model, promotes a champion, alters memory, nor grants execution authority.
Missing records do not count as zero learning and structural evidence cannot
substitute for verified economic improvement or physical product qualification.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import stat
from typing import Callable

_RESEARCH_TYPES = (
    "ResearchQuestion", "Hypothesis", "ResearchProtocol", "DatasetSnapshot",
    "FeatureSet", "ModelVersion", "StrategyVersion", "EvaluationBundle",
    "Experiment", "PromotionEvidence", "PromotionDecision", "Postmortem",
    "ChampionEligibilityDecision", "DriftFinding",
)
_MAX_RECORDS_PER_TYPE = 20000
_MAX_RECENT = 25
_SCIENTIFIC_NAMES = ("scientific-registry.json", "scientific_registry.json")
_ALLOWED_EXP_OUTCOMES = {"POSITIVE", "NEGATIVE", "NULL", "HARMFUL", "INCONCLUSIVE"}
_ALLOWED_PROMOTION = {"PROMOTE", "RETAIN", "REJECT", "ROLLBACK"}


def _regular_file(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise ValueError("agent evidence file is not an ordinary independent file")
    return True


def _safe_enum(value: object, allow: set[str]) -> str:
    if type(value) is str and value in allow:
        return value
    return "OTHER_OR_UNVERIFIED"


def _observed_identity(value: str) -> str:
    # Human-entered record IDs could contain sensitive data. Never export raw.
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verified_learning_projection(
    workspace: str | Path,
    *,
    as_of: datetime | None = None,
    registry_factory: Callable[[Path], object] | None = None,
    supervisor_factory: Callable[[Path, object], object] | None = None,
    agent_loop_factory: Callable[[Path], object] | None = None,
) -> dict[str, object]:
    root = Path(workspace)
    instant = datetime.now(timezone.utc) if as_of is None else as_of
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("learning report cutoff must be timezone aware")
    cutoff = instant.astimezone(timezone.utc).isoformat()

    paths = tuple(root / name for name in _SCIENTIFIC_NAMES)
    found = [path for path in paths if _regular_file(path)]
    if len(found) > 1:
        raise ValueError("ambiguous scientific registry sources")
    summary: dict[str, object] = {
        "science": {"state": "NOT_PRESENT", "cutoff_utc": cutoff},
        "research_runs": {"state": "NOT_PRESENT"},
        "agent_loop": {"state": "NOT_PRESENT"},
        "knowledge_retention": {
            "status": "NOT_EVALUATED",
            "meaning_uk": "Наявність моделі або записів не доводить збереження старих знань.",
        },
        "proven_live_model_learning": False,
        "economic_improvement_proven_by_this_report": False,
        "has_live_execution_authority": False,
    }
    registry: object | None = None
    if found:
        if registry_factory is None:
            from .scientific_registry import ScientificRegistry
            registry_factory = ScientificRegistry
        registry = registry_factory(found[0])
        groups: list[dict[str, object]] = []
        experiment_statuses: Counter[str] = Counter()
        promotions: Counter[str] = Counter()
        promotion_evidence_validity: Counter[str] = Counter()
        promotion_evidence_guardrail_failures = 0
        promotion_evidence_holdout_missing = 0
        total = 0
        for typ in _RESEARCH_TYPES:
            entries = registry.causal_records(typ, as_of=cutoff)
            if len(entries) > _MAX_RECORDS_PER_TYPE:
                raise ValueError("scientific history is too large for one report")
            total += len(entries)
            if total > _MAX_RECORDS_PER_TYPE:
                raise ValueError("scientific history exceeds bounded report scope")
            recent: list[dict[str, str]] = []
            for entry in entries[-_MAX_RECENT:]:
                recent.append({
                    "record_identity_sha256": _observed_identity(entry.record_id),
                    "record_sha256": entry.record_sha256,
                    "available_at": entry.available_at,
                })
            for entry in entries:
                if typ == "Experiment":
                    experiment_statuses[_safe_enum(entry.payload.get("outcome"), _ALLOWED_EXP_OUTCOMES)] += 1
                if typ == "PromotionDecision":
                    promotions[_safe_enum(entry.payload.get("action"), _ALLOWED_PROMOTION)] += 1
                if typ == "PromotionEvidence":
                    promotion_evidence_validity[_safe_enum(
                        entry.payload.get("validity"),
                        {"ELIGIBLE", "INCONCLUSIVE", "INVALID"},
                    )] += 1
                    if entry.payload.get("guardrails_passed") is False:
                        promotion_evidence_guardrail_failures += 1
                    if entry.payload.get("holdout_consumed") is not True:
                        promotion_evidence_holdout_missing += 1
            groups.append({"type": typ, "count": len(entries), "recent": recent})
        summary["science"] = {
            "state": "CANONICAL_REGISTRY_VERIFIED",
            "cutoff_utc": cutoff,
            "records_total": total,
            "types": groups,
            "experiment_outcomes": dict(sorted(experiment_statuses.items())),
            "promotion_decisions": dict(sorted(promotions.items())),
            "promotion_evidence_validity": dict(sorted(promotion_evidence_validity.items())),
            "promotion_evidence_guardrail_failures": promotion_evidence_guardrail_failures,
            "promotion_evidence_holdout_missing": promotion_evidence_holdout_missing,
            "evidence_scope": "causal_records_by_availability_not_training_performance_proof",
        }

    supervisor_path = root / "research-supervisor.json"
    if _regular_file(supervisor_path):
        if registry is None:
            raise ValueError("supervisor requires the canonical scientific registry")
        if supervisor_factory is None:
            from .research_supervisor import ResearchSupervisor
            supervisor_factory = ResearchSupervisor
        runs = supervisor_factory(supervisor_path, registry).list_runs()
        if len(runs) > _MAX_RECORDS_PER_TYPE:
            raise ValueError("research runs exceed bounded report scope")
        status_counts: Counter[str] = Counter()
        recent_runs = []
        for run in runs:
            phase = getattr(run.phase, "value", None)
            status = getattr(run.status, "value", None)
            if type(phase) is not str or type(status) is not str:
                raise ValueError("research run phase and status are not canonical")
            status_counts[status] += 1
        for run in runs[-_MAX_RECENT:]:
            recent_runs.append({
                "run_identity_sha256": _observed_identity(run.run_id),
                "phase": run.phase.value,
                "status": run.status.value,
                "checkpoint_sha256": run.checkpoint_sha256,
                "consumed_budget_units": run.consumed_budget_units,
                "updated_at": run.updated_at,
            })
        summary["research_runs"] = {
            "state": "VERIFIED_SUPERVISOR_SNAPSHOTS",
            "count": len(runs),
            "status_counts": dict(sorted(status_counts.items())),
            "recent": recent_runs,
        }

    agent_path = root / "agent-loop.json"
    if _regular_file(agent_path):
        if agent_loop_factory is None:
            from .agent_loop import AgentLoopRuntime
            agent_loop_factory = AgentLoopRuntime
        snapshot = agent_loop_factory(agent_path).snapshot()
        phase = getattr(snapshot.phase, "value", None)
        effect = getattr(snapshot.external_effect_state, "value", None)
        if type(phase) is not str or type(effect) is not str:
            raise ValueError("agent-loop state is not a canonical enum")
        summary["agent_loop"] = {
            "state": "VERIFIED_RESTART_SNAPSHOT",
            "phase": phase,
            "sequence": snapshot.sequence,
            "external_effect_state": effect,
            "updated_at": snapshot.updated_at,
            "state_sha256": snapshot.state_sha256,
            "postmortem_recorded": snapshot.postmortem_id is not None,
            "research_handoff_recorded": snapshot.research_run_id is not None,
            "economic_goal_identity_present": bool(snapshot.economic_goal_fingerprint),
        }
    return summary
