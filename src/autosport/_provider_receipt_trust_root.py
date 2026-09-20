from __future__ import annotations

"""Install the non-self-attesting complete-board authority boundary.

A local HMAC/file secret cannot prove remote provider origin against code running as
the same OS user. This guard therefore does not pretend that a restart can recreate
provider provenance from locally writable bytes. Positive authority is an ephemeral,
exact-object capability minted only by the fixed production acquisition entrypoint.
Canonical evidence may be persisted with the generic monotonic journal for integrity
and rollback detection, but a restarted process must reacquire from the provider
before it can regain positive completeness authority.

Threat/control contract: this is an application authority boundary against
caller-created objects, structural witnesses, consumer API misuse, and persisted-byte
forgery. It is not an OS sandbox against arbitrary code injection/monkeypatch inside
the trusted Autosport process; such code can replace Python functions themselves.
That stronger boundary would require provider-signed evidence or an isolated OS/service
issuer, neither of which the current provider contract supplies.
"""

import weakref
from types import MappingProxyType

from . import provider_observation_authority as provider
from .integrity import atomic_write_json


def _install_guard() -> None:
    store_type = provider.CompleteGameBoardEvidenceStore
    if getattr(store_type.save, "_ephemeral_provider_origin_guard", False):
        return

    issued: dict[int, tuple[weakref.ReferenceType, str]] = {}

    def forget_issued(snapshot_id: int, reference: weakref.ReferenceType) -> None:
        current = issued.get(snapshot_id)
        if current is not None and current[0] is reference:
            issued.pop(snapshot_id, None)

    def issue_snapshot(snapshot: provider.CompleteGameBoardSnapshot):
        snapshot_id = id(snapshot)
        reference = weakref.ref(
            snapshot,
            lambda current, snapshot_id=snapshot_id: forget_issued(snapshot_id, current),
        )
        issued[snapshot_id] = (reference, snapshot.evidence_sha256)
        return snapshot

    def assert_authoritative(snapshot: provider.CompleteGameBoardSnapshot) -> None:
        if not isinstance(snapshot, provider.CompleteGameBoardSnapshot):
            raise provider.ProviderObservationUnsupportedError(
                "complete provider authority requires CompleteGameBoardSnapshot"
            )
        current = issued.get(id(snapshot))
        if (
            current is None
            or current[0]() is not snapshot
            or current[1] != snapshot.evidence_sha256
        ):
            raise provider.ProviderObservationUnsupportedError(
                "snapshot was not issued by canonical provider acquisition evidence"
            )

    def deny_direct_issuance(*args, **kwargs):
        del args, kwargs
        raise provider.ProviderObservationUnsupportedError(
            "provider authority issuance is not a consumer API"
        )

    def capture_parlay_complete_game_board(
        *,
        api_key: str,
        request: provider.CompleteGameBoardRequest,
        timeout_seconds: float = 10.0,
    ) -> provider.CompleteGameBoardSnapshot:
        """Acquire and issue only through the fixed production Parlay boundary."""

        if not isinstance(api_key, str) or not api_key or api_key != api_key.strip():
            raise ValueError("api_key must be non-empty trimmed text")
        if any(character.isspace() for character in api_key):
            raise ValueError("api_key must not contain whitespace")
        if not isinstance(request, provider.CompleteGameBoardRequest):
            raise TypeError("request must be CompleteGameBoardRequest")
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        timeout = float(timeout_seconds)
        if not provider.math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        frame = provider._read_production_initial_state(
            request,
            api_key=api_key,
            timeout_seconds=timeout,
        )
        snapshot = provider.CompleteGameBoardSnapshot(
            request=request,
            captured_at=provider._default_clock(),
            frame_json=provider._canonical_json(dict(frame)),
        )
        return issue_snapshot(snapshot)

    def save_authoritative(store, snapshot: provider.CompleteGameBoardSnapshot):
        """Persist a live canonical capture without claiming restart origin proof."""

        assert_authoritative(snapshot)
        path = store._path(snapshot.evidence_sha256)
        authority = store._authority(snapshot.evidence_sha256)
        intended = store._state_sha256(snapshot)
        binding = store._semantic_binding_sha256(snapshot)

        with provider.WorkspaceEconomicLock(store.workspace):
            existing = store._read_path(path) if path.exists() else None
            history = authority.read_history()
            if history:
                store._recover_provenance(authority, existing)
                history = authority.read_history()
                if existing is not None:
                    if existing.to_payload() != snapshot.to_payload():
                        raise provider.ProviderObservationIntegrityError(
                            "content-addressed provider evidence conflicts with proven bytes"
                        )
                    return path
            elif existing is not None and existing.to_payload() != snapshot.to_payload():
                raise provider.ProviderObservationIntegrityError(
                    "unproven local provider evidence conflicts with production capture"
                )

            observed = (
                None
                if not history
                else history[-1].intended_state_sha256
                if history[-1].phase is provider.AuthorityPhase.COMMIT
                else None
            )
            tx_id = store._next_tx_id(authority, intended)
            try:
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
                atomic_write_json(path, snapshot.to_payload())
                published = store._read_path(path)
                if store._state_sha256(published) != intended:
                    raise provider.ProviderObservationIntegrityError(
                        "published provider evidence does not match intended capture digest"
                    )
                authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
            except provider.MonotonicWorkspaceAuthorityError as exc:
                raise provider.ProviderObservationIntegrityError(
                    "provider acquisition provenance publication failed closed"
                ) from exc
        return path

    def load_non_authoritative(store, evidence_sha256: str):
        """Validate durable integrity, then fail closed instead of recreating origin."""

        path = store._path(evidence_sha256)
        with provider.WorkspaceEconomicLock(store.workspace):
            snapshot = store._read_path(path)
            if snapshot.evidence_sha256 != provider._sha(
                evidence_sha256, "evidence_sha256"
            ):
                raise provider.ProviderObservationIntegrityError(
                    "content-addressed provider evidence path does not match payload"
                )
            authority = store._authority(snapshot.evidence_sha256)
            store._recover_provenance(authority, snapshot)
        raise provider.ProviderObservationUnsupportedError(
            "restart cannot reissue provider-origin authority; reacquire canonical provider evidence"
        )

    def deny_local_receipt_authority(*args, **kwargs):
        del args, kwargs
        raise provider.ProviderObservationUnsupportedError(
            "local receipt signing is not provider-origin authority"
        )

    setattr(save_authoritative, "_ephemeral_provider_origin_guard", True)
    setattr(capture_parlay_complete_game_board, "_sealed_provider_capture_issuer", True)
    setattr(load_non_authoritative, "_restart_provider_origin_fails_closed", True)

    # Read-only diagnostics are permitted; mutation/issuance is lexical only.
    provider._ISSUED = MappingProxyType(issued)
    provider._remember = deny_direct_issuance
    provider.assert_complete_game_board_authoritative = assert_authoritative
    provider.capture_parlay_complete_game_board = capture_parlay_complete_game_board

    # Durable local files remain integrity evidence only. No local key/HMAC is
    # allowed to turn them back into provider-origin authority after restart.
    store_type.save = save_authoritative
    store_type.load = load_non_authoritative
    store_type._write_receipt = deny_local_receipt_authority
    store_type._verify_receipt = deny_local_receipt_authority
    store_type._receipt_root = deny_local_receipt_authority
    store_type._receipt_path = deny_local_receipt_authority
    store_type._key_path = deny_local_receipt_authority
    store_type._read_receipt_key = deny_local_receipt_authority
    store_type._unsigned_receipt = deny_local_receipt_authority
    store_type._receipt_hmac = staticmethod(deny_local_receipt_authority)


_install_guard()
del _install_guard

__all__: list[str] = []
