"""Truth-bounded Betfair LIMIT currency-threshold evaluation.

This module owns only deterministic arithmetic over one versioned first-party
Currency Parameters snapshot.  It deliberately does not own account/session
identity, jurisdiction, market permission, price-ladder validity, liquidity,
funds, provider acceptance, or execution authority.

The provider does not expose an API rule-version/effective-interval identifier
for this static table.  Therefore this snapshot is useful for structural
composition and regression tests, but it can never by itself authorize a
provider write.  The below-minimum stake exception is additionally
jurisdiction-gated by Betfair (UK & International only), so that path always
remains unresolved here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum
from types import MappingProxyType

RULESET_ID = "betfair-currency-parameters-page2686993-v3"
RULESET_SOURCE_URL = (
    "https://betfair-developer-docs.atlassian.net/wiki/"
    "pages/viewpage.action?pageId=2686993&pageVersion=3"
)
PLACE_ORDERS_SOURCE_URL = (
    "https://betfair-developer-docs.atlassian.net/wiki/spaces/"
    "1smk3cen4v3lu3yomq5qye0ni/pages/2687496/"
)
RULESET_EFFECTIVE_INTERVAL_PROVEN = False
_MAX_DECIMAL_DIGITS = 64
_MAX_ABS_EXPONENT = 18


class BetfairCurrencyThresholdError(ValueError):
    """Raised when a LIMIT/currency projection is malformed."""


def _currency_code(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or value != value.upper()
    ):
        raise BetfairCurrencyThresholdError(
            "currency_code must be three-letter uppercase ASCII"
        )
    return value


def _positive_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise BetfairCurrencyThresholdError(
            f"{field} must be a positive finite Decimal"
        )
    digits = value.as_tuple().digits
    exponent = value.as_tuple().exponent
    if len(digits) > _MAX_DECIMAL_DIGITS or abs(exponent) > _MAX_ABS_EXPONENT:
        raise BetfairCurrencyThresholdError(f"{field} exceeds bounded Decimal shape")
    return value


def _exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    left_digits = len(left.as_tuple().digits)
    right_digits = len(right.as_tuple().digits)
    with localcontext() as context:
        context.prec = left_digits + right_digits + 4
        result = left * right
    return _positive_decimal(result, "gross_payout")


class BetfairCurrencyThresholdState(str, Enum):
    """Truth-bounded result for the represented standard-size LIMIT slice."""

    SNAPSHOT_STANDARD_MINIMUM_MET = "SNAPSHOT_STANDARD_MINIMUM_MET"
    SNAPSHOT_LOWER_PAYOUT_MECHANIC_MET_REQUIRES_JURISDICTION = (
        "SNAPSHOT_LOWER_PAYOUT_MECHANIC_MET_REQUIRES_JURISDICTION"
    )
    SNAPSHOT_BELOW_THRESHOLDS = "SNAPSHOT_BELOW_THRESHOLDS"
    TARGET_SIZING_OUTSIDE_SCOPE = "TARGET_SIZING_OUTSIDE_SCOPE"
    UNSUPPORTED_CURRENCY = "UNSUPPORTED_CURRENCY"


@dataclass(frozen=True, slots=True)
class BetfairCurrencyParameters:
    currency_code: str
    min_bet_size: Decimal
    min_bsp_liability: Decimal
    min_bet_payout: Decimal

    def __post_init__(self) -> None:
        _currency_code(self.currency_code)
        _positive_decimal(self.min_bet_size, "min_bet_size")
        _positive_decimal(self.min_bsp_liability, "min_bsp_liability")
        _positive_decimal(self.min_bet_payout, "min_bet_payout")


_RULE_ROWS = (
    ("GBP", "2", "10", "10"),
    ("EUR", "2", "20", "20"),
    ("USD", "3", "20", "20"),
    ("HKD", "25", "125", "125"),
    ("AUD", "5", "30", "30"),
    ("CAD", "6", "30", "30"),
    ("DKK", "30", "150", "150"),
    ("NOK", "30", "150", "150"),
    ("SEK", "30", "150", "150"),
    ("SGD", "6", "30", "30"),
    ("RON", "10", "50", "50"),
    ("BRL", "10", "50", "50"),
    ("MXN", "60", "300", "300"),
    ("PEN", "10", "50", "50"),
    ("HUF", "800", "4000", "4000"),
    ("ISK", "350", "1750", "1750"),
    ("NZD", "2", "10", "10"),
    ("ARS", "100", "500", "500"),
    ("GEL", "10", "50", "50"),
)
CURRENCY_PARAMETERS = MappingProxyType(
    {
        code: BetfairCurrencyParameters(
            currency_code=code,
            min_bet_size=Decimal(min_bet),
            min_bsp_liability=Decimal(min_bsp),
            min_bet_payout=Decimal(min_payout),
        )
        for code, min_bet, min_bsp, min_payout in _RULE_ROWS
    }
)


@dataclass(frozen=True, slots=True)
class BetfairCurrencyThresholdAssessment:
    state: BetfairCurrencyThresholdState
    currency_code: str
    side: str
    size: Decimal | None
    price: Decimal
    gross_payout: Decimal | None
    min_bet_size: Decimal | None
    min_bet_payout: Decimal | None
    ruleset_id: str = RULESET_ID
    ruleset_effective_interval_proven: bool = RULESET_EFFECTIVE_INTERVAL_PROVEN

    def __post_init__(self) -> None:
        if not isinstance(self.state, BetfairCurrencyThresholdState):
            raise BetfairCurrencyThresholdError(
                "state must be BetfairCurrencyThresholdState"
            )
        _currency_code(self.currency_code)
        if self.side not in {"BACK", "LAY"}:
            raise BetfairCurrencyThresholdError("side must be exactly BACK or LAY")
        _positive_decimal(self.price, "price")
        if self.price <= Decimal("1"):
            raise BetfairCurrencyThresholdError("price must be greater than 1")
        for field, value in (
            ("size", self.size),
            ("gross_payout", self.gross_payout),
            ("min_bet_size", self.min_bet_size),
            ("min_bet_payout", self.min_bet_payout),
        ):
            if value is not None:
                _positive_decimal(value, field)
        if self.ruleset_id != RULESET_ID:
            raise BetfairCurrencyThresholdError("ruleset_id is product-owned")
        if self.ruleset_effective_interval_proven is not False:
            raise BetfairCurrencyThresholdError(
                "static ruleset cannot claim a proven effective interval"
            )

        unsupported = self.state is BetfairCurrencyThresholdState.UNSUPPORTED_CURRENCY
        target = self.state is BetfairCurrencyThresholdState.TARGET_SIZING_OUTSIDE_SCOPE
        if unsupported:
            if any(
                value is not None
                for value in (
                    self.size,
                    self.gross_payout,
                    self.min_bet_size,
                    self.min_bet_payout,
                )
            ):
                raise BetfairCurrencyThresholdError(
                    "unsupported currency cannot carry fabricated threshold values"
                )
        elif target:
            if self.size is not None or self.gross_payout is not None:
                raise BetfairCurrencyThresholdError(
                    "target sizing cannot be reinterpreted as standard-size economics"
                )
            if self.min_bet_size is None or self.min_bet_payout is None:
                raise BetfairCurrencyThresholdError(
                    "known target-sizing currency must preserve rule thresholds"
                )
        elif any(
            value is None
            for value in (
                self.size,
                self.gross_payout,
                self.min_bet_size,
                self.min_bet_payout,
            )
        ):
            raise BetfairCurrencyThresholdError(
                "standard-size assessment requires complete threshold economics"
            )

    @property
    def snapshot_standard_minimum_met(self) -> bool:
        return self.state is BetfairCurrencyThresholdState.SNAPSHOT_STANDARD_MINIMUM_MET

    @property
    def low_stake_exception_candidate(self) -> bool:
        return (
            self.state
            is BetfairCurrencyThresholdState.SNAPSHOT_LOWER_PAYOUT_MECHANIC_MET_REQUIRES_JURISDICTION
        )

    @property
    def account_currency_bound(self) -> bool:
        return False

    @property
    def jurisdiction_bound(self) -> bool:
        return False

    @property
    def market_admissibility_proven(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False


def evaluate_standard_limit_currency_thresholds(
    *,
    currency_code: str,
    side: str,
    price: Decimal,
    size: Decimal | None,
    bet_target_type: str | None = None,
) -> BetfairCurrencyThresholdAssessment:
    """Compare a standard-size LIMIT against the historical threshold snapshot.

    ``size`` is Betfair LIMIT's backer's-stake field for both BACK and LAY.
    Target-sized orders use different economics and remain outside this module.

    A below-minimum stake with sufficient payout is *not* promoted to satisfied:
    Betfair restricts that exception by jurisdiction, and this module has no
    product-owned jurisdiction authority.
    """

    code = _currency_code(currency_code)
    if side not in {"BACK", "LAY"}:
        raise BetfairCurrencyThresholdError("side must be exactly BACK or LAY")
    value_price = _positive_decimal(price, "price")
    if value_price <= Decimal("1"):
        raise BetfairCurrencyThresholdError("price must be greater than 1")
    if bet_target_type not in {None, "PAYOUT", "BACKERS_PROFIT"}:
        raise BetfairCurrencyThresholdError(
            "bet_target_type must be PAYOUT, BACKERS_PROFIT, or None"
        )

    rules = CURRENCY_PARAMETERS.get(code)
    if rules is None:
        if size is not None:
            _positive_decimal(size, "size")
        return BetfairCurrencyThresholdAssessment(
            state=BetfairCurrencyThresholdState.UNSUPPORTED_CURRENCY,
            currency_code=code,
            side=side,
            size=None,
            price=value_price,
            gross_payout=None,
            min_bet_size=None,
            min_bet_payout=None,
        )

    if bet_target_type is not None:
        if size is not None:
            _positive_decimal(size, "size")
        return BetfairCurrencyThresholdAssessment(
            state=BetfairCurrencyThresholdState.TARGET_SIZING_OUTSIDE_SCOPE,
            currency_code=code,
            side=side,
            size=None,
            price=value_price,
            gross_payout=None,
            min_bet_size=rules.min_bet_size,
            min_bet_payout=rules.min_bet_payout,
        )

    if size is None:
        raise BetfairCurrencyThresholdError("standard-size LIMIT requires size")
    value_size = _positive_decimal(size, "size")
    gross_payout = _exact_multiply(value_size, value_price)

    if value_size >= rules.min_bet_size:
        state = BetfairCurrencyThresholdState.SNAPSHOT_STANDARD_MINIMUM_MET
    elif gross_payout >= rules.min_bet_payout:
        state = (
            BetfairCurrencyThresholdState.SNAPSHOT_LOWER_PAYOUT_MECHANIC_MET_REQUIRES_JURISDICTION
        )
    else:
        state = BetfairCurrencyThresholdState.SNAPSHOT_BELOW_THRESHOLDS

    return BetfairCurrencyThresholdAssessment(
        state=state,
        currency_code=code,
        side=side,
        size=value_size,
        price=value_price,
        gross_payout=gross_payout,
        min_bet_size=rules.min_bet_size,
        min_bet_payout=rules.min_bet_payout,
    )
