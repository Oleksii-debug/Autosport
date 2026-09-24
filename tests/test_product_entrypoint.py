from __future__ import annotations

import io
import json
import signal
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from autosport.event_lifecycle import CatalogPage
from autosport.product_entrypoint import (
    ProductEntrypointError,
    _product_stop_signals,
    run_product,
    run_product_command,
)


class _Source:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, checkpoint):
        position = 1 if checkpoint is None else int(getattr(checkpoint, "position")) + 1
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor=f"catalog-{position}",
            position=position,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()

    def resolve_event(self, delta):
        raise AssertionError("no market delta should be resolved in this test")


class _SourceWithoutResolution:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, checkpoint):
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor="catalog-1",
            position=1,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()


def _module(factory) -> types.ModuleType:
    module = types.ModuleType("autosport_test_product_source")
    module.make_source = factory
    return module


def _records(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


class SupportedProductEntrypointTests(unittest.TestCase):
    def test_supported_boundary_drives_tick_and_restores_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "product"
            source_module = _module(_Source)
            with patch.dict(
                sys.modules,
                {"autosport_test_product_source": source_module},
            ):
                first_output = io.StringIO()
                with redirect_stdout(first_output):
                    first_code = run_product(
                        workspace=workspace,
                        source_factory="autosport_test_product_source:make_source",
                        initial_bankroll="100",
                        max_cycles=1,
                        poll_seconds=0,
                        sleep=lambda _: self.fail("bounded run must not sleep"),
                        install_signal_handlers=False,
                    )
                first = _records(first_output.getvalue())
                self.assertEqual(first_code, 0)
                self.assertEqual(
                    [record["kind"] for record in first],
                    ["product_status", "product_tick", "product_status"],
                )
                self.assertTrue(all(record["paper_only"] is True for record in first))
                self.assertTrue(
                    all(record["real_money_execution"] is False for record in first)
                )
                first_session_id = first[0]["value"]["session_id"]
                self.assertEqual(first[1]["value"]["cycle_index"], 1)
                self.assertEqual(first[2]["value"]["state"], "STOPPED")

                second_output = io.StringIO()
                with redirect_stdout(second_output):
                    second_code = run_product(
                        workspace=workspace,
                        source_factory="autosport_test_product_source:make_source",
                        initial_bankroll="100",
                        max_cycles=1,
                        poll_seconds=0,
                        sleep=lambda _: self.fail("bounded run must not sleep"),
                        install_signal_handlers=False,
                    )
                second = _records(second_output.getvalue())
                self.assertEqual(second_code, 0)
                self.assertEqual(second[0]["value"]["session_id"], first_session_id)
                self.assertEqual(second[1]["value"]["cycle_index"], 2)

    def test_product_stop_signals_include_windows_sigbreak_when_available(self) -> None:
        with patch.object(signal, "SIGBREAK", 21, create=True):
            self.assertEqual(
                _product_stop_signals(),
                (int(signal.SIGINT), int(signal.SIGTERM), 21),
            )

    def test_supported_boundary_registers_and_restores_all_product_stop_signals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "product"
            source_module = _module(_Source)
            stop_signals = (int(signal.SIGINT), int(signal.SIGTERM), 21)
            previous_handlers = {signum: object() for signum in stop_signals}
            with (
                patch.dict(
                    sys.modules,
                    {"autosport_test_product_source": source_module},
                ),
                patch(
                    "autosport.product_entrypoint._product_stop_signals",
                    return_value=stop_signals,
                ),
                patch(
                    "autosport.product_entrypoint.signal.getsignal",
                    side_effect=lambda signum: previous_handlers[signum],
                ) as getsignal,
                patch("autosport.product_entrypoint.signal.signal") as set_signal,
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_product(
                        workspace=workspace,
                        source_factory="autosport_test_product_source:make_source",
                        initial_bankroll="100",
                        max_cycles=1,
                        poll_seconds=0,
                        sleep=lambda _: self.fail("bounded run must not sleep"),
                    )

            self.assertEqual(code, 0)
            self.assertEqual(
                [call.args[0] for call in getsignal.call_args_list],
                list(stop_signals),
            )
            self.assertEqual(set_signal.call_count, len(stop_signals) * 2)
            for index, signum in enumerate(stop_signals):
                self.assertEqual(set_signal.call_args_list[index].args[0], signum)
                self.assertTrue(callable(set_signal.call_args_list[index].args[1]))
                restored = set_signal.call_args_list[index + len(stop_signals)]
                self.assertEqual(restored.args, (signum, previous_handlers[signum]))

    def test_signal_lookup_failure_closes_runtime_before_handler_mutation(self) -> None:
        runtime = Mock()
        runtime.start.side_effect = AssertionError(
            "runtime must not start after signal handler lookup fails"
        )
        stop_signals = (int(signal.SIGINT), int(signal.SIGTERM), 21)
        lookup_calls: list[int] = []

        def get_signal(signum: int) -> object:
            lookup_calls.append(signum)
            if signum == stop_signals[1]:
                raise OSError("simulated signal handler lookup failure")
            return object()

        with (
            patch(
                "autosport.product_entrypoint._validated_source",
                return_value=object(),
            ),
            patch(
                "autosport.product_entrypoint.build_autonomous_product_runtime",
                return_value=runtime,
            ),
            patch(
                "autosport.product_entrypoint._product_stop_signals",
                return_value=stop_signals,
            ),
            patch(
                "autosport.product_entrypoint.signal.getsignal",
                side_effect=get_signal,
            ),
            patch("autosport.product_entrypoint.signal.signal") as set_signal,
        ):
            with self.assertRaisesRegex(
                OSError,
                "simulated signal handler lookup failure",
            ):
                run_product(
                    workspace="unused-workspace",
                    source_factory="unused:factory",
                    max_cycles=1,
                    poll_seconds=0,
                )

        self.assertEqual(lookup_calls, list(stop_signals[:2]))
        set_signal.assert_not_called()
        runtime.start.assert_not_called()
        runtime.close.assert_called_once_with()

    def test_partial_signal_install_failure_rolls_back_only_installed_handlers(
        self,
    ) -> None:
        runtime = Mock()
        runtime.start.side_effect = AssertionError(
            "runtime must not start after handler setup fails"
        )
        stop_signals = (int(signal.SIGINT), int(signal.SIGTERM), 21)
        previous_handlers = {signum: object() for signum in stop_signals}
        signal_calls: list[tuple[int, object]] = []

        def set_signal(signum: int, handler: object) -> object:
            signal_calls.append((signum, handler))
            if (
                signum == stop_signals[-1]
                and handler is not previous_handlers[signum]
            ):
                raise OSError("simulated SIGBREAK handler installation failure")
            return previous_handlers[signum]

        with (
            patch(
                "autosport.product_entrypoint._validated_source",
                return_value=object(),
            ),
            patch(
                "autosport.product_entrypoint.build_autonomous_product_runtime",
                return_value=runtime,
            ),
            patch(
                "autosport.product_entrypoint._product_stop_signals",
                return_value=stop_signals,
            ),
            patch(
                "autosport.product_entrypoint.signal.getsignal",
                side_effect=lambda signum: previous_handlers[signum],
            ),
            patch(
                "autosport.product_entrypoint.signal.signal",
                side_effect=set_signal,
            ),
        ):
            with self.assertRaisesRegex(
                OSError,
                "simulated SIGBREAK handler installation failure",
            ):
                run_product(
                    workspace="unused-workspace",
                    source_factory="unused:factory",
                    max_cycles=1,
                    poll_seconds=0,
                )

        runtime.start.assert_not_called()
        runtime.close.assert_called_once_with()
        restorations = [
            call
            for call in signal_calls
            if any(call[1] is prior for prior in previous_handlers.values())
        ]
        self.assertEqual(
            restorations,
            [
                (stop_signals[0], previous_handlers[stop_signals[0]]),
                (stop_signals[1], previous_handlers[stop_signals[1]]),
            ],
        )

    def test_missing_event_resolution_fails_before_workspace_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "must-not-exist"
            source_module = _module(_SourceWithoutResolution)
            with patch.dict(
                sys.modules,
                {"autosport_test_product_source": source_module},
            ):
                with self.assertRaisesRegex(
                    ProductEntrypointError,
                    "callable resolve_event",
                ):
                    run_product(
                        workspace=workspace,
                        source_factory="autosport_test_product_source:make_source",
                        max_cycles=1,
                        poll_seconds=0,
                        install_signal_handlers=False,
                    )
            self.assertFalse(workspace.exists())

    def test_malformed_factory_spec_fails_before_workspace_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "must-not-exist"
            with self.assertRaisesRegex(ValueError, "module:function"):
                run_product(
                    workspace=workspace,
                    source_factory="not-a-factory-spec",
                    max_cycles=1,
                    poll_seconds=0,
                    install_signal_handlers=False,
                )
            self.assertFalse(workspace.exists())

    def test_non_finite_poll_interval_fails_before_workspace_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, poll_seconds in enumerate((float("nan"), float("inf"))):
                workspace = root / f"must-not-exist-{index}"
                with self.assertRaisesRegex(
                    ValueError,
                    "finite non-negative",
                ):
                    run_product(
                        workspace=workspace,
                        source_factory="not-even-loaded:factory",
                        max_cycles=1,
                        poll_seconds=poll_seconds,
                        install_signal_handlers=False,
                    )
                self.assertFalse(workspace.exists())

    def test_unbounded_zero_poll_interval_fails_before_source_or_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "must-not-exist"
            with self.assertRaisesRegex(
                ValueError,
                "unbounded product run requires a positive poll interval",
            ):
                run_product(
                    workspace=workspace,
                    source_factory="not-even-loaded:factory",
                    max_cycles=None,
                    poll_seconds=0,
                    install_signal_handlers=False,
                )
            self.assertFalse(workspace.exists())

    def test_command_failure_output_never_echoes_source_exception_text(self) -> None:
        sentinel = "SENTINEL-CREDENTIAL-DO-NOT-PRINT"

        def failing_factory():
            raise RuntimeError(f"provider login failed token={sentinel}")

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "must-not-exist"
            source_module = _module(failing_factory)
            with patch.dict(
                sys.modules,
                {"autosport_test_product_source": source_module},
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_product_command(
                        workspace=workspace,
                        source_factory="autosport_test_product_source:make_source",
                        initial_bankroll="100",
                        max_cycles=1,
                        poll_seconds=0,
                    )
            self.assertEqual(code, 3)
            self.assertNotIn(sentinel, output.getvalue())
            self.assertNotIn("provider login failed", output.getvalue())
            records = _records(output.getvalue())
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["kind"], "product_start_failure")
            self.assertEqual(records[0]["error_code"], "product_start_failed")
            self.assertEqual(records[0]["error_type"], "RuntimeError")
            self.assertNotIn("error", records[0])
            self.assertFalse(workspace.exists())


if __name__ == "__main__":
    unittest.main()
