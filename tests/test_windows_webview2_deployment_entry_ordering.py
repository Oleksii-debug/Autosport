from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.windows_entry as windows_entry


class WebView2DeploymentEntrypointOrderingTests(unittest.TestCase):
    @staticmethod
    def _deployment_module(
        calls: list[str], *, exc: Exception | None = None
    ) -> types.ModuleType:
        module = types.ModuleType("autosport.webview2_runtime_deployment")

        class WebView2RuntimeDeploymentError(RuntimeError):
            pass

        def ensure_webview2_runtime():
            calls.append("deployment")
            if exc is not None:
                raise exc
            return types.SimpleNamespace(available=True)

        module.WebView2RuntimeDeploymentError = WebView2RuntimeDeploymentError
        module.ensure_webview2_runtime = ensure_webview2_runtime
        return module

    @staticmethod
    def _direct_preflight_module(calls: list[str]) -> types.ModuleType:
        module = types.ModuleType("autosport.webview2_runtime_preflight")

        def probe_webview2_runtime():
            calls.append("direct_preflight")
            return types.SimpleNamespace(available=True)

        module.probe_webview2_runtime = probe_webview2_runtime
        return module

    @staticmethod
    def _shell_module(calls: list[str]) -> types.ModuleType:
        module = types.ModuleType("autosport.windows_webview_shell")

        class WindowsWebViewUnavailable(RuntimeError):
            pass

        def main() -> int:
            calls.append("webview")
            return 17

        module.WindowsWebViewUnavailable = WindowsWebViewUnavailable
        module.main = main
        return module

    def test_runtime_deployment_precedes_workspace_mutation_and_shell_start(self) -> None:
        calls: list[str] = []
        workspace = Path.cwd().resolve() / ".autosport-deployment-order-test"

        def probe_workspace(_workspace: Path) -> None:
            calls.append("workspace")

        with (
            patch("autosport.paths.default_workspace", return_value=workspace),
            patch.object(
                windows_entry,
                "_probe_workspace_writable",
                side_effect=probe_workspace,
            ),
            patch.dict(
                sys.modules,
                {
                    "autosport.webview2_runtime_deployment": self._deployment_module(calls),
                    "autosport.webview2_runtime_preflight": self._direct_preflight_module(
                        calls
                    ),
                    "autosport.windows_webview_shell": self._shell_module(calls),
                },
            ),
        ):
            self.assertEqual(windows_entry.main([]), 17)

        self.assertEqual(calls, ["deployment", "workspace", "webview"])

    def test_deployment_failure_leaves_workspace_and_shell_unstarted(self) -> None:
        calls: list[str] = []
        workspace = Path.cwd().resolve() / ".autosport-deployment-failure-test"
        secret = "secret-bearing-deployment-detail"

        def probe_workspace(_workspace: Path) -> None:
            calls.append("workspace")

        direct_preflight = types.ModuleType("autosport.webview2_runtime_preflight")

        def unexpected_direct_preflight():
            calls.append("direct_preflight")
            raise AssertionError("interactive entry must use canonical deployment authority")

        direct_preflight.probe_webview2_runtime = unexpected_direct_preflight
        with (
            patch("autosport.paths.default_workspace", return_value=workspace),
            patch.object(
                windows_entry,
                "_probe_workspace_writable",
                side_effect=probe_workspace,
            ),
            patch.object(windows_entry, "_show_startup_error") as show_error,
            patch.dict(
                sys.modules,
                {
                    "autosport.webview2_runtime_deployment": self._deployment_module(
                        calls, exc=RuntimeError(secret)
                    ),
                    "autosport.webview2_runtime_preflight": direct_preflight,
                    "autosport.windows_webview_shell": self._shell_module(calls),
                },
            ),
        ):
            self.assertEqual(windows_entry.main([]), 3)

        self.assertEqual(calls, ["deployment"])
        show_error.assert_called_once()
        shown = show_error.call_args.args[0]
        self.assertEqual(shown, windows_entry._WEBVIEW2_STARTUP_ERROR)
        self.assertNotIn(secret, shown)

    def test_machine_mode_never_triggers_interactive_runtime_deployment(self) -> None:
        calls: list[str] = []
        fake_diagnostic = types.ModuleType("autosport.diagnostic")

        def run_machine_diagnostic(output: str) -> int:
            calls.append(f"diagnostic:{output}")
            return 23

        fake_diagnostic.run_machine_diagnostic = run_machine_diagnostic
        poison_deployment = types.ModuleType("autosport.webview2_runtime_deployment")

        def unexpected_deployment():
            raise AssertionError("machine mode must not deploy WebView2 Runtime")

        poison_deployment.ensure_webview2_runtime = unexpected_deployment
        with patch.dict(
            sys.modules,
            {
                "autosport.diagnostic": fake_diagnostic,
                "autosport.webview2_runtime_deployment": poison_deployment,
            },
        ):
            self.assertEqual(
                windows_entry.main(["--diagnostic-output", "report.json"]), 23
            )

        self.assertEqual(calls, ["diagnostic:report.json"])


if __name__ == "__main__":
    unittest.main()
