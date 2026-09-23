"""Reject ambiguous Autosport-owned v1 provider evidence before normalization.

Provider frame payloads stay extensible and are content-bound by ``frame_sha256``.
The Autosport-owned request/evidence envelopes are versioned schemas, so accepting
unknown keys would let modified persisted bytes normalize to the same checked state.
A replacement ``current_game_board`` also represents one current value per logical
provider market; conflicting rows for the same logical identity fail closed.
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
_ORIGINAL_SNAPSHOT_VALIDATE_FRAME = authority.CompleteGameBoardSnapshot._validate_frame


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


def _strict_snapshot_validate_frame(self, frame: Mapping[str, object]) -> None:
    """Reject two distinct current rows for one provider market identity."""

    _ORIGINAL_SNAPSHOT_VALIDATE_FRAME(self, frame)
    rows = frame.get("data")
    if not isinstance(rows, list):
        return

    logical_rows: set[tuple[object, object, object, object]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        logical_identity = (
            row.get("event_id"),
            row.get("bookmaker"),
            row.get("kind"),
            row.get("market_key"),
        )
        if logical_identity in logical_rows:
            raise authority.ProviderObservationIntegrityError(
                "provider snapshot contains conflicting rows for one logical market"
            )
        logical_rows.add(logical_identity)


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

if not getattr(
    authority.CompleteGameBoardSnapshot._validate_frame,
    "_strict_provider_logical_row_guard",
    False,
):
    setattr(
        _strict_snapshot_validate_frame,
        "_strict_provider_logical_row_guard",
        True,
    )
    authority.CompleteGameBoardSnapshot._validate_frame = (  # type: ignore[method-assign]
        _strict_snapshot_validate_frame
    )


__all__: list[str] = []