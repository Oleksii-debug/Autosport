from __future__ import annotations

import inspect

from autosport.product_windows_gui import ProductWindowsAutosportApp


def test_product_bridge_uses_canonical_session_teardown_and_exact_reopen() -> None:
    start_source = inspect.getsource(ProductWindowsAutosportApp.start_product_runtime)
    restore_source = inspect.getsource(
        ProductWindowsAutosportApp._restore_base_session_after_product
    )
    assert "self._hide_uncertain_economic_state(" in start_source
    assert "self._workspace_requires_recovery(workspace)" in start_source
    assert "list_product_source_entries()" in start_source
    assert "expected_source_id=expected_source_id" in start_source
    assert "self._open_session(" in restore_source
    assert "self._active_strategy_id" in restore_source
    assert "self._active_research_plan" in restore_source
    assert "AutosportSession(" not in restore_source


def test_product_runtime_failure_keeps_workspace_fail_closed() -> None:
    apply_source = inspect.getsource(ProductWindowsAutosportApp._apply_product_message)
    poll_source = inspect.getsource(ProductWindowsAutosportApp._poll_product_worker)
    assert "self._block_workspace_for_recovery(Path(self.workspace))" in apply_source
    assert "if self._product_last_stop is not None:" in poll_source
    assert "self._restore_base_session_after_product()" in poll_source
    assert "self._block_workspace_for_recovery(Path(self.workspace))" in poll_source


def test_packaged_bridge_does_not_replace_trusted_worker_authority() -> None:
    from autosport import product_gui_worker

    worker_source = inspect.getsource(product_gui_worker.ProductGuiWorker._run)
    assert "_profiled_runtime_builder" in worker_source
    assert "issue_trusted_runtime_code_profile" in worker_source
    assert "expected_source_id is not None" in worker_source
