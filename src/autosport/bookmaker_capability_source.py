"""Source-class isolation for bookmaker capability evidence.

This module is deliberately descriptive and read-only. It binds an existing
``BookmakerCapabilityProfile`` to the exact class of evidence source that
produced it and prevents capability facts from silently carrying across
fallback source classes or adapters.

It does not perform provider I/O, handle credentials, place/cancel/cash out
bets, settle positions, move money, or grant execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityError,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    UnknownBookmakerCapability,
    UnsupportedBookmakerCapability,
)


class CapabilitySourceBoundaryError(BookmakerCapabilityError):
    """Raised when source-scoped capability evidence violates this contract."""


class CapabilitySourceClass(str, Enum):
    """Origin class for one technical capability observation."""

    OFFICIAL_API = "official_api"
    BROWSER_UI_ADAPTER = "browser_ui_adapter"
    HISTORICAL_FIXTURE = "historical_fixture"


def _text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise CapabilitySourceBoundaryError(f"{field} must be a string")
    if not value or value != value.strip():
        raise CapabilitySourceBoundaryError(
            f"{field} must be non-empty and trimmed"
        )
    return value


def _timestamp(value: str, field: str) -> datetime:
    _text(value, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CapabilitySourceBoundaryError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CapabilitySourceBoundaryError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _sha256(value: str, field: str) -> str:
    _text(value, field)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise CapabilitySourceBoundaryError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return value


@dataclass(frozen=True, slots=True)
class SourceBoundCapabilityProfile:
    """One exact capability profile plus its evidence-source class."""

    profile: BookmakerCapabilityProfile
    source_class: CapabilitySourceClass
    observed_at: str
    source_ref: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        if type(self.profile) is not BookmakerCapabilityProfile:
            raise CapabilitySourceBoundaryError(
                "profile must be an exact BookmakerCapabilityProfile"
            )
        if type(self.source_class) is not CapabilitySourceClass:
            raise CapabilitySourceBoundaryError(
                "source_class must be a CapabilitySourceClass value"
            )
        observed = _timestamp(self.observed_at, "observed_at")
        profile_observed = _timestamp(
            self.profile.observed_at,
            "profile.observed_at",
        )
        if observed < profile_observed:
            raise CapabilitySourceBoundaryError(
                "source binding observed_at cannot predate capability profile"
            )
        _text(self.source_ref, "source_ref")
        _sha256(self.source_payload_sha256, "source_payload_sha256")

    @property
    def binding_id(self) -> str:
        payload = self.to_canonical_dict()
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False

    def state_of(
        self,
        capability: BookmakerCapability,
    ) -> BookmakerCapabilityState:
        return self.profile.state_of(capability)

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "observed_at": self.observed_at,
            "profile": self.profile.to_canonical_dict(),
            "profile_id": self.profile.profile_id,
            "source_class": self.source_class.value,
            "source_payload_sha256": self.source_payload_sha256,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True, slots=True)
class CapabilitySourceMatrix:
    """Current source-scoped capability evidence for one venue/account.

    Queries require both source class and adapter identity. Missing exact
    source/adapter evidence is UNKNOWN; facts are never borrowed from another
    source class or fallback adapter.
    """

    bindings: tuple[SourceBoundCapabilityProfile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.bindings, tuple):
            raise CapabilitySourceBoundaryError("bindings must be a tuple")
        if not self.bindings:
            raise CapabilitySourceBoundaryError(
                "bindings must contain at least one source-bound profile"
            )

        venue_id: str | None = None
        account_id: str | None = None
        seen: set[tuple[CapabilitySourceClass, str]] = set()

        for binding in self.bindings:
            if type(binding) is not SourceBoundCapabilityProfile:
                raise CapabilitySourceBoundaryError(
                    "bindings must contain exact SourceBoundCapabilityProfile values"
                )
            if venue_id is None:
                venue_id = binding.profile.venue_id
                account_id = binding.profile.account_id
            elif (
                binding.profile.venue_id != venue_id
                or binding.profile.account_id != account_id
            ):
                raise CapabilitySourceBoundaryError(
                    "all bindings must describe one exact venue/account scope"
                )

            key = (binding.source_class, binding.profile.adapter_id)
            if key in seen:
                raise CapabilitySourceBoundaryError(
                    "duplicate source_class/adapter_id capability binding"
                )
            seen.add(key)

    @property
    def venue_id(self) -> str:
        return self.bindings[0].profile.venue_id

    @property
    def account_id(self) -> str:
        return self.bindings[0].profile.account_id

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
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False

    def binding_for(
        self,
        source_class: CapabilitySourceClass,
        adapter_id: str,
    ) -> SourceBoundCapabilityProfile | None:
        if type(source_class) is not CapabilitySourceClass:
            raise CapabilitySourceBoundaryError(
                "source_class must be a CapabilitySourceClass value"
            )
        adapter_id = _text(adapter_id, "adapter_id")
        for binding in self.bindings:
            if (
                binding.source_class is source_class
                and binding.profile.adapter_id == adapter_id
            ):
                return binding
        return None

    def state_of(
        self,
        source_class: CapabilitySourceClass,
        adapter_id: str,
        capability: BookmakerCapability,
    ) -> BookmakerCapabilityState:
        if not isinstance(capability, BookmakerCapability):
            raise CapabilitySourceBoundaryError(
                "capability must be a BookmakerCapability value"
            )
        binding = self.binding_for(source_class, adapter_id)
        if binding is None:
            return BookmakerCapabilityState.UNKNOWN
        return binding.state_of(capability)

    def require(
        self,
        source_class: CapabilitySourceClass,
        adapter_id: str,
        capability: BookmakerCapability,
    ) -> None:
        state = self.state_of(source_class, adapter_id, capability)
        if state is BookmakerCapabilityState.UNKNOWN:
            raise UnknownBookmakerCapability(
                f"{self.venue_id}/{self.account_id} capability "
                f"{capability.value} is unknown for "
                f"{source_class.value}/{adapter_id}"
            )
        if state is BookmakerCapabilityState.UNSUPPORTED:
            raise UnsupportedBookmakerCapability(
                f"{self.venue_id}/{self.account_id} does not support "
                f"{capability.value} for {source_class.value}/{adapter_id}"
            )

    def to_canonical_dict(self) -> dict[str, object]:
        ordered = sorted(
            self.bindings,
            key=lambda item: (
                item.source_class.value,
                item.profile.adapter_id,
                item.binding_id,
            ),
        )
        return {
            "account_id": self.account_id,
            "bindings": [binding.to_canonical_dict() for binding in ordered],
            "venue_id": self.venue_id,
        }
