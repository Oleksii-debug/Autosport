"""Plan 6 3А — Windows update diagnostic, never installation authority."""
from pathlib import Path
import time
from unittest.mock import patch

from autosport.gui_update_diagnostic import OneShotUpdateDiagnosticWorker
from autosport.windows_webview_shell import AutosportWebController


def _until_message(worker):
    for _ in range(200):
        message = worker.poll()
        if message is not None:
            return message
        time.sleep(0.005)
    raise AssertionError("worker did not publish terminal status")


def test_valid_preflight_still_never_claims_install_authority(tmp_path):
    worker = OneShotUpdateDiagnosticWorker()
    with patch(
        "autosport.gui_update_diagnostic.run_canonical_update_diagnostic",
        return_value=(0, {
            "status": "DIAGNOSTIC_PASS_UNTRUSTED_ROOT",
            "install_authorized": False,
            "independent_trust_root_verified": False,
        }),
    ):
        assert worker.start(str(tmp_path / "request.json"))
        result = _until_message(worker)
    assert "НЕ дозволено" in result.status
    assert worker.busy is False


def test_forged_success_claim_fails_closed(tmp_path):
    worker = OneShotUpdateDiagnosticWorker()
    with patch(
        "autosport.gui_update_diagnostic.run_canonical_update_diagnostic",
        return_value=(0, {
            "status": "DIAGNOSTIC_PASS_UNTRUSTED_ROOT",
            "install_authorized": True,
            "independent_trust_root_verified": True,
        }),
    ):
        assert worker.start(str(tmp_path / "request.json"))
        result = _until_message(worker)
    assert "відхилено" in result.status
    assert worker.busy is False


def test_invalid_path_is_rejected_before_backend_call():
    worker = OneShotUpdateDiagnosticWorker()
    with patch("autosport.gui_update_diagnostic.run_canonical_update_diagnostic") as run:
        assert not worker.start("relative.json")
        assert not worker.start(" /unexpected.json ")
        assert not worker.start("")
        assert not worker.start(None)
        run.assert_not_called()


def test_product_handler_and_screen_readback(tmp_path):
    controller = AutosportWebController(tmp_path)
    try:
        assert "update_diagnostic" in controller.state()
        rejected = controller.dispatch({
            "request_id": "reject-update-1",
            "action_id": "update.diagnose",
            "payload": {"request_path": "relative.json"},
        })
        assert rejected["status"] == "rejected"
        assert "оновлення" in controller.state()["update_diagnostic"]["status"].lower()
    finally:
        controller.close()


def test_webview_ui_update_path_is_explicitly_non_authorizing():
    root = Path(__file__).resolve().parents[1] / "src" / "autosport" / "windows_web"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    assert html.count('id="update-diagnostic-button"') == 1
    assert 'id="update-diagnostic-status" role="status"' in html
    assert 'dispatch("update.diagnose"' in js
    assert "updateInfo.busy === true" in js
    assert "update-diagnostic-button" in js
