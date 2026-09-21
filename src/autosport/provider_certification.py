"""Deterministic provider certification artifact without execution authority.

The artifact composes existing bookmaker capability and integration evidence into one
immutable, content-addressed qualification projection.  It deliberately does not perform
provider I/O, persist credentials, grant legal/governance permission, authorize product
execution, mutate bankroll/risk/settlement state, or promote release/readiness truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json

from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from .bookmaker_integration_boundary import (
    BookmakerIntegrationEvidence,
    BookmakerIntegrationKind,
)


class ProviderCertificationError(ValueError):
    """Raised when certification evidence is malformed, widened, or stale."""


class ProviderTestMode(str, Enum):
    """Provider mode for which evidence was actually produced."""

    REPLAY = "replay"
    LIVE_OBSERVATION = "live_observation"
    OBSERVED_EXECUTABLE = "observed_executable"
    ACCOUNT_READ_ONLY = "account_read_only"
    SUPERVISED_EXECUTION = "supervised_execution"


class ProviderCertifiedUse(str, Enum):
    """Mechanically bounded technical use classification.

    SUPERVISED_EXECUTION_CAPABLE is technical qualification evidence only.  It is not
    product execution permission and never authorizes real-money action by itself.
    """

    REPLAY = "replay"
    LIVE_OBSERVATION = "live_observation"
    OBSERVED_EXECUTABLE = "observed_executable"
    ACCOUNT_READ_ONLY = "account_read_only"
    SUPERVISED_EXECUTION_CAPABLE = "supervised_execution_capable"


class ProviderRequalificationTrigger(str, Enum):
    """Changes that invalidate an unchanged certification artifact."""

    CAPABILITY_PROFILE_CHANGED = "capability_profile_changed"
    CAPABILITY_MANIFEST_CHANGED = "capability_manifest_changed"
    INTEGRATION_EVIDENCE_CHANGED = "integration_evidence_changed"
    ADAPTER_CODE_CHANGED = "adapter_code_changed"
    ADAPTER_CONFIG_CHANGED = "adapter_config_changed"
    TEST_EVIDENCE_CHANGED = "test_evidence_changed"


_MANDATORY_REQUALIFICATION_TRIGGERS = tuple(ProviderRequalificationTrigger)


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderCertificationError(f"{field} must be a non-empty trimmed string")
    return value


def _sha256(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ProviderCertificationError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return text


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProviderCertificationError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderCertificationError(f"{field} must include a timezone offset")
    return parsed


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ProviderCertificationError(f"{field} must be a positive integer")
    return value


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True, slots=True)
class ProviderCertificationEvidenceRef:
    """Secret-free identity of one exact qualification result.

    The binding fields make a test result non-transferable across a different provider
    profile, integration channel, account, adapter code, or adapter configuration.  This is
    still evidence metadata, not a cryptographic attestation service or execution token.
    """

    kind: str
    evidence_id: str
    evidence_sha256: str
    observed_at: str
    tested_mode: ProviderTestMode
    account_scope: str
    profile_id: str
    integration_evidence_id: str
    capability_manifest_ref: str
    capability_manifest_version: int
    capability_manifest_sha256: str
    adapter_code_ref: str
    adapter_code_sha256: str
    adapter_config_ref: str
    adapter_config_sha256: str

    def __post_init__(self) -> None:
        _text(self.kind, "kind")
        _text(self.evidence_id, "evidence_id")
        _sha256(self.evidence_sha256, "evidence_sha256")
        _timestamp(self.observed_at, "observed_at")
        if type(self.tested_mode) is not ProviderTestMode:
            raise ProviderCertificationError(
                "tested_mode must be an exact ProviderTestMode value"
            )
        _text(self.account_scope, "account_scope")
        _sha256(self.profile_id, "profile_id")
        _sha256(self.integration_evidence_id, "integration_evidence_id")
        _text(self.capability_manifest_ref, "capability_manifest_ref")
        _positive_int(self.capability_manifest_version, "capability_manifest_version")
        _sha256(self.capability_manifest_sha256, "capability_manifest_sha256")
        _text(self.adapter_code_ref, "adapter_code_ref")
        _sha256(self.adapter_code_sha256, "adapter_code_sha256")
        _text(self.adapter_config_ref, "adapter_config_ref")
        _sha256(self.adapter_config_sha256, "adapter_config_sha256")

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "account_scope": self.account_scope,
            "adapter_code_ref": self.adapter_code_ref,
            "adapter_code_sha256": self.adapter_code_sha256,
            "adapter_config_ref": self.adapter_config_ref,
            "adapter_config_sha256": self.adapter_config_sha256,
            "evidence_id": self.evidence_id,
            "evidence_sha256": self.evidence_sha256,
            "integration_evidence_id": self.integration_evidence_id,
            "capability_manifest_ref": self.capability_manifest_ref,
            "capability_manifest_version": self.capability_manifest_version,
            "capability_manifest_sha256": self.capability_manifest_sha256,
            "kind": self.kind,
            "observed_at": self.observed_at,
            "profile_id": self.profile_id,
            "tested_mode": self.tested_mode.value,
        }


@dataclass(frozen=True, slots=True)
class ProviderCapabilityProjection:
    """Explicit certification projection for one capability, including UNKNOWN."""

    capability: BookmakerCapability
    state: BookmakerCapabilityState

    def __post_init__(self) -> None:
        if type(self.capability) is not BookmakerCapability:
            raise ProviderCertificationError(
                "capability must be an exact BookmakerCapability value"
            )
        if type(self.state) is not BookmakerCapabilityState:
            raise ProviderCertificationError(
                "state must be an exact BookmakerCapabilityState value"
            )

    def to_canonical_dict(self) -> dict[str, str]:
        return {"capability": self.capability.value, "state": self.state.value}


def _derive_allowed_uses(
    tested_modes: tuple[ProviderTestMode, ...],
    capability_states: tuple[ProviderCapabilityProjection, ...],
) -> tuple[ProviderCertifiedUse, ...]:
    states = {item.capability: item.state for item in capability_states}

    def supports(capability: BookmakerCapability) -> bool:
        return states.get(capability) is BookmakerCapabilityState.SUPPORTED

    tested = set(tested_modes)
    allowed: set[ProviderCertifiedUse] = set()
    quote_read = supports(BookmakerCapability.PREMATCH_QUOTES_READ) or supports(
        BookmakerCapability.LIVE_QUOTES_READ
    )
    account_bound = supports(BookmakerCapability.ACCOUNT_IDENTITY_READ)
    limits_known = supports(BookmakerCapability.LIMITS_READ)

    if ProviderTestMode.REPLAY in tested:
        allowed.add(ProviderCertifiedUse.REPLAY)
    if ProviderTestMode.LIVE_OBSERVATION in tested and quote_read:
        allowed.add(ProviderCertifiedUse.LIVE_OBSERVATION)
    if (
        ProviderTestMode.OBSERVED_EXECUTABLE in tested
        and quote_read
        and account_bound
        and limits_known
        and supports(BookmakerCapability.BETSLIP_READ)
    ):
        allowed.add(ProviderCertifiedUse.OBSERVED_EXECUTABLE)
    account_detail_read = any(
        supports(capability)
        for capability in (
            BookmakerCapability.BALANCE_READ,
            BookmakerCapability.LIMITS_READ,
            BookmakerCapability.OPEN_POSITIONS_READ,
            BookmakerCapability.SETTLED_POSITIONS_READ,
            BookmakerCapability.BET_READBACK,
        )
    )
    if (
        ProviderTestMode.ACCOUNT_READ_ONLY in tested
        and account_bound
        and account_detail_read
    ):
        allowed.add(ProviderCertifiedUse.ACCOUNT_READ_ONLY)
    if (
        ProviderTestMode.SUPERVISED_EXECUTION in tested
        and quote_read
        and account_bound
        and limits_known
        and supports(BookmakerCapability.PLACE_BET)
        and supports(BookmakerCapability.BET_READBACK)
        and supports(BookmakerCapability.OPEN_POSITIONS_READ)
    ):
        allowed.add(ProviderCertifiedUse.SUPERVISED_EXECUTION_CAPABLE)
    return tuple(sorted(allowed, key=lambda item: item.value))


def _normalize_modes(modes: tuple[ProviderTestMode, ...]) -> tuple[ProviderTestMode, ...]:
    if type(modes) is not tuple or not modes:
        raise ProviderCertificationError("tested_modes must be a non-empty tuple")
    if any(type(mode) is not ProviderTestMode for mode in modes):
        raise ProviderCertificationError(
            "tested_modes must contain exact ProviderTestMode values"
        )
    return tuple(sorted(set(modes), key=lambda item: item.value))


def _normalize_evidence_refs(
    evidence_refs: tuple[ProviderCertificationEvidenceRef, ...],
) -> tuple[ProviderCertificationEvidenceRef, ...]:
    if type(evidence_refs) is not tuple or not evidence_refs:
        raise ProviderCertificationError("evidence_refs must be a non-empty tuple")
    if any(type(item) is not ProviderCertificationEvidenceRef for item in evidence_refs):
        raise ProviderCertificationError(
            "evidence_refs must contain exact ProviderCertificationEvidenceRef values"
        )
    normalized = tuple(
        sorted(
            set(evidence_refs),
            key=lambda item: (
                item.tested_mode.value,
                item.kind,
                item.evidence_id,
                item.evidence_sha256,
                item.observed_at,
            ),
        )
    )
    identities = [(item.kind, item.evidence_id) for item in normalized]
    if len(identities) != len(set(identities)):
        raise ProviderCertificationError(
            "evidence_refs cannot contain conflicting versions of one evidence identity"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class ProviderCertificationArtifact:
    """Content-addressed technical certification bound to exact existing evidence."""

    venue_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    profile_version: int
    profile_id: str
    profile_observed_at: str
    capability_manifest_ref: str
    capability_manifest_version: int
    capability_manifest_sha256: str
    integration_evidence_id: str
    integration_kind: BookmakerIntegrationKind
    integration_observed_at: str
    adapter_code_ref: str
    adapter_code_sha256: str
    adapter_config_ref: str
    adapter_config_sha256: str
    tested_account_scope: str
    tested_modes: tuple[ProviderTestMode, ...]
    capability_states: tuple[ProviderCapabilityProjection, ...]
    evidence_refs: tuple[ProviderCertificationEvidenceRef, ...]
    limitations: tuple[str, ...]
    allowed_uses: tuple[ProviderCertifiedUse, ...]
    requalification_triggers: tuple[ProviderRequalificationTrigger, ...]
    issued_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        for field in (
            "venue_id",
            "account_id",
            "adapter_id",
            "adapter_version",
            "adapter_code_ref",
            "adapter_config_ref",
            "tested_account_scope",
        ):
            _text(getattr(self, field), field)
        _positive_int(self.profile_version, "profile_version")
        _sha256(self.profile_id, "profile_id")
        profile_at = _timestamp(self.profile_observed_at, "profile_observed_at")
        _text(self.capability_manifest_ref, "capability_manifest_ref")
        _positive_int(self.capability_manifest_version, "capability_manifest_version")
        _sha256(self.capability_manifest_sha256, "capability_manifest_sha256")
        _sha256(self.integration_evidence_id, "integration_evidence_id")
        integration_at = _timestamp(self.integration_observed_at, "integration_observed_at")
        if integration_at < profile_at:
            raise ProviderCertificationError(
                "integration_observed_at cannot predate profile_observed_at"
            )
        _sha256(self.adapter_code_sha256, "adapter_code_sha256")
        _sha256(self.adapter_config_sha256, "adapter_config_sha256")
        issued_at = _timestamp(self.issued_at, "issued_at")
        if issued_at < integration_at:
            raise ProviderCertificationError(
                "issued_at cannot predate integration evidence"
            )
        if type(self.integration_kind) is not BookmakerIntegrationKind:
            raise ProviderCertificationError(
                "integration_kind must be an exact BookmakerIntegrationKind value"
            )
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ProviderCertificationError("schema_version must be exactly 1")
        if self.tested_account_scope != self.account_id:
            raise ProviderCertificationError(
                "tested_account_scope must equal the exact capability-profile account_id"
            )
        self._validate_modes()
        self._validate_capability_states()
        self._validate_evidence_refs(integration_at, issued_at)
        self._validate_limitations()
        self._validate_allowed_uses()
        if self.requalification_triggers != _MANDATORY_REQUALIFICATION_TRIGGERS:
            raise ProviderCertificationError(
                "requalification_triggers must contain the complete canonical trigger set"
            )

    def _validate_modes(self) -> None:
        expected = _normalize_modes(self.tested_modes)
        if self.tested_modes != expected:
            raise ProviderCertificationError(
                "tested_modes must be unique and in canonical order"
            )

    def _validate_capability_states(self) -> None:
        if type(self.capability_states) is not tuple:
            raise ProviderCertificationError("capability_states must be a tuple")
        if any(
            type(item) is not ProviderCapabilityProjection
            for item in self.capability_states
        ):
            raise ProviderCertificationError(
                "capability_states must contain exact ProviderCapabilityProjection values"
            )
        expected = tuple(sorted(BookmakerCapability, key=lambda item: item.value))
        actual = tuple(item.capability for item in self.capability_states)
        if actual != expected:
            raise ProviderCertificationError(
                "capability_states must contain every BookmakerCapability exactly once "
                "in canonical order"
            )

    def _validate_evidence_refs(
        self,
        integration_at: datetime,
        issued_at: datetime,
    ) -> None:
        expected = _normalize_evidence_refs(self.evidence_refs)
        if self.evidence_refs != expected:
            raise ProviderCertificationError(
                "evidence_refs must be unique and in canonical order"
            )
        expected_binding = (
            self.tested_account_scope,
            self.profile_id,
            self.integration_evidence_id,
            self.capability_manifest_ref,
            self.capability_manifest_version,
            self.capability_manifest_sha256,
            self.adapter_code_ref,
            self.adapter_code_sha256,
            self.adapter_config_ref,
            self.adapter_config_sha256,
        )
        modes_with_evidence: set[ProviderTestMode] = set()
        for item in self.evidence_refs:
            actual_binding = (
                item.account_scope,
                item.profile_id,
                item.integration_evidence_id,
                item.capability_manifest_ref,
                item.capability_manifest_version,
                item.capability_manifest_sha256,
                item.adapter_code_ref,
                item.adapter_code_sha256,
                item.adapter_config_ref,
                item.adapter_config_sha256,
            )
            if actual_binding != expected_binding:
                raise ProviderCertificationError(
                    "qualification evidence does not bind the exact certification identity"
                )
            observed_at = _timestamp(item.observed_at, "evidence_refs.observed_at")
            if observed_at < integration_at:
                raise ProviderCertificationError(
                    "qualification evidence cannot predate integration evidence"
                )
            if observed_at > issued_at:
                raise ProviderCertificationError(
                    "qualification evidence cannot postdate issued_at"
                )
            modes_with_evidence.add(item.tested_mode)
        if modes_with_evidence != set(self.tested_modes):
            raise ProviderCertificationError(
                "every tested_mode must have exact bound evidence and no untested mode evidence"
            )

    def _validate_limitations(self) -> None:
        if type(self.limitations) is not tuple:
            raise ProviderCertificationError("limitations must be a tuple")
        for item in self.limitations:
            _text(item, "limitations item")
        expected = tuple(sorted(set(self.limitations)))
        if self.limitations != expected:
            raise ProviderCertificationError(
                "limitations must be unique and in canonical order"
            )

    def _validate_allowed_uses(self) -> None:
        if type(self.allowed_uses) is not tuple:
            raise ProviderCertificationError("allowed_uses must be a tuple")
        if any(type(item) is not ProviderCertifiedUse for item in self.allowed_uses):
            raise ProviderCertificationError(
                "allowed_uses must contain exact ProviderCertifiedUse values"
            )
        if self.allowed_uses != _derive_allowed_uses(
            self.tested_modes,
            self.capability_states,
        ):
            raise ProviderCertificationError(
                "allowed_uses must equal the mechanically derived tested/capability projection"
            )

    @property
    def artifact_id(self) -> str:
        return sha256(_canonical_json(self.to_canonical_dict()).encode("utf-8")).hexdigest()

    @property
    def unknown_capabilities(self) -> tuple[BookmakerCapability, ...]:
        return tuple(
            item.capability
            for item in self.capability_states
            if item.state is BookmakerCapabilityState.UNKNOWN
        )

    @property
    def unsupported_capabilities(self) -> tuple[BookmakerCapability, ...]:
        return tuple(
            item.capability
            for item in self.capability_states
            if item.state is BookmakerCapabilityState.UNSUPPORTED
        )

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "adapter_code_ref": self.adapter_code_ref,
            "adapter_code_sha256": self.adapter_code_sha256,
            "adapter_config_ref": self.adapter_config_ref,
            "adapter_config_sha256": self.adapter_config_sha256,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "allowed_uses": [item.value for item in self.allowed_uses],
            "capability_manifest_ref": self.capability_manifest_ref,
            "capability_manifest_sha256": self.capability_manifest_sha256,
            "capability_manifest_version": self.capability_manifest_version,
            "capability_states": [item.to_canonical_dict() for item in self.capability_states],
            "evidence_refs": [item.to_canonical_dict() for item in self.evidence_refs],
            "integration_evidence_id": self.integration_evidence_id,
            "integration_kind": self.integration_kind.value,
            "integration_observed_at": self.integration_observed_at,
            "issued_at": self.issued_at,
            "limitations": list(self.limitations),
            "not_proven_capabilities": [
                item.value for item in self.unknown_capabilities
            ],
            "profile_id": self.profile_id,
            "profile_observed_at": self.profile_observed_at,
            "profile_version": self.profile_version,
            "requalification_triggers": [
                item.value for item in self.requalification_triggers
            ],
            "schema_version": self.schema_version,
            "tested_account_scope": self.tested_account_scope,
            "tested_modes": [item.value for item in self.tested_modes],
            "venue_id": self.venue_id,
        }

    def require_use(self, use: ProviderCertifiedUse) -> None:
        if type(use) is not ProviderCertifiedUse:
            raise ProviderCertificationError(
                "use must be an exact ProviderCertifiedUse value"
            )
        if use not in self.allowed_uses:
            raise ProviderCertificationError(
                f"provider certification does not prove technical use {use.value}"
            )

    def verify_current(
        self,
        profile: BookmakerCapabilityProfile,
        integration: BookmakerIntegrationEvidence,
        *,
        capability_manifest_ref: str,
        capability_manifest_version: int,
        capability_manifest_sha256: str,
        adapter_code_ref: str,
        adapter_code_sha256: str,
        adapter_config_ref: str,
        adapter_config_sha256: str,
        evidence_refs: tuple[ProviderCertificationEvidenceRef, ...],
    ) -> None:
        """Fail closed when any identity covered by requalification changes."""

        if type(profile) is not BookmakerCapabilityProfile:
            raise ProviderCertificationError(
                "profile must be an exact BookmakerCapabilityProfile"
            )
        if type(integration) is not BookmakerIntegrationEvidence:
            raise ProviderCertificationError(
                "integration must be exact BookmakerIntegrationEvidence"
            )
        try:
            integration.verify_profile(profile)
        except ValueError as exc:
            raise ProviderCertificationError(
                "integration evidence no longer binds the supplied capability profile"
            ) from exc
        expected_identity = (
            self.venue_id,
            self.account_id,
            self.adapter_id,
            self.adapter_version,
            self.profile_version,
            self.profile_id,
            self.profile_observed_at,
            self.integration_evidence_id,
            self.integration_kind,
            self.integration_observed_at,
        )
        actual_identity = (
            profile.venue_id,
            profile.account_id,
            profile.adapter_id,
            profile.adapter_version,
            profile.profile_version,
            profile.profile_id,
            profile.observed_at,
            integration.evidence_id,
            integration.integration_kind,
            integration.observed_at,
        )
        if actual_identity != expected_identity:
            raise ProviderCertificationError(
                "provider certification requires requalification after profile/integration drift"
            )
        expected_states = tuple(
            ProviderCapabilityProjection(
                capability=capability,
                state=profile.state_of(capability),
            )
            for capability in sorted(BookmakerCapability, key=lambda item: item.value)
        )
        if expected_states != self.capability_states:
            raise ProviderCertificationError(
                "provider certification capability projection does not match current profile"
            )
        current_manifest = (
            _text(capability_manifest_ref, "capability_manifest_ref"),
            _positive_int(capability_manifest_version, "capability_manifest_version"),
            _sha256(capability_manifest_sha256, "capability_manifest_sha256"),
        )
        certified_manifest = (
            self.capability_manifest_ref,
            self.capability_manifest_version,
            self.capability_manifest_sha256,
        )
        if current_manifest != certified_manifest:
            raise ProviderCertificationError(
                "provider certification requires requalification after capability manifest drift"
            )
        if _text(adapter_code_ref, "adapter_code_ref") != self.adapter_code_ref:
            raise ProviderCertificationError(
                "provider certification requires requalification after adapter code reference drift"
            )
        if _sha256(adapter_code_sha256, "adapter_code_sha256") != self.adapter_code_sha256:
            raise ProviderCertificationError(
                "provider certification requires requalification after adapter code drift"
            )
        if _text(adapter_config_ref, "adapter_config_ref") != self.adapter_config_ref:
            raise ProviderCertificationError(
                "provider certification requires requalification after adapter config reference drift"
            )
        if _sha256(adapter_config_sha256, "adapter_config_sha256") != self.adapter_config_sha256:
            raise ProviderCertificationError(
                "provider certification requires requalification after adapter config drift"
            )
        if _normalize_evidence_refs(evidence_refs) != self.evidence_refs:
            raise ProviderCertificationError(
                "provider certification requires requalification after test evidence drift"
            )


def build_provider_certification(
    profile: BookmakerCapabilityProfile,
    integration: BookmakerIntegrationEvidence,
    *,
    capability_manifest_ref: str,
    capability_manifest_version: int,
    capability_manifest_sha256: str,
    adapter_code_ref: str,
    adapter_code_sha256: str,
    adapter_config_ref: str,
    adapter_config_sha256: str,
    tested_modes: tuple[ProviderTestMode, ...],
    evidence_refs: tuple[ProviderCertificationEvidenceRef, ...],
    limitations: tuple[str, ...] = (),
    issued_at: str,
) -> ProviderCertificationArtifact:
    """Build one deterministic fail-closed certification from existing exact evidence."""

    if type(profile) is not BookmakerCapabilityProfile:
        raise ProviderCertificationError(
            "profile must be an exact BookmakerCapabilityProfile"
        )
    if type(integration) is not BookmakerIntegrationEvidence:
        raise ProviderCertificationError(
            "integration must be exact BookmakerIntegrationEvidence"
        )
    try:
        integration.verify_profile(profile)
    except ValueError as exc:
        raise ProviderCertificationError(
            "integration evidence does not bind the supplied capability profile"
        ) from exc
    issued = _timestamp(issued_at, "issued_at")
    profile_at = _timestamp(profile.observed_at, "profile.observed_at")
    integration_at = _timestamp(integration.observed_at, "integration.observed_at")
    if integration_at < profile_at:
        raise ProviderCertificationError(
            "integration evidence cannot predate capability profile"
        )
    if issued < integration_at:
        raise ProviderCertificationError("issued_at cannot predate integration evidence")

    manifest_ref = _text(capability_manifest_ref, "capability_manifest_ref")
    manifest_version = _positive_int(
        capability_manifest_version,
        "capability_manifest_version",
    )
    manifest_sha = _sha256(capability_manifest_sha256, "capability_manifest_sha256")
    code_ref = _text(adapter_code_ref, "adapter_code_ref")
    code_sha = _sha256(adapter_code_sha256, "adapter_code_sha256")
    config_ref = _text(adapter_config_ref, "adapter_config_ref")
    config_sha = _sha256(adapter_config_sha256, "adapter_config_sha256")
    modes = _normalize_modes(tested_modes)
    refs = _normalize_evidence_refs(evidence_refs)
    normalized_limitations = tuple(
        sorted({_text(item, "limitations item") for item in limitations})
    )
    states = tuple(
        ProviderCapabilityProjection(
            capability=capability,
            state=profile.state_of(capability),
        )
        for capability in sorted(BookmakerCapability, key=lambda item: item.value)
    )
    allowed = _derive_allowed_uses(modes, states)
    return ProviderCertificationArtifact(
        venue_id=profile.venue_id,
        account_id=profile.account_id,
        adapter_id=profile.adapter_id,
        adapter_version=profile.adapter_version,
        profile_version=profile.profile_version,
        profile_id=profile.profile_id,
        profile_observed_at=profile.observed_at,
        capability_manifest_ref=manifest_ref,
        capability_manifest_version=manifest_version,
        capability_manifest_sha256=manifest_sha,
        integration_evidence_id=integration.evidence_id,
        integration_kind=integration.integration_kind,
        integration_observed_at=integration.observed_at,
        adapter_code_ref=code_ref,
        adapter_code_sha256=code_sha,
        adapter_config_ref=config_ref,
        adapter_config_sha256=config_sha,
        tested_account_scope=profile.account_id,
        tested_modes=modes,
        capability_states=states,
        evidence_refs=refs,
        limitations=normalized_limitations,
        allowed_uses=allowed,
        requalification_triggers=_MANDATORY_REQUALIFICATION_TRIGGERS,
        issued_at=issued_at,
    )
