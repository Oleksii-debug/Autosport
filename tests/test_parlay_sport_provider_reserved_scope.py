from __future__ import annotations

import unittest

from autosport.parlay_sport_provider import ParlayApiSportProvider


class ParlaySportReservedScopeTests(unittest.TestCase):
    def test_reserved_dataset_scope_identities_fail_at_construction(self) -> None:
        for sport_key in ("unknown", "mixed"):
            with self.subTest(sport_key=sport_key):
                with self.assertRaisesRegex(ValueError, "sport"):
                    ParlayApiSportProvider(
                        sport_key,
                        "test-api-key",
                        transport=lambda *_: None,
                    )


if __name__ == "__main__":
    unittest.main()
