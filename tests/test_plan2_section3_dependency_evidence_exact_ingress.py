"""Plan 2 Section 3: empirical dependency numeric ingress is exact-Decimal only.

A caller-defined Decimal subtype must not execute virtual numeric methods while
constructing PAPER-only portfolio evidence. This is not bookmaker/fill evidence.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.portfolio_plan import PortfolioDependencyEvidence


def _evidence(**changes: object) -> PortfolioDependencyEvidence:
    fields: dict[str, object] = {
        "evidence_id": "offline-dependency-witness",
        "portfolio_sha256": "c" * 64,
        "intent_sha256s": ("d" * 64, "e" * 64),
        "candidate_sha256s": ("a" * 64, "b" * 64),
        "population_id": "replay-only",
        "method": "frozen-causal-observations",
        "sample_size": 30,
        "causal_cutoff": "2026-10-08T11:00:00+00:00",
        "as_of": "2026-10-08T11:30:00+00:00",
        "valid_until": "2026-10-08T13:00:00+00:00",
        "reproducibility_sha256": "f" * 64,
        "pairwise_dependency_upper_bounds": (
            ("a" * 64, "b" * 64, Decimal("0.4")),
        ),
        "uncertainty_fraction": Decimal("0.1"),
        "fee_fraction": Decimal("0.02"),
        "partial_fill_stress_fraction": Decimal("0.03"),
    }
    fields.update(changes)
    return PortfolioDependencyEvidence(**fields)


@pytest.mark.parametrize(
    "field",
    ("uncertainty_fraction", "fee_fraction", "partial_fill_stress_fraction"),
)
def test_fraction_subclass_is_rejected_before_attacker_method(field: str) -> None:
    class HostileDecimal(Decimal):
        calls = 0

        def is_finite(self) -> bool:
            type(self).calls += 1
            raise AssertionError("hostile Decimal method was invoked")

    with pytest.raises(ValueError, match="exact Decimal"):
        _evidence(**{field: HostileDecimal("0.2")})
    assert HostileDecimal.calls == 0


def test_pair_bound_subclass_is_rejected_before_attacker_method() -> None:
    class HostileDecimal(Decimal):
        calls = 0

        def is_finite(self) -> bool:
            type(self).calls += 1
            raise AssertionError("hostile Decimal method was invoked")

    with pytest.raises(ValueError, match="exact Decimal"):
        _evidence(
            pairwise_dependency_upper_bounds=(
                ("a" * 64, "b" * 64, HostileDecimal("0.4")),
            )
        )
    assert HostileDecimal.calls == 0


@pytest.mark.parametrize(
    ("field", "bad"),
    (
        ("uncertainty_fraction", Decimal("-0.01")),
        ("fee_fraction", Decimal("1.01")),
        ("partial_fill_stress_fraction", Decimal("NaN")),
        ("uncertainty_fraction", Decimal("Infinity")),
    ),
)
def test_invalid_exact_decimal_fractions_fail_closed(field: str, bad: Decimal) -> None:
    with pytest.raises(ValueError, match="exact Decimal"):
        _evidence(**{field: bad})


def test_canonical_evidence_roundtrip_is_unchanged() -> None:
    evidence = _evidence()
    restored = PortfolioDependencyEvidence.from_dict(evidence.to_dict())
    assert restored == evidence
    assert restored.evidence_sha256 == evidence.evidence_sha256
    assert restored.pairwise_dependency_upper_bounds[0][2] == Decimal("0.4")
