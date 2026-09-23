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
    _format_text_record,
    main as product_main,
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

    def test_text_format_drives_same_runtime_with_labelled_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "product"
            source_module = _module(_Source)
            with patch.dict(
                sys.modules,
                {"autosport_test_product_source": source_module},
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_product(
                        workspace=workspace,
                        source_factory="autosport_test_product_source:make_source",
                        initial_bankroll="100",
                        max_cycles=1,
                        poll_seconds=0,
                        output_format="text",
                        sleep=lambda _: self.fail("bounded run must not sleep"),
                        install_signal_handlers=False,
                    )

            text = output.getvalue()
            lines = text.splitlines()
            self.assertEqual(code, 0)
            self.assertEqual(lines.count("AUTOSPORT RECORD"), 3)
            self.assertEqual(lines.count("END AUTOSPORT RECORD"), 3)
            self.assertEqual(text.count('kind: "product_status"'), 2)
            self.assertEqual(text.count('kind: "product_tick"'), 1)
            self.assertEqual(text.count("paper_only: true"), 3)
            self.assertEqual(text.count("real_money_execution: false"), 3)
            self.assertIn('value.state: "STOPPED"', text)
            self.assertNotIn("\x1b", text)

    def test_text_formatter_escapes_control_characters_and_orders_labels(self) -> None:
        text = _format_text_record(
            {
                "value": {"state": "RUNNING", "note": "line\n\x1b[31mred"},
                "workspace": "C:\\autosport",
                "source_id": "provider-a",
                "real_money_execution": False,
                "paper_only": True,
                "kind": "product_status",
            }
        )

        lines = text.splitlines()
        self.assertEqual(lines[0], "AUTOSPORT RECORD")
        self.assertEqual(lines[1], 'kind: "product_status"')
        self.assertEqual(lines[2], "paper_only: true")
        self.assertEqual(lines[3], "real_money_execution: false")
        self.assertEqual(lines[4], 'source_id: "provider-a"')
        self.assertTrue(lines[5].startswith('workspace: "C:'))
        self.assertEqual(lines[-1], "END AUTOSPORT RECORD")
        self.assertIn('value.note: "line\\n\\u001b[31mred"', text)
        self.assertNotIn("\x1b", text)

    def test_text_formatter_escapes_mapping_keys_before_rendering_labels(self) -> None:
        hostile_key = "provider\n\x1b[31m.status"
        text = _format_text_record(
            {
                "kind": "product_tick",
                "paper_only": True,
                "real_money_execution": False,
                "value": {
                    hostile_key: "safe",
                    "ordinary_key": "unchanged",
                },
            }
        )

        lines = text.splitlines()
        self.assertIn('value["provider\\n\\u001b[31m.status"]: "safe"', text)
        self.assertIn('value.ordinary_key: "unchanged"', text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("provider\n", text)
        self.assertEqual(lines.count("AUTOSPORT RECORD"), 1)
        self.assertEqual(lines.count("END AUTOSPORT RECORD"), 1)


    def test_text_formatter_escapes_unicode_controls_but_preserves_readable_unicode(self) -> None:
        text = _format_text_record(
            {
                "kind": "product_status",
                "paper_only": True,
                "real_money_execution": False,
                "value": {
                    "message": "Український стан",
                    "hostile": "left\u202eright\u2028next\u0085end",
                },
            }
        )

        self.assertIn('value.message: "Український стан"', text)
        self.assertIn("\\u202e", text)
        self.assertIn("\\u2028", text)
        self.assertIn("\\u0085", text)
        self.assertNotIn("\u202e", text)
        self.assertNotIn("\u2028", text)
        self.assertNotIn("\u0085", text)

    def test_main_routes_explicit_text_format_without_changing_default_contract(self) -> None:
        with patch(
            "autosport.product_entrypoint.run_product_command",
            return_value=0,
        ) as command:
            code = product_main(
                [
                    "--source-factory",
                    "provider.module:make_source",
                    "--format",
                    "text",
                    "--max-cycles",
                    "1",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(command.call_args.kwargs["output_format"], "text")
        self.assertEqual(command.call_args.kwargs["source_factory"], "provider.module:make_source")

    def test_invalid_output_format_fails_before_source_or_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "must-not-exist"
            with self.assertRaisesRegex(ValueError, "output_format must be one of"):
                run_product(
                    workspace=workspace,
                    source_factory="not-even-loaded:factory",
                    max_cycles=1,
                    poll_seconds=0,
                    output_format="rich",
                    install_signal_handlers=False,
                )
            self.assertFalse(workspace.exists())

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

    def test_text_failure_output_never_echoes_source_exception_text(self) -> None:
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
                        output_format="text",
                    )

            text = output.getvalue()
            self.assertEqual(code, 3)
            self.assertNotIn(sentinel, text)
            self.assertNotIn("provider login failed", text)
            self.assertIn('kind: "product_start_failure"', text)
            self.assertIn('error_code: "product_start_failed"', text)
            self.assertIn('error_type: "RuntimeError"', text)
            self.assertFalse(workspace.exists())


if __name__ == "__main__":
    unittest.main()
