from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .calculation import CalculationEngine, CalculationResult
from .calculation_input import CalculationInputBoundary, MANUAL_CALCULATION_INPUT


_SERVICE_VERSION = "manual-calculation-service-v1"
_CALCULATION_VERSION = 1
_ENGINE_VERSION = "calculation-engine-v1"
_MAX_MARKET_SELECTIONS = 1_000
_MAX_PARLAY_LEGS = 100
_MAX_SERIES_ITEMS = 10_000
_LIMIT_FIELDS = (
    "numeric_text_chars",
    "identifier_chars",
    "identifier_utf8_bytes",
    "collection_items",
)


@dataclass(frozen=True, slots=True)
class ManualCalculationEvidence:
    """Structured manual-input result suitable for accessible copy/export."""

    service_version: str
    input_mode: str
    result: CalculationResult
    real_money_execution: bool
    evidence_sha256: str

    def __post_init__(self) -> None:
        if self.service_version != _SERVICE_VERSION:
            raise ValueError(f"service_version must be {_SERVICE_VERSION!r}")
        if self.input_mode != "manual":
            raise ValueError("input_mode must be exactly 'manual'")
        if self.real_money_execution is not False:
            raise ValueError("real_money_execution must be exactly false")
        _validate_calculation_result(self.result)
        expected = _sha256(_evidence_payload(self.result))
        if self.evidence_sha256 != expected:
            raise ValueError("evidence_sha256 does not match manual calculation evidence")

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
        if engine is not None and type(engine) is not CalculationEngine:
            raise ValueError("engine must be an exact CalculationEngine")
        if (
            input_boundary is not None
            and type(input_boundary) is not CalculationInputBoundary
        ):
            raise ValueError("input_boundary must be an exact CalculationInputBoundary")
        if input_boundary is not None:
            _validate_input_boundary_limits(input_boundary)
        self._engine = engine if engine is not None else CalculationEngine()
        self._input = (
            CalculationInputBoundary(input_boundary.limits)
            if input_boundary is not None
            else MANUAL_CALCULATION_INPUT
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
        _validate_calculation_result(result)
        payload = _evidence_payload(result)
        digest = _sha256(payload)
        return ManualCalculationEvidence(
            service_version=_SERVICE_VERSION,
            input_mode="manual",
            result=result,
            real_money_execution=False,
            evidence_sha256=digest,
        )


def _validate_input_boundary_limits(boundary: CalculationInputBoundary) -> None:
    canonical = MANUAL_CALCULATION_INPUT.limits
    for field in _LIMIT_FIELDS:
        supplied = getattr(boundary.limits, field)
        maximum = getattr(canonical, field)
        if supplied > maximum:
            raise ValueError(
                f"input_boundary {field} must not be looser than the canonical manual-input limit"
            )


def _validate_calculation_result(result: object) -> None:
    if type(result) is not CalculationResult:
        raise ValueError("result must be a CalculationResult")
    if type(result.version) is not int or result.version != _CALCULATION_VERSION:
        raise ValueError("calculation result version is not canonical")
    if type(result.engine_version) is not str or result.engine_version != _ENGINE_VERSION:
        raise ValueError("calculation result engine_version is not canonical")
    if type(result.classification) is not str or result.classification not in {
        "exact",
        "approximate_decimal",
    }:
        raise ValueError("calculation result classification is not canonical")
    if type(result.input_hash) is not str:
        raise ValueError("calculation result input_hash must be a string")
    if type(result.result_hash) is not str:
        raise ValueError("calculation result result_hash must be a string")

    inputs = _validate_pairs(result.inputs, field="inputs")
    input_units = _validate_pairs(result.input_units, field="input_units")
    outputs = _validate_pairs(result.outputs, field="outputs")
    output_units = _validate_pairs(result.output_units, field="output_units")
    assumptions = _validate_text_tuple(result.assumptions, field="assumptions")
    warnings = _validate_text_tuple(result.warnings, field="warnings")

    if set(dict(inputs)) != set(dict(input_units)):
        raise ValueError("calculation result input units do not match inputs")
    if set(dict(outputs)) != set(dict(output_units)):
        raise ValueError("calculation result output units do not match outputs")

    input_payload = {
        "calculation_id": _required_text(result.calculation_id, field="calculation_id"),
        "version": _CALCULATION_VERSION,
        "engine_version": _ENGINE_VERSION,
        "method": _required_text(result.method, field="method"),
        "inputs": dict(inputs),
        "input_units": dict(input_units),
        "assumptions": list(assumptions),
    }
    expected_input_hash = _canonical_hash(input_payload)
    if result.input_hash != expected_input_hash:
        raise ValueError("calculation result input_hash does not match canonical payload")

    result_payload = {
        **input_payload,
        "classification": result.classification,
        "outputs": dict(outputs),
        "output_units": dict(output_units),
        "warnings": list(warnings),
        "input_hash": expected_input_hash,
    }
    expected_result_hash = _canonical_hash(result_payload)
    if result.result_hash != expected_result_hash:
        raise ValueError("calculation result result_hash does not match canonical payload")


def _validate_pairs(value: object, *, field: str) -> tuple[tuple[str, str], ...]:
    if type(value) is not tuple:
        raise ValueError(f"calculation result {field} must be a tuple")
    validated: list[tuple[str, str]] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError(f"calculation result {field} entries must be pairs")
        key, text = item
        key = _required_text(key, field=f"{field} key")
        if type(text) is not str:
            raise ValueError(f"calculation result {field} values must be strings")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(
                f"calculation result {field} values must be UTF-8 encodable"
            ) from exc
        validated.append((key, text))
    result = tuple(validated)
    if len({key for key, _ in result}) != len(result):
        raise ValueError(f"calculation result {field} keys must be unique")
    if result != tuple(sorted(result)):
        raise ValueError(f"calculation result {field} must be canonically ordered")
    return result


def _validate_text_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ValueError(f"calculation result {field} must be a tuple")
    for item in value:
        if type(item) is not str:
            raise ValueError(f"calculation result {field} entries must be strings")
        try:
            item.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(
                f"calculation result {field} entries must be UTF-8 encodable"
            ) from exc
    return value


def _required_text(value: object, *, field: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"calculation result {field} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"calculation result {field} must be UTF-8 encodable") from exc
    return value


def _canonical_hash(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("calculation result is not canonical UTF-8 JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _evidence_payload(result: CalculationResult) -> dict[str, object]:
    return {
        "service_version": _SERVICE_VERSION,
        "input_mode": "manual",
        "result": result.as_dict(),
        "real_money_execution": False,
    }


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
