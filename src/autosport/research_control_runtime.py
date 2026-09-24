"""Supported composition seam for Autosport's governed research control plane.

This module does not create a second scientific, scheduling, or execution authority.
It only binds the existing ScientificRegistry, ResearchSupervisor,
ResearchTriggerAdapter, NightResearchCurriculum, and ResearchScheduler into one
deterministic workspace layout that can be initialized explicitly and reopened
fail-closed after restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
import time

from .integrity import durable_path_lock
from .research_curriculum import CurriculumPurpose, NightResearchCurriculum, ReplayCandidate
from .research_scheduler import ResearchScheduler, TickResult
from .research_supervisor import ResearchSupervisor
from .research_trigger_adapter import ResearchTriggerAdapter
from .scientific_registry import ScientificRegistry


class ResearchControlRuntimeError(RuntimeError):
    """The research control-plane composition cannot be opened safely."""


@dataclass(frozen=True, slots=True)
class ResearchControlPaths:
    scientific_registry: Path
    supervisor: Path
    curriculum: Path
    scheduler: Path

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> "ResearchControlPaths":
        root = Path(workspace)
        return cls(
            scientific_registry=root / "scientific_registry.json",
            supervisor=root / "research_supervisor.json",
            curriculum=root / "research_curriculum.json",
            scheduler=root / "research_scheduler.json",
        )

    def all(self) -> tuple[Path, ...]:
        return (
            self.scientific_registry,
            self.supervisor,
            self.curriculum,
            self.scheduler,
        )


@dataclass(slots=True)
class ResearchControlRuntime:
    """One product composition over the existing governed research authorities."""

    workspace: Path
    paths: ResearchControlPaths
    scientific_registry: ScientificRegistry
    supervisor: ResearchSupervisor
    trigger_adapter: ResearchTriggerAdapter
    curriculum: NightResearchCurriculum
    scheduler: ResearchScheduler

    def __post_init__(self) -> None:
        self._require_canonical_graph()

    def _require_canonical_graph(self) -> None:
        """Revalidate product composition before every public delegation."""

        root = self.workspace.resolve()
        if self.paths != ResearchControlPaths.for_workspace(self.workspace):
            raise ResearchControlRuntimeError("research control paths are not canonical")
        if not isinstance(self.scientific_registry, ScientificRegistry):
            raise TypeError("scientific_registry must be ScientificRegistry")
        if not isinstance(self.supervisor, ResearchSupervisor):
            raise TypeError("supervisor must be ResearchSupervisor")
        if not isinstance(self.trigger_adapter, ResearchTriggerAdapter):
            raise TypeError("trigger_adapter must be ResearchTriggerAdapter")
        if not isinstance(self.curriculum, NightResearchCurriculum):
            raise TypeError("curriculum must be NightResearchCurriculum")
        if not isinstance(self.scheduler, ResearchScheduler):
            raise TypeError("scheduler must be ResearchScheduler")
        if self.supervisor.scientific_registry is not self.scientific_registry:
            raise ResearchControlRuntimeError("supervisor registry authority mismatch")
        if self.trigger_adapter.supervisor is not self.supervisor:
            raise ResearchControlRuntimeError("trigger adapter supervisor authority mismatch")
        if self.curriculum.trigger_adapter is not self.trigger_adapter:
            raise ResearchControlRuntimeError("curriculum trigger adapter authority mismatch")
        if self.scheduler.trigger_sink is not self.trigger_adapter:
            raise ResearchControlRuntimeError("scheduler trigger sink authority mismatch")
        if self.scheduler.source_registry is not self.scientific_registry:
            raise ResearchControlRuntimeError("scheduler registry authority mismatch")
        component_paths = (
            self.scientific_registry.path,
            self.supervisor.path,
            self.curriculum.path,
            self.scheduler.path,
        )
        for actual, expected in zip(component_paths, self.paths.all(), strict=True):
            if actual.resolve() != expected.resolve():
                raise ResearchControlRuntimeError(
                    "research control component path is not canonical"
                )
            if actual.parent.resolve() != root:
                raise ResearchControlRuntimeError(
                    "research control state must share one canonical workspace"
                )

    def tick_scheduled(self, *, now: str) -> TickResult:
        """Advance at most one ordinary scheduled wake through canonical authority."""

        self._require_canonical_graph()
        return self.scheduler.tick(now=now)

    def run_scheduled(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        poll_seconds: float = 1.0,
        max_ticks: int | None = None,
    ) -> int:
        """Run the canonical scheduler with production-safe timing defaults."""

        self._require_canonical_graph()
        resolved_clock = (
            clock
            if clock is not None
            else lambda: datetime.now(timezone.utc)
        )
        resolved_sleep = sleep if sleep is not None else time.sleep
        return self.scheduler.run(
            clock=resolved_clock,
            sleep=resolved_sleep,
            poll_seconds=poll_seconds,
            max_ticks=max_ticks,
        )

    def queue_curriculum_wake(
        self,
        candidates: Iterable[ReplayCandidate],
        *,
        purpose: CurriculumPurpose,
        selector_policy_version: str,
        as_of: str,
        seed: int,
        budget_units: int,
        max_concurrency: int,
        active_concurrency: int,
        remaining_budget_units: int,
        deadline_at: str | None = None,
    ) -> str:
        """Delegate one governed curriculum reservation to the canonical scheduler."""

        self._require_canonical_graph()
        return self.scheduler.queue_curriculum_wake(
            self.curriculum,
            candidates,
            purpose=purpose,
            selector_policy_version=selector_policy_version,
            as_of=as_of,
            seed=seed,
            budget_units=budget_units,
            max_concurrency=max_concurrency,
            active_concurrency=active_concurrency,
            remaining_budget_units=remaining_budget_units,
            deadline_at=deadline_at,
        )

    def tick_curriculum(
        self,
        candidates: Iterable[ReplayCandidate],
        *,
        max_concurrency: int,
        active_concurrency: int,
        remaining_budget_units: int,
    ) -> TickResult:
        """Advance the oldest governed curriculum wake through existing authorities."""

        self._require_canonical_graph()
        return self.scheduler.tick_curriculum(
            self.curriculum,
            candidates,
            max_concurrency=max_concurrency,
            active_concurrency=active_concurrency,
            remaining_budget_units=remaining_budget_units,
        )


def _state_presence(paths: ResearchControlPaths) -> tuple[bool, ...]:
    return tuple(path.exists() for path in paths.all())


def _max_budget_units(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ResearchControlRuntimeError("max_budget_units must be a positive integer")
    return value


def initialize_research_control_runtime(
    workspace: str | Path,
    *,
    max_budget_units: int,
) -> ResearchControlRuntime:
    """Create a new research control plane only when every child state is absent.

    Initialization and restart are deliberately separate operations. If a crash or
    external mutation leaves only some canonical child states present, this function
    refuses to fill in the missing authorities; callers must investigate/recover the
    partial workspace instead of silently rebasing research truth.
    """

    max_budget_units = _max_budget_units(max_budget_units)
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    paths = ResearchControlPaths.for_workspace(root)
    initialization_lock = root / "research_control_runtime.init"
    with durable_path_lock(initialization_lock):
        presence = _state_presence(paths)
        registry_exists, supervisor_exists, curriculum_exists, scheduler_exists = presence
        child_presence = (supervisor_exists, curriculum_exists, scheduler_exists)
        if any(child_presence):
            if registry_exists and all(child_presence):
                raise ResearchControlRuntimeError(
                    "research control state already exists; use open_research_control_runtime"
                )
            raise ResearchControlRuntimeError(
                "research control workspace is incomplete; refusing partial-state rebootstrap"
            )

        registry = (
            ScientificRegistry(paths.scientific_registry)
            if registry_exists
            else ScientificRegistry.initialize_pristine(paths.scientific_registry)
        )
        supervisor = ResearchSupervisor.initialize_pristine(paths.supervisor, registry)
        adapter = ResearchTriggerAdapter(supervisor)
        NightResearchCurriculum.initialize_pristine(
            paths.curriculum,
            adapter,
            max_budget_units=max_budget_units,
        )
        ResearchScheduler.initialize_pristine(
            paths.scheduler,
            adapter,
            source_registry=registry,
        )
    return open_research_control_runtime(
        root,
        max_budget_units=max_budget_units,
    )


def open_research_control_runtime(
    workspace: str | Path,
    *,
    max_budget_units: int,
) -> ResearchControlRuntime:
    """Open exactly one existing canonical research control-plane composition."""

    max_budget_units = _max_budget_units(max_budget_units)
    root = Path(workspace)
    paths = ResearchControlPaths.for_workspace(root)
    presence = _state_presence(paths)
    if not all(presence):
        missing = [
            path.name
            for path, exists in zip(paths.all(), presence, strict=True)
            if not exists
        ]
        raise ResearchControlRuntimeError(
            "research control workspace is incomplete; missing: " + ", ".join(missing)
        )

    registry = ScientificRegistry(paths.scientific_registry)
    supervisor = ResearchSupervisor(paths.supervisor, registry)
    adapter = ResearchTriggerAdapter(supervisor)
    curriculum = NightResearchCurriculum(
        paths.curriculum,
        adapter,
        max_budget_units=max_budget_units,
    )
    scheduler = ResearchScheduler(
        paths.scheduler,
        adapter,
        source_registry=registry,
    )
    return ResearchControlRuntime(
        workspace=root,
        paths=paths,
        scientific_registry=registry,
        supervisor=supervisor,
        trigger_adapter=adapter,
        curriculum=curriculum,
        scheduler=scheduler,
    )
