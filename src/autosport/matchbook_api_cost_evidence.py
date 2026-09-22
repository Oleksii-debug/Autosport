from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Mapping


MATCHBOOK_PRICING_URL = "https://developers.matchbook.com/docs/pricing"
MATCHBOOK_FAIR_USAGE_URL = "https://developers.matchbook.com/docs/fair-usage-policy"
MATCHBOOK_GET_BLOCK_SIZE = 1_000_000
MATCHBOOK_GET_BLOCK_PRICE_GBP = Decimal("100")
MATCHBOOK_WRITE_QUALIFICATION = "NON_CHARGEABLE_ONLY_AT_REASONABLE_FREQUENCY"
_SCHEMA = "autosport.matchbook_api_cost_evidence.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MatchbookApiCostEvidenceError(ValueError):
    """Raised when Matchbook API usage/cost evidence is not fail-closed."""


class BillingCalendarBasis(StrEnum):
    CONFIGURED_ASSUMPTION = "CONFIGURED_ASSUMPTION"


class PolicyAmountTruth(StrEnum):
    CONFIGURED_CALENDAR_ESTIMATE_GBP = "CONFIGURED_CALENDAR_ESTIMATE_GBP"
    REMAINDER_UNRESOLVED = "REMAINDER_UNRESOLVED"
    USAGE_ORIGIN_UNBOUND = "USAGE_ORIGIN_UNBOUND"


class FxTruth(StrEnum):
    NOT_NEEDED_GBP = "NOT_NEEDED_GBP"
    UNRESOLVED_PROVIDER_FX = "UNRESOLVED_PROVIDER_FX"


class CashTruth(StrEnum):
    UNRECONCILED = "UNRECONCILED"


class MeterContinuityTruth(StrEnum):
    PROCESS_LOCAL_ONLY = "PROCESS_LOCAL_ONLY"


class UsageOriginTruth(StrEnum):
    UNBOUND_PROCESS_LOCAL = "UNBOUND_PROCESS_LOCAL"


def _text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise MatchbookApiCostEvidenceError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise MatchbookApiCostEvidenceError(
            f"{name} must be lowercase SHA-256"
        )
    return text


def _aware(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MatchbookApiCostEvidenceError(
            f"{name} must be timezone-aware datetime"
        )
    return value


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise MatchbookApiCostEvidenceError(
            f"{name} must be finite Decimal"
        )
    return value


def _dt(value: datetime) -> str:
    return _aware(value, "datetime").isoformat(timespec="microseconds")


def _money(value: Decimal) -> str:
    return format(_decimal(value, "money"), "f")


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise MatchbookApiCostEvidenceError(
            "evidence payload is not canonical JSON"
        ) from exc


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class MatchbookBillingPeriod:
    period_id: str
    starts_at: datetime
    ends_at: datetime
    timezone_name: str
    config_sha256: str
    basis: BillingCalendarBasis = field(
        default=BillingCalendarBasis.CONFIGURED_ASSUMPTION,
        init=False,
    )

    def __post_init__(self) -> None:
        _text(self.period_id, "period_id")
        start = _aware(self.starts_at, "starts_at")
        end = _aware(self.ends_at, "ends_at")
        if start >= end:
            raise MatchbookApiCostEvidenceError(
                "billing period starts_at must be before ends_at"
            )
        _text(self.timezone_name, "timezone_name")
        _sha(self.config_sha256, "config_sha256")

    @property
    def evidence_sha256(self) -> str:
        return _digest(
            {
                "schema": _SCHEMA,
                "kind": "billing_period",
                "period_id": self.period_id,
                "starts_at": _dt(self.starts_at),
                "ends_at": _dt(self.ends_at),
                "timezone_name": self.timezone_name,
                "config_sha256": self.config_sha256,
                "basis": self.basis.value,
            }
        )


def configured_billing_period(
    *,
    period_id: str,
    starts_at: datetime,
    ends_at: datetime,
    timezone_name: str,
    config_sha256: str,
) -> MatchbookBillingPeriod:
    """Create an explicit billing-calendar assumption.

    Matchbook's public pricing rule says "calendar month" but this seam does not
    establish the provider's exact billing timezone.  Until a separate provider
    authority does so, an Autosport period remains a configured assumption.
    """

    return MatchbookBillingPeriod(
        period_id=period_id,
        starts_at=starts_at,
        ends_at=ends_at,
        timezone_name=timezone_name,
        config_sha256=config_sha256,
    )


@dataclass(frozen=True, slots=True)
class MatchbookPricingPolicySnapshot:
    observed_at: datetime
    source_sha256: str
    source_url: str
    get_block_size: int
    get_block_price_gbp: Decimal
    write_qualification: str
    fair_usage_url: str

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        _sha(self.source_sha256, "source_sha256")
        if self.source_url != MATCHBOOK_PRICING_URL:
            raise MatchbookApiCostEvidenceError(
                "pricing policy source URL drift"
            )
        if self.fair_usage_url != MATCHBOOK_FAIR_USAGE_URL:
            raise MatchbookApiCostEvidenceError(
                "fair-usage policy source URL drift"
            )
        if self.get_block_size != MATCHBOOK_GET_BLOCK_SIZE:
            raise MatchbookApiCostEvidenceError(
                "Matchbook GET block size drifted from frozen public policy"
            )
        if self.get_block_price_gbp != MATCHBOOK_GET_BLOCK_PRICE_GBP:
            raise MatchbookApiCostEvidenceError(
                "Matchbook GET block price drifted from frozen public policy"
            )
        if self.write_qualification != MATCHBOOK_WRITE_QUALIFICATION:
            raise MatchbookApiCostEvidenceError(
                "Matchbook WRITE qualification drifted from frozen public policy"
            )

    @property
    def policy_sha256(self) -> str:
        return _digest(
            {
                "schema": _SCHEMA,
                "kind": "pricing_policy",
                "observed_at": _dt(self.observed_at),
                "source_sha256": self.source_sha256,
                "source_url": self.source_url,
                "get_block_size": self.get_block_size,
                "get_block_price_gbp": _money(self.get_block_price_gbp),
                "write_qualification": self.write_qualification,
                "fair_usage_url": self.fair_usage_url,
            }
        )


def public_matchbook_pricing_policy(
    *,
    observed_at: datetime,
    source_sha256: str,
) -> MatchbookPricingPolicySnapshot:
    """Bind the currently supported public Matchbook pricing contract to a source digest."""

    return MatchbookPricingPolicySnapshot(
        observed_at=observed_at,
        source_sha256=source_sha256,
        source_url=MATCHBOOK_PRICING_URL,
        get_block_size=MATCHBOOK_GET_BLOCK_SIZE,
        get_block_price_gbp=MATCHBOOK_GET_BLOCK_PRICE_GBP,
        write_qualification=MATCHBOOK_WRITE_QUALIFICATION,
        fair_usage_url=MATCHBOOK_FAIR_USAGE_URL,
    )


@dataclass(frozen=True, slots=True)
class MatchbookPolicyMath:
    request_count: int
    completed_blocks: int
    remainder_requests: int
    completed_block_gbp: Decimal
    exact_policy_gbp: Decimal | None


def calculate_policy_math(
    request_count: int,
    policy: MatchbookPricingPolicySnapshot,
) -> MatchbookPolicyMath:
    """Apply only the unambiguous whole-million part of the published GET rule."""

    if type(policy) is not MatchbookPricingPolicySnapshot:
        raise MatchbookApiCostEvidenceError(
            "policy must be exact MatchbookPricingPolicySnapshot"
        )
    if (
        type(request_count) is not int
        or isinstance(request_count, bool)
        or request_count < 0
    ):
        raise MatchbookApiCostEvidenceError(
            "request_count must be non-negative integer"
        )
    blocks, remainder = divmod(request_count, policy.get_block_size)
    completed = policy.get_block_price_gbp * blocks
    return MatchbookPolicyMath(
        request_count=request_count,
        completed_blocks=blocks,
        remainder_requests=remainder,
        completed_block_gbp=completed,
        exact_policy_gbp=completed if remainder == 0 else None,
    )


def _build_runtime_capability():
    capability_key = secrets.token_bytes(32)
    error_cls = MatchbookApiCostEvidenceError
    compare_digest = hmac.compare_digest
    hmac_digest = hmac.digest
    canonical_bytes = _canonical_bytes
    public_digest = _digest
    sha256_name = "sha256"

    def capability_proof(payload: Mapping[str, Any]) -> bytes:
        return hmac_digest(
            capability_key,
            canonical_bytes(payload),
            sha256_name,
        )

    def usage_payload(values: Mapping[str, Any]) -> dict[str, Any]:
        first_at = values["first_emitted_at"]
        last_at = values["last_emitted_at"]
        continuity = values["continuity_truth"]
        origin = values.get(
            "origin_truth",
            UsageOriginTruth.UNBOUND_PROCESS_LOCAL,
        )
        return {
            "schema": _SCHEMA,
            "kind": "usage_snapshot",
            "application_context_sha256": values["application_context_sha256"],
            "billing_period_sha256": values["billing_period_sha256"],
            "pricing_policy_sha256": values["pricing_policy_sha256"],
            "request_count": values["request_count"],
            "first_sequence": values["first_sequence"],
            "last_sequence": values["last_sequence"],
            "first_emitted_at": None if first_at is None else _dt(first_at),
            "last_emitted_at": None if last_at is None else _dt(last_at),
            "rolling_emission_sha256": values["rolling_emission_sha256"],
            "observed_at": _dt(values["observed_at"]),
            "continuity_truth": continuity.value,
            "origin_truth": origin.value,
        }

    def usage_values(snapshot: "MatchbookApiUsageSnapshot") -> dict[str, Any]:
        return {
            "application_context_sha256": snapshot.application_context_sha256,
            "billing_period_sha256": snapshot.billing_period_sha256,
            "pricing_policy_sha256": snapshot.pricing_policy_sha256,
            "request_count": snapshot.request_count,
            "first_sequence": snapshot.first_sequence,
            "last_sequence": snapshot.last_sequence,
            "first_emitted_at": snapshot.first_emitted_at,
            "last_emitted_at": snapshot.last_emitted_at,
            "rolling_emission_sha256": snapshot.rolling_emission_sha256,
            "observed_at": snapshot.observed_at,
            "continuity_truth": snapshot.continuity_truth,
            "origin_truth": snapshot.origin_truth,
        }

    @dataclass(frozen=True, slots=True)
    class MatchbookApiUsageSnapshot:
        application_context_sha256: str
        billing_period_sha256: str
        pricing_policy_sha256: str
        request_count: int
        first_sequence: int | None
        last_sequence: int | None
        first_emitted_at: datetime | None
        last_emitted_at: datetime | None
        rolling_emission_sha256: str
        observed_at: datetime
        continuity_truth: MeterContinuityTruth
        evidence_sha256: str
        _capability_proof: bytes = field(repr=False, compare=False)
        origin_truth: UsageOriginTruth = field(
            default=UsageOriginTruth.UNBOUND_PROCESS_LOCAL,
            init=False,
        )

        def __post_init__(self) -> None:
            assert_usage_shape(self)
            payload = usage_payload(usage_values(self))
            if not compare_digest(
                self._capability_proof,
                capability_proof(payload),
            ):
                raise error_cls(
                    "usage snapshot lacks current process-local meter capability"
                )
            if self.evidence_sha256 != public_digest(payload):
                raise error_cls("usage evidence digest mismatch")

    def assert_usage_shape(snapshot: MatchbookApiUsageSnapshot) -> None:
        for name in (
            "application_context_sha256",
            "billing_period_sha256",
            "pricing_policy_sha256",
            "rolling_emission_sha256",
        ):
            _sha(getattr(snapshot, name), name)
        observed = _aware(snapshot.observed_at, "observed_at")
        if snapshot.continuity_truth is not MeterContinuityTruth.PROCESS_LOCAL_ONLY:
            raise error_cls(
                "this seam cannot mint durable request-meter continuity"
            )
        if snapshot.origin_truth is not UsageOriginTruth.UNBOUND_PROCESS_LOCAL:
            raise error_cls(
                "standalone request meter cannot mint transport-observed usage"
            )
        if type(snapshot.request_count) is not int or snapshot.request_count < 0:
            raise error_cls("request_count must be non-negative integer")
        if snapshot.request_count == 0:
            if any(
                value is not None
                for value in (
                    snapshot.first_sequence,
                    snapshot.last_sequence,
                    snapshot.first_emitted_at,
                    snapshot.last_emitted_at,
                )
            ):
                raise error_cls("empty usage cannot carry emission bounds")
            return
        if (
            type(snapshot.first_sequence) is not int
            or type(snapshot.last_sequence) is not int
            or snapshot.first_sequence <= 0
        ):
            raise error_cls("usage sequence bounds are invalid")
        if (
            snapshot.last_sequence - snapshot.first_sequence + 1
            != snapshot.request_count
        ):
            raise error_cls(
                "usage sequence bounds do not match request_count"
            )
        first = _aware(snapshot.first_emitted_at, "first_emitted_at")
        last = _aware(snapshot.last_emitted_at, "last_emitted_at")
        if first > last or last > observed:
            raise error_cls(
                "usage emission/observation chronology is invalid"
            )

    def assert_usage_authoritative(snapshot: MatchbookApiUsageSnapshot) -> None:
        if type(snapshot) is not MatchbookApiUsageSnapshot:
            raise error_cls(
                "usage must be exact MatchbookApiUsageSnapshot"
            )
        assert_usage_shape(snapshot)
        payload = usage_payload(usage_values(snapshot))
        if not compare_digest(
            snapshot._capability_proof,
            capability_proof(payload),
        ):
            raise error_cls(
                "usage snapshot lacks current process-local meter capability"
            )
        if snapshot.evidence_sha256 != public_digest(payload):
            raise error_cls("usage evidence digest mismatch")

    class MatchbookApiRequestMeter:
        """O(1) process-local observation meter for Matchbook GET emissions.

        Calls to record_get_emitted preserve sequence/time/fingerprint evidence,
        but this public standalone object is not product-owned transport
        authority. Its snapshots therefore remain UNBOUND_PROCESS_LOCAL even
        when a caller records plausible physical-emission facts.

        The capability is intentionally process-local. This packet does not
        claim restart continuity or canonical transport integration.
        """

        __slots__ = (
            "_app",
            "_period",
            "_policy",
            "_count",
            "_first_seq",
            "_last_seq",
            "_first_at",
            "_last_at",
            "_rolling",
        )

        def __init__(
            self,
            *,
            application_context_sha256: str,
            billing_period: MatchbookBillingPeriod,
            pricing_policy: MatchbookPricingPolicySnapshot,
        ) -> None:
            self._app = _sha(
                application_context_sha256,
                "application_context_sha256",
            )
            if type(billing_period) is not MatchbookBillingPeriod:
                raise error_cls(
                    "meter requires exact MatchbookBillingPeriod"
                )
            if type(pricing_policy) is not MatchbookPricingPolicySnapshot:
                raise error_cls(
                    "meter requires exact MatchbookPricingPolicySnapshot"
                )
            if pricing_policy.observed_at > billing_period.starts_at:
                raise error_cls(
                    "pricing policy must be captured no later than billing-period start"
                )
            self._period = billing_period
            self._policy = pricing_policy
            self._count = 0
            self._first_seq = None
            self._last_seq = None
            self._first_at = None
            self._last_at = None
            self._rolling = public_digest(
                {
                    "schema": _SCHEMA,
                    "kind": "emission_genesis",
                    "application_context_sha256": self._app,
                    "billing_period_sha256": billing_period.evidence_sha256,
                    "pricing_policy_sha256": pricing_policy.policy_sha256,
                }
            )

        @property
        def request_count(self) -> int:
            return self._count

        @property
        def rolling_emission_sha256(self) -> str:
            return self._rolling

        def record_get_emitted(
            self,
            *,
            sequence: int,
            request_fingerprint_sha256: str,
            emitted_at: datetime,
        ) -> None:
            if (
                type(sequence) is not int
                or isinstance(sequence, bool)
                or sequence <= 0
            ):
                raise error_cls("sequence must be positive integer")
            expected = 1 if self._last_seq is None else self._last_seq + 1
            if sequence != expected:
                raise error_cls(
                    "physical GET sequence must advance exactly once; "
                    f"expected {expected}"
                )
            fingerprint = _sha(
                request_fingerprint_sha256,
                "request_fingerprint_sha256",
            )
            emitted = _aware(emitted_at, "emitted_at")
            if not (
                self._period.starts_at
                <= emitted
                < self._period.ends_at
            ):
                raise error_cls(
                    "GET emission is outside bound billing period"
                )
            if self._last_at is not None and emitted < self._last_at:
                raise error_cls("GET emission time cannot move backwards")
            next_digest = public_digest(
                {
                    "schema": _SCHEMA,
                    "kind": "get_emission",
                    "previous_sha256": self._rolling,
                    "sequence": sequence,
                    "request_fingerprint_sha256": fingerprint,
                    "emitted_at": _dt(emitted),
                }
            )
            if self._first_seq is None:
                self._first_seq = sequence
                self._first_at = emitted
            self._last_seq = sequence
            self._last_at = emitted
            self._count += 1
            self._rolling = next_digest

        def snapshot(
            self,
            *,
            observed_at: datetime,
        ) -> MatchbookApiUsageSnapshot:
            observed = _aware(observed_at, "observed_at")
            if self._last_at is not None and observed < self._last_at:
                raise error_cls(
                    "usage snapshot cannot predate last emission"
                )
            values = {
                "application_context_sha256": self._app,
                "billing_period_sha256": self._period.evidence_sha256,
                "pricing_policy_sha256": self._policy.policy_sha256,
                "request_count": self._count,
                "first_sequence": self._first_seq,
                "last_sequence": self._last_seq,
                "first_emitted_at": self._first_at,
                "last_emitted_at": self._last_at,
                "rolling_emission_sha256": self._rolling,
                "observed_at": observed,
                "continuity_truth": MeterContinuityTruth.PROCESS_LOCAL_ONLY,
            }
            payload = usage_payload(values)
            return MatchbookApiUsageSnapshot(
                **values,
                evidence_sha256=public_digest(payload),
                _capability_proof=capability_proof(payload),
            )

    def accrual_payload(values: Mapping[str, Any]) -> dict[str, Any]:
        policy_amount = values["policy_gbp_amount"]
        account_amount = values["account_currency_amount"]
        return {
            "schema": _SCHEMA,
            "kind": "api_cost_accrual",
            "usage_evidence_sha256": values["usage_evidence_sha256"],
            "billing_period_sha256": values["billing_period_sha256"],
            "pricing_policy_sha256": values["pricing_policy_sha256"],
            "request_count": values["request_count"],
            "completed_blocks": values["completed_blocks"],
            "remainder_requests": values["remainder_requests"],
            "completed_block_gbp": _money(values["completed_block_gbp"]),
            "policy_gbp_amount": (
                None if policy_amount is None else _money(policy_amount)
            ),
            "amount_truth": values["amount_truth"].value,
            "account_currency": values["account_currency"],
            "fx_truth": values["fx_truth"].value,
            "account_currency_amount": (
                None if account_amount is None else _money(account_amount)
            ),
            "cash_truth": values["cash_truth"].value,
            "allocation_state": values["allocation_state"],
            "meter_continuity_truth": values[
                "meter_continuity_truth"
            ].value,
            "usage_origin_truth": values["usage_origin_truth"].value,
        }

    def accrual_values(
        accrual: "MatchbookApiCostAccrual",
    ) -> dict[str, Any]:
        return {
            "usage_evidence_sha256": accrual.usage_evidence_sha256,
            "billing_period_sha256": accrual.billing_period_sha256,
            "pricing_policy_sha256": accrual.pricing_policy_sha256,
            "request_count": accrual.request_count,
            "completed_blocks": accrual.completed_blocks,
            "remainder_requests": accrual.remainder_requests,
            "completed_block_gbp": accrual.completed_block_gbp,
            "policy_gbp_amount": accrual.policy_gbp_amount,
            "amount_truth": accrual.amount_truth,
            "account_currency": accrual.account_currency,
            "fx_truth": accrual.fx_truth,
            "account_currency_amount": accrual.account_currency_amount,
            "cash_truth": accrual.cash_truth,
            "allocation_state": accrual.allocation_state,
            "meter_continuity_truth": accrual.meter_continuity_truth,
            "usage_origin_truth": accrual.usage_origin_truth,
        }

    @dataclass(frozen=True, slots=True)
    class MatchbookApiCostAccrual:
        usage_evidence_sha256: str
        billing_period_sha256: str
        pricing_policy_sha256: str
        request_count: int
        completed_blocks: int
        remainder_requests: int
        completed_block_gbp: Decimal
        policy_gbp_amount: Decimal | None
        amount_truth: PolicyAmountTruth
        account_currency: str
        fx_truth: FxTruth
        account_currency_amount: Decimal | None
        cash_truth: CashTruth
        allocation_state: str
        meter_continuity_truth: MeterContinuityTruth
        usage_origin_truth: UsageOriginTruth
        evidence_sha256: str
        _capability_proof: bytes = field(repr=False, compare=False)

        def __post_init__(self) -> None:
            assert_accrual_shape(self)
            payload = accrual_payload(accrual_values(self))
            if not compare_digest(
                self._capability_proof,
                capability_proof(payload),
            ):
                raise error_cls(
                    "cost accrual lacks current process-local derivation capability"
                )
            if self.evidence_sha256 != public_digest(payload):
                raise error_cls("cost accrual digest mismatch")

    def assert_accrual_shape(accrual: MatchbookApiCostAccrual) -> None:
        for name in (
            "usage_evidence_sha256",
            "billing_period_sha256",
            "pricing_policy_sha256",
            "evidence_sha256",
        ):
            _sha(getattr(accrual, name), name)
        if type(accrual.request_count) is not int or accrual.request_count < 0:
            raise error_cls("request_count must be non-negative integer")
        if (
            type(accrual.completed_blocks) is not int
            or accrual.completed_blocks < 0
        ):
            raise error_cls("completed_blocks must be non-negative integer")
        if (
            type(accrual.remainder_requests) is not int
            or accrual.remainder_requests < 0
            or accrual.remainder_requests >= MATCHBOOK_GET_BLOCK_SIZE
        ):
            raise error_cls("remainder_requests is outside one GET block")
        expected_count = (
            accrual.completed_blocks * MATCHBOOK_GET_BLOCK_SIZE
            + accrual.remainder_requests
        )
        if accrual.request_count != expected_count:
            raise error_cls("request-count block arithmetic mismatch")
        expected_completed_gbp = (
            MATCHBOOK_GET_BLOCK_PRICE_GBP * accrual.completed_blocks
        )
        if accrual.completed_block_gbp != expected_completed_gbp:
            raise error_cls("completed-block GBP arithmetic mismatch")
        _decimal(accrual.completed_block_gbp, "completed_block_gbp")
        if accrual.policy_gbp_amount is not None:
            _decimal(accrual.policy_gbp_amount, "policy_gbp_amount")
        currency = _text(accrual.account_currency, "account_currency")
        if (
            len(currency) != 3
            or not currency.isascii()
            or currency != currency.upper()
        ):
            raise error_cls(
                "account_currency must be three uppercase ASCII letters"
            )
        if accrual.usage_origin_truth is not UsageOriginTruth.UNBOUND_PROCESS_LOCAL:
            raise error_cls(
                "this seam cannot mint transport-observed usage authority"
            )
        if (
            accrual.policy_gbp_amount is not None
            or accrual.amount_truth is not PolicyAmountTruth.USAGE_ORIGIN_UNBOUND
        ):
            raise error_cls(
                "unbound usage cannot mint a provider API-cost amount"
            )
        if currency == "GBP":
            if (
                accrual.fx_truth is not FxTruth.NOT_NEEDED_GBP
                or accrual.account_currency_amount
                != accrual.policy_gbp_amount
            ):
                raise error_cls(
                    "GBP account amount/fx state is inconsistent"
                )
        elif (
            accrual.fx_truth is not FxTruth.UNRESOLVED_PROVIDER_FX
            or accrual.account_currency_amount is not None
        ):
            raise error_cls("non-GBP provider FX remains unresolved")
        if (
            accrual.cash_truth is not CashTruth.UNRECONCILED
            or accrual.allocation_state
            != "UNALLOCATED_SHARED_PROVIDER_COST"
        ):
            raise error_cls(
                "this seam cannot mint cash truth or campaign allocation"
            )
        if (
            accrual.meter_continuity_truth
            is not MeterContinuityTruth.PROCESS_LOCAL_ONLY
        ):
            raise error_cls(
                "this seam cannot mint durable request-meter continuity"
            )

    def derive_matchbook_api_cost_accrual(
        *,
        usage: MatchbookApiUsageSnapshot,
        billing_period: MatchbookBillingPeriod,
        pricing_policy: MatchbookPricingPolicySnapshot,
        account_currency: str,
    ) -> MatchbookApiCostAccrual:
        assert_usage_authoritative(usage)
        if type(billing_period) is not MatchbookBillingPeriod:
            raise error_cls(
                "billing_period must be exact MatchbookBillingPeriod"
            )
        if type(pricing_policy) is not MatchbookPricingPolicySnapshot:
            raise error_cls(
                "pricing_policy must be exact MatchbookPricingPolicySnapshot"
            )
        if usage.billing_period_sha256 != billing_period.evidence_sha256:
            raise error_cls("usage belongs to a different billing period")
        if usage.pricing_policy_sha256 != pricing_policy.policy_sha256:
            raise error_cls("usage belongs to a different pricing policy")
        math = calculate_policy_math(usage.request_count, pricing_policy)
        policy_amount = None
        amount_truth = PolicyAmountTruth.USAGE_ORIGIN_UNBOUND
        currency = _text(account_currency, "account_currency")
        if (
            len(currency) != 3
            or not currency.isascii()
            or currency != currency.upper()
        ):
            raise error_cls(
                "account_currency must be three uppercase ASCII letters"
            )
        if currency == "GBP":
            fx_truth = FxTruth.NOT_NEEDED_GBP
            account_amount = policy_amount
        else:
            fx_truth = FxTruth.UNRESOLVED_PROVIDER_FX
            account_amount = None
        values = {
            "usage_evidence_sha256": usage.evidence_sha256,
            "billing_period_sha256": billing_period.evidence_sha256,
            "pricing_policy_sha256": pricing_policy.policy_sha256,
            "request_count": usage.request_count,
            "completed_blocks": math.completed_blocks,
            "remainder_requests": math.remainder_requests,
            "completed_block_gbp": math.completed_block_gbp,
            "policy_gbp_amount": policy_amount,
            "amount_truth": amount_truth,
            "account_currency": currency,
            "fx_truth": fx_truth,
            "account_currency_amount": account_amount,
            "cash_truth": CashTruth.UNRECONCILED,
            "allocation_state": "UNALLOCATED_SHARED_PROVIDER_COST",
            "meter_continuity_truth": usage.continuity_truth,
            "usage_origin_truth": usage.origin_truth,
        }
        payload = accrual_payload(values)
        return MatchbookApiCostAccrual(
            **values,
            evidence_sha256=public_digest(payload),
            _capability_proof=capability_proof(payload),
        )

    return (
        MatchbookApiUsageSnapshot,
        MatchbookApiRequestMeter,
        MatchbookApiCostAccrual,
        derive_matchbook_api_cost_accrual,
    )


(
    MatchbookApiUsageSnapshot,
    MatchbookApiRequestMeter,
    MatchbookApiCostAccrual,
    derive_matchbook_api_cost_accrual,
) = _build_runtime_capability()


__all__ = [
    "BillingCalendarBasis",
    "CashTruth",
    "FxTruth",
    "MATCHBOOK_FAIR_USAGE_URL",
    "MATCHBOOK_GET_BLOCK_PRICE_GBP",
    "MATCHBOOK_GET_BLOCK_SIZE",
    "MATCHBOOK_PRICING_URL",
    "MATCHBOOK_WRITE_QUALIFICATION",
    "MatchbookApiCostAccrual",
    "MatchbookApiCostEvidenceError",
    "MatchbookApiRequestMeter",
    "MatchbookApiUsageSnapshot",
    "MatchbookBillingPeriod",
    "MatchbookPolicyMath",
    "MatchbookPricingPolicySnapshot",
    "MeterContinuityTruth",
    "PolicyAmountTruth",
    "UsageOriginTruth",
    "calculate_policy_math",
    "configured_billing_period",
    "derive_matchbook_api_cost_accrual",
    "public_matchbook_pricing_policy",
]
