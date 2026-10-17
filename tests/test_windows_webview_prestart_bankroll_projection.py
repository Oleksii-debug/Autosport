"""Plan 6 2B: actual WebView PAPER balance readback must share START's bank.

Execute on this exact branch without a Windows build:
    PYTHONPATH=src python -m pytest -q tests/test_windows_webview_prestart_bankroll_projection.py

These tests call the existing controller's projection method and real
AutosportSession/PaperBook persistence. Only workspace selection is redirected
to pytest's temporary directory; no substitute ledger, money, network, or GUI.
"""
from __future__ import annotations

import json
from pathlib import Path

from autosport.paper import PaperBook
import autosport.windows_webview_shell as shell_module
from autosport.windows_webview_shell import AutosportWebController


def _controller(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "paper-workspace"
    controller = AutosportWebController.__new__(AutosportWebController)
    controller.workspace = tmp_path / "root"
    controller._active_workspace = controller.workspace
    controller._recovery_required_workspaces = set()
    controller._product_runtime_economic_snapshot = None
    controller.strategy_id = "baseline-v1"
    controller.research_plan = None
    controller.status = ""
    controller.last_error = ""
    controller.bank = ""
    controller.tickets = []
    monkeypatch.setattr(
        shell_module, "workspace_for_strategy", lambda *_args: workspace
    )
    return controller, workspace


def _manifest(workspace: Path, initial_bankroll: str) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "product_composition.json").write_text(
        json.dumps(
            {
                "schema": "autosport.autonomous_product_composition",
                "schema_version": 2,
                "source_id": "parlayapi:table_tennis",
                "initial_bankroll": initial_bankroll,
                "settlement_authority_identity": None,
            }
        ),
        encoding="utf-8",
    )


def test_prestart_reads_declared_bank_before_first_paper_tick(tmp_path, monkeypatch):
    controller, workspace = _controller(tmp_path, monkeypatch)
    _manifest(workspace, "250.00")

    controller._refresh_economic_projection()

    assert workspace not in controller._recovery_required_workspaces
    assert "250.00" in controller.bank
    assert "10000" not in controller.bank
    assert not (workspace / "paper_book.json").exists()


def test_existing_paperbook_is_the_authority_after_restart(tmp_path, monkeypatch):
    controller, workspace = _controller(tmp_path, monkeypatch)
    _manifest(workspace, "250.00")
    PaperBook("175.50").save(workspace / "paper_book.json")

    controller._refresh_economic_projection()

    assert workspace not in controller._recovery_required_workspaces
    assert "175.50" in controller.bank
    assert "250.00" not in controller.bank


def test_invalid_manifest_quarantines_without_fabricating_balance(tmp_path, monkeypatch):
    controller, workspace = _controller(tmp_path, monkeypatch)
    _manifest(workspace, "definitely-not-decimal")

    controller._refresh_economic_projection()

    assert workspace in controller._recovery_required_workspaces
    assert "10000" not in controller.bank
    assert "250.00" not in controller.bank
    assert not (workspace / "paper_book.json").exists()


def test_missing_manifest_reads_existing_paperbook_without_minting_one(tmp_path, monkeypatch):
    controller, workspace = _controller(tmp_path, monkeypatch)
    workspace.mkdir(parents=True, exist_ok=True)
    PaperBook("175.50").save(workspace / "paper_book.json")

    controller._refresh_economic_projection()

    assert workspace not in controller._recovery_required_workspaces
    assert "175.50" in controller.bank
    assert not (workspace / "product_composition.json").exists()


def test_zero_bank_manifest_fails_closed_without_mutating_paperbook(tmp_path, monkeypatch):
    controller, workspace = _controller(tmp_path, monkeypatch)
    _manifest(workspace, "0")
    original = PaperBook("175.50")
    original.save(workspace / "paper_book.json")
    bytes_before = (workspace / "paper_book.json").read_bytes()

    controller._refresh_economic_projection()

    assert workspace in controller._recovery_required_workspaces
    assert "175.50" not in controller.bank
    assert (workspace / "paper_book.json").read_bytes() == bytes_before
