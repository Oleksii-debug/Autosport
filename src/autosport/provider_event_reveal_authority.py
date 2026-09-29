from __future__ import annotations

"""Provider-owned pre-result reveal authority for the complete-board consumer.

This module deliberately derives event start/reveal evidence only from the exact live
``CompleteGameBoardSnapshot`` capability minted by the fixed production provider
acquisition path.  Generic lifecycle/catalog storage remains useful causal state, but
caller-fed catalog bytes are not allowed to mint a positive pre-result boundary.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from .provider_observation_authority import (
    CompleteGameBoardSnapshot,
    assert_complete_game_board_authoritative,
)


_AUTHORITY_KIND = "parlay-complete-board-commence-time-v1"


class ProviderEventRevealAuthorityError(ValueError):
    """Authenticated complete-board evidence cannot prove an event reveal boundary."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ProviderEventRevealAuthorityError(
            f"{field} must be non-empty canonical text"
        )
    value.encode("utf-8")
    return value


def _instant(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderEventRevealAuthorityError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderEventRevealAuthorityError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, field: str) -> str:
    return _instant(value, field).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProviderEventRevealAuthorityError(
            "provider reveal evidence must be canonical finite JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderEventRevealEvidence:
    """Exact provider-snapshot binding for one event's reported start boundary."""

    event_id: str
    outcome_reveal_not_before: str
    authority_id: str
    authority_sha256: str

    def __post_init__(self) -> None:
        event_id = _text(self.event_id, "event_id")
        reveal = _timestamp(
            self.outcome_reveal_not_before,
            "outcome_reveal_not_before",
        )
        authority_id = _text(self.authority_id, "authority_id")
        authority_sha256 = _text(self.authority_sha256, "authority_sha256").lower()
        if len(authority_sha256) != 64 or any(
            ch not in "0123456789abcdef" for ch in authority_sha256
        ):
            raise ProviderEventRevealAuthorityError(
                "authority_sha256 must be canonical SHA-256 hex"
            )
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "outcome_reveal_not_before", reveal)
        object.__setattr__(self, "authority_id", authority_id)
        object.__setattr__(self, "authority_sha256", authority_sha256)


def resolve_provider_event_reveal(
    snapshot: CompleteGameBoardSnapshot,
    *,
    event_id: str,
) -> ProviderEventRevealEvidence:
    """Resolve one pre-result boundary from exact authenticated provider bytes.

    The provider contract reports ``commence_time`` together with
    ``commence_time_reported``.  Positive authority is intentionally fail-closed:
    every complete-board row for the event must carry a source-reported start time,
    all rows must agree exactly after UTC normalization, and the reported start must
    be strictly after the authoritative snapshot capture.  Missing, guessed,
    conflicting or already-started events cannot authorize a pre-result freeze.
    """

    if type(snapshot) is not CompleteGameBoardSnapshot:
        raise ProviderEventRevealAuthorityError(
            "provider reveal authority requires exact CompleteGameBoardSnapshot"
        )
    assert_complete_game_board_authoritative(snapshot)
    event_id = _text(event_id, "event_id")

    frame = snapshot.frame
    raw_rows = frame.get("data")
    if not isinstance(raw_rows, list):
        raise ProviderEventRevealAuthorityError(
            "complete provider snapshot data must be a list"
        )

    event_rows: list[Mapping[str, object]] = []
    commence_times: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ProviderEventRevealAuthorityError(
                "complete provider snapshot row must be an object"
            )
        if raw.get("event_id") != event_id:
            continue
        event_rows.append(raw)
        if raw.get("commence_time_reported") is not True:
            raise ProviderEventRevealAuthorityError(
                "provider event start time is not source-reported"
            )
        commence_times.add(_timestamp(raw.get("commence_time"), "commence_time"))

    if not event_rows:
        raise ProviderEventRevealAuthorityError(
            "event_id is absent from the authoritative complete board"
        )
    if len(commence_times) != 1:
        raise ProviderEventRevealAuthorityError(
            "provider complete-board rows disagree on event commence_time"
        )

    commence_time = next(iter(commence_times))
    if _instant(commence_time, "commence_time") <= _instant(
        snapshot.captured_at,
        "provider captured_at",
    ):
        raise ProviderEventRevealAuthorityError(
            "provider snapshot is not strictly before the reported event start"
        )

    event_row_sha256s = tuple(sorted(_digest(dict(row)) for row in event_rows))
    authority_sha256 = _digest(
        {
            "kind": _AUTHORITY_KIND,
            "provider_evidence_sha256": snapshot.evidence_sha256,
            "event_id": event_id,
            "commence_time": commence_time,
            "event_row_sha256s": list(event_row_sha256s),
        }
    )
    return ProviderEventRevealEvidence(
        event_id=event_id,
        outcome_reveal_not_before=commence_time,
        authority_id=f"{_AUTHORITY_KIND}:{snapshot.evidence_sha256}:{event_id}",
        authority_sha256=authority_sha256,
    )


__all__ = [
    "ProviderEventRevealAuthorityError",
    "ProviderEventRevealEvidence",
    "resolve_provider_event_reveal",
]
