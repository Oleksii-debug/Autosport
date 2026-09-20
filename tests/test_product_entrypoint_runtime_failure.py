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
from autosport.product_entrypoint import run_product_command


_SENTINEL = "SENTINEL-RUNTIME-CREDENTIAL-DO-NOT-PRINT"


class _HealthySource:
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


class _FailingTickSource(_HealthySource):
    def fetch_catalog_page(self, checkpoint):
        raise RuntimeError(f"provider tick failed token={_SENTINEL}")


def _module(factory) -> types.ModuleType:
    module = types.ModuleType("autosport_test_product_runtime_failure_source")
    module.make_source = factory
    return module


def _records(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line.strip()]


class ProductRuntimeFailureTests(unittest.TestCase):
    def test_post_start_tick_failure_is_secret_safe_phase_accurate_and_recoverable(self) -> None:
        module_name = "autosport_test_product_runtime_failure_source"
        factory_spec = f"{module_name}:make_source"

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "product"

            with patch.dict(sys.modules, {module_name: _module(_FailingTickSource)}):
                failed_output = io.StringIO()
                with redirect_stdout(failed_output):
                    failed_code = run_product_command(
                        workspace=workspace,
                        source_factory=factory_spec,
                        initial_bankroll="100",
                        max_cycles=1,
                        poll_seconds=0,
                    )

            failed_records = _records(failed_output.getvalue())
            self.assertEqual(failed_code, 4)
            self.assertEqual(
                [record["kind"] for record in failed_records],
                ["product_status", "product_runtime_failure"],
            )
            self.assertEqual(
                failed_records[-1]["error_code"],
                "product_runtime_failed",
            )
            self.assertEqual(failed_records[-1]["error_type"], "RuntimeError")
            self.assertNotIn(_SENTINEL, failed_output.getvalue())
            self.assertNotIn("provider tick failed", failed_output.getvalue())
            self.assertNotEqual(failed_records[-1]["kind"], "product_start_failure")

            with patch.dict(sys.modules, {module_name: _module(_HealthySource)}):
                recovered_output = io.StringIO()
                with redirect_stdout(recovered_output):
                    recovered_code = run_product_command(
                        workspace=workspace,
                        source_factory=factory_spec,
                        initial_bankroll="100",
                        max_cycles=1,
                        poll_seconds=0,
                    )

            recovered_records = _records(recovered_output.getvalue())
            self.assertEqual(recovered_code, 0)
            self.assertEqual(
                [record["kind"] for record in recovered_records],
                ["product_status", "product_tick", "product_status"],
            )
            self.assertEqual(recovered_records[-1]["value"]["state"], "STOPPED")


if __name__ == "__main__":
    unittest.main()
