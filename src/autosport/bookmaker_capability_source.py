"""Source provenance isolation for bookmaker capability evidence.

This module is descriptive and read-only. It binds an existing
``BookmakerCapabilityProfile`` either to canonical integration-channel evidence
or to historical-fixture provenance, while preventing capability facts from
silently carrying across channels or adapters.

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
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    UnknownBookmakerCapability,
    UnsupportedBookmakerCapability,
)
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationEvidence,
    BookmakerIntegrationEvidenceError,
    BookmakerIntegrationKind,
)


class CapabilitySourceBoundaryError(BookmakerCapabilityError):
    """Raised when source-scoped capability evidence violates this contract."""


class CapabilitySourceClass(str, Enum):
    """Orthogonal provenance class; API/browser channel identity is canonical elsewhere."""

    INTEGRATION = "integration"
    HISTORICAL_FIXTURE = "historical_fixture"


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CapabilitySourceBoundaryError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CapabilitySourceBoundaryError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CapabilitySourceBoundaryError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise CapabilitySourceBoundaryError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return text


def _validate_exact_profile(profile: BookmakerCapabilityProfile) -> None:
    if type(profile) is not BookmakerCapabilityProfile:
        raise CapabilitySourceBoundaryError(
            "profile must be an exact BookmakerCapabilityProfile"
        )
    if type(profile.facts) is not tuple:
        raise CapabilitySourceBoundaryError("profile.facts must be an exact tuple")
    for fact in profile.facts:
        if type(fact) is not BookmakerCapabilityFact:
            raise CapabilitySourceBoundaryError(
                "profile facts must be exact BookmakerCapabilityFact values"
            )


@dataclass(frozen=True, slots=True)
class SourceBoundCapabilityProfile:
    """One exact capability profile plus orthogonal provenance evidence.

    For live/integrated observations, ``integration_evidence`` is mandatory and
    owns the OFFICIAL_API/BROWSER_AUTOMATION distinction through the existing
    canonical ``BookmakerIntegrationKind`` contract. Historical fixtures are a
    separate provenance class and cannot carry integration-channel evidence.
    """

    profile: BookmakerCapabilityProfile
    source_class: CapabilitySourceClass
    observed_at: str
    source_ref: str
    source_payload_sha256: str
    integration_evidence: BookmakerIntegrationEvidence | None = None

    def __post_init__(self) -> None:
        _validate_exact_profile(self.profile)
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

        if self.source_class is CapabilitySourceClass.INTEGRATION:
            if type(self.integration_evidence) is not BookmakerIntegrationEvidence:
                raise CapabilitySourceBoundaryError(
                    "integration provenance requires exact BookmakerIntegrationEvidence"
                )
            try:
                self.integration_evidence.verify_profile(self.profile)
            except BookmakerIntegrationEvidenceError as exc:
                raise CapabilitySourceBoundaryError(
                    "integration evidence does not match capability profile"
                ) from exc
            integration_observed = _timestamp(
                self.integration_evidence.observed_at,
                "integration_evidence.observed_at",
            )
            if observed < integration_observed:
                raise CapabilitySourceBoundaryError(
                    "source binding observed_at cannot predate integration evidence"
                )
        elif self.integration_evidence is not None:
            raise CapabilitySourceBoundaryError(
                "historical fixture provenance cannot carry integration evidence"
            )

    @property
    def integration_kind(self) -> BookmakerIntegrationKind | None:
        if self.integration_evidence is None:
            return None
        return self.integration_evidence.integration_kind

    @property
    def scope_key(self) -> tuple[CapabilitySourceClass, BookmakerIntegrationKind | None, str]:
        return (self.source_class, self.integration_kind, self.profile.adapter_id)

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
        _validate_exact_profile(self.profile)
        return self.profile.state_of(capability)

    def to_canonical_dict(self) -> dict[str, object]:
        _validate_exact_profile(self.profile)
        integration = self.integration_evidence
        return {
            "integration_evidence": (
                None
                if integration is None
                else {
                    "evidence": integration.to_canonical_dict(),
                    "evidence_id": integration.evidence_id,
                }
            ),
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

    Integrated queries require the canonical integration kind plus adapter
    identity. Historical-fixture queries have no integration kind. Missing exact
    scope evidence is UNKNOWN; facts are never borrowed across provenance,
    integration channel, or adapter boundaries.
    """

    bindings: tuple[SourceBoundCapabilityProfile, ...]

    def __post_init__(self) -> None:
        if type(self.bindings) is not tuple:
            raise CapabilitySourceBoundaryError("bindings must be an exact tuple")
        if not self.bindings:
            raise CapabilitySourceBoundaryError(
                "bindings must contain at least one source-bound profile"
            )

        venue_id: str | None = None
        account_id: str | None = None
        seen: set[
            tuple[CapabilitySourceClass, BookmakerIntegrationKind | None, str]
        ] = set()

        for binding in self.bindings:
            if type(binding) is not SourceBoundCapabilityProfile:
                raise CapabilitySourceBoundaryError(
                    "bindings must contain exact SourceBoundCapabilityProfile values"
                )
            _validate_exact_profile(binding.profile)
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

            key = binding.scope_key
            if key in seen:
                raise CapabilitySourceBoundaryError(
                    "duplicate provenance/integration/adapter capability binding"
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

    @staticmethod
    def _validate_query_scope(
        source_class: CapabilitySourceClass,
        integration_kind: BookmakerIntegrationKind | None,
    ) -> None:
        if type(source_class) is not CapabilitySourceClass:
            raise CapabilitySourceBoundaryError(
                "source_class must be a CapabilitySourceClass value"
            )
        if source_class is CapabilitySourceClass.INTEGRATION:
            if type(integration_kind) is not BookmakerIntegrationKind:
                raise CapabilitySourceBoundaryError(
                    "integration queries require exact BookmakerIntegrationKind"
                )
        elif integration_kind is not None:
            raise CapabilitySourceBoundaryError(
                "historical fixture queries cannot specify integration_kind"
            )

    def binding_for(
        self,
        source_class: CapabilitySourceClass,
        adapter_id: str,
        *,
        integration_kind: BookmakerIntegrationKind | None = None,
    ) -> SourceBoundCapabilityProfile | None:
        self._validate_query_scope(source_class, integration_kind)
        adapter_id = _text(adapter_id, "adapter_id")
        key = (source_class, integration_kind, adapter_id)
        for binding in self.bindings:
            if binding.scope_key == key:
                return binding
        return None

    def state_of(
        self,
        source_class: CapabilitySourceClass,
        adapter_id: str,
        capability: BookmakerCapability,
        *,
        integration_kind: BookmakerIntegrationKind | None = None,
    ) -> BookmakerCapabilityState:
        if type(capability) is not BookmakerCapability:
            raise CapabilitySourceBoundaryError(
                "capability must be a BookmakerCapability value"
            )
        binding = self.binding_for(
            source_class,
            adapter_id,
            integration_kind=integration_kind,
        )
        if binding is None:
            return BookmakerCapabilityState.UNKNOWN
        return binding.state_of(capability)

    def require(
        self,
        source_class: CapabilitySourceClass,
        adapter_id: str,
        capability: BookmakerCapability,
        *,
        integration_kind: BookmakerIntegrationKind | None = None,
    ) -> None:
        state = self.state_of(
            source_class,
            adapter_id,
            capability,
            integration_kind=integration_kind,
        )
        if state is BookmakerCapabilityState.UNKNOWN:
            scope = source_class.value
            if integration_kind is not None:
                scope = f"{scope}/{integration_kind.value}"
            raise UnknownBookmakerCapability(
                f"{self.venue_id}/{self.account_id} capability "
                f"{capability.value} is unknown for {scope}/{adapter_id}"
            )
        if state is BookmakerCapabilityState.UNSUPPORTED:
            scope = source_class.value
            if integration_kind is not None:
                scope = f"{scope}/{integration_kind.value}"
            raise UnsupportedBookmakerCapability(
                f"{self.venue_id}/{self.account_id} does not support "
                f"{capability.value} for {scope}/{adapter_id}"
            )

    def to_canonical_dict(self) -> dict[str, object]:
        ordered = sorted(
            self.bindings,
            key=lambda item: (
                item.source_class.value,
                "" if item.integration_kind is None else item.integration_kind.value,
                item.profile.adapter_id,
                item.binding_id,
            ),
        )
        return {
            "account_id": self.account_id,
            "bindings": [binding.to_canonical_dict() for binding in ordered],
            "venue_id": self.venue_id,
        }
