from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from itertools import count
from pathlib import Path

import pytest

from autosport._provider_evaluation_semantic_gate import (
    _set_legacy_provider_semantic_bypass_for_tests,
)

from autosport.domain import MarketEvent
from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)


# These four legacy suites predate #623 execution-reality adoption. Their product
# assertions remain valuable, but positive paper actions now require the same
# canonical execution runtime + provider-account authority as production. Keep this
# migration harness deliberately narrow so tests outside the known legacy surface
# continue to prove that missing execution authority fails closed.
_LEGACY_PAPER_VALUE_MODULES = frozenset(
    {
        "test_economic_goal_endogenous_stake",
        "test_forecasting_truth",
        "test_paper_strategy_economic_goal",
        "test_provider_probability_agents",
    }
)
_FALLBACK_SOURCES = {
    "test_forecasting_truth": ("fixture",),
    "test_provider_probability_agents": ("s",),
}


def _execution_config(module_name: str) -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id=f"legacy-fixture-{module_name}",
        model_version="1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="legacy-test-fixture",
        seed="fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _sources_from_latest_quotes(latest_quotes: object) -> tuple[str, ...]:
    if not isinstance(latest_quotes, Mapping):
        return ()
    sources = {
        event.source_id
        for event in latest_quotes.values()
        if isinstance(event, MarketEvent)
    }
    return tuple(sorted(sources))


@pytest.fixture(autouse=True)
def _bind_legacy_paper_value_execution_authority(request, monkeypatch, tmp_path):
    """Migrate only known legacy positive-action fixtures onto canonical #623 truth."""

    module = request.module
    if module is None:
        return
    module_name = module.__name__.rsplit(".", 1)[-1]
    if module_name not in _LEGACY_PAPER_VALUE_MODULES:
        return
    if request.node.name == "test_paper_value_agent_without_execution_authority_does_not_open_ticket":
        # Preserve this negative boundary test: the legacy positive-action fixture
        # must not supply the very execution authority it is verifying is absent.
        return

    original_context = getattr(module, "AgentContext", None)
    if original_context is None:
        return

    anonymous_roots = count()

    def execution_bound_context(paper_book, *args, **kwargs):
        if kwargs.get("paper_execution") is not None:
            return original_context(paper_book, *args, **kwargs)

        decision_ledger = kwargs.get("decision_ledger")
        ledger_path = getattr(decision_ledger, "path", None)
        if ledger_path is not None:
            root = Path(ledger_path).parent
        else:
            root = tmp_path / f"paper-execution-{next(anonymous_roots)}"
            root.mkdir(parents=True, exist_ok=True)

        sources = _sources_from_latest_quotes(kwargs.get("latest_quotes"))
        if not sources:
            sources = _FALLBACK_SOURCES.get(module_name, ())
        provider_accounts = tuple(
            (source_id, f"test-account-{index}")
            for index, source_id in enumerate(sources, start=1)
        )

        kwargs["paper_execution"] = PaperExecutionAdoptionRuntime(
            book=paper_book,
            ledger=PaperExecutionLedger(root / "paper-execution.jsonl"),
            config=_execution_config(module_name),
            max_quote_age=timedelta(seconds=5),
            paper_book_path=root / "paper-execution-book.json",
        )
        kwargs["paper_provider_accounts"] = provider_accounts
        return original_context(paper_book, *args, **kwargs)

    monkeypatch.setattr(module, "AgentContext", execution_bound_context)

# These files predate the #662 product-semantic splice and exercise provider membership,
# persistence/recovery, and PAPER transition behavior rather than semantic provenance.
# Keep their old fixture path private and narrowly scoped; all other tests see the
# production fail-closed gate.
_LEGACY_PROVIDER_SEMANTIC_FIXTURES = {
    "test_provider_evaluation_universe.py",
    "test_evaluation_universe_execution_binding.py",
    "test_evaluation_universe_crash_retry.py",
}


@pytest.fixture(autouse=True)
def _legacy_provider_semantic_fixture_bridge(request):
    enabled = Path(str(request.node.fspath)).name in _LEGACY_PROVIDER_SEMANTIC_FIXTURES
    _set_legacy_provider_semantic_bypass_for_tests(enabled)
    try:
        yield
    finally:
        _set_legacy_provider_semantic_bypass_for_tests(False)
