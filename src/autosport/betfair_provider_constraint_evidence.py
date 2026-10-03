"""Truth-bounded Betfair provider-constraint evidence resolution.

This module owns only causal/versioned resolution of documentary or read-only
provider-constraint observations for the standard-size LIMIT family. Public
observations are descriptive inputs; they do not prove authenticated provider
origin, current remote rules, jurisdiction, account currency, market
admissibility, or execution authority.

Replay cannot see evidence before product availability, review expiry fails
closed, and overlapping materially different rule generations remain
CONFLICTING_UNVERIFIED. A future authenticated acquisition/review authority may
wrap a consistent structural result; this module does not create that authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from operator import attrgetter
from typing import Iterable

PROVIDER_ID = "betfair"
ORDER_FAMILY = "LIMIT_STANDARD_SIZE"
SCHEMA_VERSION = 1
_MAX_DECIMAL_DIGITS = 64
_MAX_ABS_EXPONENT = 18
_MAX_OBSERVATIONS = 256
_MAX_CANONICAL_TEXT_LENGTH = 4096


class BetfairProviderConstraintError(ValueError):
    """Malformed provider-constraint evidence or resolution request."""


class BetfairConstraintResolutionState(str, Enum):
    NO_EVIDENCE = "NO_EVIDENCE"
    NO_APPLICABLE_RULE = "NO_APPLICABLE_RULE"
    REVIEW_EXPIRED = "REVIEW_EXPIRED"
    CONSISTENT_UNVERIFIED = "CONSISTENT_UNVERIFIED"
    CONFLICTING_UNVERIFIED = "CONFLICTING_UNVERIFIED"


def _canonical_text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > _MAX_CANONICAL_TEXT_LENGTH
    ):
        raise BetfairProviderConstraintError(
            f"{field} must be bounded canonical non-empty text"
        )
    return value


def _scope(value: object) -> str:
    raw = _canonical_text(value, "jurisdiction_scope")
    if not raw.isascii() or any(
        not (character.isupper() or character.isdigit() or character == "_")
        for character in raw
    ):
        raise BetfairProviderConstraintError(
            "jurisdiction_scope must be uppercase ASCII token text"
        )
    return raw


def _currency(value: object) -> str:
    raw = _canonical_text(value, "currency_code")
    if (
        len(raw) != 3
        or not raw.isascii()
        or not raw.isalpha()
        or raw != raw.upper()
    ):
        raise BetfairProviderConstraintError(
            "currency_code must be three-letter uppercase ASCII"
        )
    return raw


def _sha256(value: object, field: str) -> str:
    raw = _canonical_text(value, field)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise BetfairProviderConstraintError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return raw


def _utc(value: object, field: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise BetfairProviderConstraintError(
            f"{field} must be timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _positive_decimal(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise BetfairProviderConstraintError(
            f"{field} must be exact finite positive Decimal"
        )
    _sign, digits, exponent = value.as_tuple()
    if len(digits) > _MAX_DECIMAL_DIGITS or abs(exponent) > _MAX_ABS_EXPONENT:
        raise BetfairProviderConstraintError(
            f"{field} exceeds bounded Decimal shape"
        )
    return value


def _decimal_text(value: Decimal) -> str:
    """Canonicalize Decimal text without consulting ambient decimal context."""
    raw = format(value, "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw


def _canonical_sha256(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _build_constraint_evidence_meta():
    """Seal hard-false authority claims away from mutable Python getters."""

    sealed_classes: set[type] = set()
    protected_names = frozenset(
        {
            "provider_origin_proven",
            "current_constraint_authority",
            "execution_authorized",
            "real_money_execution",
            "_provider_origin_proven_constant",
            "_current_constraint_authority_constant",
            "_execution_authorized_constant",
            "_real_money_execution_constant",
        }
    )

    class _BetfairConstraintEvidenceMeta(type):
        def __setattr__(cls, name: str, value: object) -> None:
            if cls in sealed_classes and name in protected_names:
                raise TypeError(
                    "provider-constraint authority surface is sealed: " + name
                )
            super().__setattr__(name, value)

        def __delattr__(cls, name: str) -> None:
            if cls in sealed_classes and name in protected_names:
                raise TypeError(
                    "provider-constraint authority surface is sealed: " + name
                )
            super().__delattr__(name)

        @classmethod
        def seal(mcls, cls: type) -> None:
            sealed_classes.add(cls)

    return _BetfairConstraintEvidenceMeta


_BetfairConstraintEvidenceMeta = _build_constraint_evidence_meta()
del _build_constraint_evidence_meta


@dataclass(frozen=True, slots=True)
class BetfairProviderConstraintObservation(metaclass=_BetfairConstraintEvidenceMeta):
    """One immutable provider-rule observation.

    Construction is public and therefore never proves provider origin. The
    record preserves enough identity/timing data for a future authenticated
    acquisition/review layer to re-resolve the exact generation.
    """

    provider_id: str
    jurisdiction_scope: str
    currency_code: str
    min_standard_size: Decimal
    min_payout: Decimal | None
    lower_minimum_payout_enabled: bool
    source_ref: str
    source_revision: str
    source_sha256: str
    retrieved_at: datetime
    reviewed_at: datetime
    available_at: datetime
    effective_from: datetime
    effective_until: datetime | None
    review_expires_at: datetime
    order_family: str = ORDER_FAMILY
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.provider_id != PROVIDER_ID:
            raise BetfairProviderConstraintError(
                "provider_id must be canonical betfair"
            )
        _scope(self.jurisdiction_scope)
        _currency(self.currency_code)
        _positive_decimal(self.min_standard_size, "min_standard_size")
        if type(self.lower_minimum_payout_enabled) is not bool:
            raise BetfairProviderConstraintError(
                "lower_minimum_payout_enabled must be bool"
            )
        if self.lower_minimum_payout_enabled:
            if self.min_payout is None:
                raise BetfairProviderConstraintError(
                    "enabled lower-minimum rule requires min_payout"
                )
            _positive_decimal(self.min_payout, "min_payout")
        elif self.min_payout is not None:
            raise BetfairProviderConstraintError(
                "disabled lower-minimum rule must not carry min_payout"
            )
        _canonical_text(self.source_ref, "source_ref")
        _canonical_text(self.source_revision, "source_revision")
        _sha256(self.source_sha256, "source_sha256")
        retrieved = _utc(self.retrieved_at, "retrieved_at")
        reviewed = _utc(self.reviewed_at, "reviewed_at")
        available = _utc(self.available_at, "available_at")
        effective_from = _utc(self.effective_from, "effective_from")
        review_expires = _utc(self.review_expires_at, "review_expires_at")
        if reviewed < retrieved:
            raise BetfairProviderConstraintError(
                "reviewed_at cannot precede retrieved_at"
            )
        if available < reviewed:
            raise BetfairProviderConstraintError(
                "available_at cannot precede reviewed_at"
            )
        if review_expires <= available:
            raise BetfairProviderConstraintError(
                "review_expires_at must be after available_at"
            )
        if self.effective_until is not None:
            effective_until = _utc(self.effective_until, "effective_until")
            if effective_until <= effective_from:
                raise BetfairProviderConstraintError(
                    "effective_until must be after effective_from"
                )
        if self.order_family != ORDER_FAMILY:
            raise BetfairProviderConstraintError(
                "order_family must be LIMIT_STANDARD_SIZE"
            )
        if (
            type(self.schema_version) is not int
            or self.schema_version != SCHEMA_VERSION
        ):
            raise BetfairProviderConstraintError(
                "schema_version must be the product-owned current version"
            )

    @property
    def semantic_sha256(self) -> str:
        return _canonical_sha256(
            {
                "schema": "autosport.betfair_standard_limit_constraint_semantics",
                "schema_version": self.schema_version,
                "provider_id": self.provider_id,
                "jurisdiction_scope": self.jurisdiction_scope,
                "currency_code": self.currency_code,
                "order_family": self.order_family,
                "min_standard_size": _decimal_text(self.min_standard_size),
                "min_payout": (
                    None
                    if self.min_payout is None
                    else _decimal_text(self.min_payout)
                ),
                "lower_minimum_payout_enabled": self.lower_minimum_payout_enabled,
            }
        )

    @property
    def generation_sha256(self) -> str:
        return _canonical_sha256(
            {
                "schema": "autosport.betfair_standard_limit_constraint_generation",
                "schema_version": self.schema_version,
                "semantic_sha256": self.semantic_sha256,
                "source_ref": self.source_ref,
                "source_revision": self.source_revision,
                "source_sha256": self.source_sha256,
                "retrieved_at": _utc(
                    self.retrieved_at, "retrieved_at"
                ).isoformat(),
                "reviewed_at": _utc(
                    self.reviewed_at, "reviewed_at"
                ).isoformat(),
                "available_at": _utc(
                    self.available_at, "available_at"
                ).isoformat(),
                "effective_from": _utc(
                    self.effective_from, "effective_from"
                ).isoformat(),
                "effective_until": (
                    None
                    if self.effective_until is None
                    else _utc(
                        self.effective_until, "effective_until"
                    ).isoformat()
                ),
                "review_expires_at": _utc(
                    self.review_expires_at, "review_expires_at"
                ).isoformat(),
            }
        )

    _provider_origin_proven_constant = False
    _current_constraint_authority_constant = False
    _execution_authorized_constant = False

    provider_origin_proven = property(attrgetter("_provider_origin_proven_constant"))
    current_constraint_authority = property(
        attrgetter("_current_constraint_authority_constant")
    )
    execution_authorized = property(attrgetter("_execution_authorized_constant"))


_BetfairConstraintEvidenceMeta.seal(BetfairProviderConstraintObservation)


@dataclass(frozen=True, slots=True)
class BetfairProviderConstraintResolution(metaclass=_BetfairConstraintEvidenceMeta):
    state: BetfairConstraintResolutionState
    provider_id: str
    jurisdiction_scope: str
    currency_code: str
    as_of: datetime
    candidate_generation_sha256s: tuple[str, ...]
    semantic_sha256: str | None
    min_standard_size: Decimal | None
    min_payout: Decimal | None
    lower_minimum_payout_enabled: bool | None
    resolution_sha256: str

    def __post_init__(self) -> None:
        if type(self.state) is not BetfairConstraintResolutionState:
            raise BetfairProviderConstraintError(
                "state must be exact BetfairConstraintResolutionState"
            )
        if self.provider_id != PROVIDER_ID:
            raise BetfairProviderConstraintError(
                "provider_id must be canonical betfair"
            )
        _scope(self.jurisdiction_scope)
        _currency(self.currency_code)
        _utc(self.as_of, "as_of")
        if type(self.candidate_generation_sha256s) is not tuple:
            raise BetfairProviderConstraintError(
                "candidate_generation_sha256s must be tuple"
            )
        if (
            tuple(sorted(self.candidate_generation_sha256s))
            != self.candidate_generation_sha256s
        ):
            raise BetfairProviderConstraintError(
                "candidate_generation_sha256s must be sorted"
            )
        for value in self.candidate_generation_sha256s:
            _sha256(value, "candidate_generation_sha256")
        _sha256(self.resolution_sha256, "resolution_sha256")

        consistent = (
            self.state
            is BetfairConstraintResolutionState.CONSISTENT_UNVERIFIED
        )
        if consistent:
            if self.semantic_sha256 is None:
                raise BetfairProviderConstraintError(
                    "consistent result requires semantic_sha256"
                )
            _sha256(self.semantic_sha256, "semantic_sha256")
            if self.min_standard_size is None:
                raise BetfairProviderConstraintError(
                    "consistent result requires min_standard_size"
                )
            _positive_decimal(self.min_standard_size, "min_standard_size")
            if type(self.lower_minimum_payout_enabled) is not bool:
                raise BetfairProviderConstraintError(
                    "consistent result requires lower-minimum flag"
                )
            if self.lower_minimum_payout_enabled:
                if self.min_payout is None:
                    raise BetfairProviderConstraintError(
                        "enabled lower-minimum rule requires min_payout"
                    )
                _positive_decimal(self.min_payout, "min_payout")
            elif self.min_payout is not None:
                raise BetfairProviderConstraintError(
                    "disabled lower-minimum rule must not carry min_payout"
                )
        elif any(
            value is not None
            for value in (
                self.semantic_sha256,
                self.min_standard_size,
                self.min_payout,
                self.lower_minimum_payout_enabled,
            )
        ):
            raise BetfairProviderConstraintError(
                "non-consistent result cannot expose provider thresholds"
            )

    _provider_origin_proven_constant = False
    _current_constraint_authority_constant = False
    _execution_authorized_constant = False
    _real_money_execution_constant = False

    provider_origin_proven = property(attrgetter("_provider_origin_proven_constant"))
    current_constraint_authority = property(
        attrgetter("_current_constraint_authority_constant")
    )
    execution_authorized = property(attrgetter("_execution_authorized_constant"))
    real_money_execution = property(attrgetter("_real_money_execution_constant"))


_BetfairConstraintEvidenceMeta.seal(BetfairProviderConstraintResolution)


def _result(
    *,
    state: BetfairConstraintResolutionState,
    jurisdiction_scope: str,
    currency_code: str,
    as_of: datetime,
    candidates: Iterable[BetfairProviderConstraintObservation],
    semantic_sha256: str | None = None,
    min_standard_size: Decimal | None = None,
    min_payout: Decimal | None = None,
    lower_minimum_payout_enabled: bool | None = None,
) -> BetfairProviderConstraintResolution:
    generations = tuple(
        sorted({observation.generation_sha256 for observation in candidates})
    )
    payload = {
        "schema": "autosport.betfair_standard_limit_constraint_resolution",
        "schema_version": SCHEMA_VERSION,
        "state": state.value,
        "provider_id": PROVIDER_ID,
        "jurisdiction_scope": jurisdiction_scope,
        "currency_code": currency_code,
        "as_of": as_of.isoformat(),
        "candidate_generation_sha256s": generations,
        "semantic_sha256": semantic_sha256,
        "min_standard_size": (
            None
            if min_standard_size is None
            else _decimal_text(min_standard_size)
        ),
        "min_payout": (
            None if min_payout is None else _decimal_text(min_payout)
        ),
        "lower_minimum_payout_enabled": lower_minimum_payout_enabled,
    }
    return BetfairProviderConstraintResolution(
        state=state,
        provider_id=PROVIDER_ID,
        jurisdiction_scope=jurisdiction_scope,
        currency_code=currency_code,
        as_of=as_of,
        candidate_generation_sha256s=generations,
        semantic_sha256=semantic_sha256,
        min_standard_size=min_standard_size,
        min_payout=min_payout,
        lower_minimum_payout_enabled=lower_minimum_payout_enabled,
        resolution_sha256=_canonical_sha256(payload),
    )


def resolve_betfair_standard_limit_constraint_evidence(
    *,
    observations: tuple[BetfairProviderConstraintObservation, ...],
    as_of: datetime,
    jurisdiction_scope: str,
    currency_code: str,
) -> BetfairProviderConstraintResolution:
    """Resolve structurally applicable provider-rule observations, fail closed.

    The return value is never current-provider or execution authority. It only
    states whether public/versioned observations are structurally consistent at
    one causal decision cut.
    """

    if type(observations) is not tuple:
        raise BetfairProviderConstraintError("observations must be tuple")
    if len(observations) > _MAX_OBSERVATIONS:
        raise BetfairProviderConstraintError(
            f"observations exceeds bounded limit {_MAX_OBSERVATIONS}"
        )
    if any(
        type(item) is not BetfairProviderConstraintObservation
        for item in observations
    ):
        raise BetfairProviderConstraintError(
            "observations must contain exact BetfairProviderConstraintObservation"
        )
    unique_by_generation = {
        item.generation_sha256: item for item in observations
    }
    normalized_observations = tuple(
        unique_by_generation[generation]
        for generation in sorted(unique_by_generation)
    )
    current = _utc(as_of, "as_of")
    scope = _scope(jurisdiction_scope)
    currency = _currency(currency_code)

    matching = tuple(
        item
        for item in normalized_observations
        if item.provider_id == PROVIDER_ID
        and item.jurisdiction_scope == scope
        and item.currency_code == currency
        and item.order_family == ORDER_FAMILY
    )
    if not matching:
        return _result(
            state=BetfairConstraintResolutionState.NO_EVIDENCE,
            jurisdiction_scope=scope,
            currency_code=currency,
            as_of=current,
            candidates=(),
        )

    causally_available = tuple(
        item
        for item in matching
        if _utc(item.available_at, "available_at") <= current
    )
    if not causally_available:
        # Future observations did not exist for this product decision cut.
        # They must not alter historical state, candidate identity, or digest.
        return _result(
            state=BetfairConstraintResolutionState.NO_EVIDENCE,
            jurisdiction_scope=scope,
            currency_code=currency,
            as_of=current,
            candidates=(),
        )

    applicable = tuple(
        item
        for item in causally_available
        if _utc(item.effective_from, "effective_from") <= current
        and (
            item.effective_until is None
            or current < _utc(item.effective_until, "effective_until")
        )
    )
    if not applicable:
        return _result(
            state=BetfairConstraintResolutionState.NO_APPLICABLE_RULE,
            jurisdiction_scope=scope,
            currency_code=currency,
            as_of=current,
            candidates=causally_available,
        )

    reviewed_current = tuple(
        item
        for item in applicable
        if current < _utc(item.review_expires_at, "review_expires_at")
    )
    if not reviewed_current:
        return _result(
            state=BetfairConstraintResolutionState.REVIEW_EXPIRED,
            jurisdiction_scope=scope,
            currency_code=currency,
            as_of=current,
            candidates=applicable,
        )

    semantics = {item.semantic_sha256 for item in reviewed_current}
    if len(semantics) != 1:
        return _result(
            state=BetfairConstraintResolutionState.CONFLICTING_UNVERIFIED,
            jurisdiction_scope=scope,
            currency_code=currency,
            as_of=current,
            candidates=reviewed_current,
        )

    exemplar = reviewed_current[0]
    return _result(
        state=BetfairConstraintResolutionState.CONSISTENT_UNVERIFIED,
        jurisdiction_scope=scope,
        currency_code=currency,
        as_of=current,
        candidates=reviewed_current,
        semantic_sha256=exemplar.semantic_sha256,
        min_standard_size=exemplar.min_standard_size,
        min_payout=exemplar.min_payout,
        lower_minimum_payout_enabled=exemplar.lower_minimum_payout_enabled,
    )
