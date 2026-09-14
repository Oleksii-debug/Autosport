import json
import tempfile
import unittest
from pathlib import Path

from autosport.replay import ReplayEngine


class ReplayJsonlIntegrityTests(unittest.TestCase):
    @staticmethod
    def _event_payload() -> dict:
        return {
            "event_id": "event-1",
            "market_id": "winner",
            "selection_id": "player-a",
            "decimal_odds": "2.10",
            "observed_ts": "2026-01-01T00:00:00+00:00",
            "source_id": "fixture",
            "sequence": 1,
            "metadata": {"nested": {"provider_sequence": 7}},
        }

    def test_valid_jsonl_and_blank_lines_remain_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.jsonl"
            path.write_text(
                "\n"
                + json.dumps(self._event_payload(), ensure_ascii=False)
                + "\n\n",
                encoding="utf-8",
            )

            engine = ReplayEngine.from_jsonl(path)

            self.assertEqual(len(engine.events), 1)
            self.assertEqual(engine.events[0].event_id, "event-1")
            self.assertEqual(engine.events[0].metadata["nested"]["provider_sequence"], 7)

    def test_duplicate_top_level_key_is_rejected_before_market_event_coercion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.jsonl"
            payload = json.dumps(self._event_payload())
            payload = payload.replace(
                '"event_id": "event-1"',
                '"event_id": "event-1", "event_id": "forged-event"',
            )
            path.write_text(payload + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"invalid replay JSONL at line 1"):
                ReplayEngine.from_jsonl(path)

    def test_duplicate_nested_metadata_key_is_rejected_recursively(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.jsonl"
            payload = json.dumps(self._event_payload())
            payload = payload.replace(
                '"provider_sequence": 7',
                '"provider_sequence": 7, "provider_sequence": 8',
            )
            path.write_text(payload + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"invalid replay JSONL at line 1"):
                ReplayEngine.from_jsonl(path)

    def test_nonstandard_json_constant_is_rejected_at_replay_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.jsonl"
            payload = json.dumps(self._event_payload())
            payload = payload.replace(
                '"provider_sequence": 7',
                '"provider_sequence": NaN',
            )
            path.write_text(payload + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"invalid replay JSONL at line 1"):
                ReplayEngine.from_jsonl(path)

    def test_overflowing_json_number_is_rejected_after_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.jsonl"
            payload = json.dumps(self._event_payload())
            payload = payload.replace(
                '"provider_sequence": 7',
                '"provider_sequence": 1e400',
            )
            path.write_text(payload + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"invalid replay JSONL at line 1"):
                ReplayEngine.from_jsonl(path)

    def test_escaped_lone_surrogate_is_rejected_before_dataset_hashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.jsonl"
            payload = json.dumps(self._event_payload())
            payload = payload.replace(
                '"provider_sequence": 7',
                r'"provider_sequence": "\ud800"'.replace(r'\"', '"'),
            )
            path.write_text(payload + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"invalid replay JSONL at line 1"):
                ReplayEngine.from_jsonl(path)

    def test_malformed_later_record_reports_physical_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.jsonl"
            path.write_text(
                json.dumps(self._event_payload()) + "\n\n" + '{"event_id":\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, r"invalid replay JSONL at line 3"):
                ReplayEngine.from_jsonl(path)

    def test_non_object_json_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.jsonl"
            path.write_text("[1, 2, 3]\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                r"invalid replay JSONL at line 1: event must be a JSON object",
            ):
                ReplayEngine.from_jsonl(path)

    def test_invalid_utf8_reports_exact_physical_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.jsonl"
            first = (json.dumps(self._event_payload()) + "\n").encode("utf-8")
            path.write_bytes(first + b'{"event_id":"\xff"}\n')

            with self.assertRaisesRegex(
                ValueError,
                r"invalid replay JSONL UTF-8 at line 2",
            ):
                ReplayEngine.from_jsonl(path)

    def test_unreadable_path_is_normalized_to_replay_input_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.jsonl"

            with self.assertRaisesRegex(ValueError, r"unable to read replay JSONL"):
                ReplayEngine.from_jsonl(missing)


if __name__ == "__main__":
    unittest.main()
