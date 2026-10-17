"""Nonblocking two-file support export for the existing Windows operator UI."""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path

from .operator_support import OperatorSupportTrace
from .json_integrity import strict_json_loads


@dataclass(frozen=True, slots=True)
class SupportExportMessage:
    completed: bool
    files: tuple[str, str] | None = None
    error_code: str | None = None
    recording_fault: bool = False


class OneShotSupportExportWorker:
    """Return bounded statuses; never forward exception prose or exported paths."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._queue: queue.Queue[SupportExportMessage] = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(
        self,
        trace: OperatorSupportTrace,
        directory: str | Path,
        *,
        strategy: str,
        research_plan_loaded: bool,
        agent_workspace: str | Path | None = None,
    ) -> bool:
        if not isinstance(trace, OperatorSupportTrace):
            return False
        if type(research_plan_loaded) is not bool:
            return False
        try:
            parent = Path(directory)
            selected_workspace = None if agent_workspace is None else Path(agent_workspace)
        except (TypeError, ValueError, OSError):
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        try:
            thread = threading.Thread(
                target=self._run,
                args=(trace, parent, strategy, research_plan_loaded, selected_workspace),
                name="autosport-support-export",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        except Exception:
            with self._lock:
                self._busy = False
            return False
        return True

    def _run(
        self,
        trace: OperatorSupportTrace,
        directory: Path,
        strategy: str,
        research_plan_loaded: bool,
        agent_workspace: Path | None,
    ) -> None:
        try:
            paths = trace.export_reports(
                directory,
                selected_strategy=strategy,
                research_plan_loaded=research_plan_loaded,
                agent_workspace=agent_workspace,
            )
            # Export has already verified the paired snapshot. Report the
            # recorded liveness truth, including absent/stale heartbeats,
            # rather than a mutable writer flag which misses those gaps.
            diagnostic_snapshot = strict_json_loads(
                paths[0].read_text(encoding="utf-8")
            )
            recorder_status = diagnostic_snapshot["coverage"]["recorder_status"]
            if recorder_status not in {
                "HEALTHY", "RECORDER_ERROR", "PARTIAL_DROPPED_EVENTS",
                "PARTIAL_PENDING_EVENTS", "NO_VERIFIED_HEARTBEAT",
                "PARTIAL_HEARTBEAT_GAPS",
            }:
                raise ValueError("unrecognized diagnostic recorder status")
            message = SupportExportMessage(
                completed=True,
                files=(paths[0].name, paths[1].name),
                recording_fault=recorder_status != "HEALTHY",
            )
        except Exception:
            message = SupportExportMessage(completed=False, error_code="SUPPORT_EXPORT_FAILED")
        self._queue.put(message)

    def poll(self) -> SupportExportMessage | None:
        try:
            result = self._queue.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy = False
        return result


@dataclass(frozen=True, slots=True)
class LearningStatusMessage:
    """Only bounded, non-sensitive summaries or a stable failure code."""

    lines: tuple[str, ...] | None = None
    error_code: str | None = None


class OneShotLearningStatusWorker:
    """Inspect canonical science/agent state off the keyboard/UI thread.

    Reuses the same verified-learning projector as the downloadable agent report;
    this is NOT a second learning engine and cannot train or promote a policy.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._queue: queue.Queue[LearningStatusMessage] = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(self, workspace: str | Path) -> bool:
        try:
            target = Path(workspace)
        except (OSError, TypeError, ValueError):
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        try:
            worker = threading.Thread(
                target=self._run, args=(target,), daemon=True,
                name="autosport-learning-status",
            )
            self._thread = worker
            worker.start()
        except Exception:
            with self._lock:
                self._busy = False
            return False
        return True

    def _run(self, workspace: Path) -> None:
        try:
            from .learning_evidence_projection import verified_learning_projection
            projection = verified_learning_projection(workspace)
            science = projection["science"]
            research = projection["research_runs"]
            loop = projection["agent_loop"]
            state_science = science["state"]
            state_research = research["state"]
            state_loop = loop["state"]
            # The known fixed states below are read from the canonical verifier,
            # never from untrusted disk strings or model-generated prose.
            if state_science not in {"NOT_PRESENT", "CANONICAL_REGISTRY_VERIFIED"}:
                raise ValueError("invalid scientific status")
            if state_research not in {"NOT_PRESENT", "VERIFIED_SUPERVISOR_SNAPSHOTS"}:
                raise ValueError("invalid research status")
            if state_loop not in {"NOT_PRESENT", "VERIFIED_RESTART_SNAPSHOT"}:
                raise ValueError("invalid agent loop status")
            lines = [
                "Науковий реєстр: " + (
                    "перевірено" if state_science == "CANONICAL_REGISTRY_VERIFIED"
                    else "записів немає"
                ),
                "Дослідницький цикл: " + (
                    "реєстр перевірено" if state_research == "VERIFIED_SUPERVISOR_SNAPSHOTS"
                    else "записів немає"
                ),
                "Цикл агентів: " + (
                    "стан відновлення перевірено" if state_loop == "VERIFIED_RESTART_SNAPSHOT"
                    else "записів немає"
                ),
            ]
            if state_science == "CANONICAL_REGISTRY_VERIFIED":
                lines.append("Канонічних наукових записів: " + str(science["records_total"]))
                lines.append("Експериментів: " + str(next(
                    x["count"] for x in science["types"] if x["type"] == "Experiment"
                )))
                lines.append("Рішень щодо промоції моделей: " + str(next(
                    x["count"] for x in science["types"] if x["type"] == "PromotionDecision"
                )))
                # Registry events are observations, not proven model gains.
                # Only allow canonical outcome names and bounded counters;
                # match their sum to the verified event count before announcing.
                for field, record_type, labels in (
                    (
                        "experiment_outcomes", "Experiment",
                        (
                            ("POSITIVE", "позитивні"),
                            ("NEGATIVE", "негативні"),
                            ("NULL", "нульові"),
                            ("HARMFUL", "шкідливі"),
                            ("INCONCLUSIVE", "неоднозначні"),
                            ("OTHER_OR_UNVERIFIED", "непідтверджені"),
                        ),
                    ),
                    (
                        "promotion_decisions", "PromotionDecision",
                        (
                            ("PROMOTE", "підвищення"),
                            ("RETAIN", "збереження"),
                            ("REJECT", "відхилення"),
                            ("ROLLBACK", "відкати"),
                            ("OTHER_OR_UNVERIFIED", "непідтверджені"),
                        ),
                    ),
                ):
                    counts = science.get(field)
                    expected = next(
                        x["count"] for x in science["types"]
                        if x["type"] == record_type
                    )
                    allowed = {key for key, _label in labels}
                    if (
                        type(counts) is not dict
                        or type(expected) is not int
                        or expected < 0
                        or any(
                            type(key) is not str or key not in allowed
                            or type(value) is not int or not 0 <= value <= 20000
                            for key, value in counts.items()
                        )
                        or sum(counts.values()) != expected
                    ):
                        raise ValueError("learning evidence counters are inconsistent")
                    label = (
                        "Результати експериментів" if record_type == "Experiment"
                        else "Рішення щодо моделей"
                    )
                    lines.append(
                        label + ": " + "; ".join(
                            f"{name} {counts.get(key, 0)}" for key, name in labels
                        )
                    )
            if state_research == "VERIFIED_SUPERVISOR_SNAPSHOTS":
                lines.append("Дослідницьких запусків: " + str(research["count"]))
            if state_loop == "VERIFIED_RESTART_SNAPSHOT":
                lines.append("Кроків циклу агентів: " + str(loop["sequence"]))
            lines.extend((
                "Усі дані — перевірені записи або явно відсутні джерела.",
                "Збереження старих знань і фактичне поліпшення моделі тут не доведені.",
                "Доступ до реальних коштів цим екраном не надається.",
            ))
            message = LearningStatusMessage(lines=tuple(lines))
        except Exception:
            message = LearningStatusMessage(error_code="LEARNING_STATUS_UNAVAILABLE")
        self._queue.put(message)

    def poll(self) -> LearningStatusMessage | None:
        try:
            result = self._queue.get_nowait()
        except queue.Empty:
            return None
        with self._lock:
            self._busy = False
        return result
