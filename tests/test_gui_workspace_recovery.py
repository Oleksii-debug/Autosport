from __future__ import annotations

import inspect

from autosport.gui import AUTOMATION_IDS, AutosportApp


def test_gui_wires_fail_closed_workspace_recovery_to_keyboard_and_uia():
    build_source = inspect.getsource(AutosportApp._build)
    accessibility_source = inspect.getsource(AutosportApp._configure_accessibility)
    recovery_source = inspect.getsource(AutosportApp.repair_workspace)
    busy_source = inspect.getsource(AutosportApp._set_replay_controls_busy)
    error_source = inspect.getsource(AutosportApp._poll_replay_worker)

    assert AUTOMATION_IDS["repair_workspace"] == 108
    assert "self.repair_button = ttk.Button" in build_source
    assert 'self.bind("<Control-Shift-R>"' in build_source
    assert '"Відновити workspace"' in accessibility_source
    assert 'AUTOMATION_IDS["repair_workspace"]' in accessibility_source
    assert "if self.replay_worker.busy" in recovery_source
    assert "if self.live_worker.busy" in recovery_source
    assert "workspace_for_strategy(self.workspace, strategy_id, research_plan)" in recovery_source
    assert "reconcile_late_crashes(replay_workspace)" in recovery_source
    assert "report.unresolved_without_summary" in recovery_source
    assert "allow-repeat" in recovery_source
    assert 'self.repair_button.state(["disabled"])' in busy_source
    assert "Control+Shift+R" in error_source
