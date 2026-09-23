from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

from autosport.product_entrypoint import _parser


class ProductEntrypointSecretSafeCliTests(unittest.TestCase):
    def test_rejected_unknown_argument_does_not_echo_secret_value(self) -> None:
        sentinel = "S3CR3T-CANARY-DO-NOT-ECHO"
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            _parser().parse_args(
                [
                    "--source-factory",
                    "example:factory",
                    f"--api-key={sentinel}",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        output = stderr.getvalue()
        self.assertIn("usage: autosport-product", output)
        self.assertIn("invalid command-line arguments", output)
        self.assertNotIn(sentinel, output)
        self.assertNotIn("--api-key=", output)

    def test_invalid_typed_value_does_not_echo_rejected_value(self) -> None:
        sentinel = "S3CR3T-CANARY-AS-NUMBER"
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            _parser().parse_args(
                [
                    "--source-factory",
                    "example:factory",
                    "--max-cycles",
                    sentinel,
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        output = stderr.getvalue()
        self.assertIn("usage: autosport-product", output)
        self.assertIn("use --help", output)
        self.assertNotIn(sentinel, output)


if __name__ == "__main__":
    unittest.main()
