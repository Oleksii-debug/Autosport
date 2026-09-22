"""Fail-closed evidence matrix for bookmaker/provider capability truth.

This module composes the existing bookmaker capability profile and integration-channel
contracts without widening either contract into execution permission.

Evidence levels are UNKNOWN, DOCUMENTED, ACCOUNT_OBSERVED, MARKET_OBSERVED,
PAPER_OR_SHADOW_PROVEN, EXECUTION_OBSERVED, and RECONCILED.

Only DOCUMENTED has a product issuer in this module. Higher levels are represented so
provider-specific authorities can integrate later, but caller-constructed rows at those
levels remain non-authoritative and evaluate to UNKNOWN. An official endpoint, a
SUPPORTED capability profile, or governance evidence therefore cannot mint account,
live, write, reconciliation, or real-money authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, IntEnum
from hashlib import sha256
import json
from threading import RLock
from urllib.parse import urlparse
from weakref import ReferenceType, ref

from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from .bookmaker_integration_boundary import (
    BookmakerIntegrationEvidence,
    BookmakerIntegrationKind,
)


_SCHEMA = "autosport.bookmaker_capability_evidence_matrix"
_SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")


class BookmakerCapabilityEvidenceError(ValueError):
    """Raised when capability evidence is malformed or would widen authority."""


class CapabilityEvidenceLevel(IntEnum):
    UNKNOWN = 0
    DOCUMENTED = 1
    ACCOUNT_OBSERVED = 2
    MARKET_OBSERVED = 3
    PAPER_OR_SHADOW_PROVEN = 4
    EXECUTION_OBSERVED = 5
    RECONCILED = 6


class CapabilityDirection(str, Enum):
    READ = "READ"
    WRITE = "WRITE"


class CapabilityEvidenceSource(str, Enum):
    DOCUMENTATION = "DOCUMENTATION"
    AUTHENTICATED_ACCOUNT_READ = "AUTHENTICATED_ACCOUNT_READ"
    LIVE_MARKET_READ = "LIVE_MARKET_READ"
    PAPER_OR_SHADOW = "PAPER_OR_SHADOW"
    EXECUTION_RECEIPT = "EXECUTION_RECEIPT"
    RECONCILIATION = "RECONCILIATION"


_SOURCE_LEVEL = {
    CapabilityEvidenceSource.DOCUMENTATION: CapabilityEvidenceLevel.DOCUMENTED,
    CapabilityEvidenceSource.AUTHENTICATED_ACCOUNT_READ:
        CapabilityEvidenceLevel.ACCOUNT_OBSERVED,
    CapabilityEvidenceSource.LIVE_MARKET_READ:
        CapabilityEvidenceLevel.MARKET_OBSERVED,
    CapabilityEvidenceSource.PAPER_OR_SHADOW:
        CapabilityEvidenceLevel.PAPER_OR_SHADOW_PROVEN,
    CapabilityEvidenceSource.EXECUTION_RECEIPT:
        CapabilityEvidenceLevel.EXECUTION_OBSERVED,
    CapabilityEvidenceSource.RECONCILIATION:
        CapabilityEvidenceLevel.RECONCILED,
}

_WRITE_CAPABILITIES = frozenset(
    {
        BookmakerCapability.PLACE_BET,
        BookmakerCapability.CASHOUT,
        BookmakerCapability.CANCEL_BET,
    }
)

_READ_ONLY_SOURCES = frozenset(
    {
        CapabilityEvidenceSource.AUTHENTICATED_ACCOUNT_READ,
        CapabilityEvidenceSource.LIVE_MARKET_READ,
    }
)


def _text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise BookmakerCapabilityEvidenceError(
            f"{field} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BookmakerCapabilityEvidenceError(
            f"{field} must be valid UTF-8 text"
        ) from exc
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _sha(value: object, field: str) -> str:
    raw = _text(value, field)
    if len(raw) != 64 or any(character not in _HEX for character in raw):
        raise BookmakerCapabilityEvidenceError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return raw


def _instant(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BookmakerCapabilityEvidenceError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BookmakerCapabilityEvidenceError(
            f"{field} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_instant(value: object, field: str) -> str:
    return _instant(value, field).isoformat().replace("+00:00", "Z")


def _optional_instant(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _canonical_instant(value, field)


def _https_url(value: object, field: str) -> str:
    raw = _text(value, field)
    parsed = urlparse(raw)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise BookmakerCapabilityEvidenceError(
            f"{field} must be a fragment-free HTTPS URL without credentials"
        )
    return raw


def _vocabulary(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise BookmakerCapabilityEvidenceError(
            "provider_status_vocabulary must be a tuple"
        )
    normalized = tuple(
        _text(item, "provider_status_vocabulary item") for item in value
    )
    if normalized != tuple(sorted(set(normalized))):
        raise BookmakerCapabilityEvidenceError(
            "provider_status_vocabulary must be sorted and unique"
        )
    return normalized


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BookmakerCapabilityEvidenceCell:
    """One exact evidence row. Construction alone never grants product authority."""

    provider_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    profile_id: str
    integration_evidence_id: str
    integration_kind: BookmakerIntegrationKind
    capability: BookmakerCapability
    technical_state: BookmakerCapabilityState
    operation: str
    direction: CapabilityDirection
    source_kind: CapabilityEvidenceSource
    evidence_level: CapabilityEvidenceLevel
    observed_at: str
    evidence_artifact_sha256: str
    expires_or_revalidate_at: str
    official_doc_url: str | None = None
    doc_observed_at: str | None = None
    sport_scope: str | None = None
    market_scope: str | None = None
    jurisdiction_scope: str | None = None
    auth_mode: str | None = None
    pagination_semantics: str | None = None
    rate_semantics: str | None = None
    currency: str | None = None
    odds_format: str | None = None
    exchange_mode: str | None = None
    provider_status_vocabulary: tuple[str, ...] = ()
    freshness_semantics: str | None = None
    idempotency_semantics: str | None = None
    settlement_revision_semantics: str | None = None
    negative_or_unknown_reason: str | None = None
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value, field in (
            (self.provider_id, "provider_id"),
            (self.account_id, "account_id"),
            (self.adapter_id, "adapter_id"),
            (self.adapter_version, "adapter_version"),
            (self.operation, "operation"),
        ):
            _text(value, field)
        _sha(self.profile_id, "profile_id")
        _sha(self.integration_evidence_id, "integration_evidence_id")
        if type(self.integration_kind) is not BookmakerIntegrationKind:
            raise BookmakerCapabilityEvidenceError(
                "integration_kind must be exact BookmakerIntegrationKind"
            )
        if type(self.capability) is not BookmakerCapability:
            raise BookmakerCapabilityEvidenceError(
                "capability must be exact BookmakerCapability"
            )
        if type(self.technical_state) is not BookmakerCapabilityState:
            raise BookmakerCapabilityEvidenceError(
                "technical_state must be exact BookmakerCapabilityState"
            )
        if type(self.direction) is not CapabilityDirection:
            raise BookmakerCapabilityEvidenceError(
                "direction must be exact CapabilityDirection"
            )
        if type(self.source_kind) is not CapabilityEvidenceSource:
            raise BookmakerCapabilityEvidenceError(
                "source_kind must be exact CapabilityEvidenceSource"
            )
        if type(self.evidence_level) is not CapabilityEvidenceLevel:
            raise BookmakerCapabilityEvidenceError(
                "evidence_level must be exact CapabilityEvidenceLevel"
            )
        if self.evidence_level is not _SOURCE_LEVEL[self.source_kind]:
            raise BookmakerCapabilityEvidenceError(
                "evidence_level must be mechanically derived from source_kind"
            )
        if (self.capability in _WRITE_CAPABILITIES) != (
            self.direction is CapabilityDirection.WRITE
        ):
            raise BookmakerCapabilityEvidenceError(
                "capability and READ/WRITE direction disagree"
            )
        if (
            self.source_kind in _READ_ONLY_SOURCES
            and self.direction is not CapabilityDirection.READ
        ):
            raise BookmakerCapabilityEvidenceError(
                "read evidence source cannot substantiate a WRITE operation"
            )

        observed = _instant(self.observed_at, "observed_at")
        expiry = _instant(
            self.expires_or_revalidate_at,
            "expires_or_revalidate_at",
        )
        if expiry <= observed:
            raise BookmakerCapabilityEvidenceError(
                "expires_or_revalidate_at must be later than observed_at"
            )
        _sha(self.evidence_artifact_sha256, "evidence_artifact_sha256")

        for field in (
            "sport_scope",
            "market_scope",
            "jurisdiction_scope",
            "auth_mode",
            "pagination_semantics",
            "rate_semantics",
            "currency",
            "odds_format",
            "exchange_mode",
            "freshness_semantics",
            "idempotency_semantics",
            "settlement_revision_semantics",
            "negative_or_unknown_reason",
        ):
            _optional_text(getattr(self, field), field)
        _vocabulary(self.provider_status_vocabulary)

        if self.official_doc_url is not None:
            _https_url(self.official_doc_url, "official_doc_url")
        doc_observed = (
            None
            if self.doc_observed_at is None
            else _instant(self.doc_observed_at, "doc_observed_at")
        )
        if (self.official_doc_url is None) != (doc_observed is None):
            raise BookmakerCapabilityEvidenceError(
                "official_doc_url and doc_observed_at must be supplied together"
            )
        if doc_observed is not None and doc_observed > observed:
            raise BookmakerCapabilityEvidenceError(
                "doc_observed_at cannot be later than evidence observed_at"
            )

        if self.evidence_level is CapabilityEvidenceLevel.DOCUMENTED:
            if self.official_doc_url is None:
                raise BookmakerCapabilityEvidenceError(
                    "DOCUMENTED evidence requires official documentation"
                )
        elif self.evidence_level >= CapabilityEvidenceLevel.ACCOUNT_OBSERVED:
            if self.auth_mode is None:
                raise BookmakerCapabilityEvidenceError(
                    "account-or-higher evidence requires explicit auth_mode"
                )

        if self.evidence_level >= CapabilityEvidenceLevel.MARKET_OBSERVED:
            if self.sport_scope is None or self.market_scope is None:
                raise BookmakerCapabilityEvidenceError(
                    "market-or-higher evidence requires exact sport_scope and market_scope"
                )
        if self.evidence_level >= CapabilityEvidenceLevel.EXECUTION_OBSERVED:
            if self.direction is not CapabilityDirection.WRITE:
                raise BookmakerCapabilityEvidenceError(
                    "execution evidence must bind one exact WRITE operation"
                )
            if self.idempotency_semantics is None:
                raise BookmakerCapabilityEvidenceError(
                    "execution evidence requires idempotency/customer-ref semantics"
                )
        if self.evidence_level is CapabilityEvidenceLevel.RECONCILED:
            if self.settlement_revision_semantics is None:
                raise BookmakerCapabilityEvidenceError(
                    "reconciled evidence requires settlement/revision semantics"
                )
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise BookmakerCapabilityEvidenceError(
                f"schema_version must be exactly {_SCHEMA_VERSION}"
            )

    @property
    def evidence_id(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @property
    def execution_authorized(self) -> bool:
        return False

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "profile_id": self.profile_id,
            "integration_evidence_id": self.integration_evidence_id,
            "integration_kind": self.integration_kind.value,
            "capability": self.capability.value,
            "technical_state": self.technical_state.value,
            "operation": self.operation,
            "direction": self.direction.value,
            "source_kind": self.source_kind.value,
            "evidence_level": self.evidence_level.name,
            "observed_at": _canonical_instant(self.observed_at, "observed_at"),
            "evidence_artifact_sha256": self.evidence_artifact_sha256,
            "expires_or_revalidate_at": _canonical_instant(
                self.expires_or_revalidate_at,
                "expires_or_revalidate_at",
            ),
            "official_doc_url": self.official_doc_url,
            "doc_observed_at": _optional_instant(
                self.doc_observed_at,
                "doc_observed_at",
            ),
            "sport_scope": self.sport_scope,
            "market_scope": self.market_scope,
            "jurisdiction_scope": self.jurisdiction_scope,
            "auth_mode": self.auth_mode,
            "pagination_semantics": self.pagination_semantics,
            "rate_semantics": self.rate_semantics,
            "currency": self.currency,
            "odds_format": self.odds_format,
            "exchange_mode": self.exchange_mode,
            "provider_status_vocabulary": list(self.provider_status_vocabulary),
            "freshness_semantics": self.freshness_semantics,
            "idempotency_semantics": self.idempotency_semantics,
            "settlement_revision_semantics": self.settlement_revision_semantics,
            "negative_or_unknown_reason": self.negative_or_unknown_reason,
        }


@dataclass(frozen=True, slots=True)
class BookmakerCapabilityEvidenceEvaluation:
    evidence_id: str
    declared_level: CapabilityEvidenceLevel
    effective_level: CapabilityEvidenceLevel
    reason: str
    evaluated_at: str
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        _sha(self.evidence_id, "evidence_id")
        if type(self.declared_level) is not CapabilityEvidenceLevel:
            raise BookmakerCapabilityEvidenceError(
                "declared_level must be exact CapabilityEvidenceLevel"
            )
        if type(self.effective_level) is not CapabilityEvidenceLevel:
            raise BookmakerCapabilityEvidenceError(
                "effective_level must be exact CapabilityEvidenceLevel"
            )
        _text(self.reason, "reason")
        _instant(self.evaluated_at, "evaluated_at")
        if self.execution_authorized is not False:
            raise BookmakerCapabilityEvidenceError(
                "capability evidence matrix cannot authorize execution"
            )


@dataclass(frozen=True, slots=True)
class _IssuedEvidence:
    value_ref: ReferenceType[BookmakerCapabilityEvidenceCell]
    evidence_id: str
    maximum_level: CapabilityEvidenceLevel


_ISSUED_LOCK = RLock()
_ISSUED: dict[int, _IssuedEvidence] = {}


def _register_issued(
    value: BookmakerCapabilityEvidenceCell,
    *,
    maximum_level: CapabilityEvidenceLevel,
) -> BookmakerCapabilityEvidenceCell:
    identifier = id(value)

    def cleanup(dead_ref: ReferenceType[BookmakerCapabilityEvidenceCell]) -> None:
        with _ISSUED_LOCK:
            current = _ISSUED.get(identifier)
            if current is not None and current.value_ref is dead_ref:
                _ISSUED.pop(identifier, None)

    value_ref = ref(value, cleanup)
    with _ISSUED_LOCK:
        _ISSUED[identifier] = _IssuedEvidence(
            value_ref=value_ref,
            evidence_id=value.evidence_id,
            maximum_level=maximum_level,
        )
    return value


def _issued_record(value: object) -> _IssuedEvidence | None:
    if type(value) is not BookmakerCapabilityEvidenceCell:
        return None
    with _ISSUED_LOCK:
        record = _ISSUED.get(id(value))
    if record is None or record.value_ref() is not value:
        return None
    if record.evidence_id != value.evidence_id:
        return None
    return record


def is_product_issued_capability_evidence(value: object) -> bool:
    """Return whether this exact immutable object has product issuance."""

    return _issued_record(value) is not None


def issue_documented_capability_evidence(
    profile: BookmakerCapabilityProfile,
    integration: BookmakerIntegrationEvidence,
    *,
    capability: BookmakerCapability,
    operation: str,
    direction: CapabilityDirection,
    official_doc_url: str,
    doc_observed_at: str,
    observed_at: str,
    evidence_artifact_sha256: str,
    expires_or_revalidate_at: str,
    sport_scope: str | None = None,
    market_scope: str | None = None,
    jurisdiction_scope: str | None = None,
    pagination_semantics: str | None = None,
    rate_semantics: str | None = None,
    currency: str | None = None,
    odds_format: str | None = None,
    exchange_mode: str | None = None,
    provider_status_vocabulary: tuple[str, ...] = (),
    freshness_semantics: str | None = None,
    idempotency_semantics: str | None = None,
    settlement_revision_semantics: str | None = None,
) -> BookmakerCapabilityEvidenceCell:
    """Issue L1 from an exact profile and exact integration binding only."""

    if type(profile) is not BookmakerCapabilityProfile:
        raise BookmakerCapabilityEvidenceError(
            "profile must be exact BookmakerCapabilityProfile"
        )
    if type(integration) is not BookmakerIntegrationEvidence:
        raise BookmakerCapabilityEvidenceError(
            "integration must be exact BookmakerIntegrationEvidence"
        )
    integration.verify_profile(profile)
    if type(capability) is not BookmakerCapability:
        raise BookmakerCapabilityEvidenceError(
            "capability must be exact BookmakerCapability"
        )

    cell = BookmakerCapabilityEvidenceCell(
        provider_id=profile.venue_id,
        account_id=profile.account_id,
        adapter_id=profile.adapter_id,
        adapter_version=profile.adapter_version,
        profile_id=profile.profile_id,
        integration_evidence_id=integration.evidence_id,
        integration_kind=integration.integration_kind,
        capability=capability,
        technical_state=profile.state_of(capability),
        operation=operation,
        direction=direction,
        source_kind=CapabilityEvidenceSource.DOCUMENTATION,
        evidence_level=CapabilityEvidenceLevel.DOCUMENTED,
        observed_at=observed_at,
        evidence_artifact_sha256=evidence_artifact_sha256,
        expires_or_revalidate_at=expires_or_revalidate_at,
        official_doc_url=official_doc_url,
        doc_observed_at=doc_observed_at,
        sport_scope=sport_scope,
        market_scope=market_scope,
        jurisdiction_scope=jurisdiction_scope,
        pagination_semantics=pagination_semantics,
        rate_semantics=rate_semantics,
        currency=currency,
        odds_format=odds_format,
        exchange_mode=exchange_mode,
        provider_status_vocabulary=provider_status_vocabulary,
        freshness_semantics=freshness_semantics,
        idempotency_semantics=idempotency_semantics,
        settlement_revision_semantics=settlement_revision_semantics,
    )
    return _register_issued(
        cell,
        maximum_level=CapabilityEvidenceLevel.DOCUMENTED,
    )


def evaluate_bookmaker_capability_evidence(
    value: BookmakerCapabilityEvidenceCell,
    *,
    as_of: str,
) -> BookmakerCapabilityEvidenceEvaluation:
    """Evaluate one row without causal backdating or stale authority."""

    if type(value) is not BookmakerCapabilityEvidenceCell:
        raise BookmakerCapabilityEvidenceError(
            "value must be exact BookmakerCapabilityEvidenceCell"
        )
    evaluated = _instant(as_of, "as_of")
    observed = _instant(value.observed_at, "observed_at")
    expiry = _instant(value.expires_or_revalidate_at, "expires_or_revalidate_at")
    record = _issued_record(value)

    effective = CapabilityEvidenceLevel.UNKNOWN
    reason = "UNISSUED_EVIDENCE"
    if record is not None:
        if evaluated < observed:
            reason = "NOT_YET_AVAILABLE"
        elif evaluated >= expiry:
            reason = "STALE_REVALIDATION_REQUIRED"
        elif value.negative_or_unknown_reason is not None:
            reason = "NEGATIVE_OR_UNKNOWN_EVIDENCE"
        else:
            effective = min(value.evidence_level, record.maximum_level)
            reason = "CURRENT_PRODUCT_ISSUED_EVIDENCE"

    return BookmakerCapabilityEvidenceEvaluation(
        evidence_id=value.evidence_id,
        declared_level=value.evidence_level,
        effective_level=effective,
        reason=reason,
        evaluated_at=evaluated.isoformat().replace("+00:00", "Z"),
    )


@dataclass(frozen=True, slots=True)
class BookmakerCapabilityEvidenceMatrix:
    """Immutable collection with exact-scope lookup and no cross-scope widening."""

    cells: tuple[BookmakerCapabilityEvidenceCell, ...]

    def __post_init__(self) -> None:
        if type(self.cells) is not tuple:
            raise BookmakerCapabilityEvidenceError("cells must be a tuple")
        seen: set[str] = set()
        for cell in self.cells:
            if type(cell) is not BookmakerCapabilityEvidenceCell:
                raise BookmakerCapabilityEvidenceError(
                    "cells must contain exact BookmakerCapabilityEvidenceCell values"
                )
            if cell.evidence_id in seen:
                raise BookmakerCapabilityEvidenceError(
                    "duplicate evidence_id in capability matrix"
                )
            seen.add(cell.evidence_id)

    def best_exact(
        self,
        *,
        provider_id: str,
        account_id: str,
        adapter_id: str,
        capability: BookmakerCapability,
        operation: str,
        direction: CapabilityDirection,
        as_of: str,
        sport_scope: str | None = None,
        market_scope: str | None = None,
    ) -> BookmakerCapabilityEvidenceEvaluation | None:
        """Return strongest current evidence only for the exact requested scope."""

        _text(provider_id, "provider_id")
        _text(account_id, "account_id")
        _text(adapter_id, "adapter_id")
        _text(operation, "operation")
        if type(capability) is not BookmakerCapability:
            raise BookmakerCapabilityEvidenceError(
                "capability must be exact BookmakerCapability"
            )
        if type(direction) is not CapabilityDirection:
            raise BookmakerCapabilityEvidenceError(
                "direction must be exact CapabilityDirection"
            )
        _optional_text(sport_scope, "sport_scope")
        _optional_text(market_scope, "market_scope")
        _instant(as_of, "as_of")

        candidates: list[
            tuple[
                CapabilityEvidenceLevel,
                datetime,
                str,
                BookmakerCapabilityEvidenceEvaluation,
            ]
        ] = []
        for cell in self.cells:
            if (
                cell.provider_id != provider_id
                or cell.account_id != account_id
                or cell.adapter_id != adapter_id
                or cell.capability is not capability
                or cell.operation != operation
                or cell.direction is not direction
                or cell.sport_scope != sport_scope
                or cell.market_scope != market_scope
            ):
                continue
            evaluation = evaluate_bookmaker_capability_evidence(cell, as_of=as_of)
            candidates.append(
                (
                    evaluation.effective_level,
                    _instant(cell.observed_at, "observed_at"),
                    cell.evidence_id,
                    evaluation,
                )
            )
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        return candidates[-1][3]
