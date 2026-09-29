from __future__ import annotations

import unittest

from autosport.continuous_session import (
    _ValidatedQuoteOutcomes,
    _settlement_quote_outcomes_sha256,
)


class SettlementQuoteOutcomesHelperExactTypeTests(unittest.TestCase):
    def test_digest_helper_rejects_validated_subclass_before_virtual_dispatch(self) -> None:
        class HostileValidatedQuoteOutcomes(_ValidatedQuoteOutcomes):
            virtual_reads = 0

            def __len__(self) -> int:
                type(self).virtual_reads += 1
                raise AssertionError("hostile quote_outcomes __len__ executed")

            def __iter__(self):
                type(self).virtual_reads += 1
                raise AssertionError("hostile quote_outcomes __iter__ executed")

            def __getitem__(self, key: str) -> str:
                type(self).virtual_reads += 1
                raise AssertionError("hostile quote_outcomes __getitem__ executed")

        # Forge only the container shape so the digest helper itself, rather than the
        # now-sealed compatibility constructor, remains the authority under test.
        hostile = dict.__new__(HostileValidatedQuoteOutcomes)
        dict.__init__(hostile, {"quote-1": "win"})
        HostileValidatedQuoteOutcomes.virtual_reads = 0

        with self.assertRaisesRegex(ValueError, "non-empty exact dict"):
            _settlement_quote_outcomes_sha256(hostile)

        self.assertEqual(HostileValidatedQuoteOutcomes.virtual_reads, 0)


if __name__ == "__main__":
    unittest.main()