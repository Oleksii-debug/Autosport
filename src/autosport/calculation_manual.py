from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .calculation import CalculationEngine, CalculationResult
from .calculation_input import CalculationInputBoundary, MANUAL_CALCULATION_INPUT


_SERVICE_VERSION = "manual-calculation-service-v1"
_MAX_MARKET_SELECTIONS = 1_000
_MAX_PARLAY_LEGS = 100
_MAX_SERIES_ITEMS = 10_000


@dataclass(frozen=True, slots=True)
class ManualCalculationEvidence:
    """Structured manual-input result suitable for accessible copy/export."""

    service_version: str
    input_mode: str
    result: CalculationResult
    real_money_execution: bool
    evidence_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "service_version": self.service_version,
            "input_mode": self.input_mode,
            "result": self.result.as_dict(),
            "real_money_execution": self.real_money_execution,
            "evidence_sha256": self.evidence_sha256,
        }

    def to_text(self) -> str:
        """Return deterministic UTF-8-safe strict JSON for keyboard copy/export."""

        payload = self.as_dict()
        try:
            text = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ) + "\n"
            text.encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ValueError("manual calculation evidence is not canonical UTF-8 JSON") from exc
        return text


class ManualCalculationService:
    """Application boundary for user-authored V1 calculator input.

    Human-facing surfaces should call this service instead of invoking
    CalculationEngine directly. Every raw numeric value, collection, identifier,
    and enum-like text value is bounded and validated by CalculationInputBoundary
    before the mathematical engine is entered. CalculationEngine remains the sole
    formula/result-hash authority.
    """

    def __init__(
        self,
        engine: CalculationEngine | None = None,
        input_boundary: CalculationInputBoundary | None = None,
    ) -> None:
        if engine is not None and not isinstance(engine, CalculationEngine):
            raise ValueError("engine must be a CalculationEngine")
        if input_boundary is not None and not isinstance(
            input_boundary, CalculationInputBoundary
        ):
            raise ValueError("input_boundary must be a CalculationInputBoundary")
        self._engine = engine if engine is not None else CalculationEngine()
        self._input = (
            input_boundary if input_boundary is not None else MANUAL_CALCULATION_INPUT
        )

    def odds_conversion(self, decimal_odds: object) -> ManualCalculationEvidence:
        odds = self._decimal(decimal_odds, "decimal_odds")
        return self._bind(self._engine.odds_conversion(odds))

    def american_to_decimal_odds(
        self, american_odds: object
    ) -> ManualCalculationEvidence:
        american = self._decimal(american_odds, "american_odds")
        return self._bind(self._engine.american_to_decimal_odds(american))

    def fractional_to_decimal_odds(
        self,
        numerator: object,
        denominator: object,
    ) -> ManualCalculationEvidence:
        numerator_text = self._decimal(numerator, "fractional_numerator")
        denominator_text = self._decimal(denominator, "fractional_denominator")
        return self._bind(
            self._engine.fractional_to_decimal_odds(
                numerator_text,
                denominator_text,
            )
        )

    def implied_probability(self, decimal_odds: object) -> ManualCalculationEvidence:
        odds = self._decimal(decimal_odds, "decimal_odds")
        return self._bind(self._engine.implied_probability(odds))

    def multiplicative_devig(
        self,
        selection_odds: object,
    ) -> ManualCalculationEvidence:
        odds = self._input.decimal_mapping(
            selection_odds,
            field="selection_odds",
            maximum_items=_MAX_MARKET_SELECTIONS,
        )
        return self._bind(self._engine.multiplicative_devig(odds))

    def expected_return(
        self,
        probability: object,
        decimal_odds: object,
        stake: object = "1",
    ) -> ManualCalculationEvidence:
        probability_text = self._decimal(probability, "probability")
        odds = self._decimal(decimal_odds, "decimal_odds")
        stake_text = self._decimal(stake, "stake")
        return self._bind(
            self._engine.expected_return(probability_text, odds, stake_text)
        )

    def paper_payout(
        self,
        stake: object,
        decimal_odds: object,
    ) -> ManualCalculationEvidence:
        stake_text = self._decimal(stake, "stake")
        odds = self._decimal(decimal_odds, "decimal_odds")
        return self._bind(self._engine.paper_payout(stake_text, odds))

    def fractional_kelly(
        self,
        probability: object,
        decimal_odds: object,
        *,
        fraction: object = "1",
        cap: object = "1",
    ) -> ManualCalculationEvidence:
        probability_text = self._decimal(probability, "probability")
        odds = self._decimal(decimal_odds, "decimal_odds")
        fraction_text = self._decimal(fraction, "fraction")
        cap_text = self._decimal(cap, "cap")
        return self._bind(
            self._engine.fractional_kelly(
                probability_text,
                odds,
                fraction=fraction_text,
                cap=cap_text,
            )
        )

    def performance_summary(
        self,
        net_profit: object,
        turnover: object,
        starting_bankroll: object,
    ) -> ManualCalculationEvidence:
        profit_text = self._decimal(net_profit, "net_profit")
        turnover_text = self._decimal(turnover, "turnover")
        bankroll_text = self._decimal(starting_bankroll, "starting_bankroll")
        return self._bind(
            self._engine.performance_summary(
                profit_text,
                turnover_text,
                bankroll_text,
            )
        )

    def return_dispersion(
        self,
        values: object,
        *,
        sample: object = False,
    ) -> ManualCalculationEvidence:
        bounded_values = self._input.decimal_sequence(
            values,
            field="return",
            maximum_items=_MAX_SERIES_ITEMS,
        )
        if type(sample) is not bool:
            raise ValueError("sample must be a boolean")
        return self._bind(
            self._engine.return_dispersion(bounded_values, sample=sample)
        )

    def normal_confidence_interval(
        self,
        mean: object,
        standard_error: object,
        z_multiplier: object,
        *,
        assumption: object,
    ) -> ManualCalculationEvidence:
        mean_text = self._decimal(mean, "mean")
        error_text = self._decimal(standard_error, "standard_error")
        multiplier_text = self._decimal(z_multiplier, "z_multiplier")
        assumption_text = self._choice(
            assumption,
            field="assumption",
            allowed=("normal_approximation_acknowledged",),
        )
        return self._bind(
            self._engine.normal_confidence_interval(
                mean_text,
                error_text,
                multiplier_text,
                assumption=assumption_text,
            )
        )

    def paper_parlay(
        self,
        stake: object,
        decimal_odds: object,
        *,
        probabilities: object | None = None,
        probability_assumption: object | None = None,
    ) -> ManualCalculationEvidence:
        stake_text = self._decimal(stake, "stake")
        odds = self._input.decimal_sequence(
            decimal_odds,
            field="decimal_odds",
            maximum_items=_MAX_PARLAY_LEGS,
        )

        bounded_probabilities: tuple[str, ...] | None = None
        bounded_assumption: str | None = None
        if probabilities is None:
            if probability_assumption is not None:
                raise ValueError(
                    "probability_assumption requires supplied leg probabilities"
                )
        else:
            bounded_probabilities = self._input.decimal_sequence(
                probabilities,
                field="probabilities",
                maximum_items=_MAX_PARLAY_LEGS,
            )
            bounded_assumption = self._choice(
                probability_assumption,
                field="probability_assumption",
                allowed=("independent",),
            )

        return self._bind(
            self._engine.paper_parlay(
                stake_text,
                odds,
                probabilities=bounded_probabilities,
                probability_assumption=bounded_assumption,
            )
        )

    def finite_scenario_table(
        self,
        scenario_profits: object,
        *,
        completeness: object,
    ) -> ManualCalculationEvidence:
        profits = self._input.decimal_mapping(
            scenario_profits,
            field="scenario_profits",
            maximum_items=_MAX_MARKET_SELECTIONS,
        )
        completeness_text = self._choice(
            completeness,
            field="completeness",
            allowed=("complete", "partial"),
        )
        return self._bind(
            self._engine.finite_scenario_table(
                profits,
                completeness=completeness_text,
            )
        )

    def maximum_drawdown(self, balances: object) -> ManualCalculationEvidence:
        bounded_balances = self._input.decimal_sequence(
            balances,
            field="balances",
            maximum_items=_MAX_SERIES_ITEMS,
        )
        return self._bind(self._engine.maximum_drawdown(bounded_balances))

    def _decimal(self, value: object, field: str) -> str:
        return self._input.decimal_text(value, field=field)

    def _choice(
        self,
        value: object,
        *,
        field: str,
        allowed: tuple[str, ...],
    ) -> str:
        text = self._input.identifier(value, field=field)
        if text not in allowed:
            allowed_text = ", ".join(repr(item) for item in allowed)
            raise ValueError(f"{field} must be exactly one of: {allowed_text}")
        return text

    @staticmethod
    def _bind(result: CalculationResult) -> ManualCalculationEvidence:
        if type(result) is not CalculationResult:
            raise ValueError("calculation engine must return CalculationResult")
        payload = {
            "service_version": _SERVICE_VERSION,
            "input_mode": "manual",
            "result": result.as_dict(),
            "real_money_execution": False,
        }
        digest = _sha256(payload)
        return ManualCalculationEvidence(
            service_version=_SERVICE_VERSION,
            input_mode="manual",
            result=result,
            real_money_execution=False,
            evidence_sha256=digest,
        )


def _sha256(payload: dict[str, object]) -> str:
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded = canonical.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("manual calculation evidence is not canonical UTF-8 JSON") from exc
    return hashlib.sha256(encoded).hexdigest()
