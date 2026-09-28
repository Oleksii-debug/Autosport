from __future__ import annotations

import unittest

from autosport.product_entrypoint import _parser


class ProductEntrypointValidCliContractTests(unittest.TestCase):
    def test_valid_arguments_preserve_supported_cli_contract(self) -> None:
        args = _parser().parse_args(
            [
                "--workspace",
                "workspace-dir",
                "--source-factory",
                "example:factory",
                "--bankroll",
                "250",
                "--max-cycles",
                "3",
                "--poll-seconds",
                "0.5",
            ]
        )

        self.assertEqual(str(args.workspace), "workspace-dir")
        self.assertEqual(args.source_factory, "example:factory")
        self.assertEqual(args.bankroll, "250")
        self.assertEqual(args.max_cycles, 3)
        self.assertEqual(args.poll_seconds, 0.5)


if __name__ == "__main__":
    unittest.main()
