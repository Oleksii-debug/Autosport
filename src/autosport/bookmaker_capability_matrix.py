"""Deterministic cross-provider technical capability evidence matrix.

The matrix composes existing :class:`BookmakerCapabilityProfile` evidence. It is a
read-only evidence artifact: a provider may technically report support for a write-like
capability, but this module never turns that fact into execution permission or real-money
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json

from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityError,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
)


CAPABILITY_MATRIX_SCHEMA_VERSION = 1


class BookmakerCapabilityMatrixError(BookmakerCapabilityError):
    """Raised when cross-provider capability evidence is ambiguous or inconsistent."""


def _timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BookmakerCapabilityMatrixError(f"{field} must be a non-empty trimmed string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BookmakerCapabilityMatrixError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BookmakerCapabilityMatrixError(
            f"{field} must include a timezone offset"
        )
    return parsed


@dataclass(frozen=True, slots=True)
class BookmakerCapabilityMatrix:
    """Point-in-time technical capability evidence across provider account scopes.

    Every profile is rendered against the complete current ``BookmakerCapability``
    vocabulary. Missing facts therefore remain explicit ``unknown`` values instead of
    being silently treated as support. The matrix deliberately fixes both
    ``provider_write_authorized`` and ``real_money_execution`` to ``False`` in its
    canonical representation: technical support evidence is not operational authority.
    """

    profiles: tuple[BookmakerCapabilityProfile, ...]
    as_of: str
    schema_version: int = CAPABILITY_MATRIX_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != CAPABILITY_MATRIX_SCHEMA_VERSION
        ):
            raise BookmakerCapabilityMatrixError(
                f"schema_version must be integer {CAPABILITY_MATRIX_SCHEMA_VERSION}"
            )
        if not isinstance(self.profiles, tuple):
            raise BookmakerCapabilityMatrixError("profiles must be a tuple")
        if not self.profiles:
            raise BookmakerCapabilityMatrixError("profiles must not be empty")

        as_of = _timestamp(self.as_of, "as_of")
        scopes: set[tuple[str, str, str]] = set()
        profile_ids: set[str] = set()
        for profile in self.profiles:
            if type(profile) is not BookmakerCapabilityProfile:
                raise BookmakerCapabilityMatrixError(
                    "profiles must contain exact BookmakerCapabilityProfile values"
                )
            for fact in profile.facts:
                if type(fact) is not BookmakerCapabilityFact:
                    raise BookmakerCapabilityMatrixError(
                        "profile facts must contain exact BookmakerCapabilityFact values"
                    )

            scope = (profile.venue_id, profile.account_id, profile.adapter_id)
            if scope in scopes:
                raise BookmakerCapabilityMatrixError(
                    "duplicate venue/account/adapter capability scope: "
                    + "/".join(scope)
                )
            scopes.add(scope)

            profile_id = profile.profile_id
            if profile_id in profile_ids:
                raise BookmakerCapabilityMatrixError(
                    f"duplicate capability profile identity: {profile_id}"
                )
            profile_ids.add(profile_id)

            observed_at = _timestamp(profile.observed_at, "profile.observed_at")
            if observed_at > as_of:
                raise BookmakerCapabilityMatrixError(
                    "capability profile observation cannot be after matrix as_of: "
                    + "/".join(scope)
                )

    @property
    def matrix_id(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @property
    def provider_write_authorized(self) -> bool:
        """Technical evidence never grants provider-write authority."""

        return False

    @property
    def real_money_execution(self) -> bool:
        """Technical evidence never proves real-money execution readiness."""

        return False

    def to_canonical_dict(self) -> dict[str, object]:
        capabilities = tuple(sorted(BookmakerCapability, key=lambda item: item.value))
        rows = []
        for profile in sorted(
            self.profiles,
            key=lambda item: (
                item.venue_id,
                item.account_id,
                item.adapter_id,
                item.adapter_version,
                item.profile_version,
                item.profile_id,
            ),
        ):
            rows.append(
                {
                    "account_id": profile.account_id,
                    "adapter_id": profile.adapter_id,
                    "adapter_version": profile.adapter_version,
                    "observed_at": profile.observed_at,
                    "profile_id": profile.profile_id,
                    "profile_version": profile.profile_version,
                    "source_payload_sha256": profile.source_payload_sha256,
                    "source_ref": profile.source_ref,
                    "states": {
                        capability.value: profile.state_of(capability).value
                        for capability in capabilities
                    },
                    "venue_id": profile.venue_id,
                }
            )
        return {
            "as_of": self.as_of,
            "capability_vocabulary": [capability.value for capability in capabilities],
            "profiles": rows,
            "provider_write_authorized": self.provider_write_authorized,
            "real_money_execution": self.real_money_execution,
            "schema_version": self.schema_version,
        }
