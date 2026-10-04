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

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from operator import attrgetter, itemgetter

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


def _canonical_text(
    value: object,
    field: str,
    _max_text_length=_MAX_CANONICAL_TEXT_LENGTH,
    _error_type=BetfairProviderConstraintError,
) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > _max_text_length
    ):
        raise _error_type(
            f"{field} must be bounded canonical non-empty text"
        )
    return value


def _scope(
    value: object,
    _canonical_text_fn=_canonical_text,
    _error_type=BetfairProviderConstraintError,
) -> str:
    raw = _canonical_text_fn(value, "jurisdiction_scope")
    if not raw.isascii() or any(
        not (character.isupper() or character.isdigit() or character == "_")
        for character in raw
    ):
        raise _error_type(
            "jurisdiction_scope must be uppercase ASCII token text"
        )
    return raw


def _currency(
    value: object,
    _canonical_text_fn=_canonical_text,
    _error_type=BetfairProviderConstraintError,
) -> str:
    raw = _canonical_text_fn(value, "currency_code")
    if (
        len(raw) != 3
        or not raw.isascii()
        or not raw.isalpha()
        or raw != raw.upper()
    ):
        raise _error_type(
            "currency_code must be three-letter uppercase ASCII"
        )
    return raw


def _sha256(
    value: object,
    field: str,
    _canonical_text_fn=_canonical_text,
    _error_type=BetfairProviderConstraintError,
) -> str:
    raw = _canonical_text_fn(value, field)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise _error_type(f"{field} must be lowercase SHA-256 hex")
    return raw


def _utc(
    value: object,
    field: str,
    _datetime_type=datetime,
    _timedelta_type=timedelta,
    _utc_zone=timezone.utc,
    _one_day=timedelta(days=1),
    _error_type=BetfairProviderConstraintError,
) -> datetime:
    if type(value) is not _datetime_type or value.tzinfo is None:
        raise _error_type(f"{field} must be timezone-aware datetime")
    try:
        offset = value.utcoffset()
    except Exception as exc:
        raise _error_type(f"{field} has invalid timezone offset") from exc
    if (
        type(offset) is not _timedelta_type
        or not (-_one_day < offset < _one_day)
    ):
        raise _error_type(f"{field} must have a bounded concrete UTC offset")
    try:
        naive_utc = value.replace(tzinfo=None) - offset
    except (OverflowError, ValueError) as exc:
        raise _error_type(f"{field} cannot be normalized to UTC") from exc
    return naive_utc.replace(tzinfo=_utc_zone)


def _positive_decimal(
    value: object,
    field: str,
    _decimal_type=Decimal,
    _max_digits=_MAX_DECIMAL_DIGITS,
    _max_abs_exponent=_MAX_ABS_EXPONENT,
    _error_type=BetfairProviderConstraintError,
) -> Decimal:
    if type(value) is not _decimal_type or not value.is_finite() or value <= 0:
        raise _error_type(f"{field} must be exact finite positive Decimal")
    _sign, digits, exponent = value.as_tuple()
    if len(digits) > _max_digits or abs(exponent) > _max_abs_exponent:
        raise _error_type(f"{field} exceeds bounded Decimal shape")
    return value


def _decimal_text(value: Decimal) -> str:
    """Canonicalize Decimal text without consulting ambient decimal context."""
    raw = format(value, "f")
    if "." in raw:
        raw = raw.rstrip("0").rstrip(".")
    return raw


def _canonical_sha256(
    payload: object,
    _json_dumps=json.dumps,
    _sha256_fn=hashlib.sha256,
) -> str:
    raw = _json_dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_fn(raw).hexdigest()

def _build_constraint_evidence_meta():
    """Seal hard-false authority claims away from mutable Python getters."""

    sealed_classes: set[type] = set()
    protected_names = frozenset(
        {
            "__new__",
            "__getnewargs__",
            "__getattribute__",
            "__getitem__",
            "__iter__",
            "__len__",
            "provider_id",
            "jurisdiction_scope",
            "currency_code",
            "min_standard_size",
            "min_payout",
            "lower_minimum_payout_enabled",
            "source_ref",
            "source_revision",
            "source_sha256",
            "retrieved_at",
            "reviewed_at",
            "available_at",
            "effective_from",
            "effective_until",
            "review_expires_at",
            "order_family",
            "schema_version",
            "semantic_sha256",
            "generation_sha256",
            "state",
            "as_of",
            "candidate_generation_sha256s",
            "resolution_sha256",
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
        def __new__(mcls, name, bases, namespace, **kwargs):
            if any(base in sealed_classes for base in bases):
                raise TypeError(
                    "provider-constraint authority surface classes are final"
                )
            return super().__new__(mcls, name, bases, namespace, **kwargs)

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


class BetfairProviderConstraintObservation(
    tuple,
    metaclass=_BetfairConstraintEvidenceMeta,
):
    """One immutable, public, structurally unverified rule observation."""

    __slots__ = ()

    def __new__(
        cls,
        provider_id: str,
        jurisdiction_scope: str,
        currency_code: str,
        min_standard_size: Decimal,
        min_payout: Decimal | None,
        lower_minimum_payout_enabled: bool,
        source_ref: str,
        source_revision: str,
        source_sha256: str,
        retrieved_at: datetime,
        reviewed_at: datetime,
        available_at: datetime,
        effective_from: datetime,
        effective_until: datetime | None,
        review_expires_at: datetime,
        order_family: str = ORDER_FAMILY,
        schema_version: int = SCHEMA_VERSION,
        _provider_id=PROVIDER_ID,
        _order_family=ORDER_FAMILY,
        _schema_version=SCHEMA_VERSION,
        _scope_fn=_scope,
        _currency_fn=_currency,
        _positive_decimal_fn=_positive_decimal,
        _canonical_text_fn=_canonical_text,
        _sha256_fn=_sha256,
        _utc_fn=_utc,
        _canonical_sha256_fn=_canonical_sha256,
        _decimal_text_fn=_decimal_text,
        _error_type=BetfairProviderConstraintError,
    ):
        if provider_id != _provider_id:
            raise _error_type("provider_id must be canonical betfair")
        _scope_fn(jurisdiction_scope)
        _currency_fn(currency_code)
        _positive_decimal_fn(min_standard_size, "min_standard_size")
        if type(lower_minimum_payout_enabled) is not bool:
            raise _error_type("lower_minimum_payout_enabled must be bool")
        if lower_minimum_payout_enabled:
            if min_payout is None:
                raise _error_type(
                    "enabled lower-minimum rule requires min_payout"
                )
            _positive_decimal_fn(min_payout, "min_payout")
        elif min_payout is not None:
            raise _error_type(
                "disabled lower-minimum rule must not carry min_payout"
            )
        _canonical_text_fn(source_ref, "source_ref")
        _canonical_text_fn(source_revision, "source_revision")
        _sha256_fn(source_sha256, "source_sha256")

        retrieved = _utc_fn(retrieved_at, "retrieved_at")
        reviewed = _utc_fn(reviewed_at, "reviewed_at")
        available = _utc_fn(available_at, "available_at")
        effective_start = _utc_fn(effective_from, "effective_from")
        review_expiry = _utc_fn(review_expires_at, "review_expires_at")
        if reviewed < retrieved:
            raise _error_type("reviewed_at cannot precede retrieved_at")
        if available < reviewed:
            raise _error_type("available_at cannot precede reviewed_at")
        if review_expiry <= available:
            raise _error_type("review_expires_at must be after available_at")
        effective_end = None
        if effective_until is not None:
            effective_end = _utc_fn(effective_until, "effective_until")
            if effective_end <= effective_start:
                raise _error_type(
                    "effective_until must be after effective_from"
                )
        if order_family != _order_family:
            raise _error_type("order_family must be LIMIT_STANDARD_SIZE")
        if type(schema_version) is not int or schema_version != _schema_version:
            raise _error_type(
                "schema_version must be the product-owned current version"
            )

        semantic_sha256 = _canonical_sha256_fn(
            {
                "schema": "autosport.betfair_standard_limit_constraint_semantics",
                "schema_version": schema_version,
                "provider_id": provider_id,
                "jurisdiction_scope": jurisdiction_scope,
                "currency_code": currency_code,
                "order_family": order_family,
                "min_standard_size": _decimal_text_fn(min_standard_size),
                "min_payout": (
                    None
                    if min_payout is None
                    else _decimal_text_fn(min_payout)
                ),
                "lower_minimum_payout_enabled": lower_minimum_payout_enabled,
            }
        )
        generation_sha256 = _canonical_sha256_fn(
            {
                "schema": "autosport.betfair_standard_limit_constraint_generation",
                "schema_version": schema_version,
                "semantic_sha256": semantic_sha256,
                "source_ref": source_ref,
                "source_revision": source_revision,
                "source_sha256": source_sha256,
                "retrieved_at": retrieved.isoformat(),
                "reviewed_at": reviewed.isoformat(),
                "available_at": available.isoformat(),
                "effective_from": effective_start.isoformat(),
                "effective_until": (
                    None if effective_end is None else effective_end.isoformat()
                ),
                "review_expires_at": review_expiry.isoformat(),
            }
        )

        return tuple.__new__(
            cls,
            (
                provider_id,
                jurisdiction_scope,
                currency_code,
                min_standard_size,
                min_payout,
                lower_minimum_payout_enabled,
                source_ref,
                source_revision,
                source_sha256,
                retrieved,
                reviewed,
                available,
                effective_start,
                effective_end,
                review_expiry,
                order_family,
                schema_version,
                semantic_sha256,
                generation_sha256,
            ),
        )

    provider_id = property(itemgetter(0))
    jurisdiction_scope = property(itemgetter(1))
    currency_code = property(itemgetter(2))
    min_standard_size = property(itemgetter(3))
    min_payout = property(itemgetter(4))
    lower_minimum_payout_enabled = property(itemgetter(5))
    source_ref = property(itemgetter(6))
    source_revision = property(itemgetter(7))
    source_sha256 = property(itemgetter(8))
    retrieved_at = property(itemgetter(9))
    reviewed_at = property(itemgetter(10))
    available_at = property(itemgetter(11))
    effective_from = property(itemgetter(12))
    effective_until = property(itemgetter(13))
    review_expires_at = property(itemgetter(14))
    order_family = property(itemgetter(15))
    schema_version = property(itemgetter(16))

    semantic_sha256 = property(itemgetter(17))
    generation_sha256 = property(itemgetter(18))

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

    def __getnewargs__(self):
        return tuple(self[:17])


_BetfairConstraintEvidenceMeta.seal(BetfairProviderConstraintObservation)


class BetfairProviderConstraintResolution(
    tuple,
    metaclass=_BetfairConstraintEvidenceMeta,
):
    """Immutable structural resolution; never provider/execution authority."""

    __slots__ = ()

    def __new__(
        cls,
        state: BetfairConstraintResolutionState,
        provider_id: str,
        jurisdiction_scope: str,
        currency_code: str,
        as_of: datetime,
        candidate_generation_sha256s: tuple[str, ...],
        semantic_sha256: str | None,
        min_standard_size: Decimal | None,
        min_payout: Decimal | None,
        lower_minimum_payout_enabled: bool | None,
        resolution_sha256: str,
        _state_type=BetfairConstraintResolutionState,
        _provider_id=PROVIDER_ID,
        _scope_fn=_scope,
        _currency_fn=_currency,
        _utc_fn=_utc,
        _sha256_fn=_sha256,
        _positive_decimal_fn=_positive_decimal,
        _canonical_sha256_fn=_canonical_sha256,
        _decimal_text_fn=_decimal_text,
        _schema_version=SCHEMA_VERSION,
        _error_type=BetfairProviderConstraintError,
    ):
        if type(state) is not _state_type:
            raise _error_type(
                "state must be exact BetfairConstraintResolutionState"
            )
        if provider_id != _provider_id:
            raise _error_type("provider_id must be canonical betfair")
        _scope_fn(jurisdiction_scope)
        _currency_fn(currency_code)
        current = _utc_fn(as_of, "as_of")
        if type(candidate_generation_sha256s) is not tuple:
            raise _error_type("candidate_generation_sha256s must be tuple")
        if tuple(sorted(candidate_generation_sha256s)) != candidate_generation_sha256s:
            raise _error_type("candidate_generation_sha256s must be sorted")
        if len(set(candidate_generation_sha256s)) != len(candidate_generation_sha256s):
            raise _error_type("candidate_generation_sha256s must be unique")
        for value in candidate_generation_sha256s:
            _sha256_fn(value, "candidate_generation_sha256")
        _sha256_fn(resolution_sha256, "resolution_sha256")

        consistent = state is _state_type.CONSISTENT_UNVERIFIED
        if consistent:
            if semantic_sha256 is None:
                raise _error_type(
                    "consistent result requires semantic_sha256"
                )
            _sha256_fn(semantic_sha256, "semantic_sha256")
            if min_standard_size is None:
                raise _error_type(
                    "consistent result requires min_standard_size"
                )
            _positive_decimal_fn(min_standard_size, "min_standard_size")
            if type(lower_minimum_payout_enabled) is not bool:
                raise _error_type(
                    "consistent result requires lower-minimum flag"
                )
            if lower_minimum_payout_enabled:
                if min_payout is None:
                    raise _error_type(
                        "enabled lower-minimum rule requires min_payout"
                    )
                _positive_decimal_fn(min_payout, "min_payout")
            elif min_payout is not None:
                raise _error_type(
                    "disabled lower-minimum rule must not carry min_payout"
                )
        elif any(
            value is not None
            for value in (
                semantic_sha256,
                min_standard_size,
                min_payout,
                lower_minimum_payout_enabled,
            )
        ):
            raise _error_type(
                "non-consistent result cannot expose provider thresholds"
            )

        expected_resolution_sha256 = _canonical_sha256_fn(
            {
                "schema": "autosport.betfair_standard_limit_constraint_resolution",
                "schema_version": _schema_version,
                "state": state.value,
                "provider_id": provider_id,
                "jurisdiction_scope": jurisdiction_scope,
                "currency_code": currency_code,
                "as_of": current.isoformat(),
                "candidate_generation_sha256s": candidate_generation_sha256s,
                "semantic_sha256": semantic_sha256,
                "min_standard_size": (
                    None
                    if min_standard_size is None
                    else _decimal_text_fn(min_standard_size)
                ),
                "min_payout": (
                    None
                    if min_payout is None
                    else _decimal_text_fn(min_payout)
                ),
                "lower_minimum_payout_enabled": lower_minimum_payout_enabled,
            }
        )
        if resolution_sha256 != expected_resolution_sha256:
            raise _error_type(
                "resolution_sha256 does not match canonical resolution content"
            )

        return tuple.__new__(
            cls,
            (
                state,
                provider_id,
                jurisdiction_scope,
                currency_code,
                current,
                candidate_generation_sha256s,
                semantic_sha256,
                min_standard_size,
                min_payout,
                lower_minimum_payout_enabled,
                resolution_sha256,
            ),
        )

    state = property(itemgetter(0))
    provider_id = property(itemgetter(1))
    jurisdiction_scope = property(itemgetter(2))
    currency_code = property(itemgetter(3))
    as_of = property(itemgetter(4))
    candidate_generation_sha256s = property(itemgetter(5))
    semantic_sha256 = property(itemgetter(6))
    min_standard_size = property(itemgetter(7))
    min_payout = property(itemgetter(8))
    lower_minimum_payout_enabled = property(itemgetter(9))
    resolution_sha256 = property(itemgetter(10))

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

    def __getnewargs__(self):
        return tuple(self)


_BetfairConstraintEvidenceMeta.seal(BetfairProviderConstraintResolution)


def _build_constraint_resolver():
    """Capture the structural resolver graph once at module load."""

    observation_type = BetfairProviderConstraintObservation
    result_type = BetfairProviderConstraintResolution
    state_type = BetfairConstraintResolutionState
    error_type = BetfairProviderConstraintError

    provider_id = PROVIDER_ID
    order_family = ORDER_FAMILY
    schema_version = SCHEMA_VERSION
    max_observations = _MAX_OBSERVATIONS

    utc_fn = _utc
    scope_fn = _scope
    currency_fn = _currency
    decimal_text_fn = _decimal_text
    canonical_sha256_fn = _canonical_sha256

    observation_new = observation_type.__new__
    result_new = result_type.__new__
    tuple_getitem = tuple.__getitem__
    tuple_len = tuple.__len__

    # Resolver truth must come from the immutable tuple payload itself. Captured
    # operator.itemgetter objects still dispatch through a subclass's mutable
    # __getitem__, so they are not a raw-slot authority boundary.
    def raw_slot(item: tuple, index: int):
        return tuple_getitem(item, index)

    def make_result(
        *,
        state: BetfairConstraintResolutionState,
        jurisdiction_scope: str,
        currency_code: str,
        as_of: datetime,
        candidates: tuple[BetfairProviderConstraintObservation, ...],
        semantic_sha256: str | None = None,
        min_standard_size: Decimal | None = None,
        min_payout: Decimal | None = None,
        lower_minimum_payout_enabled: bool | None = None,
    ) -> BetfairProviderConstraintResolution:
        generations = tuple(
            sorted({raw_slot(item, 18) for item in candidates})
        )
        payload = {
            "schema": "autosport.betfair_standard_limit_constraint_resolution",
            "schema_version": schema_version,
            "state": state.value,
            "provider_id": provider_id,
            "jurisdiction_scope": jurisdiction_scope,
            "currency_code": currency_code,
            "as_of": as_of.isoformat(),
            "candidate_generation_sha256s": generations,
            "semantic_sha256": semantic_sha256,
            "min_standard_size": (
                None
                if min_standard_size is None
                else decimal_text_fn(min_standard_size)
            ),
            "min_payout": (
                None if min_payout is None else decimal_text_fn(min_payout)
            ),
            "lower_minimum_payout_enabled": lower_minimum_payout_enabled,
        }
        return result_new(
            result_type,
            state=state,
            provider_id=provider_id,
            jurisdiction_scope=jurisdiction_scope,
            currency_code=currency_code,
            as_of=as_of,
            candidate_generation_sha256s=generations,
            semantic_sha256=semantic_sha256,
            min_standard_size=min_standard_size,
            min_payout=min_payout,
            lower_minimum_payout_enabled=lower_minimum_payout_enabled,
            resolution_sha256=canonical_sha256_fn(payload),
        )

    def resolve_betfair_standard_limit_constraint_evidence(
        *,
        observations: tuple[BetfairProviderConstraintObservation, ...],
        as_of: datetime,
        jurisdiction_scope: str,
        currency_code: str,
    ) -> BetfairProviderConstraintResolution:
        """Resolve structurally applicable observations at one causal cut."""

        if type(observations) is not tuple:
            raise error_type("observations must be tuple")
        if len(observations) > max_observations:
            raise error_type(
                f"observations exceeds bounded limit {max_observations}"
            )
        if any(type(item) is not observation_type for item in observations):
            raise error_type(
                "observations must contain exact "
                "BetfairProviderConstraintObservation"
            )

        canonical_observations = []
        for item in observations:
            if tuple_len(item) != 19:
                raise error_type(
                    "observation tuple shape does not match canonical schema"
                )
            try:
                rebuilt = observation_new(
                    observation_type,
                    *(raw_slot(item, index) for index in range(17)),
                )
            except error_type:
                raise
            except Exception as exc:
                raise error_type(
                    "observation canonical reconstruction failed"
                ) from exc
            if tuple_len(rebuilt) != 19 or any(
                raw_slot(rebuilt, index) != raw_slot(item, index)
                for index in range(19)
            ):
                raise error_type(
                    "observation payload/digests do not match canonical reconstruction"
                )
            canonical_observations.append(rebuilt)

        unique_by_generation = {
            raw_slot(item, 18): item for item in canonical_observations
        }
        normalized_observations = tuple(
            unique_by_generation[generation]
            for generation in sorted(unique_by_generation)
        )
        current = utc_fn(as_of, "as_of")
        scope = scope_fn(jurisdiction_scope)
        currency = currency_fn(currency_code)

        matching = tuple(
            item
            for item in normalized_observations
            if raw_slot(item, 0) == provider_id
            and raw_slot(item, 1) == scope
            and raw_slot(item, 2) == currency
            and raw_slot(item, 15) == order_family
        )
        if not matching:
            return make_result(
                state=state_type.NO_EVIDENCE,
                jurisdiction_scope=scope,
                currency_code=currency,
                as_of=current,
                candidates=(),
            )

        causally_available = tuple(
            item
            for item in matching
            if utc_fn(raw_slot(item, 11), "available_at") <= current
        )
        if not causally_available:
            # Future evidence did not exist for this product decision cut.
            return make_result(
                state=state_type.NO_EVIDENCE,
                jurisdiction_scope=scope,
                currency_code=currency,
                as_of=current,
                candidates=(),
            )

        applicable = tuple(
            item
            for item in causally_available
            if utc_fn(raw_slot(item, 12), "effective_from") <= current
            and (
                raw_slot(item, 13) is None
                or current < utc_fn(raw_slot(item, 13), "effective_until")
            )
        )
        if not applicable:
            return make_result(
                state=state_type.NO_APPLICABLE_RULE,
                jurisdiction_scope=scope,
                currency_code=currency,
                as_of=current,
                candidates=causally_available,
            )

        reviewed_current = tuple(
            item
            for item in applicable
            if current < utc_fn(raw_slot(item, 14), "review_expires_at")
        )
        if not reviewed_current:
            return make_result(
                state=state_type.REVIEW_EXPIRED,
                jurisdiction_scope=scope,
                currency_code=currency,
                as_of=current,
                candidates=applicable,
            )

        semantics = {raw_slot(item, 17) for item in reviewed_current}
        if len(semantics) != 1:
            return make_result(
                state=state_type.CONFLICTING_UNVERIFIED,
                jurisdiction_scope=scope,
                currency_code=currency,
                as_of=current,
                candidates=reviewed_current,
            )

        exemplar = reviewed_current[0]
        return make_result(
            state=state_type.CONSISTENT_UNVERIFIED,
            jurisdiction_scope=scope,
            currency_code=currency,
            as_of=current,
            candidates=reviewed_current,
            semantic_sha256=raw_slot(exemplar, 17),
            min_standard_size=raw_slot(exemplar, 3),
            min_payout=raw_slot(exemplar, 4),
            lower_minimum_payout_enabled=raw_slot(exemplar, 5),
        )

    return resolve_betfair_standard_limit_constraint_evidence


resolve_betfair_standard_limit_constraint_evidence = _build_constraint_resolver()
del _build_constraint_resolver
