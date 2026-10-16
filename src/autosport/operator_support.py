"""Operator-owned diagnostic and agent evidence exports, not execution authority.

This module reuses the existing verified forensic session journal.  It samples
application liveness at one-second intervals while running, records explicitly
instrumented activities, and exports two inspectable UTF-8 JSON documents.
Uninstrumented OS/browser/provider work is never represented as observed.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import stat
import threading
import queue
import re
from typing import Callable, Iterable

from .forensic_session_journal import ForensicSessionJournal, read_verified_records
from .gui_evidence_export import resolve_evidence_output_destination
from .learning_evidence_projection import verified_learning_projection
from .integrity import atomic_write_json

REPORT_VERSION = 1
REPORT_DAYS = 3
MAX_RECORDS = 350_000
_MAX_PENDING_ACTIVITY = 1024
_OBSERVED_SECOND = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
TRACE_DIRECTORY = "operator-support-trace"
DIAGNOSTIC_FILENAME = "Autosport-diagnostics.json"
AGENTS_FILENAME = "Autosport-agents.json"
_STATUS = frozenset({"active", "idle", "started", "completed", "rejected", "failed", "stopped", "unknown"})
_COMPONENT = frozenset({"interface", "runtime", "agent", "research", "learning", "memory", "strategy", "source", "recovery", "export"})
_ACTION = frozenset({
    "dataset.select", "strategy.set", "research_plan.select", "replay_speed.set",
    "live_mode.set", "replay.run", "live.refresh", "recovery.run",
    "evidence.export", "support.export", "agents.export", "agents.status", "product_source.configure",
    "product_runtime.start", "product_runtime.stop", "surface.select",
    "owner.preview", "owner.initialize", "manual.calculate", "manual.clear",
    "product.started", "product.tick", "product.stopped", "product.error",
    "replay.finished", "live.finished", "recovery.finished", "research.selected",
    "memory.observed", "learning.observed",
})
# Ukrainian explanations are presentation only; they cannot replace canonical
# strategy identity, claim model performance, or grant any execution capability.
_STRATEGY_GOALS_UK = {
    "baseline-v1": (
        "Прозора тестова симуляція: віртуальний квиток створюється лише "
        "для причинно підтвердженої тестової події з окремим сигналом."
    ),
    "observe-only-v1": (
        "Контрольна стратегія спостереження: відстежує стан ринку "
        "без створення навіть віртуальних квитків."
    ),
    "research-replay-v1": (
        "Дослідницький симуляційний повтор: перевіряє дані, прогнози, "
        "критику, портфельні наслідки та обмеження ризику."
    ),
}
_AGENT_PURPOSES_UK = {
    "market-mirror": "Формувати причинно узгоджений стан котирувань та їхню історію.",
    "paper-baseline": "Пропонувати лише контрольовані віртуальні тестові дії.",
    "research-replay-pipeline": (
        "Перевіряти прогнози, дослідницькі гіпотези, критичні зауваження, "
        "наслідки для портфеля і паперовий ризик без права на реальні ставки."
    ),
}


def strategy_goal_uk(strategy_id: str) -> str:
    return _STRATEGY_GOALS_UK.get(
        strategy_id,
        "Мету цієї стратегії ще не локалізовано; "
        "її технічний ідентифікатор відображається окремо.",
    )


def agent_purpose_uk(agent_id: str) -> str:
    return _AGENT_PURPOSES_UK.get(
        agent_id,
        "Роль цього агента ще не локалізовано; ідентифікатор "
        "не підтверджує виконання конкретної роботи.",
    )


# Expose metadata only, NEVER the contents of economic, model or agent state.
_ARTIFACTS = {
    "decisions.jsonl": "Канонічні рішення та докази",
    "run_registry.json": "Зареєстровані запуски стратегій",
    "paper_book.json": "Паперові операції та результати",
    "source_health.json": "Стан джерел даних",
    "scientific-registry.json": "Канонічна історія досліджень і промоцій",
    "scientific_registry.json": "Можливий канонічний реєстр досліджень",
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
        self._pending: queue.Queue[tuple[str, str, str, int | None, str]] = queue.Queue(
            maxsize=_MAX_PENDING_ACTIVITY
        )
        self._lost_activity_count = 0
        self._lost_activity_lock = threading.Lock()
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
                self._drain_pending()
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

    def observe(
        self,
        *,
        component: str,
        action: str,
        status: str,
        cycle: int | None = None,
    ) -> None:
        """Nonblocking UI-facing event observation; never wait for disk I/O.

        Capture the original UTC second before queuing so a slow recorder never
        silently changes the meaning of event time. Overflow is surfaced in
        both reports instead of blocking STOP or falsely claiming completeness.
        """
        component = _canonical_component(component)
        action = _canonical_action(action)
        status = _canonical_status(status)
        cycle = _bounded_cycle(cycle)
        observed_second = _seconds(_utc(self._clock).isoformat().replace("+00:00", "Z"))
        # Never acquire the recorder's disk-I/O lock here. A hung filesystem
        # must not delay keyboard commands or the emergency STOP lane.
        if self._closed or self._fault is not None:
            return
        try:
            self._pending.put_nowait((component, action, status, cycle, observed_second))
        except queue.Full:
            with self._lost_activity_lock:
                self._lost_activity_count += 1

    def _drain_pending(self) -> None:
        """Only a background worker or export worker invokes durable writes."""
        with self._guard:
            while True:
                try:
                    component, action, status, cycle, observed = self._pending.get_nowait()
                except queue.Empty:
                    return
                self._record_verified(
                    component=component, action=action,
                    status=status, cycle=cycle, observed_second=observed,
                )

    def _record_verified(
        self,
        *,
        component: str,
        action: str,
        status: str,
        cycle: int | None,
        observed_second: str | None,
    ) -> None:
        if observed_second is not None and not _OBSERVED_SECOND.fullmatch(observed_second):
            raise ValueError("observed activity time is not canonical UTC second")
        if self._closed or self._fault is not None:
            return
        try:
            self._open_day()
            assert self._journal is not None
            payload = {"component": component, "action": action, "status": status}
            if cycle is not None:
                payload["cycle"] = cycle
            if observed_second is not None:
                payload["observed_second_utc"] = observed_second
            self._journal.append_material("support.activity", payload)
            if action == "product.started" and status == "started":
                self._phase = "active"
            if action == "product.stopped" and status == "stopped":
                self._phase = "idle"
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
        """Synchronous trusted test/science API; UI callers must use observe()."""
        component = _canonical_component(component)
        action = _canonical_action(action)
        status = _canonical_status(status)
        cycle = _bounded_cycle(cycle)
        with self._guard:
            self._record_verified(
                component=component, action=action, status=status,
                cycle=cycle, observed_second=None,
            )

    @property
    def fault(self) -> str | None:
        with self._guard:
            return self._fault

    @property
    def recorder_incomplete(self) -> bool:
        """A partial recorder never self-certifies complete activity capture."""
        with self._lost_activity_lock:
            lost = self._lost_activity_count
        return self._fault is not None or lost > 0 or not self._pending.empty()

    def _collect_records(self) -> tuple:
        with self._guard:
            # A recorder failure is *why* operators may need diagnostics. Keep
            # exporting previously committed verified records and mark gaps;
            # never call the damaged writer again or invent missing effects.
            if self._fault is None:
                self._drain_pending()
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

    def _resolve_agent_workspace(self, proposed: str | Path | None) -> Path:
        """Admit root or one exact canonical strategy storage directory only.

        Reports never read arbitrary user-selected paths or follow a strategy
        symlink out of Autosport's economic workspace authority.
        """
        try:
            root = self.workspace.resolve(strict=True)
            target = root if proposed is None else Path(proposed).resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise SupportExportError("selected strategy workspace cannot be resolved") from exc
        if target != root and target.parent != root / "strategies":
            raise SupportExportError("agent report workspace is outside canonical strategy scope")
        # A valid in-scope symlink alias also misattributes selected agent identity.
        if proposed is not None and Path(proposed).is_symlink():
            raise SupportExportError("agent report workspace is a symbolic link")
        return target

    def _artifact_inventory(self, agent_workspace: Path) -> list[dict[str, object]]:
        artifacts = []
        for basename, purpose in _ARTIFACTS.items():
            path = agent_workspace / basename
            # lexists distinguishes a missing member from a broken symlink.
            # The latter is malformed evidence and must fail closed.
            if not os.path.lexists(path):
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

    def _reports(self, *, selected_strategy: str, research_plan_loaded: bool,
                 agent_workspace: str | Path | None = None) -> tuple[dict, dict]:
        from .strategies import available_strategies, strategy_spec

        selection = strategy_spec(selected_strategy)
        selected_workspace = self._resolve_agent_workspace(agent_workspace)
        records = self._collect_records()
        activities = []
        seconds: dict[str, dict[str, object]] = {}
        agent_activity = []
        status_counts: Counter[str] = Counter()
        for record in records:
            if record.event_type not in {"support.activity", "lifecycle.heartbeat", "lifecycle.startup", "lifecycle.shutdown", "lifecycle.unclean_restart"}:
                raise SupportExportError("unsupported event in operator support trace")
            second = _seconds(record.timestamp_utc)
            if record.event_type == "support.activity":
                observed_second = record.payload.get("observed_second_utc")
                if observed_second is not None:
                    if type(observed_second) is not str or _OBSERVED_SECOND.fullmatch(observed_second) is None:
                        raise SupportExportError("support activity has an invalid observed second")
                    second = observed_second
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
            required = {"component", "action", "status"}
            optional = {"cycle", "observed_second_utc"}
            if not required.issubset(payload) or not set(payload).issubset(required | optional):
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
        # A single causal cutoff for both reports and scientific verification.
        snapshot_instant = _utc(self._clock)
        now = snapshot_instant.isoformat().replace("+00:00", "Z")
        with self._lost_activity_lock:
            lost_activities = self._lost_activity_count
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
                "recorder_status": (
                    self._fault if self._fault is not None
                    else ("PARTIAL_DROPPED_EVENTS" if lost_activities else "HEALTHY")
                ),
                "recorder_fault_present": self._fault is not None,
                "lost_activity_count": lost_activities,
                "pending_unflushed_activity_count": self._pending.qsize(),
                "complete_activity_capture_proven": False,
                "event_time_basis": "observed_utc_second_when_available_else_journal_timestamp",
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
            "goal_uk": strategy_goal_uk(spec.strategy_id),
            "agents": list(spec.agent_names),
            "agent_roles": [
                {"agent_id": name, "purpose_uk": agent_purpose_uk(name)}
                for name in spec.agent_names
            ],
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
            "lost_activity_count": lost_activities,
            "pending_unflushed_activity_count": self._pending.qsize(),
            "memory_and_learning_artifacts": self._artifact_inventory(selected_workspace),
            "agent_workspace_scope": (
                "ROOT" if selected_workspace == self.workspace.resolve(strict=True)
                else "CANONICAL_STRATEGY"
            ),
            "verified_learning_evidence": verified_learning_projection(
                selected_workspace, as_of=snapshot_instant
            ),
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
        # Both separately downloadable files describe one consistent snapshot.
        # This also detects a crash that left an old agent report next to a
        # newly written diagnostic report (or vice versa).
        pair_sha256 = hashlib.sha256(
            json.dumps(
                {"diagnostics": diagnostics, "agents": agents},
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        diagnostics["report_pair_sha256"] = pair_sha256
        agents["report_pair_sha256"] = pair_sha256
        return diagnostics, agents

    def export_reports(
        self,
        output_directory: str | Path,
        *,
        selected_strategy: str,
        research_plan_loaded: bool = False,
        agent_workspace: str | Path | None = None,
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
            agent_workspace=agent_workspace,
        )
        # The files are deliberately independent; failure cannot produce a
        # false "both reports exported" success status in the controller.
        atomic_write_json(diagnostic_path, diagnostics)
        atomic_write_json(agent_path, agents)
        verify_report_pair(diagnostic_path, agent_path)
        return diagnostic_path, agent_path

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._guard:
            if self._fault is None:
                self._drain_pending()
            if self._closed:
                return
            self._closed = True
            if self._journal is not None and self._fault is None:
                self._journal.close({"observation_phase": self._phase})


def verify_report_pair(diagnostic_path: str | Path, agent_path: str | Path) -> str:
    """Verify that BOTH written files belong to one unmodified evidence snapshot.

    This is a digest, not a signature, and cannot grant financial, learning or
    release authority. The user may send both JSONs to support for rechecking.
    """
    from .json_integrity import strict_json_loads

    def read(path: Path) -> dict:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SupportExportError("support report is not one regular file")
        if info.st_size > 128 * 1024 * 1024:
            raise SupportExportError("support report exceeds bounded read size")
        result = strict_json_loads(path.read_text(encoding="utf-8"))
        if type(result) is not dict:
            raise SupportExportError("support report must be a JSON object")
        return result

    diagnostics = read(Path(diagnostic_path))
    agents = read(Path(agent_path))
    supplied = diagnostics.pop("report_pair_sha256", None)
    other = agents.pop("report_pair_sha256", None)
    if (
        type(supplied) is not str or len(supplied) != 64
        or any(char not in "0123456789abcdef" for char in supplied)
        or supplied != other
    ):
        raise SupportExportError("two support reports have different snapshot identities")
    if (
        diagnostics.get("report_kind") != "autosport-operator-diagnostics"
        or agents.get("report_kind") != "autosport-agent-research-memory"
        or diagnostics.get("generated_at_utc") != agents.get("generated_at_utc")
    ):
        raise SupportExportError("two support reports have mismatched semantics")
    for report in (diagnostics, agents):
        if any(report.get(field) is not False for field in (
            "real_money_execution", "human_tested",
            "nvda_verified", "whole_product_complete",
        )):
            raise SupportExportError("support report claims unverified release authority")
    computed = hashlib.sha256(
        json.dumps(
            {"diagnostics": diagnostics, "agents": agents},
            sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if computed != supplied:
        raise SupportExportError("support report pair digest mismatch")
    return supplied


__all__ = [
    "OperatorSupportTrace", "SupportExportError", "DIAGNOSTIC_FILENAME",
    "AGENTS_FILENAME", "strategy_goal_uk", "agent_purpose_uk",
    "verify_report_pair",
]
