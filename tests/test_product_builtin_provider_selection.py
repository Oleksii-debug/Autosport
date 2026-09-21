from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.event_lifecycle import CatalogPage
from autosport.product_entrypoint import ProductEntrypointError, _parser, run_product
from autosport.product_source import create_parlay_product_source_for_workspace


class _Source:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def fetch_catalog_page(self, checkpoint):
        position = 1 if checkpoint is None else int(checkpoint.position) + 1
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


class BuiltinProductProviderSelectionTests(unittest.TestCase):
    def test_workspace_bound_parlay_factory_does_not_require_duplicate_workspace_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "product"
            with patch.dict(
                "os.environ",
                {
                    "AUTOSPORT_PARLAY_API_KEY": "test-only-api-key",
                    "AUTOSPORT_PARLAY_LAWFUL_TERMS_REF": "terms:parlayapi:v1",
                    "AUTOSPORT_PARLAY_RETENTION_REF": "retention:parlayapi:v1",
                },
                clear=True,
            ):
                source = create_parlay_product_source_for_workspace(workspace)
            self.assertEqual(source.workspace, workspace.resolve())

    def test_builtin_provider_passes_selected_workspace_into_canonical_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "product"
            observed: list[Path] = []

            def make_source(value):
                observed.append(Path(value))
                return _Source(Path(value))

            with patch(
                "autosport.product_entrypoint.create_parlay_product_source_for_workspace",
                side_effect=make_source,
            ):
                code = run_product(
                    workspace=workspace,
                    provider="parlay-table-tennis",
                    initial_bankroll="100",
                    max_cycles=1,
                    poll_seconds=0,
                    sleep=lambda _: self.fail("bounded run must not sleep"),
                    install_signal_handlers=False,
                )
            self.assertEqual(code, 0)
            self.assertEqual(observed, [workspace])
            self.assertTrue((workspace / "product_composition.json").is_file())

    def test_source_selection_is_exactly_one_of_builtin_or_external(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ProductEntrypointError, "select exactly one"):
                run_product(
                    workspace=root / "none",
                    max_cycles=1,
                    poll_seconds=0,
                    install_signal_handlers=False,
                )
            self.assertFalse((root / "none").exists())

            with self.assertRaisesRegex(ProductEntrypointError, "select exactly one"):
                run_product(
                    workspace=root / "both",
                    provider="parlay-table-tennis",
                    source_factory="module:factory",
                    max_cycles=1,
                    poll_seconds=0,
                    install_signal_handlers=False,
                )
            self.assertFalse((root / "both").exists())

    def test_cli_exposes_stable_builtin_provider_alias(self) -> None:
        parser = _parser()
        parsed = parser.parse_args(
            [
                "--workspace",
                "operator-workspace",
                "--provider",
                "parlay-table-tennis",
                "--max-cycles",
                "1",
            ]
        )
        self.assertEqual(parsed.provider, "parlay-table-tennis")
        self.assertIsNone(parsed.source_factory)

        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--provider",
                    "parlay-table-tennis",
                    "--source-factory",
                    "module:factory",
                ]
            )


if __name__ == "__main__":
    unittest.main()
