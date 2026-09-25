from __future__ import annotations

from pathlib import Path

import pytest

import autosport.betfair_settlement_revisions as settlement


@pytest.mark.parametrize(
    "method_name",
    ["_monotonic_state_digest", "_monotonic_authority"],
)
def test_baseline_guard_rejects_monotonic_authority_dispatch_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path / "monotonic-authority").resolve()),
    )

    def forged_dispatch(self):
        return None

    monkeypatch.setattr(
        settlement.BetfairSettlementRevisionStore,
        method_name,
        forged_dispatch,
    )

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="settlement monotonic authority dispatch changed",
    ):
        settlement.BetfairSettlementRevisionStore(
            tmp_path / "settlement.jsonl"
        )
