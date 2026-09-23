from __future__ import annotations

import unittest
from unittest.mock import patch

from autosport.data_tools_entry import main


class DataToolsStrategyComparisonEntryTests(unittest.TestCase):
    def test_console_entry_routes_compare_strategies_to_canonical_engine(self) -> None:
        argv = [
            "compare-strategies",
            "baseline-run.json",
            "research-run.json",
            "--baseline-strategy",
            "baseline-v1",
            "--output",
            "comparison.json",
        ]
        with patch("autosport.strategy_comparison.main", return_value=0) as comparison_main:
            rc = main(argv)

        self.assertEqual(rc, 0)
        comparison_main.assert_called_once_with(argv[1:])

    def test_compare_strategies_propagates_fail_closed_return_code(self) -> None:
        argv = ["compare-strategies", "a.json", "b.json"]
        with patch("autosport.strategy_comparison.main", return_value=2) as comparison_main:
            rc = main(argv)

        self.assertEqual(rc, 2)
        comparison_main.assert_called_once_with(argv[1:])


if __name__ == "__main__":
    unittest.main()
