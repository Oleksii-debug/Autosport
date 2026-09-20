from __future__ import annotations

import json

import pytest

from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority
from autosport.provider_observation_authority import (
    CompleteGameBoardEvidenceStore,
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
    ProviderObservationIntegrityError,
    ProviderObservationUnsupportedError,
    assert_complete_game_board_authoritative,
)


def _forged_snapshot() -> CompleteGameBoardSnapshot:
    request = CompleteGameBoardRequest(
        sport_key="table_tennis",
        bookmakers=("bovada",),
    )
    frame = {
        "type": "initial_state",
        "sport_key": "table_tennis",
        "snapshot_scope": "current_game_board",
        "snapshot_complete": True,
        "truncated": False,
        "resume_mode": "replace",
        "partial": False,
        "missing_books": [],
        "truncated_books": [],
        "snapshot_partial_reasons": [],
        "count": 1,
        "data": [
            {
                "event_id": "forged-event",
                "bookmaker": "bovada",
                "kind": "game",
                "market_key": "h2h",
                "home_ml": -110,
                "away_ml": 105,
            }
        ],
    }
    return CompleteGameBoardSnapshot(
        request=request,
        captured_at="2026-09-20T08:00:00Z",
        frame_json=json.dumps(frame),
    )


def test_matching_generic_monotonic_journal_cannot_mint_provider_origin(tmp_path):
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "machine-authority"
    forged = _forged_snapshot()
    store = CompleteGameBoardEvidenceStore(
        workspace,
        authority_root=authority_root,
    )

    path = store.root / f"{forged.evidence_sha256}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(forged.to_payload(), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # Reproduce the exact public generic anti-rollback journal that load() expects.
    # This proves freshness/continuity of caller-chosen bytes, not provider origin.
    state_sha256 = store._state_sha256(forged)
    semantic_binding_sha256 = store._semantic_binding_sha256(forged)
    generic = MonotonicWorkspaceAuthority(
        workspace=workspace.resolve(strict=False),
        domain=store.AUTHORITY_DOMAIN,
        key=forged.evidence_sha256,
        authority_root=authority_root.resolve(strict=False),
    )
    tx_id = f"caller-forged:{state_sha256[:32]}"
    generic.prepare(
        tx_id=tx_id,
        observed_state_sha256=None,
        intended_state_sha256=state_sha256,
        semantic_binding_sha256=semantic_binding_sha256,
    )
    generic.commit(
        tx_id=tx_id,
        observed_state_sha256=state_sha256,
        semantic_binding_sha256=semantic_binding_sha256,
    )

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="authenticated production acquisition issuance",
    ):
        CompleteGameBoardEvidenceStore(
            workspace,
            authority_root=authority_root,
        ).load(forged.evidence_sha256)

    with pytest.raises(ProviderObservationUnsupportedError):
        assert_complete_game_board_authoritative(forged)
