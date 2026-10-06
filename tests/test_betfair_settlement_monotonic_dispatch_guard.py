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
        str((tmp_path.parent / f"{tmp_path.name}-monotonic-authority").resolve()),
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


@pytest.mark.parametrize(
    "method_name",
    ["read_history", "prepare", "commit", "recover"],
)
def test_baseline_guard_rejects_monotonic_transition_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path.parent / f"{tmp_path.name}-authority-rebind").resolve()),
    )

    def forged_transition(self, *args, **kwargs):
        return ()

    monkeypatch.setattr(
        settlement.MonotonicWorkspaceAuthority,
        method_name,
        forged_transition,
    )

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="settlement monotonic authority dispatch changed",
    ):
        settlement.BetfairSettlementRevisionStore(
            tmp_path / "settlement.jsonl"
        )


@pytest.mark.parametrize(
    "method_name",
    ["read_history", "prepare", "commit", "recover"],
)
def test_baseline_guard_rejects_monotonic_transition_code_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path.parent / f"{tmp_path.name}-authority-code").resolve()),
    )
    method = vars(settlement.MonotonicWorkspaceAuthority)[method_name]
    original_code = method.__code__
    monkeypatch.setattr(
        method,
        "__code__",
        original_code.replace(co_name=f"drifted_{method_name}"),
    )

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="settlement monotonic authority dispatch changed",
    ):
        settlement.BetfairSettlementRevisionStore(
            tmp_path / "settlement.jsonl"
        )
