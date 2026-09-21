from __future__ import annotations

import unittest
from pathlib import Path

from autosport.localization import CATALOG_VERSION, catalog as canonical_catalog, text
from autosport.localization_product_cli import catalog, product_cli_text
from autosport.product_entrypoint import _parser


class ProductEntrypointCliLocalizationTests(unittest.TestCase):
    def test_help_is_ukrainian_and_covers_supported_operator_options(self) -> None:
        rendered = _parser().format_help()

        self.assertTrue(rendered.startswith("використання: autosport-product"))
        self.assertIn("Параметри:", rendered)
        self.assertIn("канонічний довговічний PAPER-продукт Autosport", rendered)
        self.assertIn("Показати цю довідку та завершити роботу.", rendered)
        self.assertIn("--workspace ШЛЯХ", rendered)
        self.assertIn("--source-factory МОДУЛЬ:ФУНКЦІЯ", rendered)
        self.assertIn("--bankroll СУМА", rendered)
        self.assertIn("--max-cycles N", rendered)
        self.assertIn("--poll-seconds СЕКУНДИ", rendered)
        self.assertIn("реального виконання", rendered)

        self.assertNotIn("Run the canonical durable Autosport PAPER product", rendered)
        self.assertNotIn("show this help message and exit", rendered)
        self.assertNotIn("external product source factory", rendered)
        self.assertNotIn("optional bounded cycle count", rendered)

    def test_localization_does_not_change_parser_semantics(self) -> None:
        args = _parser().parse_args(
            [
                "--workspace",
                "operator-workspace",
                "--source-factory",
                "provider.module:make_source",
                "--bankroll",
                "125.50",
                "--max-cycles",
                "7",
                "--poll-seconds",
                "2.5",
            ]
        )

        self.assertEqual(args.workspace, Path("operator-workspace"))
        self.assertEqual(args.source_factory, "provider.module:make_source")
        self.assertEqual(args.bankroll, "125.50")
        self.assertEqual(args.max_cycles, 7)
        self.assertEqual(args.poll_seconds, 2.5)

    def test_product_cli_resources_are_in_the_canonical_catalog(self) -> None:
        self.assertEqual(CATALOG_VERSION, 8)
        resources = catalog()
        self.assertIs(resources, canonical_catalog())
        self.assertEqual(
            product_cli_text("product.cli.help"),
            text("product.cli.help"),
        )
        self.assertEqual(
            resources["product.cli.usage_prefix"],
            "використання: ",
        )

    def test_product_cli_catalog_is_immutable_and_has_no_fallback(self) -> None:
        resources = catalog()
        with self.assertRaises(TypeError):
            resources["product.cli.help"] = "forged"  # type: ignore[index]
        with self.assertRaisesRegex(KeyError, "missing product CLI localization key"):
            product_cli_text("product.cli.missing")
        with self.assertRaisesRegex(ValueError, "unsupported product CLI locale"):
            product_cli_text("product.cli.help", locale="en-US")


if __name__ == "__main__":
    unittest.main()
