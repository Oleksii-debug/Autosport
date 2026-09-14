import unittest
from decimal import Decimal

from autosport.providers import CanonicalNormalizer, InMemoryProvider, ProviderBatch, ProviderQuote


class ProviderSourceIdentityIntegrityTests(unittest.TestCase):
    @staticmethod
    def _quote(**overrides: object) -> ProviderQuote:
        values: dict[str, object] = {
            "provider_event_id": "match-1",
            "provider_market_id": "winner",
            "provider_selection_id": "player-a",
            "decimal_odds": Decimal("1.80"),
            "observed_ts": "2026-09-13T18:00:00+00:00",
            "sequence": 1,
        }
        values.update(overrides)
        return ProviderQuote(**values)  # type: ignore[arg-type]

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

    def test_provider_identity_components_must_be_typed_nonempty_trimmed_strings(self):
        for field_name in ("provider_event_id", "provider_market_id", "provider_selection_id"):
            with self.subTest(field_name=field_name, value=7):
                with self.assertRaisesRegex(TypeError, field_name):
                    self._quote(**{field_name: 7})
            for value in ("", " value", "value ", "   "):
                with self.subTest(field_name=field_name, value=value):
                    with self.assertRaisesRegex(ValueError, field_name):
                        self._quote(**{field_name: value})

    def test_provider_identity_components_reject_quote_key_delimiter(self):
        for field_name in ("provider_event_id", "provider_market_id", "provider_selection_id"):
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(ValueError, "reserved identity delimiter"):
                    self._quote(**{field_name: "left|right"})

    def test_provider_sequence_identity_requires_non_boolean_integer(self):
        for value in (True, False, 1.0, "1", Decimal("1")):
            with self.subTest(value=value):
                with self.assertRaisesRegex(TypeError, "sequence"):
                    self._quote(sequence=value)
        self.assertEqual(self._quote(sequence=0).sequence, 0)
        self.assertEqual(self._quote(sequence=-1).sequence, -1)

    def test_deployed_parlay_source_and_colon_bearing_market_identity_are_byte_stable(self):
        quote = self._quote(
            provider_event_id="match-1",
            provider_market_id="book:h2h:-1.5",
            provider_selection_id="player-a",
        )
        event = CanonicalNormalizer().normalize("parlayapi:table_tennis", quote)
        self.assertEqual(event.source_id, "parlayapi:table_tennis")
        self.assertEqual(event.event_id, "parlayapi:table_tennis:match-1")
        self.assertEqual(event.market_id, "parlayapi:table_tennis:book:h2h:-1.5")
        self.assertEqual(event.selection_id, "parlayapi:table_tennis:player-a")
        self.assertEqual(
            event.quote_key,
            "parlayapi:table_tennis:match-1|parlayapi:table_tennis:book:h2h:-1.5|parlayapi:table_tennis:player-a",
        )

    def test_valid_safe_source_identity_is_preserved_exactly_in_canonical_ids(self):
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
