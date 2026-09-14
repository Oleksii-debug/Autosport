import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import load_dataset


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _event() -> dict:
    return {
        "event_id": "tt-001",
        "market_id": "winner",
        "selection_id": "alice",
        "decimal_odds": "1.80",
        "observed_ts": "2026-01-01T10:00:01+00:00",
        "source_id": "fixture",
        "sequence": 1,
        "market_type": "winner",
        "source_ts": "2026-01-01T10:00:00+00:00",
        "ingest_ts": "2026-01-01T10:00:01+00:00",
        "metadata": {},
    }


def _write_schema1_dataset(
    root: Path,
    *,
    market_text: str | None = None,
    results_text: str | None = None,
    manifest_mutator=None,
) -> Path:
    if market_text is None:
        market_text = json.dumps(_event(), sort_keys=True) + "\n"
    if results_text is None:
        results_text = json.dumps(
            {
                "schema_version": 1,
                "quote_outcomes": {"tt-001|winner|alice": "win"},
            },
            sort_keys=True,
        ) + "\n"

    market_path = root / "market.jsonl"
    results_path = root / "results.json"
    market_path.write_text(market_text, encoding="utf-8")
    results_path.write_text(results_text, encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "name": "strict fixture",
        "sport": "table_tennis",
        "market_file": market_path.name,
        "results_file": results_path.name,
        "market_sha256": _sha256_bytes(market_path.read_bytes()),
        "results_sha256": _sha256_bytes(results_path.read_bytes()),
    }
    if manifest_mutator is not None:
        manifest_mutator(manifest)
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


class DatasetJsonInputIntegrityTests(unittest.TestCase):
    def test_valid_schema1_dataset_remains_loadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = load_dataset(_write_schema1_dataset(Path(tmp)))
            self.assertEqual(len(dataset.load_market_events()), 1)
            self.assertEqual(
                dataset.load_results_after_replay(),
                {"tt-001|winner|alice": "win"},
            )

    def test_manifest_rejects_duplicate_json_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_schema1_dataset(Path(tmp))
            manifest_path = root / "manifest.json"
            text = manifest_path.read_text(encoding="utf-8")
            text = text.replace(
                '"schema_version": 1',
                '"schema_version": 1, "schema_version": 1',
                1,
            )
            manifest_path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key: schema_version"):
                load_dataset(root)

    def test_manifest_rejects_nonstandard_json_constant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_schema1_dataset(Path(tmp))
            manifest_path = root / "manifest.json"
            text = manifest_path.read_text(encoding="utf-8")
            text = text.replace('"name": "strict fixture"', '"name": NaN', 1)
            manifest_path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-standard JSON constant: NaN"):
                load_dataset(root)

    def test_manifest_schema_requires_exact_integer_type(self):
        for value in (True, 1.0, "1"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                root = _write_schema1_dataset(
                    Path(tmp),
                    manifest_mutator=lambda manifest, value=value: manifest.__setitem__(
                        "schema_version", value
                    ),
                )
                with self.assertRaisesRegex(ValueError, "unsupported dataset schema"):
                    load_dataset(root)

    def test_manifest_identity_fields_are_not_string_coerced(self):
        for field, value in (("name", 123), ("sport", True), ("market_file", 7)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = _write_schema1_dataset(
                    Path(tmp),
                    manifest_mutator=lambda manifest, field=field, value=value: manifest.__setitem__(
                        field, value
                    ),
                )
                with self.assertRaises(ValueError):
                    load_dataset(root)

    def test_manifest_paths_and_digests_do_not_trim_surrounding_whitespace(self):
        for field in ("market_file", "results_file", "market_sha256", "results_sha256"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                def mutate(manifest, field=field):
                    manifest[field] = f" {manifest[field]} "

                root = _write_schema1_dataset(Path(tmp), manifest_mutator=mutate)
                with self.assertRaisesRegex(ValueError, "canonical"):
                    load_dataset(root)

    def test_market_jsonl_rejects_duplicate_key_before_deserialization(self):
        event_text = json.dumps(_event(), sort_keys=True)
        event_text = event_text.replace(
            '"sequence": 1',
            '"sequence": 1, "sequence": 1',
            1,
        ) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            dataset = load_dataset(
                _write_schema1_dataset(Path(tmp), market_text=event_text)
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key: sequence"):
                dataset.load_market_events()

    def test_market_jsonl_rejects_nonstandard_constant(self):
        event_text = json.dumps(_event(), sort_keys=True)
        event_text = event_text.replace('"decimal_odds": "1.80"', '"decimal_odds": Infinity', 1) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            dataset = load_dataset(
                _write_schema1_dataset(Path(tmp), market_text=event_text)
            )
            with self.assertRaisesRegex(ValueError, "non-standard JSON constant: Infinity"):
                dataset.load_market_events()

    def test_market_jsonl_rejects_standard_number_overflow(self):
        event_text = json.dumps(_event(), sort_keys=True)
        event_text = event_text.replace(
            '"metadata": {}',
            '"metadata": {"overflow": 1e400}',
            1,
        ) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            dataset = load_dataset(
                _write_schema1_dataset(Path(tmp), market_text=event_text)
            )
            with self.assertRaisesRegex(
                ValueError,
                "market line 1 contains invalid JSON value: non-finite JSON number",
            ):
                dataset.load_market_events()

    def test_market_jsonl_rejects_escaped_lone_surrogate(self):
        event_text = json.dumps(_event(), sort_keys=True)
        surrogate_escape = "\\" + "ud800"
        event_text = event_text.replace(
            '"metadata": {}',
            f'"metadata": {{"bad": "{surrogate_escape}"}}',
            1,
        ) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            dataset = load_dataset(
                _write_schema1_dataset(Path(tmp), market_text=event_text)
            )
            with self.assertRaisesRegex(
                ValueError,
                "market line 1 contains invalid JSON value: JSON string contains invalid Unicode scalar",
            ):
                dataset.load_market_events()

    def test_market_jsonl_rejects_non_json_whitespace_pseudo_blank_record(self):
        event_text = json.dumps(_event(), sort_keys=True) + "\n\v\n"
        with tempfile.TemporaryDirectory() as tmp:
            dataset = load_dataset(
                _write_schema1_dataset(Path(tmp), market_text=event_text)
            )
            with self.assertRaisesRegex(ValueError, "market line 2 is not valid JSON"):
                dataset.load_market_events()

    def test_market_jsonl_accepts_json_whitespace_blank_records(self):
        event_text = (
            " \t\r\n"
            + json.dumps(_event(), sort_keys=True)
            + "\n\t \r\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            dataset = load_dataset(
                _write_schema1_dataset(Path(tmp), market_text=event_text)
            )
            events = dataset.load_market_events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_id, "tt-001")

    def test_results_reject_duplicate_key_before_reveal(self):
        results_text = (
            '{"schema_version":1,"quote_outcomes":{"tt-001|winner|alice":"win"},'
            '"quote_outcomes":{"tt-001|winner|alice":"win"}}\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            dataset = load_dataset(
                _write_schema1_dataset(Path(tmp), results_text=results_text)
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key: quote_outcomes"):
                dataset.load_results_after_replay()

    def test_results_schema_requires_exact_integer_type(self):
        for value in (True, 1.0, "1"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                results_text = json.dumps(
                    {
                        "schema_version": value,
                        "quote_outcomes": {"tt-001|winner|alice": "win"},
                    }
                ) + "\n"
                dataset = load_dataset(
                    _write_schema1_dataset(Path(tmp), results_text=results_text)
                )
                with self.assertRaisesRegex(ValueError, "unsupported results schema"):
                    dataset.load_results_after_replay()

    def test_results_do_not_string_coerce_outcome_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            results_text = json.dumps(
                {
                    "schema_version": 1,
                    "quote_outcomes": {"tt-001|winner|alice": True},
                }
            ) + "\n"
            dataset = load_dataset(
                _write_schema1_dataset(Path(tmp), results_text=results_text)
            )
            with self.assertRaisesRegex(ValueError, "quote_outcomes values must be strings"):
                dataset.load_results_after_replay()


if __name__ == "__main__":
    unittest.main()
