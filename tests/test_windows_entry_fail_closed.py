from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.windows_entry as windows_entry
from autosport.windows_entry import main


class WindowsEntrypointFailClosedTests(unittest.TestCase):
    @staticmethod
    def _failing_webview_module() -> types.ModuleType:
        module = types.ModuleType("autosport.windows_webview_shell")

        class WindowsWebViewUnavailable(RuntimeError):
            pass

        def unexpected_start() -> int:
            raise AssertionError("malformed packaged args must fail before WebView2 shell import")

        module.WindowsWebViewUnavailable = WindowsWebViewUnavailable
        module.main = unexpected_start
        return module

    @staticmethod
    def _deployment_module(*, available: bool = True, exc: Exception | None = None) -> types.ModuleType:
        module = types.ModuleType("autosport.webview2_runtime_deployment")

        def ensure_webview2_runtime():
            if exc is not None:
                raise exc
            return types.SimpleNamespace(available=available)

        module.ensure_webview2_runtime = ensure_webview2_runtime
        return module

    def _interactive_patches(self):
        workspace = Path.cwd().resolve() / ".autosport-entry-test-workspace"
        return (
            patch("autosport.paths.default_workspace", return_value=workspace),
            patch.object(windows_entry, "_probe_workspace_writable", return_value=None),
        )

    def test_unknown_packaged_argument_fails_before_webview_shell(self) -> None:
        with patch.dict(
            sys.modules,
            {"autosport.windows_webview_shell": self._failing_webview_module()},
        ):
            self.assertEqual(main(["--not-a-real-autosport-mode"]), 2)

    def test_known_machine_mode_with_wrong_arity_fails_before_webview_shell(self) -> None:
        with patch.dict(
            sys.modules,
            {"autosport.windows_webview_shell": self._failing_webview_module()},
        ):
            self.assertEqual(main(["--diagnostic-output"]), 2)
            self.assertEqual(
                main(["--research-demo-audit-output", "only-one-path"]),
                2,
            )

    def test_no_args_requires_preflight_before_webview_startup(self) -> None:
        calls: list[str] = []
        fake_emergency_stop = types.ModuleType(
            "autosport.windows_webview_emergency_stop"
        )
        fake_shell = types.ModuleType("autosport.windows_webview_shell")

        class EmergencyStopWebController:
            def __init__(self, workspace: Path) -> None:
                calls.append("controller")
                self.workspace = workspace

        class AutosportWebBridge:
            def __init__(self, controller: EmergencyStopWebController) -> None:
                calls.append("bridge")
                self.controller = controller

        class WindowsWebViewUnavailable(RuntimeError):
            pass

        def launch_windows_shell(bridge: AutosportWebBridge) -> int:
            self.assertIsInstance(bridge.controller, EmergencyStopWebController)
            calls.append("webview")
            return 17

        fake_emergency_stop.EmergencyStopWebController = EmergencyStopWebController
        fake_shell.AutosportWebBridge = AutosportWebBridge
        fake_shell.WindowsWebViewUnavailable = WindowsWebViewUnavailable
        fake_shell.launch_windows_shell = launch_windows_shell
        path_patch, workspace_patch = self._interactive_patches()
        with (
            path_patch,
            workspace_patch,
            patch.dict(
                sys.modules,
                {
                    "autosport.webview2_runtime_deployment": self._deployment_module(
                        available=True
                    ),
                    "autosport.windows_webview_emergency_stop": fake_emergency_stop,
                    "autosport.windows_webview_shell": fake_shell,
                },
            ),
        ):
            self.assertEqual(main([]), 17)

        self.assertEqual(calls, ["controller", "bridge", "webview"])

    def test_unavailable_runtime_fails_before_webview_shell_start(self) -> None:
        calls: list[str] = []
        fake_shell = types.ModuleType("autosport.windows_webview_shell")

        class WindowsWebViewUnavailable(RuntimeError):
            pass

        def shell_main() -> int:
            calls.append("webview")
            return 17

        fake_shell.WindowsWebViewUnavailable = WindowsWebViewUnavailable
        fake_shell.main = shell_main
        path_patch, workspace_patch = self._interactive_patches()
        with (
            path_patch,
            workspace_patch,
            patch.object(windows_entry, "_show_startup_error") as show_error,
            patch.dict(
                sys.modules,
                {
                    "autosport.webview2_runtime_deployment": self._deployment_module(
                        available=False
                    ),
                    "autosport.windows_webview_shell": fake_shell,
                },
            ),
        ):
            self.assertEqual(main([]), 3)

        self.assertEqual(calls, [])
        show_error.assert_called_once_with(windows_entry._WEBVIEW2_STARTUP_ERROR)

    def test_preflight_exception_fails_closed_without_detail_leak(self) -> None:
        path_patch, workspace_patch = self._interactive_patches()
        secret = "secret-bearing-preflight-detail"
        with (
            path_patch,
            workspace_patch,
            patch.object(windows_entry, "_show_startup_error") as show_error,
            patch.dict(
                sys.modules,
                {
                    "autosport.webview2_runtime_deployment": self._deployment_module(
                        exc=RuntimeError(secret)
                    ),
                    "autosport.windows_webview_shell": self._failing_webview_module(),
                },
            ),
        ):
            self.assertEqual(main([]), 3)

        shown = show_error.call_args.args[0]
        self.assertEqual(shown, windows_entry._WEBVIEW2_STARTUP_ERROR)
        self.assertNotIn(secret, shown)

    def test_valid_machine_mode_dispatches_without_runtime_preflight(self) -> None:
        calls: list[str] = []
        fake_diagnostic = types.ModuleType("autosport.diagnostic")

        def run_machine_diagnostic(output: str) -> int:
            calls.append(f"diagnostic:{output}")
            return 23

        fake_diagnostic.run_machine_diagnostic = run_machine_diagnostic
        poison_deployment = types.ModuleType("autosport.webview2_runtime_deployment")

        def unexpected_deployment():
            raise AssertionError("machine mode must not invoke interactive runtime preflight")

        poison_deployment.ensure_webview2_runtime = unexpected_deployment
        with patch.dict(
            sys.modules,
            {
                "autosport.diagnostic": fake_diagnostic,
                "autosport.webview2_runtime_deployment": poison_deployment,
            },
        ):
            self.assertEqual(main(["--diagnostic-output", "report.json"]), 23)

        self.assertEqual(calls, ["diagnostic:report.json"])

    def test_semantic_audit_flags_route_to_webview_audit_module(self) -> None:
        calls: list[str] = []
        fake_audit = types.ModuleType("autosport.windows_webview_audit")

        def accessibility(output: str) -> int:
            calls.append(f"a11y:{output}")
            return 31

        def keyboard(output: str) -> int:
            calls.append(f"keyboard:{output}")
            return 32

        fake_audit.run_accessibility_audit = accessibility
        fake_audit.run_keyboard_audit = keyboard
        with patch.dict(
            sys.modules,
            {"autosport.windows_webview_audit": fake_audit},
        ):
            self.assertEqual(main(["--accessibility-audit-output", "a.json"]), 31)
            self.assertEqual(main(["--keyboard-audit-output", "k.json"]), 32)

        self.assertEqual(calls, ["a11y:a.json", "keyboard:k.json"])


if __name__ == "__main__":
    unittest.main()
