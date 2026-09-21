from __future__ import annotations

from autosport.research_control_runtime import initialize_research_control_runtime


def test_product_scheduler_loop_has_production_timing_defaults(tmp_path) -> None:
    runtime = initialize_research_control_runtime(tmp_path, max_budget_units=8)

    # A supported product-level runtime must be able to execute its bounded
    # headless scheduler loop without requiring the caller to invent the clock
    # and sleeping authority. Low-level ResearchScheduler injection remains a
    # deterministic testing seam, not a prerequisite for product operation.
    assert runtime.run_scheduled(max_ticks=1) == 1

    snapshot = runtime.scheduler.snapshot()
    assert snapshot["occurrences"] == {}
