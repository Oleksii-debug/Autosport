from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from itertools import count
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from supervised_clock_test_support import install_trusted_clock

# Install one stable deterministic supervised-execution clock before test collection
# imports Betfair provider-write composition. Individual tests vary only the helper's
# private state; the function identity/code captured by production authority seals
# remains unchanged.
install_trusted_clock()

# Root-selection production now correctly treats post-composition replacement of the
# OS account-location resolver as an authority violation. Tests that need a sandbox
# must therefore install one stable process-local resolver *before* importing the
# product package. The resolver identity never changes after composition; scoped
# fixtures below vary only this test-process state. When no sandbox is active the
# shim delegates to the real OS resolver, so unrelated tests retain normal behavior.
_ROOT_SELECTION_TEST_HOME: Path | None = None

if os.name == "nt":
    import ctypes as _root_selection_ctypes

    _real_root_selection_shell32 = _root_selection_ctypes.windll.shell32  # type: ignore[attr-defined]
    _real_root_selection_get_folder_path = _real_root_selection_shell32.SHGetFolderPathW

    def _pytest_root_selection_get_folder_path(_hwnd, _csidl, _token, _flags, buffer):
        caller_module = sys._getframe(1).f_globals.get("__name__")
        if (
            _ROOT_SELECTION_TEST_HOME is None
            or caller_module != "autosport._monotonic_root_selection_os_resolver_guard"
        ):
            return _real_root_selection_get_folder_path(
                _hwnd,
                _csidl,
                _token,
                _flags,
                buffer,
            )
        buffer.value = str(_ROOT_SELECTION_TEST_HOME)
        return 0

    _real_root_selection_shell32.SHGetFolderPathW = _pytest_root_selection_get_folder_path
else:
    import pwd as _root_selection_pwd

    _real_root_selection_getpwuid = _root_selection_pwd.getpwuid

    def _pytest_root_selection_getpwuid(uid):
        caller_module = sys._getframe(1).f_globals.get("__name__")
        if (
            _ROOT_SELECTION_TEST_HOME is None
            or caller_module != "autosport._monotonic_root_selection_os_resolver_guard"
        ):
            return _real_root_selection_getpwuid(uid)
        return SimpleNamespace(pw_dir=str(_ROOT_SELECTION_TEST_HOME))

    _root_selection_pwd.getpwuid = _pytest_root_selection_getpwuid

from autosport import monotonic_authority_root_binding as _root_selection
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


_ROOT_SELECTION_PRODUCT_STORE_TEST = "test_monotonic_root_selection_product_store.py"
_LEGACY_MONOTONIC_ROOT_COMPOSITION_TESTS = frozenset(
    {
        "test_dataset_snapshot_lineage.py",
        "test_point_in_time_evidence.py",
        "test_point_in_time_holdout_concurrency.py",
        "test_trial_family_accounting.py",
        "test_trial_family_accounting_registry_order.py",
        "test_trial_family_cross_ledger_witness.py",
        "test_trial_family_witness_mint_guard.py",
    }
)


@pytest.fixture(autouse=True)
def _align_legacy_monotonic_root_composition(request, tmp_path, monkeypatch):
    """Align default and explicit roots in legacy same-workspace fixtures only."""

    test_file = Path(str(request.node.fspath)).name
    if test_file not in _LEGACY_MONOTONIC_ROOT_COMPOSITION_TESTS:
        return
    authority_root = (
        tmp_path.parent / f"{tmp_path.name}-machine-authority"
    ).resolve(strict=False)
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(authority_root))


@pytest.fixture(autouse=True)
def _isolate_monotonic_root_selection_store(request, tmp_path_factory):
    """Isolate selector receipts per test without resolver rebinding.

    The fake account home is visible only to the root-selection resolver guard.
    Other product account-root resolvers retain the actual OS location.
    """

    global _ROOT_SELECTION_TEST_HOME

    test_file = Path(str(request.node.fspath)).name
    if test_file == _ROOT_SELECTION_PRODUCT_STORE_TEST:
        yield
        return

    sandbox = tmp_path_factory.mktemp("root-selection-product-state")
    previous = _ROOT_SELECTION_TEST_HOME
    _ROOT_SELECTION_TEST_HOME = sandbox
    try:
        yield
    finally:
        _ROOT_SELECTION_TEST_HOME = previous


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



# Existing provider-write suites predate the exact final-send confirmation gate.
# Supply only those legacy suites with a deterministic durable review+receipt.
# Dedicated confirmation tests are intentionally excluded.
_BETFAIR_OPERATOR_CONFIRMATION_BRIDGE_MODULES = frozenset(
    {
        "test_betfair_supervised_execution",
        "test_betfair_final_durable_approval_fence",
        "test_betfair_placeorders_customer_order_ref_echo_falsifier",
        "test_betfair_placeorders_executable_authority_code_seal",
        "test_betfair_placeorders_execution_errorcode_coherence",
        "test_betfair_placeorders_network_origin_authority",
        "test_betfair_placeorders_response_truth",
        "test_betfair_placeorders_single_instruction_status",
        "test_betfair_placeorders_terminal_identity",
        "test_betfair_placeorders_urllib_opener_origin",
        "test_betfair_stop_ledger_boundary",
        "test_betfair_trusted_runtime_write_boundary",
    }
)


@pytest.fixture(autouse=True)
def _bind_betfair_operator_confirmation(request, monkeypatch):
    module = request.module
    if module is None:
        return
    module_name = module.__name__.rsplit(".", 1)[-1]
    if module_name not in _BETFAIR_OPERATOR_CONFIRMATION_BRIDGE_MODULES:
        return
    original_execute = getattr(module, "execute_betfair_supervised_action", None)
    if not callable(original_execute):
        return

    from autosport.betfair_execution_confirmation import (
        CONFIRMATION_FILENAME,
        betfair_execution_confirmation_spec,
    )
    from autosport.supervised_confirmation import SupervisedConfirmationAuthority

    receipt_cache: dict[tuple[str, str, str, str], tuple[str, str]] = {}
    confirmation_now = datetime.fromisoformat("2026-09-19T08:00:02.200000+00:00")

    def execute_with_confirmation(ledger, bound, approval, *args, **kwargs):
        if (
            kwargs.get("confirmation_receipt_id") is None
            and kwargs.get("confirmation_review_sha256") is None
        ):
            action_id = kwargs.get("action_id")
            attempt_id = kwargs.get("attempt_id")
            if type(action_id) is str and type(attempt_id) is str:
                workspace = Path(ledger.path).parent.resolve()
                key = (
                    str(workspace),
                    bound.execution_plan.plan_id,
                    action_id,
                    attempt_id,
                )
                receipt_identity = receipt_cache.get(key)
                if receipt_identity is None:
                    spec = betfair_execution_confirmation_spec(
                        bound,
                        approval,
                        action_id=action_id,
                        attempt_id=attempt_id,
                        review_id=f"pytest-final-send-review-{attempt_id}",
                        risk_evidence_sha256="f" * 64,
                    )
                    authority = SupervisedConfirmationAuthority(
                        workspace / CONFIRMATION_FILENAME,
                        clock=lambda: confirmation_now,
                    )
                    review = authority.prepare_review(
                        review_id=spec.review_id,
                        decision_id=spec.decision_id,
                        bookmaker_id=spec.bookmaker_id,
                        account_id=spec.account_id,
                        decision_sha256=spec.decision_sha256,
                        approval_evidence_sha256=spec.approval_evidence_sha256,
                        risk_evidence_sha256=spec.risk_evidence_sha256,
                        review_payload=spec.review_payload,
                        ttl_seconds=120,
                    )
                    receipt = authority.confirm_review(
                        review_id=review.review_id,
                        expected_review_sha256=review.review_sha256,
                    )
                    receipt_identity = (
                        receipt.receipt_id,
                        review.review_sha256,
                    )
                    receipt_cache[key] = receipt_identity
                kwargs["confirmation_receipt_id"] = receipt_identity[0]
                kwargs["confirmation_review_sha256"] = receipt_identity[1]
        return original_execute(ledger, bound, approval, *args, **kwargs)

    monkeypatch.setattr(
        module,
        "execute_betfair_supervised_action",
        execute_with_confirmation,
    )

@pytest.fixture(autouse=True)
def _deterministic_betfair_mid_frame_reconnect_clock(request, monkeypatch):
    """Isolate the partial-frame recovery test from wall-clock scheduling jitter.

    The production transport deliberately applies reconnect backoff after an abnormal
    established-session failure. Dedicated backoff tests assert that contract. The
    partial-frame test has a different purpose: proving stale bytes cannot cross a
    reconnect boundary. Advance a deterministic monotonic clock only for that one
    scenario so it reaches the next eligible reconnect instant without sleeping.
    """

    if Path(str(request.node.fspath)).name != "test_betfair_stream_transport.py":
        return
    if request.node.name != "test_disconnect_mid_frame_discards_partial_bytes_before_reconnect":
        return

    from autosport import betfair_stream_transport as stream

    now = [100.0]

    def monotonic() -> float:
        value = now[0]
        now[0] += 1.0
        return value

    monkeypatch.setattr(stream.time, "monotonic", monotonic)