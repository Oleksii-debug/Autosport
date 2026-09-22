from __future__ import annotations

import importlib
import importlib.util
import unittest


class SecondSportNonProductionAuthorityFenceTests(unittest.TestCase):
    """The engineering fixture must never be an importable Autosport product API."""

    def test_test_only_second_sport_authority_issuer_is_not_shipped(self) -> None:
        self.assertIsNone(
            importlib.util.find_spec("autosport.second_sport_conformance"),
            "test-only second-sport authority issuer must remain outside src/autosport",
        )

    def test_production_import_cannot_reach_test_only_authority_issuer(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("autosport.second_sport_conformance")


if __name__ == "__main__":
    unittest.main()
