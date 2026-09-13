import unittest
from decimal import Decimal

from autosport.providers import CanonicalNormalizer, InMemoryProvider, ProviderBatch, ProviderQuote


class ProviderSourceIdentityIntegrityTests(unittest.TestCase):
    @staticmethod
    def _quote() -> ProviderQuote:
        return ProviderQuote(
            provider_event_id="match-1",
            provider_market_id="winner",
            provider_selection_id="player-a",
            decimal_odds=Decimal("1.80"),
            observed_ts="2026-09-13T18:00:00+00:00",
            sequence=1,
        )

    def test_reserved_delimiter_source_id_is_rejected_before_normalization(self):
        with self.assertRaisesRegex(ValueError, "reserved identity delimiter"):
            ProviderBatch("feed|eu", (self._quote(),))
        with self.assertRaisesRegex(ValueError, "reserved identity delimiter"):
            CanonicalNormalizer().normalize("feed|eu", self._quote())
        with self.assertRaisesRegex(ValueError, "reserved identity delimiter"):
            InMemoryProvider("feed|eu", [self._quote()])

    def test_source_id_must_be_trimmed_string(self):
        for source_id in ("", " feed", "feed ", "   "):
            with self.subTest(source_id=source_id):
                with self.assertRaises(ValueError):
                    ProviderBatch(source_id, ())
        with self.assertRaises(TypeError):
            ProviderBatch(7, ())  # type: ignore[arg-type]

    def test_valid_source_identity_is_preserved_exactly_in_canonical_ids(self):
        event = CanonicalNormalizer().normalize("feed_eu", self._quote())
        self.assertEqual(event.source_id, "feed_eu")
        self.assertEqual(event.event_id, "feed_eu:match-1")
        self.assertEqual(event.market_id, "feed_eu:winner")
        self.assertEqual(event.selection_id, "feed_eu:player-a")

    def test_distinct_valid_sources_produce_distinct_quote_keys(self):
        normalizer = CanonicalNormalizer()
        first = normalizer.normalize("feed_eu", self._quote())
        second = normalizer.normalize("feed-us", self._quote())
        self.assertNotEqual(first.quote_key, second.quote_key)
        self.assertNotEqual(first.dedupe_key, second.dedupe_key)

    def test_in_memory_provider_rejects_non_integer_or_boolean_batch_limits_without_advancing(self):
        provider = InMemoryProvider("fixture", [self._quote()])
        for value in (True, False, 1.5, 0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    provider.read_batch(value)  # type: ignore[arg-type]
        batch = provider.read_batch(1)
        self.assertEqual(len(batch.quotes), 1)
        self.assertEqual(batch.cursor, "1")


if __name__ == "__main__":
    unittest.main()
