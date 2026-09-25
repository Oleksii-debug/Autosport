from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from autosport.event_lifecycle import CatalogPage
from autosport.product_entrypoint import (
    ProductEntrypointError,
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
