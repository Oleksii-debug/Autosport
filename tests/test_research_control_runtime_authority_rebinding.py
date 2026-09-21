from __future__ import annotations

import pytest

from autosport.research_control_runtime import (
    ResearchControlRuntimeError,
    initialize_research_control_runtime,
)


def test_runtime_rejects_scheduler_rebound_to_another_workspace(tmp_path):
    canonical = initialize_research_control_runtime(
        tmp_path / "canonical",
        max_budget_units=8,
    )
    other = initialize_research_control_runtime(
        tmp_path / "other",
        max_budget_units=8,
    )

    # The composition validated canonical wiring only once in __post_init__.
    # A later same-object field mutation must not let product façade methods
    # delegate through another workspace's scheduler authority.
    object.__setattr__(canonical, "scheduler", other.scheduler)

    with pytest.raises(ResearchControlRuntimeError):
        canonical.tick_scheduled(now="2026-09-21T12:00:00Z")
