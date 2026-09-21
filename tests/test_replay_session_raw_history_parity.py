from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import ReplayDataset
from autosport.domain import MarketEvent
from autosport.session import AutosportSession


class ReplaySessionRawHistoryParityTests(unittest.TestCase):
    @staticmethod
    def _event(
        *,
        sequence: int,
        observed_ts: str,
        ingest_ts: str,
    ) -> MarketEvent:
        return MarketEvent.from_dict(
            {
                "event_id": "same-event",
                "market_id": "winner",
                "selection_id": "selection-a",
                "decimal_odds": "2.0",
                "observed_ts": observed_ts,
                "ingest_ts": ingest_ts,
                "source_id": "source-a",
                "sequence": sequence,
            }
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _dataset(self, root: Path) -> ReplayDataset:
        # The newer provider sequence was live-visible first. The older sequence
        # arrived five minutes later and is therefore raw forensic history only.
        late_stale = self._event(
            sequence=1,
            observed_ts="2026-01-01T00:00:00+00:00",
            ingest_ts="2026-01-01T00:10:00+00:00",
        )
        newer = self._event(
            sequence=2,
            observed_ts="2026-01-01T00:05:00+00:00",
            ingest_ts="2026-01-01T00:05:00+00:00",
        )

        market_path = root / "market.jsonl"
        with market_path.open("w", encoding="utf-8", newline="\n") as handle:
            for event in (late_stale, newer):
                handle.write(
                    json.dumps(
                        event.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )

        results_path = root / "results.json"
        results_path.write_text(
            json.dumps(
                {"schema_version": 1, "quote_outcomes": {}},
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

        return ReplayDataset(
            root=root,
            name="raw-history-parity",
            sport="unknown",
            market_path=market_path,
            results_path=results_path,
            market_sha256=self._sha256(market_path),
            results_sha256=self._sha256(results_path),
        )

    def test_session_keeps_late_stale_arrival_in_durable_history_without_regressing_current(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dataset_root = root / "dataset"
            dataset_root.mkdir()
            dataset = self._dataset(dataset_root)

            session = AutosportSession(
                root / "workspace",
                "10000",
                strategy_id="observe-only-v1",
            )
            try:
                result = session.run_dataset(dataset)

                # Replay audit truth counts both causal arrivals even though only
                # sequence 2 is live-current/strategy-visible.
                self.assertEqual(result.replay.event_count, 2)

                # Canonical live ingestion is durable-first: the late lower
                # sequence belongs in append-only market history for forensic and
                # restart parity even though it must not replace current state.
                history = session.store.events("same-event")
                self.assertEqual([event.sequence for event in history], [1, 2])
                self.assertEqual(
                    {event.ingest_ts for event in history},
                    {
                        "2026-01-01T00:05:00+00:00",
                        "2026-01-01T00:10:00+00:00",
                    },
                )

                current = session.store.current_by_source()
                self.assertEqual(len(current), 1)
                self.assertEqual(next(iter(current.values())).sequence, 2)
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
