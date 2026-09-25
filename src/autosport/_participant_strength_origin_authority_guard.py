from __future__ import annotations

"""Seal participant-strength positive origin reads to durable product authority.

The participant-strength lineage already re-resolves supplied RatingSnapshots through
``OpponentIntelligenceStore`` and binds durable bytes to MonotonicWorkspaceAuthority.
Two Python/runtime seams must stay fail-closed at that positive boundary:

* existing caller-authored JSON must never become trusted merely because ``_load``
  sees a valid self-consistent payload and an empty authority history; and
* an exact ``OpponentIntelligenceStore`` instance/class must not shadow or rebind the
  resolver/verification methods used to mint a registered forecast.

This module adds no store, rating model, identity authority, or forecast authority. It
only fences the existing #845 origin mechanism and public registered-forecast seam.
"""

from . import opponent_intelligence as _opponent
from . import participant_strength as _strength


Store = _opponent.OpponentIntelligenceStore
_ANCHOR_NAME = "_participant_strength_origin_authority_pristine_v1"
_EMIT_ANCHOR_NAME = "_participant_strength_origin_emit_pristine_v1"
_METHOD_NAMES = (
    "resolve_rating_snapshot",
    "_verified_durable_state",
    "_recover_origin_authority",
)


def _load_pristine_methods():
    existing = getattr(_opponent, _ANCHOR_NAME, None)
    if existing is None:
        existing = (
            Store,
            Store.resolve_rating_snapshot,
            Store._verified_durable_state,
            Store._recover_origin_authority,
        )
        setattr(_opponent, _ANCHOR_NAME, existing)
    if (
        type(existing) is not tuple
        or len(existing) != 4
        or existing[0] is not Store
        or not all(callable(item) for item in existing[1:])
    ):
        raise RuntimeError("participant-strength origin authority anchor is invalid")
    return existing[1], existing[2], existing[3]


def _load_pristine_emit():
    existing = getattr(_strength, _EMIT_ANCHOR_NAME, None)
    if existing is None:
        existing = _strength.emit_registered_strength_forecast
        setattr(_strength, _EMIT_ANCHOR_NAME, existing)
    if not callable(existing):
        raise RuntimeError("participant-strength forecast authority anchor is invalid")
    return existing


_PRISTINE_RESOLVE, _PRISTINE_VERIFY, _PRISTINE_RECOVER = _load_pristine_methods()
_PRISTINE_EMIT = _load_pristine_emit()


def _recover_without_retroactive_bootstrap(
    self: Store,
    observed_state_sha256: str | None,
    *,
    allow_bootstrap: bool,
):
    # A new/pristine store has observed_state_sha256=None and the existing authority
    # implementation already permits that empty origin before its first PREPARE.
    # Existing bytes without prior authority history are not adoptable evidence.
    del allow_bootstrap
    return _PRISTINE_RECOVER(
        self,
        observed_state_sha256,
        allow_bootstrap=False,
    )


Store._recover_origin_authority = _recover_without_retroactive_bootstrap
_EXPECTED_METHODS = (
    _PRISTINE_RESOLVE,
    _PRISTINE_VERIFY,
    _recover_without_retroactive_bootstrap,
)


def _require_exact_store_dispatch(store: Store) -> None:
    if type(store) is not Store:
        raise _strength.ParticipantStrengthError(
            "opponent store must be the exact product OpponentIntelligenceStore"
        )
    namespace = getattr(store, "__dict__", None)
    if type(namespace) is not dict:
        raise _strength.ParticipantStrengthError(
            "opponent store authority namespace is unavailable"
        )
    if any(name in namespace for name in _METHOD_NAMES):
        raise _strength.ParticipantStrengthError(
            "opponent store origin authority methods cannot be instance-shadowed"
        )
    current = tuple(getattr(Store, name, None) for name in _METHOD_NAMES)
    if current != _EXPECTED_METHODS:
        raise _strength.ParticipantStrengthError(
            "opponent store origin authority methods cannot be class-rebound"
        )


def emit_registered_strength_forecast(
    *,
    registry,
    artifact_store,
    opponent_store,
    evidence,
    model_version_id: str,
    strategy_version_id: str,
    quote_key: str,
):
    _require_exact_store_dispatch(opponent_store)
    return _PRISTINE_EMIT(
        registry=registry,
        artifact_store=artifact_store,
        opponent_store=opponent_store,
        evidence=evidence,
        model_version_id=model_version_id,
        strategy_version_id=strategy_version_id,
        quote_key=quote_key,
    )


_strength.emit_registered_strength_forecast = emit_registered_strength_forecast
