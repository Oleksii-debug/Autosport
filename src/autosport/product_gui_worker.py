from __future__ import annotations

import math
import queue
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Callable

from .continuous_session import (
    ContinuousSessionStatus,
    ContinuousTickResult,
    SessionStoppedError,
)
from .domain import TicketStatus
from .paper import PaperBook
from .portfolio import PortfolioEngine
from .operator_source_registry import (
    list_product_source_entries,
    resolve_product_source_runtime_binding,
)
from .product_entrypoint import ProductEntrypointError, _validated_source
from .product_runtime import AutonomousProductRuntime, build_autonomous_product_runtime
from .trusted_runtime_code_profile import (
    TrustedRuntimeCodeProfile,
    TrustedRuntimeCodeProfileError,
    _clear_started_product_runtime_origin,
    _register_started_product_runtime_origin,
    issue_trusted_runtime_code_profile,
    require_authoritative_trusted_runtime_code_profile,
    require_product_owned_source_factory_identity,
    revoke_trusted_runtime_code_profile,
)
from .workspace_lock import WorkspaceEconomicLock


RuntimeBuilder = Callable[[Path, str, str], AutonomousProductRuntime]
ProfiledRuntimeBuilder = Callable[..., AutonomousProductRuntime]
SourceFactory = Callable[[], object]


@dataclass(frozen=True, slots=True)
class _ProfiledSourceBinding:
    factory_spec: str
    provider_source_id: str
    factory: SourceFactory


def _capture_product_source_bindings() -> tuple[_ProfiledSourceBinding, ...]:
    """Freeze shipped registry metadata and callable identities at module import."""

    bindings: list[_ProfiledSourceBinding] = []
    for entry in list_product_source_entries():
        bound_entry, factory = resolve_product_source_runtime_binding(
            entry.factory_spec,
            entry.expected_provider_source_id,
        )
        if bound_entry is not entry:
            raise RuntimeError(
                "product source registry returned inconsistent entry identity"
            )
        bindings.append(
            _ProfiledSourceBinding(
                factory_spec=entry.factory_spec,
                provider_source_id=entry.expected_provider_source_id,
                factory=factory,
            )
        )
    return tuple(bindings)


_CAPTURED_PRODUCT_SOURCE_BINDINGS = _capture_product_source_bindings()


def _capture_profiled_runtime_builder(
    *,
    source_bindings: tuple[_ProfiledSourceBinding, ...],
    runtime_factory: Callable[..., AutonomousProductRuntime],
    runtime_type: type[AutonomousProductRuntime],
    path_type: type[Path],
) -> ProfiledRuntimeBuilder:
    """Capture the trusted source-to-runtime chain without dynamic source loading."""

    captured_bindings = tuple(source_bindings)

    def canonical_workspace(value: object) -> Path:
        try:
            return path_type(value).expanduser().resolve(strict=False)
        except (TypeError, ValueError, OSError, RuntimeError) as exc:
            raise ProductEntrypointError(
                "product runtime workspace cannot be resolved"
            ) from exc

    def build(
        workspace: Path,
        source_factory: str,
        initial_bankroll: str,
        *,
        expected_source_id: str | None = None,
    ) -> AutonomousProductRuntime:
        if (
            type(expected_source_id) is not str
            or not expected_source_id
            or expected_source_id.strip() != expected_source_id
        ):
            raise ProductEntrypointError(
                "profiled runtime requires an exact expected source identity"
            )
        matches = tuple(
            binding
            for binding in captured_bindings
            if binding.factory_spec == source_factory
            and binding.provider_source_id == expected_source_id
        )
        if len(matches) != 1:
            raise ProductEntrypointError(
                "configured source factory is not product-owned by this build"
            )
        binding = matches[0]

        # Do not route the profiled path through product_entrypoint._validated_source:
        # that compatibility helper intentionally performs a dynamic module:function
        # load. The authority path calls the exact import-time registry callable.
        source = binding.factory()
        for field in ("source_id", "stream_epoch"):
            value = getattr(source, field, None)
            if type(value) is not str or not value or value.strip() != value:
                raise ProductEntrypointError(
                    f"product source {field} must be a non-empty trimmed string"
                )
        for method in ("fetch_catalog_page", "fetch_deltas", "resolve_event"):
            if not callable(getattr(source, method, None)):
                raise ProductEntrypointError(
                    f"product source must provide callable {method}"
                )

        expected_workspace = canonical_workspace(workspace)
        source_workspace = getattr(source, "workspace", None)
        if (
            source_workspace is not None
            and canonical_workspace(source_workspace) != expected_workspace
        ):
            raise ProductEntrypointError(
                "product source workspace must match product runtime workspace"
            )
        if source.source_id != expected_source_id:
            raise ProductEntrypointError(
                "product source identity does not match the configured source"
            )

        runtime = runtime_factory(
            workspace=workspace,
            source=source,
            initial_bankroll=initial_bankroll,
        )
        if type(runtime) is not runtime_type:
            raise ProductEntrypointError(
                "profiled runtime factory returned a non-canonical runtime type"
            )

        try:
            runtime_workspace = canonical_workspace(runtime.workspace)
            runtime_source_id = runtime.manifest.source_id
            runtime_source = runtime.collector.source
        except (AttributeError, TypeError, ValueError) as exc:
            try:
                runtime.close()
            except BaseException:
                pass
            raise ProductEntrypointError(
                "profiled runtime composition cannot prove exact source origin"
            ) from exc
        if (
            runtime_workspace != expected_workspace
            or runtime_source_id != expected_source_id
            or runtime_source is not source
        ):
            try:
                runtime.close()
            except BaseException:
                pass
            raise ProductEntrypointError(
                "profiled runtime does not retain the exact closed-registry source"
            )
        return runtime

    return build


_PROFILED_RUNTIME_BUILDER = _capture_profiled_runtime_builder(
    source_bindings=_CAPTURED_PRODUCT_SOURCE_BINDINGS,
    runtime_factory=build_autonomous_product_runtime,
    runtime_type=AutonomousProductRuntime,
    path_type=Path,
)

def _runtime_builder(
    workspace: Path,
    source_factory: str,
    initial_bankroll: str,
    *,
    expected_source_id: str | None = None,
) -> AutonomousProductRuntime:
    """Compatibility/test seam; trusted worker authority never calls this path."""

    if expected_source_id is not None and (
        type(expected_source_id) is not str
        or not expected_source_id
        or expected_source_id.strip() != expected_source_id
    ):
        raise ValueError("expected_source_id must be a non-empty trimmed string")
    if expected_source_id is not None:
        try:
            require_product_owned_source_factory_identity(
                source_factory=source_factory,
                expected_provider_source_id=expected_source_id,
            )
        except TrustedRuntimeCodeProfileError as exc:
            raise ProductEntrypointError(
                "configured source factory is not product-owned by this build"
            ) from exc
    source = _validated_source(source_factory, workspace=workspace)
    if expected_source_id is not None:
        try:
            require_product_owned_source_factory_identity(
                source_factory=source_factory,
                expected_provider_source_id=expected_source_id,
            )
        except TrustedRuntimeCodeProfileError as exc:
            raise ProductEntrypointError(
                "configured source factory changed during source construction"
            ) from exc
    if expected_source_id is not None and source.source_id != expected_source_id:
        raise ProductEntrypointError(
            "product source identity does not match the configured source"
        )
    return build_autonomous_product_runtime(
        workspace=workspace,
        source=source,
        initial_bankroll=initial_bankroll,
    )


# Compatibility/debug alias only. Trust eligibility never compares against this
# mutable module-global binding.
_CANONICAL_RUNTIME_BUILDER = _PROFILED_RUNTIME_BUILDER


def _safe_error_type(exc: BaseException) -> str:
    """Return a bounded identifier only; exception detail never crosses to the UI."""

    try:
        name = type.__getattribute__(type(exc), "__name__")
    except BaseException:
        return "BaseException"
    if (
        type(name) is not str
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) is None
    ):
        return "BaseException"
    return name


@dataclass(frozen=True, slots=True)
class ProductGuiEconomicTicket:
    """Immutable presentation copy of one canonical durable PAPER ticket."""

    ticket_id: str
    status: str
    stake: Decimal
    combined_odds: Decimal
    payout: Decimal
    legs: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.ticket_id) is not str
            or not self.ticket_id
            or self.ticket_id.strip() != self.ticket_id
        ):
            raise ValueError("economic ticket id must be non-empty trimmed text")
        if self.status not in {"open", "won", "lost", "void"}:
            raise ValueError("economic ticket status is not canonical")
        for field_name, value in (
            ("stake", self.stake),
            ("combined_odds", self.combined_odds),
            ("payout", self.payout),
        ):
            if type(value) is not Decimal or not value.is_finite():
                raise ValueError(f"economic ticket {field_name} must be finite Decimal")
        if self.stake <= 0 or self.combined_odds <= 0 or self.payout < 0:
            raise ValueError("economic ticket monetary values are outside canonical range")
        if (
            type(self.legs) is not tuple
            or not self.legs
            or any(
                type(leg) is not str or not leg or leg.strip() != leg
                for leg in self.legs
            )
        ):
            raise ValueError("economic ticket legs must be non-empty canonical tuple")


@dataclass(frozen=True, slots=True)
class ProductGuiEconomicSnapshot:
    """Read-only economic truth captured after one canonical runtime tick."""

    workspace: Path
    session_id: str
    source_id: str
    cycle_index: int
    cycle_last_success_at: str | None
    paper_book_sha256: str | None
    balance: Decimal
    committed_stake: Decimal
    tickets: tuple[ProductGuiEconomicTicket, ...]
    portfolio_mode: str
    portfolio_scenario_count: int
    portfolio_worst_case: Decimal
    portfolio_best_case: Decimal
    portfolio_mean_case: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Path) or not self.workspace.is_absolute():
            raise ValueError("economic snapshot workspace must be an absolute Path")
        for field_name, value in (
            ("session_id", self.session_id),
            ("source_id", self.source_id),
        ):
            if type(value) is not str or not value or value.strip() != value:
                raise ValueError(
                    f"economic snapshot {field_name} must be non-empty trimmed text"
                )
        if (
            type(self.cycle_index) is not int
            or isinstance(self.cycle_index, bool)
            or self.cycle_index < 0
        ):
            raise ValueError("economic snapshot cycle_index must be a nonnegative int")
        if self.cycle_last_success_at is not None:
            value = self.cycle_last_success_at
            if (
                type(value) is not str
                or not value
                or value.strip() != value
            ):
                raise ValueError(
                    "economic snapshot cycle_last_success_at must be canonical timestamp or None"
                )
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    "economic snapshot cycle_last_success_at must be valid ISO-8601"
                ) from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError(
                    "economic snapshot cycle_last_success_at must be timezone-aware"
                )
        if self.paper_book_sha256 is not None and (
            type(self.paper_book_sha256) is not str
            or len(self.paper_book_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.paper_book_sha256
            )
        ):
            raise ValueError("economic snapshot PaperBook SHA must be lowercase SHA-256")
        for field_name, value in (
            ("balance", self.balance),
            ("committed_stake", self.committed_stake),
            ("portfolio_worst_case", self.portfolio_worst_case),
            ("portfolio_best_case", self.portfolio_best_case),
            ("portfolio_mean_case", self.portfolio_mean_case),
        ):
            if type(value) is not Decimal or not value.is_finite():
                raise ValueError(f"economic snapshot {field_name} must be finite Decimal")
        if self.balance < 0:
            raise ValueError("economic snapshot balance cannot be negative")
        if self.committed_stake < 0:
            raise ValueError("economic snapshot committed_stake cannot be negative")
        if type(self.tickets) is not tuple or any(
            type(ticket) is not ProductGuiEconomicTicket for ticket in self.tickets
        ):
            raise ValueError("economic snapshot tickets must be canonical tuple")
        ticket_ids = tuple(ticket.ticket_id for ticket in self.tickets)
        if len(ticket_ids) != len(set(ticket_ids)):
            raise ValueError("economic snapshot ticket ids must be unique")
        expected_committed = sum(
            (
                ticket.stake
                for ticket in self.tickets
                if ticket.status == "open"
            ),
            Decimal("0"),
        )
        if self.committed_stake != expected_committed:
            raise ValueError(
                "economic snapshot committed stake conflicts with open tickets"
            )
        if self.portfolio_mode not in {
            "exact",
            "approximate",
            "conservative-enumeration",
            "conservative-approximate",
        }:
            raise ValueError("economic snapshot portfolio mode is not canonical")
        if (
            type(self.portfolio_scenario_count) is not int
            or isinstance(self.portfolio_scenario_count, bool)
            or self.portfolio_scenario_count < 1
        ):
            raise ValueError(
                "economic snapshot portfolio_scenario_count must be positive int"
            )
        if not (
            self.portfolio_worst_case
            <= self.portfolio_mean_case
            <= self.portfolio_best_case
        ):
            raise ValueError(
                "economic snapshot portfolio bounds are internally inconsistent"
            )


def _capture_runtime_economic_snapshot(
    runtime: AutonomousProductRuntime,
    tick: ContinuousTickResult,
    _runtime_type: type[AutonomousProductRuntime] = AutonomousProductRuntime,
    _tick_type: type[ContinuousTickResult] = ContinuousTickResult,
    _paper_book_type: type[PaperBook] = PaperBook,
    _portfolio_engine_type: type[PortfolioEngine] = PortfolioEngine,
    _economic_lock_type: type[WorkspaceEconomicLock] = WorkspaceEconomicLock,
    _snapshot_type: type[ProductGuiEconomicSnapshot] = ProductGuiEconomicSnapshot,
    _ticket_type: type[ProductGuiEconomicTicket] = ProductGuiEconomicTicket,
    _ticket_status_type: type[TicketStatus] = TicketStatus,
    _decimal_type: type[Decimal] = Decimal,
    _path_type: type[Path] = Path,
    _sha256=sha256,
) -> ProductGuiEconomicSnapshot:
    """Capture economic presentation truth without reopening AutosportSession.

    The caller keeps the canonical runtime operation fence held across the completed
    tick and this read. The existing workspace economic lock serializes the exact
    PaperBook byte snapshot against canonical economic writers.
    """

    if type(runtime) is not _runtime_type or type(tick) is not _tick_type:
        raise RuntimeError("economic snapshot requires exact canonical runtime tick")
    workspace = _path_type(runtime.workspace)
    coordinator = runtime.coordinator
    if (
        not workspace.is_absolute()
        or _path_type(coordinator.workspace) != workspace
        or tick.session_id != coordinator.session_id
        or tick.source_id != runtime.manifest.source_id
        or tick.cycle_index < 0
    ):
        raise RuntimeError("economic snapshot runtime identity mismatch")

    with _economic_lock_type(workspace):
        book_path = _path_type(coordinator.paper_book_path)
        if book_path != workspace / "paper_book.json":
            raise RuntimeError(
                "economic snapshot PaperBook path is not canonical for runtime workspace"
            )
        if book_path.exists():
            payload = book_path.read_bytes()
            paper_book_sha256 = _sha256(payload).hexdigest()
            book = _paper_book_type.load_bytes(payload)
        else:
            paper_book_sha256 = None
            book = _paper_book_type(runtime.manifest.initial_bankroll)

        tickets = tuple(
            _ticket_type(
                ticket_id=ticket.ticket_id,
                status=ticket.status.value,
                stake=ticket.stake,
                combined_odds=ticket.combined_odds,
                payout=ticket.payout,
                legs=tuple(
                    f"{leg.event_id}/{leg.market_id}/{leg.selection_id}@{leg.locked_odds}"
                    for leg in ticket.legs
                ),
            )
            for ticket in book.tickets.values()
        )
        committed_stake = sum(
            (
                ticket.stake
                for ticket in book.tickets.values()
                if ticket.status is _ticket_status_type.OPEN
            ),
            _decimal_type("0"),
        )
        portfolio = _portfolio_engine_type().analyse(list(book.tickets.values()))

    return _snapshot_type(
        workspace=workspace,
        session_id=tick.session_id,
        source_id=tick.source_id,
        cycle_index=tick.cycle_index,
        cycle_last_success_at=tick.last_success_at,
        paper_book_sha256=paper_book_sha256,
        balance=book.balance,
        committed_stake=committed_stake,
        tickets=tickets,
        portfolio_mode=portfolio.mode,
        portfolio_scenario_count=portfolio.scenario_count,
        portfolio_worst_case=portfolio.worst_case,
        portfolio_best_case=portfolio.best_case,
        portfolio_mean_case=portfolio.mean_case,
    )


@dataclass(frozen=True, slots=True)
class ProductGuiMessage:
    """One secret-safe projection from the canonical background product runtime."""

    kind: str
    status: ContinuousSessionStatus | None = None
    tick: ContinuousTickResult | None = None
    error_type: str | None = None
    stop_reason: str | None = None
    economic: ProductGuiEconomicSnapshot | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"STARTED", "TICK", "STOPPED", "ERROR"}:
            raise ValueError("unsupported product GUI message kind")
        payload_count = sum(
            value is not None
            for value in (self.status, self.tick, self.error_type)
        )
        if payload_count != 1:
            raise ValueError("product GUI message must contain exactly one payload")
        if self.kind in {"STARTED", "STOPPED"} and self.status is None:
            raise ValueError("status message requires ContinuousSessionStatus")
        if self.kind == "TICK" and self.tick is None:
            raise ValueError("tick message requires ContinuousTickResult")
        if self.economic is not None:
            if self.kind != "TICK" or self.tick is None:
                raise ValueError("economic snapshot is valid only for TICK messages")
            if type(self.economic) is not ProductGuiEconomicSnapshot:
                raise ValueError("economic snapshot must be canonical")
            if (
                self.economic.session_id != self.tick.session_id
                or self.economic.source_id != self.tick.source_id
                or self.economic.cycle_index != self.tick.cycle_index
            ):
                raise ValueError("economic snapshot does not match runtime tick identity")
        if self.kind == "ERROR" and self.error_type is None:
            raise ValueError("error message requires error_type")
        if self.kind != "STOPPED" and self.stop_reason is not None:
            raise ValueError("stop_reason is valid only for STOPPED messages")


class ProductGuiWorker:
    """Drive one canonical durable PAPER runtime off the WebView/GUI thread.

    This class owns no market, money, settlement, provider, learning, or execution
    semantics. It only serializes lifetime of the existing AutonomousProductRuntime
    and projects bounded messages to presentation.
    """

    def __init__(self, *, runtime_builder: RuntimeBuilder | None = None) -> None:
        # None is the only public constructor state eligible for the profiled path.
        # Supplying any builder is structurally unprofiled, regardless of mutable
        # module-global aliases.
        self._runtime_builder = runtime_builder
        self._messages: queue.Queue[ProductGuiMessage] = queue.Queue()
        self._lock = threading.Lock()
        self._busy = False
        self._thread: threading.Thread | None = None
        self._runtime: AutonomousProductRuntime | None = None
        self._stop_event = threading.Event()
        self._stop_reason = "operator_stop"
        self._trusted_runtime_profile: TrustedRuntimeCodeProfile | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def stop_requested(self) -> bool:
        """Expose only canonical in-process STOP acceptance for presentation."""

        with self._lock:
            return bool(self._busy and self._stop_event.is_set())

    @property
    def trusted_runtime_profile(self) -> TrustedRuntimeCodeProfile | None:
        """Return the current process-issued profile, never a persisted authority."""

        with self._lock:
            return self._trusted_runtime_profile

    def start(
        self,
        *,
        workspace: str | Path,
        source_factory: str,
        expected_source_id: str | None = None,
        initial_bankroll: str = "10000",
        poll_seconds: float = 30.0,
    ) -> bool:
        if (
            type(source_factory) is not str
            or not source_factory
            or source_factory.strip() != source_factory
        ):
            raise ValueError("source_factory must be a non-empty trimmed string")
        if expected_source_id is not None and (
            type(expected_source_id) is not str
            or not expected_source_id
            or expected_source_id.strip() != expected_source_id
        ):
            raise ValueError("expected_source_id must be a non-empty trimmed string")
        if (
            isinstance(poll_seconds, bool)
            or not isinstance(poll_seconds, (int, float))
            or not math.isfinite(float(poll_seconds))
            or poll_seconds <= 0
        ):
            raise ValueError("poll_seconds must be a finite positive number")

        root = Path(workspace)
        with self._lock:
            if self._busy:
                return False
            if self._trusted_runtime_profile is not None:
                raise RuntimeError(
                    "trusted runtime profile remained active after prior run"
                )
            self._messages = queue.Queue()
            self._busy = True
            self._stop_event = threading.Event()
            self._stop_reason = "operator_stop"

        start_gate = threading.Event()
        cancelled = threading.Event()
        try:
            thread = threading.Thread(
                target=self._run_when_committed,
                args=(
                    root,
                    source_factory,
                    expected_source_id,
                    initial_bankroll,
                    float(poll_seconds),
                    start_gate,
                    cancelled,
                ),
                name="autosport-product-runtime-webview",
                daemon=False,
            )
        except BaseException:
            self._release_unstarted_slot()
            raise

        self._thread = thread
        committed = False
        try:
            thread.start()
            committed = True
            start_gate.set()
        except BaseException:
            if committed:
                start_gate.set()
                return True
            cancelled.set()
            start_gate.set()
            self._release_unstarted_slot()
            raise
        return True

    def request_stop(self, reason: str = "operator_stop") -> bool:
        if type(reason) is not str or not reason or reason.strip() != reason:
            raise ValueError("stop reason must be a non-empty trimmed string")
        runtime: AutonomousProductRuntime | None
        resolved_reason: str
        with self._lock:
            if not self._busy:
                return False
            if not self._stop_event.is_set():
                self._stop_reason = reason
                self._stop_event.set()
                # STOP acceptance is also the trust-revocation linearization point.
                # Keep it serialized with profile issuance so a request that wins
                # before issuance can never be followed by a positive profile, and a
                # request that arrives later returns only after authority is revoked.
                runtime_profile = self._trusted_runtime_profile
                if runtime_profile is not None:
                    revoke_trusted_runtime_code_profile(runtime_profile)
                    self._trusted_runtime_profile = None
                runtime_for_origin = self._runtime
                if runtime_for_origin is not None:
                    _clear_started_product_runtime_origin(runtime_for_origin)
            resolved_reason = self._stop_reason
            runtime = self._runtime

        if runtime is not None:
            request_runtime_stop = getattr(runtime, "request_stop", None)
            if callable(request_runtime_stop):
                request_runtime_stop(resolved_reason)
        return True

    def poll(self) -> ProductGuiMessage | None:
        try:
            message = self._messages.get_nowait()
        except queue.Empty:
            return None
        if message.kind in {"STOPPED", "ERROR"}:
            # A terminal message is part of runtime lifecycle truth, not optional
            # telemetry. Keep the admission slot owned until presentation consumes
            # it so a successor start cannot replace the queue and erase terminal
            # STOP/ERROR evidence before the controller applies recovery/status truth.
            with self._lock:
                self._busy = False
        return message

    def join(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _release_unstarted_slot(self) -> None:
        self._thread = None
        with self._lock:
            self._busy = False

    def _run_when_committed(
        self,
        workspace: Path,
        source_factory: str,
        expected_source_id: str | None,
        initial_bankroll: str,
        poll_seconds: float,
        start_gate: threading.Event,
        cancelled: threading.Event,
    ) -> None:
        start_gate.wait()
        if cancelled.is_set():
            return
        self._run(
            workspace=workspace,
            source_factory=source_factory,
            expected_source_id=expected_source_id,
            initial_bankroll=initial_bankroll,
            poll_seconds=poll_seconds,
        )

    def _run(
        self,
        *,
        workspace: Path,
        source_factory: str,
        expected_source_id: str | None,
        initial_bankroll: str,
        poll_seconds: float,
        _profiled_runtime_builder: ProfiledRuntimeBuilder = _PROFILED_RUNTIME_BUILDER,
        _economic_snapshot_builder: Callable[
            [AutonomousProductRuntime, ContinuousTickResult],
            ProductGuiEconomicSnapshot,
        ] = _capture_runtime_economic_snapshot,
        _economic_snapshot_builder_code=_capture_runtime_economic_snapshot.__code__,
        _economic_snapshot_builder_defaults=_capture_runtime_economic_snapshot.__defaults__,
        _runtime_type: type[AutonomousProductRuntime] = AutonomousProductRuntime,
    ) -> None:
        runtime: AutonomousProductRuntime | None = None
        runtime_profile: TrustedRuntimeCodeProfile | None = None
        terminal_error: BaseException | None = None
        stopped_status: ContinuousSessionStatus | None = None
        stop_reason: str | None = None
        try:
            if expected_source_id is not None:
                if self._runtime_builder is not None:
                    raise ProductEntrypointError(
                        "configured source identity forbids "
                        "caller-injected runtime builders"
                    )
                # The authoritative path uses a function-definition-time captured
                # closure containing the exact import-time registry factory result.
                runtime = _profiled_runtime_builder(
                    workspace,
                    source_factory,
                    initial_bankroll,
                    expected_source_id=expected_source_id,
                )
            elif self._runtime_builder is None:
                # Compatibility-only default mode remains deliberately unprofiled.
                runtime = _runtime_builder(
                    workspace,
                    source_factory,
                    initial_bankroll,
                )
            else:
                runtime = self._runtime_builder(
                    workspace,
                    source_factory,
                    initial_bankroll,
                )
            with self._lock:
                self._runtime = runtime

            # A STOP requested while construction was in flight must win before START.
            if self._stop_event.is_set():
                stop_reason = self._stop_reason
                stopped_status = runtime.stop(stop_reason)
            else:
                started_status = runtime.start()
                if expected_source_id is not None:
                    # Serialize STOP acceptance and trusted-profile issuance through the
                    # worker lifecycle lock. This gives the two operations one ordering:
                    # STOP first => no profile; issuance first => request_stop() revokes
                    # the profile before returning to its caller.
                    with self._lock:
                        if not self._stop_event.is_set():
                            _register_started_product_runtime_origin(
                                runtime,
                                source_factory=source_factory,
                                expected_provider_source_id=expected_source_id,
                            )
                            runtime_profile = issue_trusted_runtime_code_profile(runtime)
                            self._trusted_runtime_profile = runtime_profile
                self._messages.put(
                    ProductGuiMessage(kind="STARTED", status=started_status)
                )

                while not self._stop_event.is_set():
                    try:
                        economic = None
                        if (
                            _economic_snapshot_builder.__code__
                            is not _economic_snapshot_builder_code
                            or _economic_snapshot_builder.__defaults__
                            is not _economic_snapshot_builder_defaults
                            or _economic_snapshot_builder.__kwdefaults__ is not None
                        ):
                            raise RuntimeError(
                                "runtime economic snapshot authority implementation changed"
                            )
                        if type(runtime) is _runtime_type and runtime_profile is not None:
                            # Economic presentation truth is issued only for the same
                            # currently-authoritative profiled runtime already used by
                            # the packaged product path. Compatibility/dynamic-source
                            # runtimes may tick, but cannot mint this projection.
                            try:
                                require_authoritative_trusted_runtime_code_profile(
                                    runtime_profile,
                                    workspace=runtime.workspace,
                                )
                            except TrustedRuntimeCodeProfileError:
                                if self._stop_event.is_set():
                                    break
                                raise
                            # Keep one outer canonical runtime-operation fence across
                            # tick completion and economic readback. runtime.tick()
                            # re-enters the same RLock through its existing decorator.
                            with runtime._operation_fence:
                                tick = runtime.tick()
                                if self._stop_event.is_set():
                                    break
                                try:
                                    require_authoritative_trusted_runtime_code_profile(
                                        runtime_profile,
                                        workspace=runtime.workspace,
                                    )
                                except TrustedRuntimeCodeProfileError:
                                    if self._stop_event.is_set():
                                        break
                                    raise
                                economic = _economic_snapshot_builder(runtime, tick)
                                if self._stop_event.is_set():
                                    break
                                try:
                                    require_authoritative_trusted_runtime_code_profile(
                                        runtime_profile,
                                        workspace=runtime.workspace,
                                    )
                                except TrustedRuntimeCodeProfileError:
                                    if self._stop_event.is_set():
                                        break
                                    raise
                        else:
                            # Compatibility-only injected runtimes are never product
                            # economic authority and therefore cannot mint a snapshot.
                            tick = runtime.tick()
                    except SessionStoppedError:
                        if not self._stop_event.is_set():
                            raise
                        break
                    if self._stop_event.is_set():
                        break
                    self._messages.put(
                        ProductGuiMessage(kind="TICK", tick=tick, economic=economic)
                    )
                    if self._stop_event.wait(poll_seconds):
                        break

                stop_reason = self._stop_reason
                stopped_status = runtime.stop(stop_reason)
        except BaseException as exc:
            terminal_error = exc
            # Compensation is based on possession of a canonical runtime, not on
            # successful return from start(). This closes the in-process partial-START
            # hole while the durable crash-window recovery remains owned by #821.
            if runtime is not None:
                try:
                    runtime.stop("runtime_error")
                except BaseException:
                    pass
        finally:
            if runtime_profile is not None:
                revoke_trusted_runtime_code_profile(runtime_profile)
                with self._lock:
                    if self._trusted_runtime_profile is runtime_profile:
                        self._trusted_runtime_profile = None
            if runtime is not None:
                _clear_started_product_runtime_origin(runtime)
                try:
                    runtime.close()
                except BaseException as exc:
                    if terminal_error is None:
                        terminal_error = exc
            terminal_message: ProductGuiMessage | None = None
            if terminal_error is not None:
                terminal_message = ProductGuiMessage(
                    kind="ERROR",
                    error_type=_safe_error_type(terminal_error),
                )
            elif stopped_status is not None and stop_reason is not None:
                terminal_message = ProductGuiMessage(
                    kind="STOPPED",
                    status=stopped_status,
                    stop_reason=stop_reason,
                )

            # Revoke the runtime reference before exposing terminal truth. The busy
            # slot deliberately remains held until poll() consumes STOPPED/ERROR;
            # otherwise a successor start can replace _messages in the publication
            # gap and permanently drop the prior run's terminal state.
            with self._lock:
                self._runtime = None
                if terminal_message is None:
                    self._busy = False
            if terminal_message is not None:
                self._messages.put(terminal_message)
