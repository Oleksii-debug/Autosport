"""Fail-closed projection over supplied bookmaker account balance observations.

This module does not establish provider source authority. ``BookmakerAccountSnapshot`` is a
public, caller-constructible contract, so values accepted here remain supplied observations
rather than proof that a provider network returned them. The projection is useful for typed
reconciliation/display and deterministic multi-account analysis only. It must never authorize
bankroll allocation, risk, execution, money movement, settlement, or FX conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from typing import Iterable

from autosport.bookmaker_capability import BookmakerAccountSnapshot, BookmakerCapability


class ProviderLiquidityError(ValueError):
    """Raised when supplied balance observations cannot support a safe projection."""


def _aware_timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProviderLiquidityError(f"{field} must be a non-empty trimmed ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProviderLiquidityError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderLiquidityError(f"{field} must include a timezone offset")
    return parsed


def _canonical_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)


@dataclass(frozen=True, slots=True, init=False)
class ProviderBalanceProjectionComponent:
    """One supplied balance observation inside a non-authoritative projection.

    Instances are derived from ``BookmakerAccountSnapshot`` only to prevent callers from
    bypassing the projection validation rules. This does *not* make the upstream snapshot
    provider-issued: the upstream contract remains caller-constructible. Exposure/commission
    fields are preserved as supplied observations and are never subtracted here.
    """

    venue_id: str
    account_id: str
    adapter_id: str
    profile_id: str
    observation_id: str
    currency: str
    supplied_available_balance: Decimal
    observed_at: str
    snapshot_observed_at: str
    source_payload_sha256: str
    supplied_exposure: Decimal | None
    supplied_retained_commission: Decimal | None
    supplied_exposure_limit: Decimal | None

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "ProviderBalanceProjectionComponent is derived only by "
            "project_provider_balances"
        )

    @classmethod
    def _from_snapshot(
        cls, snapshot: BookmakerAccountSnapshot
    ) -> "ProviderBalanceProjectionComponent":
        balance = snapshot.balance
        if balance is None:
            raise ProviderLiquidityError(
                "balance observation is required; missing supplied balance is unknown, not zero"
            )
        instance = object.__new__(cls)
        values = {
            "venue_id": snapshot.profile.venue_id,
            "account_id": snapshot.profile.account_id,
            "adapter_id": snapshot.profile.adapter_id,
            "profile_id": snapshot.profile.profile_id,
            "observation_id": balance.observation_id,
            "currency": balance.currency,
            "supplied_available_balance": balance.available_balance,
            "observed_at": balance.observed_at,
            "snapshot_observed_at": snapshot.observed_at,
            "source_payload_sha256": balance.source_payload_sha256,
            "supplied_exposure": balance.exposure,
            "supplied_retained_commission": balance.retained_commission,
            "supplied_exposure_limit": balance.exposure_limit,
        }
        for field, value in values.items():
            object.__setattr__(instance, field, value)
        return instance

    @property
    def source_authority_proven(self) -> bool:
        """Shape-valid supplied snapshots do not prove provider acquisition authority."""
        return False

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "currency": self.currency,
            "observation_id": self.observation_id,
            "observed_at": self.observed_at,
            "profile_id": self.profile_id,
            "source_authority_proven": self.source_authority_proven,
            "source_payload_sha256": self.source_payload_sha256,
            "supplied_available_balance": str(self.supplied_available_balance),
            "supplied_exposure": _canonical_decimal(self.supplied_exposure),
            "supplied_exposure_limit": _canonical_decimal(self.supplied_exposure_limit),
            "supplied_retained_commission": _canonical_decimal(
                self.supplied_retained_commission
            ),
            "snapshot_observed_at": self.snapshot_observed_at,
            "venue_id": self.venue_id,
        }

    @property
    def component_id(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class GlobalProviderBalanceProjection:
    """Deterministic non-authoritative projection over supplied provider-account values.

    This intentionally has no unqualified scalar ``total``. Same-currency supplied
    observations can be summed for analysis, while mixed currencies remain separate.
    The projection never proves provider acquisition, bankroll ownership, reservation,
    allocation authority, execution feasibility, FX valuation, or cross-provider atomicity.
    """

    decision_at: str
    max_age_seconds: Decimal
    components: tuple[ProviderBalanceProjectionComponent, ...]
    oldest_observed_at: str
    newest_observed_at: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "GlobalProviderBalanceProjection is derived only by project_provider_balances"
        )

    @classmethod
    def _create(
        cls,
        *,
        decision_at: str,
        max_age: timedelta,
        components: tuple[ProviderBalanceProjectionComponent, ...],
    ) -> "GlobalProviderBalanceProjection":
        observed = [
            (_aware_timestamp(item.observed_at, "component.observed_at"), item)
            for item in components
        ]
        oldest = min(observed, key=lambda pair: pair[0])[1].observed_at
        newest = max(observed, key=lambda pair: pair[0])[1].observed_at
        instance = object.__new__(cls)
        object.__setattr__(instance, "decision_at", decision_at)
        object.__setattr__(
            instance,
            "max_age_seconds",
            Decimal(str(max_age.total_seconds())),
        )
        object.__setattr__(instance, "components", components)
        object.__setattr__(instance, "oldest_observed_at", oldest)
        object.__setattr__(instance, "newest_observed_at", newest)
        return instance

    @property
    def source_authority_proven(self) -> bool:
        """A projection over caller-constructible snapshots is never acquisition proof."""
        return False

    @property
    def allocation_authority_proven(self) -> bool:
        """No reservation/risk policy is bound by this projection."""
        return False

    @property
    def atomicity_proven(self) -> bool:
        """Cross-provider atomicity is never established by timestamp coincidence."""
        return False

    @property
    def currencies(self) -> tuple[str, ...]:
        return tuple(sorted({item.currency for item in self.components}))

    @property
    def observation_span_seconds(self) -> Decimal:
        oldest = _aware_timestamp(self.oldest_observed_at, "oldest_observed_at")
        newest = _aware_timestamp(self.newest_observed_at, "newest_observed_at")
        return Decimal(str((newest - oldest).total_seconds()))

    def supplied_balance_in(self, currency: str) -> Decimal:
        """Sum supplied same-currency observations without promoting source authority."""
        if currency not in self.currencies:
            raise ProviderLiquidityError(
                f"no fresh supplied balance observation for currency {currency!r}"
            )
        return sum(
            (
                item.supplied_available_balance
                for item in self.components
                if item.currency == currency
            ),
            start=Decimal("0"),
        )

    def single_currency_supplied_balance(self) -> Decimal:
        if len(self.currencies) != 1:
            raise ProviderLiquidityError(
                "mixed-currency balance projection has no unqualified scalar total"
            )
        return self.supplied_balance_in(self.currencies[0])

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "allocation_authority_proven": self.allocation_authority_proven,
            "atomicity_proven": self.atomicity_proven,
            "components": [
                item.to_canonical_dict()
                for item in sorted(
                    self.components,
                    key=lambda item: (item.currency, item.venue_id, item.account_id),
                )
            ],
            "decision_at": self.decision_at,
            "max_age_seconds": str(self.max_age_seconds),
            "newest_observed_at": self.newest_observed_at,
            "oldest_observed_at": self.oldest_observed_at,
            "source_authority_proven": self.source_authority_proven,
        }

    @property
    def projection_id(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


def project_provider_balances(
    snapshots: Iterable[BookmakerAccountSnapshot],
    *,
    decision_at: str,
    max_age: timedelta,
) -> GlobalProviderBalanceProjection:
    """Project fresh supplied account observations without widening their authority.

    The function rejects future/stale observations, duplicate provider/account scope,
    missing balances, and an empty evidence set. It never subtracts supplied exposure
    from supplied ``available_balance``. Passing a shape-valid snapshot here does not
    prove provider acquisition; the returned object explicitly records that limitation.
    """

    decision_time = _aware_timestamp(decision_at, "decision_at")
    if not isinstance(max_age, timedelta):
        raise ProviderLiquidityError("max_age must be a datetime.timedelta")
    if max_age < timedelta(0):
        raise ProviderLiquidityError("max_age must be non-negative")

    resolved: list[ProviderBalanceProjectionComponent] = []
    seen_accounts: set[tuple[str, str]] = set()

    for snapshot in snapshots:
        if not isinstance(snapshot, BookmakerAccountSnapshot):
            raise ProviderLiquidityError(
                "snapshots must contain only BookmakerAccountSnapshot values"
            )
        snapshot.profile.require(BookmakerCapability.BALANCE_READ)
        if BookmakerCapability.BALANCE_READ not in snapshot.observed_capabilities:
            raise ProviderLiquidityError(
                "balance_read was not observed for supplied account snapshot"
            )
        if snapshot.balance is None:
            raise ProviderLiquidityError(
                "balance observation is required; missing supplied balance is unknown, not zero"
            )

        snapshot_time = _aware_timestamp(snapshot.observed_at, "snapshot.observed_at")
        balance_time = _aware_timestamp(
            snapshot.balance.observed_at,
            "balance.observed_at",
        )
        if snapshot_time > decision_time:
            raise ProviderLiquidityError("supplied account snapshot is from the future")
        if balance_time > decision_time:
            raise ProviderLiquidityError("supplied balance observation is from the future")
        if decision_time - balance_time > max_age:
            raise ProviderLiquidityError("supplied balance observation is stale")

        scope = (snapshot.profile.venue_id, snapshot.profile.account_id)
        if scope in seen_accounts:
            raise ProviderLiquidityError(
                "duplicate provider/account projection scope would double-count supplied capital"
            )
        seen_accounts.add(scope)
        resolved.append(ProviderBalanceProjectionComponent._from_snapshot(snapshot))

    if not resolved:
        raise ProviderLiquidityError(
            "at least one fresh supplied balance observation is required; absence is unknown"
        )

    components = tuple(
        sorted(
            resolved,
            key=lambda item: (item.currency, item.venue_id, item.account_id),
        )
    )
    return GlobalProviderBalanceProjection._create(
        decision_at=decision_at,
        max_age=max_age,
        components=components,
    )
