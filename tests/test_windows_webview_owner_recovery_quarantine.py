from __future__ import annotations

from pathlib import Path

from autosport.economic_goal_store import EconomicGoalStore
from autosport.owner_economic_authority import (
    INITIAL_OWNER_FORM_DEFAULTS,
    OwnerEconomicReviewSnapshot,
)
from autosport.windows_webview_shell import AutosportWebController


def _controller(workspace: Path) -> AutosportWebController:
    controller = AutosportWebController.__new__(AutosportWebController)
    controller._recovery_required_workspaces = set()
    controller._owner_review = None
    controller.owner_review_lines = []
    controller.owner_state = "absent"
    controller.owner_summary = ""
    controller.owner_lines = []
    controller.owner_can_initialize = True
    controller.last_error = ""
    controller.status = ""
    controller.log = []
    controller._owner_workspace = lambda: workspace
    controller._busy = lambda: False
    controller._poll_workers = lambda: None
    return controller


def test_owner_projection_blocks_and_revokes_review_for_quarantined_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "research-workspace"
    controller = _controller(workspace)
    review = OwnerEconomicReviewSnapshot.from_form(
        dict(INITIAL_OWNER_FORM_DEFAULTS),
        emergency_stop=False,
    )
    controller._owner_review = (workspace, review)
    controller.owner_review_lines = list(review.lines_uk)
    controller._recovery_required_workspaces.add(workspace)

    controller._refresh_owner_projection()

    assert controller.owner_state == "blocked"
    assert controller.owner_can_initialize is False
    assert "відновлення" in controller.owner_summary
    assert controller.owner_lines == [controller.owner_summary]
    assert controller._owner_review is None
    assert controller.owner_review_lines == []


def test_owner_preview_observes_terminal_quarantine_before_accepting_review(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "research-workspace"
    controller = _controller(workspace)
    poll_calls: list[str] = []

    def poll_workers() -> None:
        poll_calls.append("poll")
        controller._recovery_required_workspaces.add(workspace)

    controller._poll_workers = poll_workers

    result = controller._action_owner_preview(
        {
            "values": dict(INITIAL_OWNER_FORM_DEFAULTS),
            "emergency_stop": False,
        }
    )

    assert poll_calls == ["poll"]
    assert result["status"] == "rejected"
    assert "відновлення" in result["message"]
    assert controller._owner_review is None
    assert controller.owner_review_lines == []


def test_owner_initialize_observes_terminal_quarantine_before_durable_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "research-workspace"
    controller = _controller(workspace)
    values = dict(INITIAL_OWNER_FORM_DEFAULTS)
    review = OwnerEconomicReviewSnapshot.from_form(
        values,
        emergency_stop=False,
    )
    controller._owner_review = (workspace, review)
    controller.owner_review_lines = list(review.lines_uk)
    poll_calls: list[str] = []

    def poll_workers() -> None:
        poll_calls.append("poll")
        controller._recovery_required_workspaces.add(workspace)

    controller._poll_workers = poll_workers

    result = controller._action_owner_initialize(
        {
            "values": values,
            "emergency_stop": False,
            "confirmed": True,
        }
    )

    assert poll_calls == ["poll"]
    assert result["status"] == "rejected"
    assert "відновлення" in result["message"]
    assert controller._owner_review is None
    assert controller.owner_review_lines == []
    assert not (workspace / EconomicGoalStore.FILE_NAME).exists()
