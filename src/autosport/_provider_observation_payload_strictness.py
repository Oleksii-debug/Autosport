"""Reject unknown Autosport-owned v1 provider evidence fields before normalization.

Provider frame payloads stay extensible and are content-bound by ``frame_sha256``.
The Autosport-owned request/evidence envelopes are versioned schemas, so accepting
unknown keys would let modified persisted bytes normalize to the same checked state.
"""

from __future__ import annotations

from typing import Mapping

from . import provider_observation_authority as authority


_REQUEST_KEYS = frozenset(
    {"sport_key", "bookmakers", "markets", "kind", "limit", "max_age_s"}
)
_SNAPSHOT_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "request",
        "captured_at",
        "frame_json",
        "frame_sha256",
        "row_sha256s",
        "evidence_sha256",
    }
)

_ORIGINAL_REQUEST_FROM_PAYLOAD = authority.CompleteGameBoardRequest.from_payload
_ORIGINAL_SNAPSHOT_FROM_PAYLOAD = authority.CompleteGameBoardSnapshot.from_payload


def _strict_request_from_payload(cls, payload: Mapping[str, object]):
    if not isinstance(payload, Mapping) or set(payload) != _REQUEST_KEYS:
        raise authority.ProviderObservationIntegrityError(
            "complete game-board request payload fields mismatch"
        )
    return _ORIGINAL_REQUEST_FROM_PAYLOAD(payload)


def _strict_snapshot_from_payload(cls, payload: Mapping[str, object]):
    if not isinstance(payload, Mapping) or set(payload) != _SNAPSHOT_KEYS:
        raise authority.ProviderObservationIntegrityError(
            "complete game-board evidence payload fields mismatch"
        )
    return _ORIGINAL_SNAPSHOT_FROM_PAYLOAD(payload)


if not getattr(
    authority.CompleteGameBoardRequest.from_payload,
    "_strict_provider_payload_guard",
    False,
):
    setattr(_strict_request_from_payload, "_strict_provider_payload_guard", True)
    authority.CompleteGameBoardRequest.from_payload = classmethod(  # type: ignore[method-assign]
        _strict_request_from_payload
    )

if not getattr(
    authority.CompleteGameBoardSnapshot.from_payload,
    "_strict_provider_payload_guard",
    False,
):
    setattr(_strict_snapshot_from_payload, "_strict_provider_payload_guard", True)
    authority.CompleteGameBoardSnapshot.from_payload = classmethod(  # type: ignore[method-assign]
        _strict_snapshot_from_payload
    )


__all__: list[str] = []
