from __future__ import annotations

import hashlib
import json
import os

import pytest

from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority
from autosport.provider_observation_authority import (
    CompleteGameBoardEvidenceStore,
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
    ProviderObservationIntegrityError,
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


def test_caller_selected_generic_root_cannot_supply_provider_receipt_trust(tmp_path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    caller_root = (tmp_path / "caller-machine-authority").resolve()
    store = CompleteGameBoardEvidenceStore(workspace, authority_root=caller_root)
    forged = _forged_snapshot()

    evidence_path = store.root / f"{forged.evidence_sha256}.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(forged.to_payload(), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # Seed the exact receipt/key layout that the old implementation trusted below
    # the caller-selected authority_root. If that root selected provider-origin
    # trust, these self-authored credentials would authenticate the forged bytes.
    workspace_sha256 = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()
    forged_receipt_root = (
        caller_root / "provider-acquisition-receipt-v1" / workspace_sha256
    )
    forged_receipt_root.mkdir(parents=True, exist_ok=True)
    key = b"caller-controlled-provider-key!!"[:32]
    assert len(key) == 32
    (forged_receipt_root / "receipt.key").write_text(key.hex(), encoding="ascii")
    unsigned = store._unsigned_receipt(forged)
    receipt = dict(unsigned)
    receipt["hmac_sha256"] = store._receipt_hmac(key, unsigned)
    receipts = forged_receipt_root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / f"{forged.evidence_sha256}.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # Also mint the exact public generic monotonic history. Both caller-controlled
    # trust artifacts are valid, but neither is allowed to establish provider origin.
    intended = store._state_sha256(forged)
    binding = store._semantic_binding_sha256(forged)
    generic = MonotonicWorkspaceAuthority(
        workspace=workspace,
        domain=store.AUTHORITY_DOMAIN,
        key=forged.evidence_sha256,
        authority_root=caller_root,
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

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="production-owned acquisition receipt",
    ):
        CompleteGameBoardEvidenceStore(
            workspace,
            authority_root=caller_root,
        ).load(forged.evidence_sha256)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not authoritative on Windows")
def test_provider_receipt_key_with_broad_permissions_fails_closed(tmp_path, monkeypatch) -> None:
    product_root = (tmp_path / "product-machine-authority").resolve()
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(product_root))
    store = CompleteGameBoardEvidenceStore((tmp_path / "workspace").resolve())

    store._read_receipt_key(create=True)
    key_path = store._key_path()
    key_path.chmod(0o644)

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="permissions are too broad",
    ):
        store._read_receipt_key(create=False)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not a portable Windows test primitive")
def test_provider_receipt_key_symlink_fails_closed(tmp_path, monkeypatch) -> None:
    product_root = (tmp_path / "product-machine-authority").resolve()
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(product_root))
    store = CompleteGameBoardEvidenceStore((tmp_path / "workspace").resolve())

    key_path = store._key_path()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "attacker-key"
    target.write_text((b"attacker-controlled-provider-key!"[:32]).hex(), encoding="ascii")
    target.chmod(0o600)
    key_path.symlink_to(target)

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="must be a regular file",
    ):
        store._read_receipt_key(create=False)
