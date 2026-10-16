"""Operator-owned diagnostic and agent evidence exports, not execution authority.

This module reuses the existing verified forensic session journal.  It samples
application liveness at one-second intervals while running, records explicitly
instrumented activities, and exports two inspectable UTF-8 JSON documents.
Uninstrumented OS/browser/provider work is never represented as observed.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import threading
from typing import Callable, Iterable

from .forensic_session_journal import ForensicSessionJournal, read_verified_records
from .gui_evidence_export import resolve_evidence_output_destination
from .integrity import atomic_write_json

REPORT_VERSION = 1
REPORT_DAYS = 3
MAX_RECORDS = 350_000
TRACE_DIRECTORY = "operator-support-trace"
DIAGNOSTIC_FILENAME = "Autosport-diagnostics.json"
AGENTS_FILENAME = "Autosport-agents.json"
_STATUS = frozenset({"active", "idle", "started", "completed", "rejected", "failed", "stopped", "unknown"})
_COMPONENT = frozenset({"interface", "runtime", "agent", "research", "learning", "memory", "strategy", "source", "recovery", "export"})
_ACTION = frozenset({
    "dataset.select", "strategy.set", "research_plan.select", "replay_speed.set",
    "live_mode.set", "replay.run", "live.refresh", "recovery.run",
    "evidence.export", "support.export", "agents.export", "product_source.configure",
    "product_runtime.start", "product_runtime.stop", "surface.select",
    "owner.preview", "owner.initialize", "manual.calculate", "manual.clear",
    "product.started", "product.tick", "product.stopped", "product.error",
    "replay.finished", "live.finished", "recovery.finished", "research.selected",
    "memory.observed", "learning.observed",
})
# Expose metadata only, NEVER the contents of economic, model or agent state.
_ARTIFACTS = {
    "decisions.jsonl": "Канонічні рішення та докази",
    "run_registry.json": "Зареєстровані запуски стратегій",
    "paper_book.json": "Паперові операції та результати",
    "source_health.json": "Стан джерел даних",
    "scientific_registry.jsonl": "Історія досліджень, гіпотез і перевірок",
    "research-supervisor.json": "Контроль дослідницьких запусків",
    "agent-loop.json": "Стан циклу агентів і постмортеми",
    "sport-memory.json": "Історична пам’ять спортивних об’єктів",
}


class SupportExportError(ValueError):
    """The requested support evidence cannot be exported reliably or safely."""


def _utc(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise SupportExportError("support clock must be timezone aware")
    return now.astimezone(timezone.utc)


def _seconds(value: str) -> str:
    return value[:19] + "Z"


def _canonical_component(value: object) -> str:
    if type(value) is not str or value not in _COMPONENT:
        raise ValueError("support component is not an allowed product component")
    return value


def _canonical_status(value: object) -> str:
    if type(value) is not str or value not in _STATUS:
        raise ValueError("support status is not an allowed fixed status")
    return value


def _canonical_action(value: object) -> str:
    if type(value) is not str or value not in _ACTION:
        raise ValueError("support action is not an allowed product action")
    return value


def _bounded_cycle(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 1_000_000_000:
        raise ValueError("support cycle must be a bounded nonnegative integer")
    return value


class OperatorSupportTrace:
    """Single-writer daily journals with a one-second liveness heartbeat.

    The journal is **diagnostic** and must never be used for financial/risk/
    learning authority.  If instrumentation fails the fault remains explicit;
    the application is never declared healthy merely because a report exists.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        auto_start: bool = True,
        journal_factory: Callable[..., ForensicSessionJournal] = ForensicSessionJournal,
        journal_reader: Callable[..., tuple] = read_verified_records,
    ) -> None:
        self.workspace = Path(workspace)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._factory = journal_factory
        self._reader = journal_reader
        self._guard = threading.RLock()
        self._stop = threading.Event()
        self._closed = False
        self._fault: str | None = None
        self._phase = "idle"
        self._journal: ForensicSessionJournal | None = None
        self._day: str | None = None
        self._thread: threading.Thread | None = None
        self._open_day()
        if auto_start:
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name="autosport-support-second-sampler",
                daemon=True,
            )
            self._thread.start()

    def _trace_file(self, day: str) -> Path:
        return self.workspace / TRACE_DIRECTORY / f"support-{day}.jsonl"

    def _open_day(self) -> None:
        day = _utc(self._clock).date().isoformat()
        if self._day == day:
            return
        if self._journal is not None:
            self._journal.close({"observation_phase": self._phase})
        self._journal = self._factory(self._trace_file(day), clock=self._clock)
        self._day = day

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.heartbeat()
            except Exception:
                # Report collection defects truthfully; no raw error string is
                # stored because it can contain a path or a credential.
                with self._guard:
                    self._fault = "RECORDER_ERROR"
                return

    def heartbeat(self) -> None:
        with self._guard:
            if self._closed or self._fault is not None:
                return
            try:
                self._open_day()
                assert self._journal is not None
                self._journal.heartbeat({"component": "runtime", "status": self._phase})
            except Exception:
                self._fault = "RECORDER_ERROR"
                raise

    def record(
        self,
        *,
        component: str,
        action: str,
        status: str,
        cycle: int | None = None,
    ) -> None:
        component = _canonical_component(component)
        action = _canonical_action(action)
        status = _canonical_status(status)
        cycle = _bounded_cycle(cycle)
        with self._guard:
            if self._closed or self._fault is not None:
                return
            try:
                self._open_day()
                assert self._journal is not None
                payload = {"component": component, "action": action, "status": status}
                if cycle is not None:
                    payload["cycle"] = cycle
                self._journal.append_material("support.activity", payload)
                if action == "product.started" and status == "started":
                    self._phase = "active"
                if action == "product.stopped" and status == "stopped":
                    self._phase = "idle"
            except Exception:
                self._fault = "RECORDER_ERROR"
                raise

    @property
    def fault(self) -> str | None:
        with self._guard:
            return self._fault

    def _collect_records(self) -> tuple:
        with self._guard:
            if self._fault is not None:
                raise SupportExportError("support trace has an unverified recorder fault")
            days = tuple((_utc(self._clock).date() - timedelta(days=delta)).isoformat()
                         for delta in range(REPORT_DAYS - 1, -1, -1))
            result = []
            for day in days:
                path = self._trace_file(day)
                if day == self._day and self._journal is not None:
                    records = self._journal.snapshot()
                elif path.is_file():
                    records = self._reader(path)
                else:
                    continue
                result.extend(records)
                if len(result) > MAX_RECORDS:
                    raise SupportExportError("support trace is too large for a bounded export")
            return tuple(result)

    def _artifact_inventory(self) -> list[dict[str, object]]:
        artifacts = []
        for basename, purpose in _ARTIFACTS.items():
            path = self.workspace / basename
            if not path.exists():
                artifacts.append({"type": basename, "purpose_uk": purpose, "state": "NOT_PRESENT"})
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SupportExportError("agent evidence member is not an ordinary independent file")
            # A filename is only an evidence hint.  Never copy account records,
            # provider data, prompts or scientific trial raw content to support.
            artifacts.append({
                "type": basename, "purpose_uk": purpose, "state": "PRESENT_UNVERIFIED",
                "size_bytes": info.st_size,
            })
        return artifacts

    def _reports(self, *, selected_strategy: str, research_plan_loaded: bool) -> tuple[dict, dict]:
        from .strategies import available_strategies, strategy_spec

        selection = strategy_spec(selected_strategy)
        records = self._collect_records()
        activities = []
        seconds: dict[str, dict[str, object]] = {}
        agent_activity = []
        status_counts: Counter[str] = Counter()
        for record in records:
            if record.event_type not in {"support.activity", "lifecycle.heartbeat", "lifecycle.startup", "lifecycle.shutdown", "lifecycle.unclean_restart"}:
                raise SupportExportError("unsupported event in operator support trace")
            second = _seconds(record.timestamp_utc)
            slot = seconds.setdefault(second, {"second_utc": second, "heartbeat": False, "activities": 0})
            if record.event_type == "lifecycle.heartbeat":
                slot["heartbeat"] = True
                continue
            if record.event_type != "support.activity":
                continue
            payload = dict(record.payload)
            component = _canonical_component(payload.get("component"))
            action = _canonical_action(payload.get("action"))
            status = _canonical_status(payload.get("status"))
            cycle = _bounded_cycle(payload.get("cycle"))
            if set(payload) not in ({"component", "action", "status"}, {"component", "action", "status", "cycle"}):
                raise SupportExportError("operator support record contains unknown fields")
            event = {"second_utc": second, "component": component, "action": action, "status": status, "sequence": record.seq}
            if cycle is not None:
                event["cycle"] = cycle
            activities.append(event)
            slot["activities"] = int(slot["activities"]) + 1
            status_counts[status] += 1
            if component in {"agent", "strategy", "research", "learning", "memory", "runtime"}:
                agent_activity.append(event)

        heartbeats = sorted(x for x, v in seconds.items() if v["heartbeat"])
        missed = 0
        for left, right in zip(heartbeats, heartbeats[1:]):
            left_dt = datetime.fromisoformat(left.replace("Z", "+00:00"))
            right_dt = datetime.fromisoformat(right.replace("Z", "+00:00"))
            missed += max(0, int((right_dt - left_dt).total_seconds()) - 1)
        now = _utc(self._clock).isoformat().replace("+00:00", "Z")
        diagnostics = {
            "schema_version": REPORT_VERSION,
            "report_kind": "autosport-operator-diagnostics",
            "generated_at_utc": now,
            "coverage": {
                "scope": "instrumented_events_and_one_second_process_heartbeat_only",
                "requested_days": REPORT_DAYS,
                "first_heartbeat_utc": heartbeats[0] if heartbeats else None,
                "last_heartbeat_utc": heartbeats[-1] if heartbeats else None,
                "missing_heartbeat_seconds_between_observations": missed,
                "record_count": len(records),
                "activity_count": len(activities),
                "recorder_status": "HEALTHY" if self._fault is None else self._fault,
                "does_not_prove_os_or_provider_operations": True,
            },
            "seconds": [seconds[key] for key in sorted(seconds)],
            "activities": activities,
            "status_counts": dict(sorted(status_counts.items())),
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
            "whole_product_complete": False,
        }
        strategy_catalog = [{
            "strategy_id": spec.strategy_id,
            "goal_description": spec.description,
            "agents": list(spec.agent_names),
            "composition_sha256": spec.agent_composition_sha256,
            "opens_paper_tickets": spec.opens_paper_tickets,
            "requires_research_plan": spec.requires_research_plan,
            "selected": spec.strategy_id == selection.strategy_id,
        } for spec in available_strategies()]
        agents = {
            "schema_version": REPORT_VERSION,
            "report_kind": "autosport-agent-research-memory",
            "generated_at_utc": now,
            "current_strategy_id": selection.strategy_id,
            "current_agent_names": list(selection.agent_names),
            "current_agent_composition_sha256": selection.agent_composition_sha256,
            "research_plan_loaded": research_plan_loaded,
            "strategies": strategy_catalog,
            "instrumented_agent_activity": agent_activity,
            "memory_and_learning_artifacts": self._artifact_inventory(),
            "learning_verification": "NOT_INFERRED_FROM_CODE_OR_ARTIFACT_PRESENCE",
            "known_limits_uk": (
                "Наведені цілі — описи оголошених стратегій, а не підтверджені "
                "внутрішні наміри агентів. Пам’ять показана як наявність артефактів, "
                "не як прочитаний чи доведено засвоєний зміст. "
                "Недоступні дії та пропущені секунди не домислюються."
            ),
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
            "whole_product_complete": False,
        }
        return diagnostics, agents

    def export_reports(
        self,
        output_directory: str | Path,
        *,
        selected_strategy: str,
        research_plan_loaded: bool = False,
    ) -> tuple[Path, Path]:
        if type(research_plan_loaded) is not bool:
            raise SupportExportError("research plan status must be an exact boolean")
        folder = Path(output_directory)
        # Reuse the canonical evidence-export path security contract.  An
        # arbitrary sibling of the workspace is not an authorized destination.
        diagnostic_path = resolve_evidence_output_destination(
            self.workspace, folder / DIAGNOSTIC_FILENAME
        )
        agent_path = resolve_evidence_output_destination(
            self.workspace, folder / AGENTS_FILENAME
        )
        if not folder.is_dir():
            raise SupportExportError("support export directory must already exist")
        diagnostics, agents = self._reports(
            selected_strategy=selected_strategy,
            research_plan_loaded=research_plan_loaded,
        )
        # The files are deliberately independent; failure cannot produce a
        # false "both reports exported" success status in the controller.
        atomic_write_json(diagnostic_path, diagnostics)
        atomic_write_json(agent_path, agents)
        return diagnostic_path, agent_path

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._guard:
            if self._closed:
                return
            self._closed = True
            if self._journal is not None and self._fault is None:
                self._journal.close({"observation_phase": self._phase})


__all__ = ["OperatorSupportTrace", "SupportExportError", "DIAGNOSTIC_FILENAME", "AGENTS_FILENAME"]
