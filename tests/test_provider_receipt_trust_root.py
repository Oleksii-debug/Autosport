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
    return CompleteGameBoardSnapshot(
        request=CompleteGameBoardRequest(
            sport_key="table_tennis",
            bookmakers=("bovada",),
        ),
        captured_at="2026-09-20T08:00:00Z",
        frame_json=json.dumps(
            {
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
        ),
    )


def _publish_forged_evidence_and_generic_history(
    store: CompleteGameBoardEvidenceStore,
    snapshot: CompleteGameBoardSnapshot,
    *,
    authority_root=None,
) -> None:
    evidence_path = store.root / f"{snapshot.evidence_sha256}.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(snapshot.to_payload(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    intended = store._state_sha256(snapshot)
    binding = store._semantic_binding_sha256(snapshot)
    generic = MonotonicWorkspaceAuthority(
        workspace=store.workspace,
        domain=store.AUTHORITY_DOMAIN,
        key=snapshot.evidence_sha256,
        authority_root=authority_root,
    )
    tx_id = f"caller-controlled:{intended[:32]}"
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


def test_caller_selected_generic_root_cannot_recreate_provider_origin(tmp_path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    caller_root = (tmp_path / "caller-machine-authority").resolve()
    store = CompleteGameBoardEvidenceStore(workspace, authority_root=caller_root)
    forged = _forged_snapshot()
    _publish_forged_evidence_and_generic_history(
        store,
        forged,
        authority_root=caller_root,
    )

    # Even a complete, matching local continuity history is not remote provenance.
    with pytest.raises(
        ProviderObservationUnsupportedError,
        match="restart cannot reissue provider-origin authority",
    ):
        CompleteGameBoardEvidenceStore(
            workspace,
            authority_root=caller_root,
        ).load(forged.evidence_sha256)


def test_monotonic_root_env_override_cannot_recreate_provider_origin(
    tmp_path,
    monkeypatch,
) -> None:
    attacker_root = (tmp_path / "attacker-machine-authority").resolve()
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(attacker_root))
    workspace = (tmp_path / "workspace").resolve()
    store = CompleteGameBoardEvidenceStore(workspace)
    forged = _forged_snapshot()

    # The documented override may select the generic rollback journal. It still
    # cannot turn locally authored durable state into provider-origin authority.
    _publish_forged_evidence_and_generic_history(store, forged, authority_root=None)

    with pytest.raises(
        ProviderObservationUnsupportedError,
        match="restart cannot reissue provider-origin authority",
    ):
        CompleteGameBoardEvidenceStore(workspace).load(forged.evidence_sha256)


def test_local_receipt_signing_surface_is_non_authorizing(tmp_path) -> None:
    store = CompleteGameBoardEvidenceStore((tmp_path / "workspace").resolve())
    forged = _forged_snapshot()

    denied = (
        lambda: store._receipt_root(),
        lambda: store._receipt_path(forged.evidence_sha256),
        lambda: store._key_path(),
        lambda: store._read_receipt_key(create=True),
        lambda: store._unsigned_receipt(forged),
        lambda: store._receipt_hmac(b"x" * 32, {"forged": True}),
        lambda: store._write_receipt(forged),
        lambda: store._verify_receipt(forged),
    )
    for operation in denied:
        with pytest.raises(
            ProviderObservationUnsupportedError,
            match="local receipt signing is not provider-origin authority",
        ):
            operation()
