import unittest

from autosport.parlayapi_provider import ProviderPayloadError, _decode_provider_json


class ParlayApiJsonIngressTests(unittest.TestCase):
    def test_rejects_duplicate_object_keys_recursively(self):
        payloads = (
            b'{"events":[],"events":[{"id":"tt-2"}]}',
            b'{"events":[{"id":"tt-1","id":"tt-2"}]}',
            b'{"events":[{"bookmakers":[{"key":"a","key":"b"}]}]}',
        )
        for raw in payloads:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ProviderPayloadError, "duplicate JSON key"):
                    _decode_provider_json(raw)

    def test_rejects_non_standard_non_finite_constants(self):
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            raw = b'{"events":[{"price":' + token + b'}]}'
            with self.subTest(token=token):
                with self.assertRaisesRegex(ProviderPayloadError, "non-standard JSON constant"):
                    _decode_provider_json(raw)

    def test_accepts_strict_valid_json_without_normalizing_identity(self):
        raw = (
            '{"events":[{"id":"тт-001","bookmakers":[{"key":"book-a",'
            '"markets":[{"key":"h2h","outcomes":[{"name":"Player A","price":1.8}]}]}]}]}'
        ).encode("utf-8")
        decoded = _decode_provider_json(raw)
        self.assertEqual(decoded["events"][0]["id"], "тт-001")
        self.assertEqual(decoded["events"][0]["bookmakers"][0]["key"], "book-a")

    def test_rejects_invalid_utf8_as_provider_payload_error(self):
        with self.assertRaisesRegex(ProviderPayloadError, "invalid UTF-8 JSON"):
            _decode_provider_json(b'{"events":["\xff"]}')


if __name__ == "__main__":
    unittest.main()
