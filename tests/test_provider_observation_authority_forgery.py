from __future__ import annotations

import json

import pytest

from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority
from autosport.provider_observation_authority import (
    CompleteGameBoardEvidenceStore,
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
    ProviderObservationUnsupportedError,
)


def _forged_snapshot() -> CompleteGameBoardSnapshot:
    request = CompleteGameBoardRequest(
        sport_key="table_tennis",
        bookmakers=("bovada",),
        max_age_s=600,
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


def test_generic_monotonic_authority_cannot_restore_provider_origin(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "machine-authority"
    store = CompleteGameBoardEvidenceStore(
        workspace,
        authority_root=authority_root,
    )
    forged = _forged_snapshot()

    forged_path = store.root / f"{forged.evidence_sha256}.json"
    forged_path.parent.mkdir(parents=True, exist_ok=True)
    forged_path.write_text(
        json.dumps(forged.to_payload(), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # Reproduce the exact generic continuity witness a caller can legitimately mint.
    # It can prove local state continuity, but it can never recreate remote origin.
    generic = MonotonicWorkspaceAuthority(
        workspace=workspace,
        domain=store.AUTHORITY_DOMAIN,
        key=forged.evidence_sha256,
        authority_root=authority_root,
    )
    intended = store._state_sha256(forged)
    binding = store._semantic_binding_sha256(forged)
    tx_id = f"caller-forgery:{intended[:32]}"
    generic.prepare(
        tx_id=tx_id,
        observed_state_sha256=None,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    generic.commit(
        tx_id=tx_id,
        observed_state_sha256=intended,
        semantic_binding_sha256=binding,
    )

    with pytest.raises(
        ProviderObservationUnsupportedError,
        match="restart cannot reissue provider-origin authority",
    ):
        CompleteGameBoardEvidenceStore(
            workspace,
            authority_root=authority_root,
        ).load(forged.evidence_sha256)
