from types import SimpleNamespace

import pytest

from autosport import _paper_execution_freshness as freshness
from autosport import _paper_execution_reality_legacy as legacy


@pytest.mark.parametrize(
    "expires_at",
    (
        "2026-01-01T00:00:02+00:00",
        "2025-12-31T23:59:59+00:00",
    ),
)
def test_observed_future_quote_is_paper_state_error(expires_at: str) -> None:
    action = SimpleNamespace(
        quote_observed_at="2026-01-01T00:00:01+00:00",
        expires_at=expires_at,
    )
    observation = SimpleNamespace(observed_at="2026-01-01T00:00:00+00:00")
    config = SimpleNamespace(max_quote_age_ms=1_000)

    with pytest.raises(
        legacy.PaperExecutionStateError,
        match="PAPER execution timestamp cannot predate decision quote",
    ):
        freshness._observed_attempt(
            action=action,
            observation=observation,
            config=config,
        )


def test_synthetic_negative_quote_age_is_paper_state_error() -> None:
    def original(**_kwargs):
        raise ValueError("quote age must be non-negative")

    with pytest.raises(
        legacy.PaperExecutionStateError,
        match="PAPER execution timestamp cannot predate decision quote",
    ):
        freshness._exact_synthetic(original)


def test_synthetic_unrelated_value_error_is_not_reclassified() -> None:
    def original(**_kwargs):
        raise ValueError("requested stake must be positive")

    with pytest.raises(ValueError, match="requested stake must be positive"):
        freshness._exact_synthetic(original)
