from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from autosport.product_entrypoint import _parser


class ProductEntrypointCliErrorLocalizationFalsifierTests(unittest.TestCase):
    def _parse_failure(self, argv: list[str]) -> str:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                _parser().parse_args(argv)
        self.assertEqual(raised.exception.code, 2)
        return stderr.getvalue()

    def _assert_ukrainian_argparse_failure(
        self,
        rendered: str,
        *,
        expected_option: str,
        forbidden_english: tuple[str, ...],
    ) -> None:
        self.assertIn("використання:", rendered)
        self.assertIn("помилка:", rendered)
        self.assertIn(expected_option, rendered)
        for fragment in forbidden_english:
            self.assertNotIn(fragment, rendered.lower())

    def test_missing_required_source_factory_failure_is_ukrainian(self) -> None:
        rendered = self._parse_failure([])

        self._assert_ukrainian_argparse_failure(
            rendered,
            expected_option="--source-factory",
            forbidden_english=(
                "usage:",
                "error:",
                "the following arguments are required",
            ),
        )

    def test_invalid_integer_failure_is_ukrainian(self) -> None:
        rendered = self._parse_failure(
            [
                "--source-factory",
                "provider.module:make_source",
                "--max-cycles",
                "не-число",
            ]
        )

        self._assert_ukrainian_argparse_failure(
            rendered,
            expected_option="--max-cycles",
            forbidden_english=("usage:", "error:", "invalid int value"),
        )

    def test_unrecognized_option_and_missing_value_failures_are_ukrainian(self) -> None:
        unknown = self._parse_failure(
            [
                "--source-factory",
                "provider.module:make_source",
                "--unknown-option",
            ]
        )
        self._assert_ukrainian_argparse_failure(
            unknown,
            expected_option="--unknown-option",
            forbidden_english=("usage:", "error:", "unrecognized arguments"),
        )

        missing_value = self._parse_failure(["--source-factory"])
        self._assert_ukrainian_argparse_failure(
            missing_value,
            expected_option="--source-factory",
            forbidden_english=("usage:", "error:", "expected one argument"),
        )

    def test_unknown_argparse_error_shape_fails_closed_to_ukrainian_generic_copy(self) -> None:
        stderr = io.StringIO()
        parser = _parser()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                parser.error("future English parser wording must not leak")

        self.assertEqual(raised.exception.code, 2)
        rendered = stderr.getvalue()
        self.assertIn("використання:", rendered)
        self.assertIn("помилка:", rendered)
        self.assertIn("Некоректні аргументи командного рядка.", rendered)
        self.assertNotIn("future English parser wording must not leak", rendered)

    def test_operator_metavars_are_resolved_through_localization_boundary(self) -> None:
        def fake_text(key: str, **_values: object) -> str:
            return f"<{key}>"

        with patch(
            "autosport.product_entrypoint.product_cli_text",
            side_effect=fake_text,
        ):
            parser = _parser()

        actions = {action.dest: action for action in parser._actions}
        expected_keys = {
            "workspace": "product.cli.workspace.metavar",
            "source_factory": "product.cli.source_factory.metavar",
            "bankroll": "product.cli.bankroll.metavar",
            "max_cycles": "product.cli.max_cycles.metavar",
            "poll_seconds": "product.cli.poll_seconds.metavar",
        }
        for dest, key in expected_keys.items():
            self.assertEqual(actions[dest].metavar, f"<{key}>")


if __name__ == "__main__":
    unittest.main()
