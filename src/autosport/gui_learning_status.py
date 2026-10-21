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
        if type(workspace) is not Path or not workspace.is_absolute():
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
            lines = (
                "Канонічний реєстр науки: " + science["state"],
                "Підтверджених наукових записів: " + str(total),
                "Перевірених дослідницьких запусків: " + str(runs),
                "Стан AgentLoop: " + loop["state"],
                "Доказу реального навчання моделей тут немає.",
                "Доказу економічного покращення або права на ставки тут немає.",
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
