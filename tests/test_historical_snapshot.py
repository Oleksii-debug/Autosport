from __future__ import annotations

from contextlib import contextmanager
import json
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import autosport.historical_snapshot as historical_snapshot
from autosport.historical_snapshot import _atomic_write_jsonl, capture_historical_snapshot
from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
)


class _Transport:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> HttpJsonResponse:
        self.urls.append(url)
        self.headers.append(dict(headers))
        return HttpJsonResponse(self.payload, 200, {"x-api-version": "test"})


def _payload(
    *,
    timestamp: str = "2026-09-12T10:00:00Z",
    last_update: str | None = None,
    event_id: str = "tt-1",
) -> dict[str, object]:
    market: dict[str, object] = {
        "key": "h2h",
        "outcomes": [
            {"name": "Player A", "price": 1.8},
            {"name": "Player B", "price": 2.1},
        ],
    }
    if last_update is not None:
        market["last_update"] = last_update
    return {
        "timestamp": timestamp,
        "previous_timestamp": "2026-09-12T09:55:00Z",
        "next_timestamp": "2026-09-12T10:05:00Z",
        "data": [
            {
                "id": event_id,
                "sport_key": "table_tennis",
                "commence_time": "2026-09-12T11:00:00Z",
                "home_team": "Player A",
                "away_team": "Player B",
                "bookmakers": [
                    {
                        "key": "bovada",
                        "title": "Bovada",
                        "markets": [market],
                    }
                ],
            }
        ],
    }


class HistoricalSnapshotTests(unittest.TestCase):
    def _provider(self, transport: _Transport) -> ParlayApiTableTennisProvider:
        return ParlayApiTableTennisProvider(
            "secret-key-must-not-leak",
            transport=transport,
            clock=lambda: "2026-09-13T02:00:00+00:00",
            sleeper=lambda _: None,
        )

    def test_capture_uses_provider_snapshot_time_when_quote_last_update_is_missing(self) -> None:
        transport = _Transport(_payload())
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            market_path = Path(temp) / "market.jsonl"
            evidence_path = Path(temp) / "evidence.json"
            report = capture_historical_snapshot(
                provider,
                requested_at="2026-09-12T10:03:00Z",
                output_path=market_path,
                evidence_path=evidence_path,
            )
            rows = [json.loads(line) for line in market_path.read_text(encoding="utf-8").splitlines()]
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertEqual(report.quote_count, 2)
        self.assertEqual(report.snapshot_timestamp_fallback_count, 2)
        self.assertEqual(report.bookmaker_keys, ("bovada",))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["source_ts"], "2026-09-12T10:00:00Z")
            self.assertEqual(row["observed_ts"], "2026-09-12T10:00:00Z")
            self.assertEqual(row["ingest_ts"], "2026-09-13T02:00:00+00:00")
            self.assertEqual(row["metadata"]["source_time_semantics"], "provider_historical_snapshot_timestamp")
            self.assertFalse(row["metadata"]["provider_quote_last_update_present"])
        self.assertTrue(evidence["point_in_time_snapshot_contains_odds"])
        self.assertFalse(evidence["point_in_time_odds_market_coverage_verified"])
        self.assertFalse(evidence["historical_window_market_coverage_verified"])
        self.assertFalse(evidence["sealed_outcomes_present"])
        self.assertFalse(evidence["replay_corpus_ready"])
        self.assertFalse(evidence["licensing_or_retention_verified"])
        self.assertFalse(evidence["real_money_execution"])
        serialized = json.dumps(evidence) + json.dumps(rows)
        self.assertNotIn("secret-key-must-not-leak", serialized)
        self.assertEqual(transport.headers[0]["X-API-Key"], "secret-key-must-not-leak")
        query = parse_qs(urlparse(transport.urls[0]).query)
        self.assertEqual(query["date"], ["2026-09-12T10:03:00Z"])
        self.assertEqual(query["markets"], ["h2h,spreads,totals"])
        self.assertEqual(query["oddsFormat"], ["decimal"])

    def test_real_quote_last_update_is_preserved(self) -> None:
        transport = _Transport(_payload(last_update="2026-09-12T09:59:30Z"))
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            market_path = Path(temp) / "market.jsonl"
            report = capture_historical_snapshot(
                provider,
                requested_at="2026-09-12T10:03:00Z",
                output_path=market_path,
            )
            rows = [json.loads(line) for line in market_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(report.snapshot_timestamp_fallback_count, 0)
        self.assertEqual({row["source_ts"] for row in rows}, {"2026-09-12T09:59:30Z"})
        self.assertEqual({row["metadata"]["source_time_semantics"] for row in rows}, {"provider_quote_last_update"})

    def test_future_quote_update_fails_closed(self) -> None:
        transport = _Transport(_payload(last_update="2026-09-12T10:00:01Z"))
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            market_path = Path(temp) / "market.jsonl"
            with self.assertRaisesRegex(ProviderPayloadError, "after historical snapshot"):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=market_path,
                )
            self.assertFalse(market_path.exists())

    def test_snapshot_after_requested_time_fails_closed(self) -> None:
        transport = _Transport(_payload(timestamp="2026-09-12T10:04:00Z"))
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ProviderPayloadError, "after requested_at"):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=Path(temp) / "market.jsonl",
                )

    def test_missing_response_timestamp_fails_closed(self) -> None:
        payload = _payload()
        payload.pop("timestamp")
        transport = _Transport(payload)
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ProviderPayloadError, "requires non-empty timestamp"):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=Path(temp) / "market.jsonl",
                )

    def test_capture_rejects_output_evidence_alias_before_provider_io(self) -> None:
        transport = _Transport(_payload())
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            shared_path = Path(temp) / "shared.json"
            with self.assertRaisesRegex(ValueError, "output and evidence paths must be distinct"):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=shared_path,
                    evidence_path=shared_path,
                )

        self.assertEqual(transport.urls, [])

    def test_capture_serializes_market_and_evidence_pair_publication(self) -> None:
        provider_a = self._provider(_Transport(_payload(event_id="tt-a")))
        provider_b = self._provider(_Transport(_payload(event_id="tt-b")))
        a_market_published = threading.Event()
        release_a = threading.Event()
        b_lock_attempted = threading.Event()
        b_writer_entered = threading.Event()
        errors: list[BaseException] = []
        errors_lock = threading.Lock()
        reports: dict[str, object] = {}
        original_writer = historical_snapshot._atomic_write_jsonl
        original_lock = historical_snapshot.durable_path_lock

        def controlled_writer(path: Path, rows) -> None:
            original_writer(path, rows)
            if threading.current_thread().name == "capture-a":
                a_market_published.set()
                if not release_a.wait(timeout=5):
                    raise AssertionError("timed out waiting to release capture-a publication")
            elif threading.current_thread().name == "capture-b":
                b_writer_entered.set()

        @contextmanager
        def observed_lock(path: Path):
            if threading.current_thread().name == "capture-b":
                b_lock_attempted.set()
            with original_lock(path):
                yield

        with tempfile.TemporaryDirectory() as temp:
            market_path = Path(temp) / "market.jsonl"
            evidence_path = Path(temp) / "evidence.json"

            def capture(label: str, provider: ParlayApiTableTennisProvider) -> None:
                try:
                    reports[label] = capture_historical_snapshot(
                        provider,
                        requested_at="2026-09-12T10:03:00Z",
                        output_path=market_path,
                        evidence_path=evidence_path,
                    )
                except BaseException as exc:
                    with errors_lock:
                        errors.append(exc)

            with mock.patch.object(
                historical_snapshot,
                "_atomic_write_jsonl",
                side_effect=controlled_writer,
            ), mock.patch.object(
                historical_snapshot,
                "durable_path_lock",
                side_effect=observed_lock,
            ):
                thread_a = threading.Thread(
                    target=capture,
                    args=("a", provider_a),
                    name="capture-a",
                    daemon=True,
                )
                thread_b = threading.Thread(
                    target=capture,
                    args=("b", provider_b),
                    name="capture-b",
                    daemon=True,
                )
                thread_a.start()
                self.assertTrue(a_market_published.wait(timeout=5))
                thread_b.start()
                self.assertTrue(b_lock_attempted.wait(timeout=5))
                self.assertFalse(b_writer_entered.is_set())
                release_a.set()
                thread_a.join(timeout=5)
                thread_b.join(timeout=5)

            self.assertFalse(thread_a.is_alive())
            self.assertFalse(thread_b.is_alive())
            self.assertEqual(errors, [])
            report_a = reports["a"]
            report_b = reports["b"]
            self.assertNotEqual(report_a.response_sha256, report_b.response_sha256)
            final_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(
                historical_snapshot._sha256(market_path),
                report_b.market_sha256,
            )
            self.assertEqual(final_evidence["market_sha256"], report_b.market_sha256)
            self.assertEqual(final_evidence["response_sha256"], report_b.response_sha256)

    def test_atomic_writer_uses_isolated_temp_files_for_concurrent_publication(self) -> None:
        rows_a = [
            {"writer": "a", "index": 1},
            {"writer": "a", "index": 2},
        ]
        rows_b = [
            {"writer": "b", "index": 1},
            {"writer": "b", "index": 2},
        ]
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        errors_lock = threading.Lock()

        def synchronized_rows(rows: list[dict[str, object]]):
            barrier.wait(timeout=5)
            yield from rows

        def publish(path: Path, rows: list[dict[str, object]]) -> None:
            try:
                _atomic_write_jsonl(path, synchronized_rows(rows))
            except BaseException as exc:
                with errors_lock:
                    errors.append(exc)

        with tempfile.TemporaryDirectory() as temp:
            market_path = Path(temp) / "market.jsonl"
            threads = [
                threading.Thread(target=publish, args=(market_path, rows_a), daemon=True),
                threading.Thread(target=publish, args=(market_path, rows_b), daemon=True),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            published = [
                json.loads(line)
                for line in market_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn(published, (rows_a, rows_b))
            self.assertEqual(
                list(Path(temp).glob(f".{market_path.name}.*.tmp")),
                [],
            )
            self.assertFalse(market_path.with_name(market_path.name + ".tmp").exists())

    def test_empty_snapshot_is_machine_visible_but_not_coverage_verified(self) -> None:
        payload = _payload()
        payload["data"] = []
        transport = _Transport(payload)
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            evidence_path = Path(temp) / "evidence.json"
            report = capture_historical_snapshot(
                provider,
                requested_at="2026-09-12T10:03:00Z",
                output_path=Path(temp) / "market.jsonl",
                evidence_path=evidence_path,
            )
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertFalse(report.has_data)
        self.assertFalse(evidence["has_data"])
        self.assertFalse(evidence["point_in_time_snapshot_contains_odds"])
        self.assertFalse(evidence["point_in_time_odds_market_coverage_verified"])
        self.assertFalse(evidence["historical_window_market_coverage_verified"])
        self.assertFalse(evidence["replay_corpus_ready"])


if __name__ == "__main__":
    unittest.main()
