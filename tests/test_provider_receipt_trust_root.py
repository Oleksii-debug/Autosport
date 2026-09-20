from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority
from autosport.provider_observation_authority import (
    CompleteGameBoardEvidenceStore,
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
    ProviderObservationIntegrityError,
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


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _old_unsigned_receipt(
    store: CompleteGameBoardEvidenceStore,
    snapshot: CompleteGameBoardSnapshot,
) -> dict[str, object]:
    return {
        "schema": "autosport.provider_acquisition_receipt",
        "schema_version": 1,
        "workspace_sha256": hashlib.sha256(
            str(store.workspace).encode("utf-8")
        ).hexdigest(),
        "evidence_sha256": snapshot.evidence_sha256,
        "state_sha256": store._state_sha256(snapshot),
        "semantic_binding_sha256": store._semantic_binding_sha256(snapshot),
    }


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


def test_caller_selected_generic_root_cannot_supply_provider_receipt_trust(tmp_path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    caller_root = (tmp_path / "caller-machine-authority").resolve()
    store = CompleteGameBoardEvidenceStore(workspace, authority_root=caller_root)
    forged = _forged_snapshot()
    _publish_forged_evidence_and_generic_history(
        store,
        forged,
        authority_root=caller_root,
    )

    # Seed the exact caller-root receipt/key layout trusted by the predecessor.
    # This uses only test-owned bytes and standard HMAC, not product issuer helpers.
    workspace_sha256 = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()
    forged_receipt_root = (
        caller_root / "provider-acquisition-receipt-v1" / workspace_sha256
    )
    forged_receipt_root.mkdir(parents=True, exist_ok=True)
    key = b"caller-controlled-provider-key!!"[:32]
    assert len(key) == 32
    (forged_receipt_root / "receipt.key").write_text(key.hex(), encoding="ascii")
    unsigned = _old_unsigned_receipt(store, forged)
    receipt = dict(unsigned)
    receipt["hmac_sha256"] = hmac.new(
        key,
        _canonical_json(unsigned).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    receipts = forged_receipt_root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / f"{forged.evidence_sha256}.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="production-owned acquisition receipt",
    ):
        CompleteGameBoardEvidenceStore(
            workspace,
            authority_root=caller_root,
        ).load(forged.evidence_sha256)


def test_consumer_store_cannot_mint_production_root_receipt_for_forged_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    product_root = (tmp_path / "product-machine-authority").resolve()
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(product_root))
    workspace = (tmp_path / "workspace").resolve()
    store = CompleteGameBoardEvidenceStore(workspace)
    forged = _forged_snapshot()

    # The generic rollback journal is intentionally public and caller-mintable.
    # Even after publishing exact bytes plus matching history, consumer code must
    # have no store operation that can obtain/sign/write provider-origin credentials.
    _publish_forged_evidence_and_generic_history(store, forged, authority_root=None)

    denied = (
        lambda: store._receipt_root(),
        lambda: store._receipt_path(forged.evidence_sha256),
        lambda: store._key_path(),
        lambda: store._read_receipt_key(create=True),
        lambda: store._unsigned_receipt(forged),
        lambda: store._receipt_hmac(b"x" * 32, {"forged": True}),
    )
    for operation in denied:
        with pytest.raises(
            ProviderObservationUnsupportedError,
            match="signing material is not a consumer API",
        ):
            operation()

    with pytest.raises(
        ProviderObservationIntegrityError,
        match="production-owned acquisition receipt",
    ):
        CompleteGameBoardEvidenceStore(workspace).load(forged.evidence_sha256)
