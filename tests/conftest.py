from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from itertools import count
import os
import sys
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest

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
import autosport.trusted_runtime_code_profile as trusted_runtime_profile
from autosport._provider_evaluation_semantic_gate import (
    _set_legacy_provider_semantic_bypass_for_tests,
)

from autosport.continuous_session import SessionState
from autosport.domain import MarketEvent
from autosport.execution_stop_authority import ExecutionStopAuthority
from autosport.product_runtime import AutonomousProductRuntime
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


# The final #1212 Betfair provider-truth suites predate the #1155 STOP-admission
# composition. Their positive provider cases must now supply the same explicit
# durable ARMED authority production requires. Keep the bridge exact and local to
# the deliberately recomposed carrier; the dedicated STOP-composition suite is
# intentionally excluded so missing/STOPPED/corrupt authority remains fail-closed.
_RECOMPOSED_BETFAIR_PROVIDER_MODULES = frozenset(
    {
        "test_betfair_supervised_execution",
        "test_betfair_placeorders_customer_order_ref_echo_falsifier",
        "test_betfair_placeorders_executable_authority_code_seal",
        "test_betfair_placeorders_execution_errorcode_coherence",
        "test_betfair_placeorders_network_origin_authority",
        "test_betfair_placeorders_response_truth",
        "test_betfair_placeorders_single_instruction_status",
        "test_betfair_placeorders_terminal_identity",
        "test_betfair_placeorders_urllib_opener_origin",
    }
)


@pytest.fixture(autouse=True)
def _bind_recomposed_betfair_stop_authority(request, monkeypatch):
    module = request.module
    if module is None:
        return
    module_name = module.__name__.rsplit(".", 1)[-1]
    if module_name not in _RECOMPOSED_BETFAIR_PROVIDER_MODULES:
        return

    # These restored provider suites use a frozen 2026-09-19 approval timeline.
    # Their original module-local autouse clock does not follow helpers imported by
    # sibling test modules, so bind the same reserve instant at this shared bridge.
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: "2026-09-19T08:00:03+00:00",
    )

    original_prepared = getattr(module, "_prepared", None)
    if not callable(original_prepared):
        return

    def prepared_with_armed_stop(tmp, *args, **kwargs):
        prepared = original_prepared(tmp, *args, **kwargs)
        stop_path = Path(tmp) / "execution-stop.jsonl"
        if not stop_path.exists():
            authority = ExecutionStopAuthority(stop_path)
            stopped = authority.initialize_stopped(
                operator_id="test-owner",
                reason="explicit test safety baseline",
                command_id="betfair-provider-stop-fixture-init",
            )
            authority.arm(
                operator_id="test-owner",
                reason="explicit positive provider test authority",
                confirmation_id="betfair-provider-stop-fixture-confirmation",
                expected_revision=stopped.revision,
                command_id="betfair-provider-stop-fixture-arm",
            )
        return prepared

    monkeypatch.setattr(module, "_prepared", prepared_with_armed_stop)


# #1891 makes a RUNNING closed-registry runtime profile an independent provider-write
# prerequisite. The restored #1212 positive suites predate that authority. Give only
# those exact suites a process-local test issuance for each workspace they prepare;
# every other suite remains capable of proving that missing/stale profile authority
# fails closed. The STOP-ledger falsifier gets only this profile prerequisite so it
# can still isolate missing STOP as the deterministic denial under test.
_BETFAIR_TRUSTED_PROFILE_MODULES = _RECOMPOSED_BETFAIR_PROVIDER_MODULES | {
    "test_betfair_stop_ledger_boundary"
}
_PROFILE_FACTORY_SPEC = "autosport.product_source:create_parlay_product_source"
_PROFILE_PROVIDER_SOURCE_ID = "parlayapi:table_tennis"


class _BetfairRuntimeLeaseStub:
    authority_active = True


class _BetfairStartTransitionStoreStub:
    @staticmethod
    def pending() -> None:
        return None


class _BetfairRunningCoordinator:
    @staticmethod
    def status() -> SimpleNamespace:
        return SimpleNamespace(state=SessionState.RUNNING)


class _BetfairRunningCollector:
    @staticmethod
    def status() -> dict[str, object | None]:
        return {"stopped_at": None, "stop_reason": None}


def _betfair_fixture_runtime(workspace: Path) -> AutonomousProductRuntime:
    runtime = object.__new__(AutonomousProductRuntime)
    runtime.workspace = workspace
    runtime.manifest = SimpleNamespace(source_id=_PROFILE_PROVIDER_SOURCE_ID)
    runtime.coordinator = _BetfairRunningCoordinator()
    runtime.collector = _BetfairRunningCollector()
    runtime._runtime_lease = _BetfairRuntimeLeaseStub()
    runtime._start_transition_store = _BetfairStartTransitionStoreStub()
    runtime._closed = False
    runtime._operation_fence = RLock()
    return runtime


@pytest.fixture(autouse=True)
def _bind_recomposed_betfair_trusted_runtime_profile(request, monkeypatch):
    module = request.module
    if module is None:
        yield
        return
    module_name = module.__name__.rsplit(".", 1)[-1]
    if module_name not in _BETFAIR_TRUSTED_PROFILE_MODULES:
        yield
        return

    prepared_owner = module
    original_prepared = getattr(prepared_owner, "_prepared", None)
    if not callable(original_prepared):
        prepared_owner = getattr(module, "provider_tests", None)
        original_prepared = getattr(prepared_owner, "_prepared", None)
    if prepared_owner is None or not callable(original_prepared):
        yield
        return

    issued_by_workspace: dict[str, tuple[AutonomousProductRuntime, object]] = {}

    def ensure_profile(tmp: str | Path) -> None:
        workspace = Path(tmp).resolve()
        key = str(workspace)
        if key in issued_by_workspace:
            return
        runtime = _betfair_fixture_runtime(workspace)
        trusted_runtime_profile._register_started_product_runtime_origin(
            runtime,
            source_factory=_PROFILE_FACTORY_SPEC,
            expected_provider_source_id=_PROFILE_PROVIDER_SOURCE_ID,
        )
        profile = trusted_runtime_profile.issue_trusted_runtime_code_profile(runtime)
        issued_by_workspace[key] = (runtime, profile)

    def prepared_with_trusted_runtime(tmp, *args, **kwargs):
        prepared = original_prepared(tmp, *args, **kwargs)
        ensure_profile(tmp)
        return prepared

    monkeypatch.setattr(prepared_owner, "_prepared", prepared_with_trusted_runtime)
    try:
        yield
    finally:
        for runtime, profile in tuple(issued_by_workspace.values()):
            trusted_runtime_profile.revoke_trusted_runtime_code_profile(profile)
            trusted_runtime_profile._clear_started_product_runtime_origin(runtime)


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