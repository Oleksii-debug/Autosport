from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    Underflow,
    localcontext,
)
from typing import Mapping, Sequence


_CALCULATION_VERSION = 1
_ENGINE_VERSION = "calculation-engine-v1"
_MAX_SIGNIFICANT_DIGITS = 80
_MAX_ADJUSTED_EXPONENT = 100
_MAX_MARKET_SELECTIONS = 1_000
_MAX_SERIES_ITEMS = 10_000
_MAX_PARLAY_LEGS = 100


def _build_decimal_context() -> Context:
    """Build the engine context without inheriting mutable process defaults."""

    return Context(
        prec=160,
        rounding=ROUND_HALF_EVEN,
        Emin=-999,
        Emax=999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow, Underflow],
    )


_CONTEXT = _build_decimal_context()


@dataclass(frozen=True, slots=True)
class CalculationResult:
    calculation_id: str
    version: int
    engine_version: str
    method: str
    classification: str
    inputs: tuple[tuple[str, str], ...]
    input_units: tuple[tuple[str, str], ...]
    assumptions: tuple[str, ...]
    outputs: tuple[tuple[str, str], ...]
    output_units: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]
    input_hash: str
    result_hash: str

    @property
    def exact(self) -> bool:
        return self.classification == "exact"

    def as_dict(self) -> dict[str, object]:
        return {
            "calculation_id": self.calculation_id,
            "version": self.version,
            "engine_version": self.engine_version,
            "method": self.method,
            "classification": self.classification,
            "inputs": dict(self.inputs),
            "input_units": dict(self.input_units),
            "assumptions": list(self.assumptions),
            "outputs": dict(self.outputs),
            "output_units": dict(self.output_units),
            "warnings": list(self.warnings),
            "input_hash": self.input_hash,
            "result_hash": self.result_hash,
        }


class CalculationEngine:
    """Deterministic, presentation-independent V1 research calculators.

    Inputs deliberately reject binary floats and booleans. Money-like values are
    represented as Decimal text and exact calculations fail closed if the private
    Decimal context would round. Division/statistical-model calculations are
    explicitly labelled approximate_decimal rather than being presented as exact.
    """

    def odds_conversion(self, decimal_odds: Decimal | str | int) -> CalculationResult:
        """Convert decimal odds without silently applying bookmaker rounding conventions."""

        odds = _decimal(decimal_odds, field="decimal_odds", greater_than=Decimal("1"))
        net = _exact_subtract(odds, Decimal("1"), context="decimal odds conversion")
        numerator, denominator = net.as_integer_ratio()
        fractional_numerator = Decimal(numerator)
        fractional_denominator = Decimal(denominator)
        if odds >= Decimal("2"):
            with localcontext(_CONTEXT) as ctx:
                ctx.clear_flags()
                american = net * Decimal("100")
                if ctx.flags[Inexact]:
                    raise ValueError("odds conversion would require rounding")
            classification = "exact"
            warnings: tuple[str, ...] = (
                "American output is the unrounded mathematical conversion; bookmaker display conventions may round it",
            )
        else:
            american = _divide(Decimal("-100"), net)
            classification = "approximate_decimal"
            warnings = (
                "American output is the unrounded mathematical conversion in the deterministic decimal context",
                "bookmaker display conventions may round American odds",
            )
        return _result(
            calculation_id="odds_conversion",
            method="decimal_to_fractional_and_american_unrounded",
            classification=classification,
            inputs={"decimal_odds": odds},
            input_units={"decimal_odds": "decimal_odds"},
            assumptions=("decimal odds are the supplied analysis input",),
            outputs={
                "decimal_odds": odds,
                "fractional_numerator": fractional_numerator,
                "fractional_denominator": fractional_denominator,
                "american_odds_unrounded": american,
            },
            output_units={
                "decimal_odds": "decimal_odds",
                "fractional_numerator": "fractional_odds_numerator",
                "fractional_denominator": "fractional_odds_denominator",
                "american_odds_unrounded": "american_odds",
            },
            warnings=warnings,
        )

    def american_to_decimal_odds(
        self,
        american_odds: Decimal | str | int,
    ) -> CalculationResult:
        american = _decimal(american_odds, field="american_odds")
        if Decimal("-100") < american < Decimal("100"):
            raise ValueError("american_odds must be at most -100 or at least 100")
        if american >= Decimal("100"):
            try:
                with localcontext(_CONTEXT) as ctx:
                    ctx.clear_flags()
                    decimal_odds = Decimal("1") + (american / Decimal("100"))
                    if ctx.flags[Inexact]:
                        raise ValueError("American-to-decimal conversion would require rounding")
            except DecimalException as exc:
                raise ValueError("American-to-decimal conversion is outside the supported decimal range") from exc
            classification = "exact"
            warnings: tuple[str, ...] = ()
        else:
            decimal_odds = _approximate_add(
                Decimal("1"),
                _divide(Decimal("100"), abs(american)),
                context="American-to-decimal conversion",
            )
            classification = "approximate_decimal"
            warnings = ("division is rounded in the deterministic decimal context",)
        return _result(
            calculation_id="american_to_decimal_odds",
            method="american_to_decimal_unrounded",
            classification=classification,
            inputs={"american_odds": american},
            input_units={"american_odds": "american_odds"},
            assumptions=("American odds are supplied without bookmaker display rounding reversal",),
            outputs={"decimal_odds": decimal_odds},
            output_units={"decimal_odds": "decimal_odds"},
            warnings=warnings,
        )

    def fractional_to_decimal_odds(
        self,
        numerator: Decimal | str | int,
        denominator: Decimal | str | int,
    ) -> CalculationResult:
        numerator_value = _decimal(numerator, field="fractional_numerator", greater_than=Decimal("0"))
        denominator_value = _decimal(denominator, field="fractional_denominator", greater_than=Decimal("0"))
        decimal_odds = _approximate_add(
            Decimal("1"),
            _divide(numerator_value, denominator_value),
            context="fractional-to-decimal conversion",
        )
        return _result(
            calculation_id="fractional_to_decimal_odds",
            method="fractional_ratio_to_decimal",
            classification="approximate_decimal",
            inputs={
                "fractional_numerator": numerator_value,
                "fractional_denominator": denominator_value,
            },
            input_units={
                "fractional_numerator": "fractional_odds_numerator",
                "fractional_denominator": "fractional_odds_denominator",
            },
            assumptions=("fractional odds are supplied as a positive numerator/denominator ratio",),
            outputs={"decimal_odds": decimal_odds},
            output_units={"decimal_odds": "decimal_odds"},
            warnings=("division is rounded in the deterministic decimal context",),
        )

    def implied_probability(self, decimal_odds: Decimal | str | int) -> CalculationResult:
        odds = _decimal(decimal_odds, field="decimal_odds", greater_than=Decimal("1"))
        probability = _divide(Decimal("1"), odds)
        return _result(
            calculation_id="implied_probability",
            method="reciprocal_decimal_odds",
            classification="approximate_decimal",
            inputs={"decimal_odds": odds},
            input_units={"decimal_odds": "decimal_odds"},
            assumptions=("decimal odds are the supplied analysis input",),
            outputs={
                "break_even_probability": probability,
                "implied_probability": probability,
            },
            output_units={
                "break_even_probability": "probability",
                "implied_probability": "probability",
            },
            warnings=("division is rounded in the deterministic decimal context",),
        )

    def multiplicative_devig(
        self,
        selection_odds: Mapping[str, Decimal | str | int],
    ) -> CalculationResult:
        if not isinstance(selection_odds, Mapping) or len(selection_odds) < 2:
            raise ValueError("multiplicative de-vig requires at least two selections")
        if len(selection_odds) > _MAX_MARKET_SELECTIONS:
            raise ValueError("multiplicative de-vig selection count exceeds the supported limit")
        normalized: dict[str, Decimal] = {}
        for raw_key, raw_odds in selection_odds.items():
            key = _text_key(raw_key, field="selection")
            if key in normalized:
                raise ValueError("selection identifiers must be unique")
            normalized[key] = _decimal(
                raw_odds,
                field=f"decimal_odds[{key}]",
                greater_than=Decimal("1"),
            )
        ordered = sorted(normalized.items())
        raw_probabilities = {key: _divide(Decimal("1"), odds) for key, odds in ordered}
        with localcontext(_CONTEXT) as ctx:
            ctx.clear_flags()
            total = sum(raw_probabilities.values(), Decimal("0"))
        if not total.is_finite() or total <= 0:
            raise ValueError("implied probability total must be finite and positive")
        fair = {key: _divide(probability, total) for key, probability in raw_probabilities.items()}

        inputs = {f"decimal_odds.{key}": odds for key, odds in ordered}
        outputs: dict[str, Decimal] = {
            "overround": total,
            "market_margin": _exact_subtract(
                total,
                Decimal("1"),
                context="market margin",
            ),
        }
        output_units: dict[str, str] = {
            "overround": "probability_sum",
            "market_margin": "fraction",
        }
        for key in sorted(raw_probabilities):
            outputs[f"raw_implied_probability.{key}"] = raw_probabilities[key]
            outputs[f"fair_probability.{key}"] = fair[key]
            outputs[f"fair_decimal_odds.{key}"] = _divide(Decimal("1"), fair[key])
            output_units[f"raw_implied_probability.{key}"] = "probability"
            output_units[f"fair_probability.{key}"] = "probability"
            output_units[f"fair_decimal_odds.{key}"] = "decimal_odds"
        input_units = {name: "decimal_odds" for name in inputs}
        return _result(
            calculation_id="multiplicative_devig",
            method="multiplicative_normalization",
            classification="approximate_decimal",
            inputs=inputs,
            input_units=input_units,
            assumptions=(
                "all selections belong to one supplied market",
                "multiplicative normalization is a modelling method, not objective fair value",
            ),
            outputs=outputs,
            output_units=output_units,
            warnings=("division is rounded in the deterministic decimal context",),
        )

    def expected_return(
        self,
        probability: Decimal | str | int,
        decimal_odds: Decimal | str | int,
        stake: Decimal | str | int = Decimal("1"),
    ) -> CalculationResult:
        p = _decimal(
            probability,
            field="probability",
            minimum=Decimal("0"),
            maximum=Decimal("1"),
        )
        odds = _decimal(decimal_odds, field="decimal_odds", greater_than=Decimal("1"))
        stake_value = _decimal(stake, field="stake", minimum=Decimal("0"))
        try:
            with localcontext(_CONTEXT) as ctx:
                ctx.clear_flags()
                expected_return_per_unit = p * odds
                expected_profit_per_unit = expected_return_per_unit - Decimal("1")
                expected_profit = expected_profit_per_unit * stake_value
                if ctx.flags[Inexact]:
                    raise ValueError("expected-return arithmetic would require rounding")
        except DecimalException as exc:
            raise ValueError("expected-return arithmetic is outside the supported decimal range") from exc
        return _result(
            calculation_id="expected_return",
            method="bernoulli_expected_return",
            classification="exact",
            inputs={"probability": p, "decimal_odds": odds, "stake": stake_value},
            input_units={
                "probability": "probability",
                "decimal_odds": "decimal_odds",
                "stake": "paper_currency",
            },
            assumptions=("probability is supplied by the caller and is not inferred by this calculator",),
            outputs={
                "expected_return_per_unit": expected_return_per_unit,
                "expected_profit_per_unit": expected_profit_per_unit,
                "expected_profit": expected_profit,
            },
            output_units={
                "expected_return_per_unit": "multiple",
                "expected_profit_per_unit": "paper_currency_per_unit_stake",
                "expected_profit": "paper_currency",
            },
        )

    def paper_payout(
        self,
        stake: Decimal | str | int,
        decimal_odds: Decimal | str | int,
    ) -> CalculationResult:
        stake_value = _decimal(stake, field="stake", minimum=Decimal("0"))
        odds = _decimal(decimal_odds, field="decimal_odds", greater_than=Decimal("1"))
        try:
            with localcontext(_CONTEXT) as ctx:
                ctx.clear_flags()
                payout = stake_value * odds
                profit = payout - stake_value
                if ctx.flags[Inexact]:
                    raise ValueError("paper payout arithmetic would require rounding")
        except DecimalException as exc:
            raise ValueError("paper payout arithmetic is outside the supported decimal range") from exc
        return _result(
            calculation_id="paper_payout",
            method="decimal_odds_payout",
            classification="exact",
            inputs={"stake": stake_value, "decimal_odds": odds},
            input_units={"stake": "paper_currency", "decimal_odds": "decimal_odds"},
            assumptions=("paper-only calculation; no real-money execution authority",),
            outputs={"payout": payout, "profit": profit},
            output_units={"payout": "paper_currency", "profit": "paper_currency"},
        )

    def fractional_kelly(
        self,
        probability: Decimal | str | int,
        decimal_odds: Decimal | str | int,
        fraction: Decimal | str | int = Decimal("1"),
        cap: Decimal | str | int = Decimal("1"),
    ) -> CalculationResult:
        p = _decimal(
            probability,
            field="probability",
            minimum=Decimal("0"),
            maximum=Decimal("1"),
        )
        odds = _decimal(decimal_odds, field="decimal_odds", greater_than=Decimal("1"))
        fraction_value = _decimal(
            fraction,
            field="fraction",
            minimum=Decimal("0"),
            maximum=Decimal("1"),
        )
        cap_value = _decimal(
            cap,
            field="cap",
            minimum=Decimal("0"),
            maximum=Decimal("1"),
        )
        try:
            with localcontext(_CONTEXT):
                edge = p * odds - Decimal("1")
                raw_kelly = Decimal("0") if edge <= 0 else _divide(edge, odds - Decimal("1"))
                fractional = raw_kelly * fraction_value
                recommended = min(fractional, cap_value)
        except DecimalException as exc:
            raise ValueError("Kelly arithmetic is outside the supported decimal range") from exc
        return _result(
            calculation_id="fractional_kelly",
            method="capped_fractional_kelly",
            classification="approximate_decimal",
            inputs={
                "probability": p,
                "decimal_odds": odds,
                "fraction": fraction_value,
                "cap": cap_value,
            },
            input_units={
                "probability": "probability",
                "decimal_odds": "decimal_odds",
                "fraction": "fraction_of_full_kelly",
                "cap": "fraction_of_bankroll",
            },
            assumptions=(
                "probability is a caller-supplied research assumption",
                "single-position Kelly formula; dependence with other positions is not modelled",
                "paper research only; result is not execution authority",
            ),
            outputs={
                "edge_per_unit": edge,
                "full_kelly_fraction": raw_kelly,
                "fractional_kelly_fraction": fractional,
                "capped_fraction": recommended,
            },
            output_units={
                "edge_per_unit": "paper_currency_per_unit_stake",
                "full_kelly_fraction": "fraction_of_bankroll",
                "fractional_kelly_fraction": "fraction_of_bankroll",
                "capped_fraction": "fraction_of_bankroll",
            },
            warnings=("Kelly division is rounded in the deterministic decimal context",),
        )

    def performance_summary(
        self,
        net_profit: Decimal | str | int,
        turnover: Decimal | str | int,
        starting_bankroll: Decimal | str | int,
    ) -> CalculationResult:
        profit = _decimal(net_profit, field="net_profit")
        turnover_value = _decimal(turnover, field="turnover", greater_than=Decimal("0"))
        bankroll = _decimal(
            starting_bankroll,
            field="starting_bankroll",
            greater_than=Decimal("0"),
        )
        roi = _divide(profit, turnover_value)
        yield_value = _divide(profit, turnover_value)
        bankroll_return = _divide(profit, bankroll)
        return _result(
            calculation_id="performance_summary",
            method="paper_return_ratios",
            classification="approximate_decimal",
            inputs={
                "net_profit": profit,
                "turnover": turnover_value,
                "starting_bankroll": bankroll,
            },
            input_units={
                "net_profit": "paper_currency",
                "turnover": "paper_currency",
                "starting_bankroll": "paper_currency",
            },
            assumptions=(
                "profit, turnover, and bankroll refer to the same paper accounting scope",
                "turnover is the settled-stake denominator used by canonical product ROI and yield semantics",
            ),
            outputs={
                "roi": roi,
                "yield": yield_value,
                "bankroll_return": bankroll_return,
                "turnover": turnover_value,
            },
            output_units={
                "roi": "fraction",
                "yield": "fraction",
                "bankroll_return": "fraction",
                "turnover": "paper_currency",
            },
            warnings=("ratio division is rounded in the deterministic decimal context",),
        )

    def return_dispersion(
        self,
        values: Sequence[Decimal | str | int],
        *,
        sample: bool = False,
    ) -> CalculationResult:
        series = _decimal_series(values, field="return", minimum=None)
        if not isinstance(sample, bool):
            raise ValueError("sample must be a boolean")
        if sample and len(series) < 2:
            raise ValueError("sample variance requires at least two observations")
        denominator = len(series) - 1 if sample else len(series)
        try:
            with localcontext(_CONTEXT):
                total = sum(series, Decimal("0"))
                mean = total / Decimal(len(series))
                squared = sum(((value - mean) * (value - mean) for value in series), Decimal("0"))
                variance = squared / Decimal(denominator)
                standard_deviation = variance.sqrt()
        except DecimalException as exc:
            raise ValueError("dispersion arithmetic is outside the supported decimal range") from exc
        method = "sample_variance_n_minus_1" if sample else "population_variance_n"
        assumptions = (
            "the supplied return series is the complete population of interest",
        ) if not sample else (
            "the supplied return series is treated as a sample and uses the n-1 variance denominator",
        )
        return _result(
            calculation_id="return_dispersion",
            method=method,
            classification="approximate_decimal",
            inputs={f"return.{index}": value for index, value in enumerate(series)},
            input_units={f"return.{index}": "return_value" for index in range(len(series))},
            assumptions=assumptions,
            outputs={
                "count": Decimal(len(series)),
                "mean": mean,
                "variance": variance,
                "standard_deviation": standard_deviation,
            },
            output_units={
                "count": "count",
                "mean": "return_value",
                "variance": "return_value_squared",
                "standard_deviation": "return_value",
            },
            warnings=("division and square-root operations are rounded in the deterministic decimal context",),
        )

    def normal_confidence_interval(
        self,
        mean: Decimal | str | int,
        standard_error: Decimal | str | int,
        z_multiplier: Decimal | str | int,
        *,
        assumption: str | None = None,
    ) -> CalculationResult:
        if assumption != "normal_approximation_acknowledged":
            raise ValueError(
                "normal confidence interval requires explicit assumption='normal_approximation_acknowledged'"
            )
        mean_value = _decimal(mean, field="mean")
        standard_error_value = _decimal(
            standard_error,
            field="standard_error",
            minimum=Decimal("0"),
        )
        z_value = _decimal(z_multiplier, field="z_multiplier", greater_than=Decimal("0"))
        try:
            with localcontext(_CONTEXT) as ctx:
                ctx.clear_flags()
                margin = standard_error_value * z_value
                lower = mean_value - margin
                upper = mean_value + margin
                if ctx.flags[Inexact]:
                    raise ValueError("confidence-interval arithmetic would require rounding")
        except DecimalException as exc:
            raise ValueError("confidence-interval arithmetic is outside the supported decimal range") from exc
        return _result(
            calculation_id="normal_confidence_interval",
            method="caller_supplied_normal_z_interval",
            classification="approximate_decimal",
            inputs={
                "mean": mean_value,
                "standard_error": standard_error_value,
                "z_multiplier": z_value,
            },
            input_units={
                "mean": "analysis_value",
                "standard_error": "analysis_value",
                "z_multiplier": "standard_normal_multiplier",
            },
            assumptions=(
                "caller explicitly acknowledges that a normal-approximation interval is appropriate for the supplied statistic",
                "standard_error and z_multiplier are caller-supplied evidence and are not inferred by this calculator",
            ),
            outputs={
                "lower_bound": lower,
                "upper_bound": upper,
                "margin": margin,
                "mean": mean_value,
            },
            output_units={
                "lower_bound": "analysis_value",
                "upper_bound": "analysis_value",
                "margin": "analysis_value",
                "mean": "analysis_value",
            },
            warnings=(
                "interval coverage is a modelling claim and is valid only when the caller's stated normal-approximation assumptions are justified",
            ),
        )

    def paper_parlay(
        self,
        stake: Decimal | str | int,
        decimal_odds: Sequence[Decimal | str | int],
        *,
        probabilities: Sequence[Decimal | str | int] | None = None,
        probability_assumption: str | None = None,
    ) -> CalculationResult:
        odds_values = _decimal_series(
            decimal_odds,
            field="decimal_odds",
            minimum=None,
            greater_than=Decimal("1"),
            minimum_items=2,
            maximum_items=_MAX_PARLAY_LEGS,
        )
        stake_value = _decimal(stake, field="stake", minimum=Decimal("0"))
        combined_odds = _exact_product(odds_values, context="parlay odds")
        try:
            with localcontext(_CONTEXT) as ctx:
                ctx.clear_flags()
                payout = stake_value * combined_odds
                profit = payout - stake_value
                if ctx.flags[Inexact]:
                    raise ValueError("paper parlay payout arithmetic would require rounding")
        except DecimalException as exc:
            raise ValueError("paper parlay payout is outside the supported decimal range") from exc

        inputs: dict[str, Decimal] = {"stake": stake_value}
        input_units: dict[str, str] = {"stake": "paper_currency"}
        for index, odds in enumerate(odds_values):
            inputs[f"decimal_odds.{index}"] = odds
            input_units[f"decimal_odds.{index}"] = "decimal_odds"
        outputs: dict[str, Decimal] = {
            "combined_decimal_odds": combined_odds,
            "payout": payout,
            "profit": profit,
        }
        output_units: dict[str, str] = {
            "combined_decimal_odds": "decimal_odds",
            "payout": "paper_currency",
            "profit": "paper_currency",
        }
        if probabilities is None:
            if probability_assumption is not None:
                raise ValueError("probability_assumption requires supplied leg probabilities")
            classification = "exact"
            assumptions = (
                "paper-only payout calculation; no joint probability or dependence model is applied",
                "no real-money execution authority",
            )
            warnings: tuple[str, ...] = ()
        else:
            probability_values = _decimal_series(
                probabilities,
                field="probability",
                minimum=Decimal("0"),
                maximum=Decimal("1"),
                minimum_items=2,
                maximum_items=_MAX_PARLAY_LEGS,
            )
            if len(probability_values) != len(odds_values):
                raise ValueError("parlay probabilities must match the number of odds legs")
            if probability_assumption != "independent":
                raise ValueError(
                    "parlay joint probability requires explicit probability_assumption='independent'"
                )
            joint_probability = _approximate_product(probability_values, context="parlay probability")
            outputs["joint_probability"] = joint_probability
            output_units["joint_probability"] = "probability"
            for index, probability in enumerate(probability_values):
                inputs[f"probability.{index}"] = probability
                input_units[f"probability.{index}"] = "probability"
            classification = "approximate_decimal"
            assumptions = (
                "caller explicitly assumes all supplied parlay leg outcomes are mutually independent",
                "paper research only; the independence assumption is not inferred or verified by this calculator",
                "no real-money execution authority",
            )
            warnings = (
                "joint probability is model-dependent and must not be used when material dependence exists between legs",
            )
        return _result(
            calculation_id="paper_parlay",
            method="decimal_odds_parlay_with_optional_independent_probability",
            classification=classification,
            inputs=inputs,
            input_units=input_units,
            assumptions=assumptions,
            outputs=outputs,
            output_units=output_units,
            warnings=warnings,
        )

    def finite_scenario_table(
        self,
        scenario_profits: Mapping[str, Decimal | str | int],
        *,
        completeness: str,
    ) -> CalculationResult:
        if not isinstance(scenario_profits, Mapping) or not scenario_profits:
            raise ValueError("scenario_profits must contain at least one named scenario")
        if len(scenario_profits) > _MAX_MARKET_SELECTIONS:
            raise ValueError("scenario count exceeds the supported limit")
        if completeness not in {"complete", "partial"}:
            raise ValueError("scenario completeness must be exactly 'complete' or 'partial'")
        normalized: dict[str, Decimal] = {}
        for raw_name, raw_profit in scenario_profits.items():
            name = _text_key(raw_name, field="scenario")
            if name in normalized:
                raise ValueError("scenario identifiers must be unique")
            normalized[name] = _decimal(raw_profit, field=f"scenario_profit[{name}]")
        ordered = sorted(normalized.items())
        profits = [value for _, value in ordered]
        outputs: dict[str, Decimal] = {
            "scenario_count": Decimal(len(ordered)),
            "worst_case": min(profits),
            "best_case": max(profits),
        }
        output_units: dict[str, str] = {
            "scenario_count": "count",
            "worst_case": "paper_currency",
            "best_case": "paper_currency",
        }
        inputs: dict[str, Decimal] = {}
        input_units: dict[str, str] = {}
        for name, profit in ordered:
            input_name = f"scenario_profit.{name}"
            inputs[input_name] = profit
            input_units[input_name] = "paper_currency"
            outputs[input_name] = profit
            output_units[input_name] = "paper_currency"
        if completeness == "complete":
            method = "finite_scenario_table_caller_asserted_complete"
            assumptions = (
                "caller explicitly asserts that the supplied scenarios exhaust the modeled state space",
                "the calculator does not independently prove scenario completeness",
            )
            warnings = (
                "global worst/best claims are valid only if upstream evidence justifies the caller's completeness assertion",
            )
        else:
            method = "finite_scenario_table_partial"
            assumptions = (
                "the supplied scenario set is explicitly partial and does not claim to exhaust the modeled state space",
            )
            warnings = (
                "worst_case and best_case apply only to the supplied partial scenarios and are not global portfolio bounds",
            )
        return _result(
            calculation_id="finite_scenario_table",
            method=method,
            classification="exact",
            inputs=inputs,
            input_units=input_units,
            assumptions=assumptions,
            outputs=outputs,
            output_units=output_units,
            warnings=warnings,
        )

    def maximum_drawdown(
        self,
        balances: Sequence[Decimal | str | int],
    ) -> CalculationResult:
        values = _decimal_series(
            balances,
            field="balance",
            minimum=Decimal("0"),
        )
        peak = values[0]
        max_absolute = Decimal("0")
        max_fraction = Decimal("0")
        for value in values:
            if value > peak:
                peak = value
            absolute = _exact_subtract(peak, value, context="maximum drawdown")
            fraction = Decimal("0") if peak == 0 else _divide(absolute, peak)
            if absolute > max_absolute:
                max_absolute = absolute
            if fraction > max_fraction:
                max_fraction = fraction
        return _result(
            calculation_id="maximum_drawdown",
            method="peak_to_trough_running_max",
            classification="approximate_decimal",
            inputs={f"balance.{index}": value for index, value in enumerate(values)},
            input_units={f"balance.{index}": "paper_currency" for index in range(len(values))},
            assumptions=("balances are supplied in chronological order",),
            outputs={
                "maximum_drawdown_absolute": max_absolute,
                "maximum_drawdown_fraction": max_fraction,
            },
            output_units={
                "maximum_drawdown_absolute": "paper_currency",
                "maximum_drawdown_fraction": "fraction",
            },
            warnings=("drawdown fraction division is rounded in the deterministic decimal context",),
        )


def _text_key(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be UTF-8 encodable") from exc
    return value


def _decimal(
    value: Decimal | str | int,
    *,
    field: str,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    greater_than: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (Decimal, str, int)):
        raise ValueError(f"{field} must be Decimal, integer, or canonical decimal text")
    if isinstance(value, str):
        if not value or value != value.strip():
            raise ValueError(f"{field} must be canonical decimal text")
        text = value
    else:
        text = str(value)
    try:
        parsed = Decimal(text)
    except DecimalException as exc:
        raise ValueError(f"{field} must be a valid decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    digits = parsed.as_tuple().digits
    if len(digits) > _MAX_SIGNIFICANT_DIGITS:
        raise ValueError(f"{field} exceeds the supported precision")
    if parsed != 0 and abs(parsed.adjusted()) > _MAX_ADJUSTED_EXPONENT:
        raise ValueError(f"{field} exceeds the supported magnitude")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field} must be at least {_canonical_decimal(minimum)}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{field} must be at most {_canonical_decimal(maximum)}")
    if greater_than is not None and parsed <= greater_than:
        raise ValueError(f"{field} must be greater than {_canonical_decimal(greater_than)}")
    return parsed


def _decimal_series(
    values: Sequence[Decimal | str | int],
    *,
    field: str,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    greater_than: Decimal | None = None,
    minimum_items: int = 1,
    maximum_items: int = _MAX_SERIES_ITEMS,
) -> list[Decimal]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{field} series must be a sequence")
    if len(values) < minimum_items:
        if minimum_items == 1:
            raise ValueError(f"at least one {field} is required")
        raise ValueError(f"at least {minimum_items} {field} values are required")
    if len(values) > maximum_items:
        raise ValueError(f"{field} series exceeds the supported item limit")
    return [
        _decimal(
            value,
            field=f"{field}[{index}]",
            minimum=minimum,
            maximum=maximum,
            greater_than=greater_than,
        )
        for index, value in enumerate(values)
    ]


def _approximate_add(left: Decimal, right: Decimal, *, context: str) -> Decimal:
    try:
        with localcontext(_CONTEXT):
            result = left + right
    except DecimalException as exc:
        raise ValueError(f"{context} is outside the supported decimal range") from exc
    if not result.is_finite():
        raise ValueError(f"{context} produced a non-finite result")
    return result


def _exact_subtract(left: Decimal, right: Decimal, *, context: str) -> Decimal:
    try:
        with localcontext(_CONTEXT) as ctx:
            ctx.clear_flags()
            result = left - right
            if ctx.flags[Inexact]:
                raise ValueError(f"{context} arithmetic would require rounding")
    except DecimalException as exc:
        raise ValueError(f"{context} is outside the supported decimal range") from exc
    if not result.is_finite():
        raise ValueError(f"{context} produced a non-finite result")
    return result


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        raise ValueError("division by zero")
    try:
        with localcontext(_CONTEXT):
            result = numerator / denominator
    except (DecimalException, ZeroDivisionError) as exc:
        raise ValueError("calculation is outside the supported decimal range") from exc
    if not result.is_finite():
        raise ValueError("calculation produced a non-finite result")
    return result


def _exact_product(values: Sequence[Decimal], *, context: str) -> Decimal:
    try:
        with localcontext(_CONTEXT) as ctx:
            ctx.clear_flags()
            result = Decimal("1")
            for value in values:
                result *= value
            if ctx.flags[Inexact]:
                raise ValueError(f"{context} arithmetic would require rounding")
    except DecimalException as exc:
        raise ValueError(f"{context} is outside the supported decimal range") from exc
    if not result.is_finite():
        raise ValueError(f"{context} produced a non-finite result")
    return result


def _approximate_product(values: Sequence[Decimal], *, context: str) -> Decimal:
    try:
        with localcontext(_CONTEXT):
            result = Decimal("1")
            for value in values:
                result *= value
    except DecimalException as exc:
        raise ValueError(f"{context} is outside the supported decimal range") from exc
    if not result.is_finite():
        raise ValueError(f"{context} produced a non-finite result")
    return result


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite decimal cannot be canonicalized")
    if value == 0:
        return "0"
    fixed = format(value, "f")
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    return fixed


def _hash_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result(
    *,
    calculation_id: str,
    method: str,
    classification: str,
    inputs: Mapping[str, Decimal],
    input_units: Mapping[str, str],
    assumptions: tuple[str, ...],
    outputs: Mapping[str, Decimal],
    output_units: Mapping[str, str],
    warnings: tuple[str, ...] = (),
) -> CalculationResult:
    if classification not in {"exact", "approximate_decimal"}:
        raise ValueError("unsupported calculation classification")
    if set(inputs) != set(input_units):
        raise ValueError("every calculation input must have one unit")
    if set(outputs) != set(output_units):
        raise ValueError("every calculation output must have one unit")
    canonical_inputs = tuple(sorted((key, _canonical_decimal(value)) for key, value in inputs.items()))
    canonical_input_units = tuple(sorted(input_units.items()))
    canonical_outputs = tuple(sorted((key, _canonical_decimal(value)) for key, value in outputs.items()))
    canonical_output_units = tuple(sorted(output_units.items()))
    input_payload = {
        "calculation_id": calculation_id,
        "version": _CALCULATION_VERSION,
        "engine_version": _ENGINE_VERSION,
        "method": method,
        "inputs": dict(canonical_inputs),
        "input_units": dict(canonical_input_units),
        "assumptions": list(assumptions),
    }
    input_hash = _hash_payload(input_payload)
    result_payload = {
        **input_payload,
        "classification": classification,
        "outputs": dict(canonical_outputs),
        "output_units": dict(canonical_output_units),
        "warnings": list(warnings),
        "input_hash": input_hash,
    }
    result_hash = _hash_payload(result_payload)
    return CalculationResult(
        calculation_id=calculation_id,
        version=_CALCULATION_VERSION,
        engine_version=_ENGINE_VERSION,
        method=method,
        classification=classification,
        inputs=canonical_inputs,
        input_units=canonical_input_units,
        assumptions=assumptions,
        outputs=canonical_outputs,
        output_units=canonical_output_units,
        warnings=warnings,
        input_hash=input_hash,
        result_hash=result_hash,
    )