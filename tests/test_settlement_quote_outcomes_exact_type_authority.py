from __future__ import annotations

import unittest

from autosport.continuous_session import (
    SettlementResolution,
    _ValidatedQuoteOutcomes,
)


_AT = "2026-09-26T21:10:00+00:00"


class SettlementQuoteOutcomesExactTypeAuthorityTests(unittest.TestCase):
    def test_validated_outcomes_subclass_is_rejected_before_virtual_dispatch(self) -> None:
        class HostileValidatedQuoteOutcomes(_ValidatedQuoteOutcomes):
            virtual_reads = 0

            def __len__(self) -> int:
                type(self).virtual_reads += 1
                raise AssertionError("hostile quote_outcomes __len__ executed")

            def items(self):
                type(self).virtual_reads += 1
                raise AssertionError("hostile quote_outcomes items executed")

            @property
            def validated_sha256(self) -> str:
                type(self).virtual_reads += 1
                raise AssertionError("hostile validated_sha256 executed")

        hostile = HostileValidatedQuoteOutcomes(
            {"quote-1": "win"},
            validated_sha256="a" * 64,
        )
        HostileValidatedQuoteOutcomes.virtual_reads = 0
        resolution = SettlementResolution(
            event_identity="provider-a:event-1",
            settlement_ref="provider-result:exact-type",
            quote_outcomes=hostile,
            evidence_id="receipt-exact-type",
            evidence_sha256="b" * 64,
            available_at=_AT,
        )

        with self.assertRaisesRegex(ValueError, "non-empty exact dict"):
            resolution.validate(as_of=_AT)

        self.assertEqual(HostileValidatedQuoteOutcomes.virtual_reads, 0)


if __name__ == "__main__":
    unittest.main()
