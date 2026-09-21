"""Deterministic, provider-neutral projection of bookmaker capability evidence.

This module does not discover capabilities and cannot authorize provider I/O,
execution, or product readiness. It only projects already-authoritative
:class:`BookmakerCapabilityProfile` evidence into a complete comparison matrix.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .bookmaker_capability import BookmakerCapability, BookmakerCapabilityProfile


_SCHEMA_VERSION = "autosport.bookmaker_capability_matrix.v1"


class BookmakerCapabilityMatrixError(ValueError):
    """Raised when capability profiles cannot form an unambiguous matrix."""


@dataclass(frozen=True, slots=True, init=False)
class BookmakerCapabilityMatrix:
    """Immutable comparison view over authoritative capability profiles.

    Profiles are retained as the source of truth. Capability cells are always
    derived with ``BookmakerCapabilityProfile.state_of``; callers cannot inject
    a separately-computed state into this matrix.
    """

    _profiles: tuple[BookmakerCapabilityProfile, ...]

    def __init__(
        self,
        profiles: Iterable[BookmakerCapabilityProfile] = (),
    ) -> None:
        materialized = tuple(profiles)
        for profile in materialized:
            if type(profile) is not BookmakerCapabilityProfile:
                raise BookmakerCapabilityMatrixError(
                    "profiles must contain exact BookmakerCapabilityProfile values"
                )

        ordered = tuple(sorted(materialized, key=self._sort_key))
        seen_profile_ids: set[str] = set()
        seen_scope_versions: set[tuple[str, str, str, str, int]] = set()
        for profile in ordered:
            if profile.profile_id in seen_profile_ids:
                raise BookmakerCapabilityMatrixError(
                    f"duplicate profile_id: {profile.profile_id}"
                )
            seen_profile_ids.add(profile.profile_id)

            scope_version = self._scope_version_key(profile)
            if scope_version in seen_scope_versions:
                raise BookmakerCapabilityMatrixError(
                    "duplicate provider scope/version: "
                    f"{profile.venue_id}/{profile.account_id}/"
                    f"{profile.adapter_id}@{profile.adapter_version} "
                    f"profile_version={profile.profile_version}"
                )
            seen_scope_versions.add(scope_version)

        object.__setattr__(self, "_profiles", ordered)

    @staticmethod
    def _scope_version_key(
        profile: BookmakerCapabilityProfile,
    ) -> tuple[str, str, str, str, int]:
        return (
            profile.venue_id,
            profile.account_id,
            profile.adapter_id,
            profile.adapter_version,
            profile.profile_version,
        )

    @classmethod
    def _sort_key(
        cls,
        profile: BookmakerCapabilityProfile,
    ) -> tuple[str, str, str, str, int, str]:
        return (*cls._scope_version_key(profile), profile.profile_id)

    @property
    def profiles(self) -> tuple[BookmakerCapabilityProfile, ...]:
        """Profiles in deterministic provider/scope/version order."""

        return self._profiles

    @property
    def capabilities(self) -> tuple[BookmakerCapability, ...]:
        """Every canonical capability in deterministic wire-value order."""

        return tuple(sorted(BookmakerCapability, key=lambda item: item.value))

    def state_of(
        self,
        profile_id: str,
        capability: BookmakerCapability,
    ):
        """Return the authoritative tri-state value for one matrix cell."""

        if type(capability) is not BookmakerCapability:
            raise BookmakerCapabilityMatrixError(
                "capability must be an exact BookmakerCapability value"
            )
        for profile in self._profiles:
            if profile.profile_id == profile_id:
                return profile.state_of(capability)
        raise BookmakerCapabilityMatrixError(f"unknown profile_id: {profile_id}")

    def to_summary(self) -> dict[str, object]:
        """Return a deterministic evidence-rich representation for UX/reports."""

        capabilities = self.capabilities
        rows: list[dict[str, object]] = []
        for profile in self._profiles:
            cells = {
                capability.value: {
                    "state": profile.state_of(capability).value,
                    "profile_id": profile.profile_id,
                    "observed_at": profile.observed_at,
                    "source_ref": profile.source_ref,
                    "source_payload_sha256": profile.source_payload_sha256,
                }
                for capability in capabilities
            }
            rows.append(
                {
                    "profile_id": profile.profile_id,
                    "venue_id": profile.venue_id,
                    "account_id": profile.account_id,
                    "adapter_id": profile.adapter_id,
                    "adapter_version": profile.adapter_version,
                    "profile_version": profile.profile_version,
                    "observed_at": profile.observed_at,
                    "source_ref": profile.source_ref,
                    "source_payload_sha256": profile.source_payload_sha256,
                    "capabilities": cells,
                }
            )

        return {
            "schema_version": _SCHEMA_VERSION,
            "capability_order": [capability.value for capability in capabilities],
            "rows": rows,
        }
