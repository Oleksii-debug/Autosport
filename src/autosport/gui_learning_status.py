"""Read-only agent-learning evidence worker using the existing scientific authorities.

The worker never initiates learning, sends orders, promotes a policy, or
converts missing records into measured positive outcomes.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import queue
import threading

from .learning_evidence_projection import verified_learning_projection


@dataclass(frozen=True, slots=True)
class LearningStatusResult:
    lines: tuple[str, ...]


class OneShotLearningStatusWorker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._queue: queue.Queue[LearningStatusResult] = queue.Queue(maxsize=1)

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(self, workspace: Path) -> bool:
        if not isinstance(workspace, Path) or not workspace.is_absolute():
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        try:
            thread = threading.Thread(
                target=self._run, args=(workspace,),
                name="autosport-agent-learning-evidence", daemon=False,
            )
            thread.start()
        except Exception:
            with self._lock:
                self._busy = False
            return False
        return True

    def _run(self, workspace: Path) -> None:
        try:
            report = verified_learning_projection(workspace)
            if not isinstance(report, dict):
                raise ValueError("invalid projection")
            science = report["science"]
            research = report["research_runs"]
            loop = report["agent_loop"]
            for item in (science, research, loop):
                if not isinstance(item, dict):
                    raise ValueError("invalid evidence section")
            allowed_science = {"NOT_PRESENT", "CANONICAL_REGISTRY_VERIFIED"}
            allowed_research = {"NOT_PRESENT", "VERIFIED_SUPERVISOR_SNAPSHOTS"}
            allowed_loop = {"NOT_PRESENT", "VERIFIED_RESTART_SNAPSHOT"}
            if (
                science.get("state") not in allowed_science
                or research.get("state") not in allowed_research
                or loop.get("state") not in allowed_loop
                or report.get("proven_live_model_learning") is not False
                or report.get("economic_improvement_proven_by_this_report") is not False
                or report.get("has_live_execution_authority") is not False
            ):
                raise ValueError("untrusted learning claim")
            total = science.get("records_total", 0)
            runs = research.get("count", 0)
            if any(type(v) is not int or v < 0 or v > 20000 for v in (total, runs)):
                raise ValueError("invalid counts")
            model_count = 0
            evaluation_count = 0
            promotion_count = 0
            if science["state"] == "CANONICAL_REGISTRY_VERIFIED":
                types = science.get("types")
                if not isinstance(types, list):
                    raise ValueError("missing canonical science count groups")
                counts = {}
                for group in types:
                    if not isinstance(group, dict):
                        raise ValueError("invalid science group")
                    kind, count = group.get("type"), group.get("count")
                    if (
                        type(kind) is not str or kind in counts
                        or type(count) is not int or count < 0 or count > 20000
                    ):
                        raise ValueError("invalid canonical group counter")
                    counts[kind] = count
                model_count = counts.get("ModelVersion", 0)
                evaluation_count = counts.get("EvaluationBundle", 0)
                promotion_count = counts.get("PromotionDecision", 0)
            lines = (
                "Канонічний реєстр науки: " + science["state"],
                "Підтверджених наукових записів: " + str(total),
                "Зареєстрованих версій моделей: " + str(model_count),
                "Записів оцінювання: " + str(evaluation_count),
                "Рішень про просування моделей: " + str(promotion_count),
                "Перевірених дослідницьких запусків: " + str(runs),
                "Стан AgentLoop: " + loop["state"],
                "Записи самі по собі не доводять навчання або економічного покращення.",
                "Права на реальні ставки цей звіт не надає.",
            )
        except Exception:
            lines = (
                "Перевірений стан агентів недоступний; навчання не підтверджено.",
            )
        self._queue.put(LearningStatusResult(lines))

    def poll(self) -> LearningStatusResult | None:
        try:
            result = self._queue.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy = False
        return result
