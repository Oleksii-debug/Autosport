"""Expected-red contract falsifier for #1253 / parent PR #810.

The PAPER risk-report surface must preserve maximum historical drawdown as
separate evidence from current drawdown. A recovered path such as
100 -> 50 -> 120 has current drawdown 0 but historical maximum drawdown 50.
"""

from dataclasses import fields, is_dataclass

from autosport.risk_reporting import PaperRiskReport


def _report_field_names() -> set[str]:
    assert is_dataclass(PaperRiskReport), "PaperRiskReport must remain immutable dataclass evidence"
    return {field.name for field in fields(PaperRiskReport)}


def _require_semantic_field(
    names: set[str],
    *,
    candidates: tuple[str, ...],
    concept: str,
) -> str:
    for candidate in candidates:
        if candidate in names:
            return candidate
    raise AssertionError(
        f"PaperRiskReport lacks first-class {concept} evidence; "
        f"accepted field names for this dependent falsifier: {candidates!r}. "
        f"Actual fields: {sorted(names)!r}"
    )


def test_recovery_cannot_erase_historical_max_drawdown_contract() -> None:
    """Current drawdown and maximum historical drawdown must be distinct."""

    names = _report_field_names()

    assert "current_drawdown_amount" in names, (
        "Parent #810 contract changed: this falsifier targets the existing "
        "current_drawdown_amount report surface."
    )

    max_amount = _require_semantic_field(
        names,
        candidates=(
            "historical_max_drawdown_amount",
            "max_historical_drawdown_amount",
            "max_drawdown_amount",
        ),
        concept="maximum historical drawdown amount",
    )
    assert max_amount != "current_drawdown_amount"

    max_fraction = _require_semantic_field(
        names,
        candidates=(
            "historical_max_drawdown_fraction",
            "max_historical_drawdown_fraction",
            "observed_max_drawdown_fraction",
        ),
        concept="observed maximum historical drawdown fraction",
    )
    assert max_fraction != "max_drawdown_fraction", (
        "Observed historical drawdown fraction must remain distinct from "
        "EconomicGoal.max_drawdown_fraction policy ceiling."
    )

    _require_semantic_field(
        names,
        candidates=(
            "historical_max_drawdown_peak_id",
            "max_drawdown_peak_id",
            "max_drawdown_peak_identity",
        ),
        concept="maximum-drawdown peak identity",
    )
    _require_semantic_field(
        names,
        candidates=(
            "historical_max_drawdown_trough_id",
            "max_drawdown_trough_id",
            "max_drawdown_trough_identity",
        ),
        concept="maximum-drawdown trough identity",
    )


def test_max_drawdown_contract_is_not_satisfied_by_goal_limit_only() -> None:
    """A max-drawdown policy ceiling is not observed historical drawdown."""

    names = _report_field_names()
    observed_amount_fields = {
        "historical_max_drawdown_amount",
        "max_historical_drawdown_amount",
        "max_drawdown_amount",
    }
    observed_fraction_fields = {
        "historical_max_drawdown_fraction",
        "max_historical_drawdown_fraction",
        "observed_max_drawdown_fraction",
    }
    assert names & observed_amount_fields, (
        "A goal/policy max_drawdown_fraction or drawdown_loss_room is a limit, "
        "not evidence of the maximum drawdown actually observed over the frozen "
        "causal PAPER equity path required by #1253."
    )
    assert names & observed_fraction_fields, (
        "The policy field max_drawdown_fraction cannot stand in for the "
        "observed historical maximum drawdown fraction."
    )
