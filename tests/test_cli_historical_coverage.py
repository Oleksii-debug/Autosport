import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from autosport.cli import run_historical_coverage
from autosport.parlayapi_provider import HistoricalCoverageReport, HistoricalCoverageSource


class _FakeProvider:
    report: HistoricalCoverageReport | None = None
    seen_key: str | None = None

    def __init__(self, api_key: str) -> None:
        type(self).seen_key = api_key

    def historical_coverage(self, date_from: str, date_to: str) -> HistoricalCoverageReport:
        if self.report is None:
            raise AssertionError("fake report not configured")
        if self.report.date_from != date_from or self.report.date_to != date_to:
            raise AssertionError("unexpected date window")
        return self.report


class HistoricalCoverageCliTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeProvider.seen_key = None

    def test_data_available_writes_machine_evidence_without_secret(self):
        _FakeProvider.report = HistoricalCoverageReport(
            sport_key="table_tennis",
            date_from="2026-09-01",
            date_to="2026-09-12",
            historical_window_hours=720,
            historical_window_from="2026-08-14T00:00:00Z",
            observed_at="2026-09-13T01:00:00+00:00",
            response_sha256="a" * 64,
            sources=(
                HistoricalCoverageSource(
                    source="bovada",
                    rows=100,
                    first_date="2026-09-01",
                    last_date="2026-09-12",
                    priced_rows=94,
                ),
            ),
            api_version="3.2.0",
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"AUTOSPORT_PARLAYAPI_KEY": "never-write-this-key"},
            clear=False,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                code = run_historical_coverage(
                    Path(tmp),
                    date_from="2026-09-01",
                    date_to="2026-09-12",
                    output=None,
                    provider_factory=_FakeProvider,
                )
            self.assertEqual(code, 0)
            self.assertEqual(_FakeProvider.seen_key, "never-write-this-key")
            evidence_path = Path(tmp) / "historical-coverage.json"
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["requested_window_access_verified"])
            self.assertTrue(payload["has_data"])
            self.assertFalse(payload["licensing_or_retention_verified"])
            self.assertFalse(payload["real_money_execution"])
            self.assertEqual(payload["total_priced_rows"], 94)
            self.assertNotIn("never-write-this-key", evidence_path.read_text(encoding="utf-8"))
            self.assertIn("historical_coverage=DATA_AVAILABLE", output.getvalue())

    def test_no_data_is_persisted_but_returns_nonzero(self):
        _FakeProvider.report = HistoricalCoverageReport(
            sport_key="table_tennis",
            date_from="2026-09-01",
            date_to="2026-09-12",
            historical_window_hours=720,
            historical_window_from="2026-08-14T00:00:00Z",
            observed_at="2026-09-13T01:00:00+00:00",
            response_sha256="b" * 64,
            sources=(),
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"AUTOSPORT_PARLAYAPI_KEY": "key"},
            clear=False,
        ):
            code = run_historical_coverage(
                Path(tmp),
                date_from="2026-09-01",
                date_to="2026-09-12",
                output=None,
                provider_factory=_FakeProvider,
            )
            self.assertEqual(code, 6)
            payload = json.loads((Path(tmp) / "historical-coverage.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["has_data"])
            self.assertEqual(payload["sources"], [])

    def test_missing_key_blocks_before_provider_construction(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            code = run_historical_coverage(
                Path(tmp),
                date_from="2026-09-01",
                date_to="2026-09-12",
                output=None,
                provider_factory=_FakeProvider,
            )
            self.assertEqual(code, 2)
            self.assertIsNone(_FakeProvider.seen_key)
            self.assertFalse((Path(tmp) / "historical-coverage.json").exists())


if __name__ == "__main__":
    unittest.main()
