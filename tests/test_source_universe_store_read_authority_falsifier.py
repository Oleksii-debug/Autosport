from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.source_universe_commitment import (
    SourceUniverseCommitmentError,
    build_source_universe_commitment,
)


class _ReboundCollectorDeltaStore(CollectorDeltaStore):
    def collector_cycle_evidence(
        self,
        *,
        source_id: str,
        start_cycle_seq: int,
        end_cycle_seq: int,
    ):
        assert source_id == "source-x"
        assert start_cycle_seq == 1
        assert end_cycle_seq == 1
        return (
            {
                "source_id": "source-x",
                "cycle_seq": 1,
                "run_id": "forged-run",
                "stream_epoch": "forged-epoch",
                "attempted_at": "2026-09-21T18:00:00+00:00",
                "terminal": {
                    "status": "SUCCESS",
                    "completed_at": "2026-09-21T18:00:01+00:00",
                    "observed_deltas": [],
                },
            },
        )


def test_rebound_store_read_cannot_mint_complete_provider_observation(
    tmp_path: Path,
) -> None:
    store = _ReboundCollectorDeltaStore(tmp_path / "collector.db")

    try:
        commitment = build_source_universe_commitment(
            store,
            source_id="source-x",
            start_cycle_seq=1,
            end_cycle_seq=1,
        )
    except (TypeError, SourceUniverseCommitmentError):
        return

    assert commitment.provider_observation_complete is False
