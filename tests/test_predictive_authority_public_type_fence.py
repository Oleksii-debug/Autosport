from __future__ import annotations

import importlib.util
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from autosport.forecasting import ForecastRecord
from autosport.opportunity import QuoteRef
from autosport.predictive_authority import (
    resolve_authoritative_forecast_ref,
    resolve_forecast_predictive_authority,
)
from autosport.predictive_qualification import (
    ForecastCalibrationQualification,
    PredictiveAdmissionPolicy,
    PredictiveQualificationError,
)
from autosport.scientific_registry import ScientificRegistry


_HELPER_PATH = Path(__file__).with_name("test_predictive_authority.py")
_SPEC = importlib.util.spec_from_file_location(
    "_autosport_predictive_authority_type_fence_helpers",
    _HELPER_PATH,
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load predictive-authority test helpers")
_HELPERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPERS)


_FORGED_POLICY_SHA256 = "0" * 64
_FORGED_QUALIFICATION_SHA256 = "0" * 64


def _clone_as(cls: type[Any], value: object, **overrides: object):
    payload = {field.name: getattr(value, field.name) for field in fields(value)}
    payload.update(overrides)
    return cls(**payload)


class _ForgedScientificRegistry(ScientificRegistry):
    """Public-method-compatible registry facade with no canonical durable identity."""

    def __init__(self, delegate: ScientificRegistry) -> None:
        self._delegate = delegate

    def get(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        return self._delegate.get(*args, **kwargs)

    def champion_strategy(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        return self._delegate.champion_strategy(*args, **kwargs)

    def causal_records(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        return self._delegate.causal_records(*args, **kwargs)


class _ForgedPolicy(PredictiveAdmissionPolicy):
    @property
    def sha256(self) -> str:
        return _FORGED_POLICY_SHA256


class _ForgedQualification(ForecastCalibrationQualification):
    @property
    def sha256(self) -> str:
        return _FORGED_QUALIFICATION_SHA256


class _ForgedForecastRecord(ForecastRecord):
    pass


class _ForgedQuoteRef(QuoteRef):
    pass


def test_registry_subclass_cannot_supply_positive_predictive_authority(tmp_path) -> None:
    policy = _HELPERS._policy()
    qualification = _HELPERS._qualification()
    registry, _ = _HELPERS._promoted_registry(tmp_path, qualification, policy)
    forged = _ForgedScientificRegistry(registry)

    with pytest.raises(
        PredictiveQualificationError,
        match="registry must be exact canonical ScientificRegistry",
    ):
        resolve_authoritative_forecast_ref(
            forged,
            _HELPERS._forecast(),
            _HELPERS._quote(),
            decision_time=_HELPERS._DECISION_TIME,
            policy=policy,
            qualification=qualification,
        )


def test_policy_subclass_cannot_present_committed_digest_with_permissive_fields(
    tmp_path,
) -> None:
    global _FORGED_POLICY_SHA256

    policy = _HELPERS._policy()
    qualification = _HELPERS._qualification()
    registry, _ = _HELPERS._promoted_registry(tmp_path, qualification, policy)
    _FORGED_POLICY_SHA256 = policy.sha256
    forged = _clone_as(
        _ForgedPolicy,
        policy,
        maximum_uncertainty=Decimal("1"),
        maximum_calibration_error_upper=Decimal("1"),
        minimum_selective_coverage=Decimal("0"),
        maximum_selective_risk=Decimal("1"),
        maximum_evidence_age_seconds=999999999,
    )

    with pytest.raises(
        PredictiveQualificationError,
        match="policy must be exact canonical PredictiveAdmissionPolicy",
    ):
        resolve_forecast_predictive_authority(
            registry,
            _HELPERS._forecast(),
            decision_time=_HELPERS._DECISION_TIME,
            policy=forged,
            qualification=qualification,
        )


def test_qualification_subclass_cannot_present_committed_digest_with_changed_fields(
    tmp_path,
) -> None:
    global _FORGED_QUALIFICATION_SHA256

    policy = _HELPERS._policy()
    qualification = _HELPERS._qualification()
    registry, _ = _HELPERS._promoted_registry(tmp_path, qualification, policy)
    _FORGED_QUALIFICATION_SHA256 = qualification.sha256
    forged = _clone_as(
        _ForgedQualification,
        qualification,
        effective_sample_size=999999,
        calibration_error=Decimal("0"),
        calibration_error_upper=Decimal("0"),
        selective_coverage=Decimal("1"),
        selective_risk=Decimal("0"),
    )

    with pytest.raises(
        PredictiveQualificationError,
        match=(
            "qualification must be exact canonical "
            "ForecastCalibrationQualification"
        ),
    ):
        resolve_forecast_predictive_authority(
            registry,
            _HELPERS._forecast(),
            decision_time=_HELPERS._DECISION_TIME,
            policy=policy,
            qualification=forged,
        )


def test_forecast_and_quote_subclasses_cannot_reach_positive_mint(tmp_path) -> None:
    policy = _HELPERS._policy()
    qualification = _HELPERS._qualification()
    registry, _ = _HELPERS._promoted_registry(tmp_path, qualification, policy)
    forecast = _HELPERS._forecast()
    quote = _HELPERS._quote()

    forged_forecast = _clone_as(_ForgedForecastRecord, forecast)
    with pytest.raises(
        PredictiveQualificationError,
        match="forecast must be exact canonical ForecastRecord",
    ):
        resolve_authoritative_forecast_ref(
            registry,
            forged_forecast,
            quote,
            decision_time=_HELPERS._DECISION_TIME,
            policy=policy,
            qualification=qualification,
        )

    forged_quote = _clone_as(_ForgedQuoteRef, quote)
    with pytest.raises(
        PredictiveQualificationError,
        match="quote must be exact canonical QuoteRef",
    ):
        resolve_authoritative_forecast_ref(
            registry,
            forecast,
            forged_quote,
            decision_time=_HELPERS._DECISION_TIME,
            policy=policy,
            qualification=qualification,
        )
