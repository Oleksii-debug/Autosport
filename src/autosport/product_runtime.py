from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    DesktopDeltaConsumer,
    canonical_event_digest,
)
from .collector_service import CollectorServiceSource, HeadlessCollectorService
from .continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionStatus,
    ContinuousTickResult,
    SettlementLearningHandoff,
    SettlementOutcomeAuthority,
)
from .domain import MarketEvent
from .event_lifecycle import ContinuousEventLifecycle
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
    MarketMirror,
)
from .storage import SQLiteMarketStore


class ProductCompositionError(RuntimeError):
    """The durable product composition cannot be verified safely."""


class ProductCollectorSource(CollectorServiceSource, Protocol):
    """One acquisition source plus canonical delta-to-event resolution.

    Provider credentials and network policy remain outside this composition root. The
    root owns only how the already-normalized source is connected to Autosport's
    canonical durable authorities.
    """

    def resolve_event(self, delta: CollectorDelta) -> MarketEvent:
        ...


@dataclass(frozen=True, slots=True)
class ProductCompositionManifest:
    source_id: str
    initial_bankroll: str


class _ManifestStore:
    _SCHEMA = "autosport.autonomous_product_composition"
    _VERSION = 1
    _FIELDS = {"schema", "schema_version", "source_id", "initial_bankroll"}

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def _text(value: object, field: str) -> str:
        if type(value) is not str or not value or value.strip() != value:
            raise ProductCompositionError(f"{field} must be a non-empty trimmed string")
        return value

    def _read_raw(self) -> dict[str, object]:
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ProductCompositionError("cannot verify product composition manifest") from exc
        if (
            type(raw) is not dict
            or set(raw) != self._FIELDS
            or raw.get("schema") != self._SCHEMA
            or raw.get("schema_version") != self._VERSION
        ):
            raise ProductCompositionError("product composition manifest schema mismatch")
        self._text(raw.get("source_id"), "source_id")
        self._text(raw.get("initial_bankroll"), "initial_bankroll")
        return raw

    def load_or_create(
        self,
        *,
        source_id: str,
        initial_bankroll: str,
    ) -> ProductCompositionManifest:
        source_id = self._text(source_id, "source_id")
        initial_bankroll = self._text(initial_bankroll, "initial_bankroll")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            atomic_write_json(
                self.path,
                {
                    "schema": self._SCHEMA,
                    "schema_version": self._VERSION,
                    "source_id": source_id,
                    "initial_bankroll": initial_bankroll,
                },
            )
        raw = self._read_raw()
        if raw["source_id"] != source_id:
            raise ProductCompositionError(
                "configured source_id conflicts with durable product composition"
            )
        if raw["initial_bankroll"] != initial_bankroll:
            raise ProductCompositionError(
                "configured initial_bankroll conflicts with durable product composition"
            )
        return ProductCompositionManifest(
            source_id=source_id,
            initial_bankroll=initial_bankroll,
        )


class _MarketApplicationReceipts:
    """Durable receipt seam for collector-delta application and crash replay.

    Market state stays authoritative in SQLiteMarketStore. This store records only the
    evidence needed for DesktopDeltaConsumer to prove that a specific canonical delta
    was already applied. If a process dies after the SQLite append but before receipt
    persistence, replaying the same event is idempotent and then mints the missing
    receipt; conflicting content still fails in SQLiteMarketStore/canonical digest
    validation.
    """

    _SCHEMA = "autosport.product_market_application_receipts"
    _VERSION = 1
    _FIELDS = {"schema", "schema_version", "receipts"}
    _RECEIPT_FIELDS = {
        "delta_id",
        "canonical_event_digest",
        "receipt_id",
        "applied_at",
    }

    def __init__(
        self,
        path: Path,
        *,
        market_store: SQLiteMarketStore,
        invalidations: BoundedMirrorInvalidationBuffer,
        clock: Callable[[], str],
    ) -> None:
        self.path = path
        self.market_store = market_store
        self.invalidations = invalidations
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            atomic_write_json(
                self.path,
                {
                    "schema": self._SCHEMA,
                    "schema_version": self._VERSION,
                    "receipts": [],
                },
            )
        self._read()

    def _read(self) -> dict[str, object]:
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ProductCompositionError(
                "cannot verify product market application receipts"
            ) from exc
        if (
            type(raw) is not dict
            or set(raw) != self._FIELDS
            or raw.get("schema") != self._SCHEMA
            or raw.get("schema_version") != self._VERSION
            or type(raw.get("receipts")) is not list
        ):
            raise ProductCompositionError(
                "product market application receipt schema mismatch"
            )
        seen: set[str] = set()
        for item in raw["receipts"]:
            if type(item) is not dict or set(item) != self._RECEIPT_FIELDS:
                raise ProductCompositionError(
                    "product market application receipt entry schema mismatch"
                )
            try:
                receipt = DesktopApplicationReceipt(
                    delta_id=item["delta_id"],
                    canonical_event_digest=item["canonical_event_digest"],
                    receipt_id=item["receipt_id"],
                    applied_at=item["applied_at"],
                )
                receipt.validate()
            except (TypeError, ValueError) as exc:
                raise ProductCompositionError(
                    "product market application receipt is invalid"
                ) from exc
            if receipt.delta_id in seen:
                raise ProductCompositionError(
                    "duplicate delta_id in product market application receipts"
                )
            seen.add(receipt.delta_id)
        return raw

    def lookup(self, delta: CollectorDelta) -> DesktopApplicationReceipt | None:
        delta.validate()
        for item in self._read()["receipts"]:
            if item["delta_id"] != delta.delta_id:
                continue
            if item["canonical_event_digest"] != delta.canonical_event_digest:
                raise ProductCompositionError(
                    "durable application receipt conflicts with collector delta"
                )
            receipt = DesktopApplicationReceipt(
                delta_id=item["delta_id"],
                canonical_event_digest=item["canonical_event_digest"],
                receipt_id=item["receipt_id"],
                applied_at=item["applied_at"],
            )
            receipt.validate()
            return receipt
        return None

    def apply(
        self,
        delta: CollectorDelta,
        event: MarketEvent,
    ) -> DesktopApplicationReceipt:
        delta.validate()
        if not isinstance(event, MarketEvent):
            raise TypeError("event must be MarketEvent")
        digest = canonical_event_digest(event)
        if digest != delta.canonical_event_digest:
            raise ProductCompositionError(
                "resolved market event conflicts with collector delta digest"
            )
        existing = self.lookup(delta)
        if existing is not None:
            return existing

        # Persist first. A crash after this append is safe: exact replay is idempotent.
        self.market_store.append(event)
        self.invalidations.accept_persisted(event)
        receipt = DesktopApplicationReceipt(
            delta_id=delta.delta_id,
            canonical_event_digest=digest,
            receipt_id=f"product-market:{delta.delta_id}:{digest}",
            applied_at=self.clock(),
        )
        receipt.validate()

        raw = self._read()
        if any(item["delta_id"] == delta.delta_id for item in raw["receipts"]):
            return self.lookup(delta) or receipt
        raw["receipts"].append(
            {
                "delta_id": receipt.delta_id,
                "canonical_event_digest": receipt.canonical_event_digest,
                "receipt_id": receipt.receipt_id,
                "applied_at": receipt.applied_at,
            }
        )
        atomic_write_json(self.path, raw)
        return self.lookup(delta) or receipt


@dataclass(slots=True)
class AutonomousProductRuntime:
    """One supported headless composition of the integrated PAPER product authorities."""

    workspace: Path
    manifest: ProductCompositionManifest
    coordinator: ContinuousSessionCoordinator
    collector: HeadlessCollectorService
    market_store: SQLiteMarketStore
    lifecycle: ContinuousEventLifecycle
    mirror: MarketMirror
    invalidations: BoundedMirrorInvalidationBuffer
    dependencies: FocusedMirrorDependencyIndex

    def start(self) -> ContinuousSessionStatus:
        self.collector.resume()
        self.coordinator.resume()
        return self.status()

    def pause(self) -> ContinuousSessionStatus:
        self.coordinator.pause()
        return self.status()

    def resume(self) -> ContinuousSessionStatus:
        return self.start()

    def stop(self, reason: str = "operator_stop") -> ContinuousSessionStatus:
        self.collector.stop(reason)
        self.coordinator.stop(reason)
        return self.status()

    def status(self) -> ContinuousSessionStatus:
        return self.coordinator.status()

    def tick(self) -> ContinuousTickResult:
        return self.coordinator.tick()

    def close(self) -> None:
        self.market_store.close()


def build_autonomous_product_runtime(
    *,
    workspace: str | Path,
    source: ProductCollectorSource,
    clock: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    initial_bankroll: str = "10000",
    outcome_authority: SettlementOutcomeAuthority | None = None,
    settlement_learning_handoff: SettlementLearningHandoff | None = None,
) -> AutonomousProductRuntime:
    """Construct or restore one canonical headless PAPER product runtime.

    This function deliberately composes existing Autosport authorities instead of
    recreating collector, market, lifecycle, settlement, or learning truth. The
    provider-specific source supplies only acquisition and canonical event resolution.
    Product-owned paths and identities are deterministic from ``workspace`` so restart
    reopens the same durable session rather than constructing a second runtime graph.
    """

    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    source_id = getattr(source, "source_id", None)
    if type(source_id) is not str or not source_id or source_id.strip() != source_id:
        raise ProductCompositionError("source.source_id must be a non-empty trimmed string")
    if not callable(getattr(source, "resolve_event", None)):
        raise ProductCompositionError("source.resolve_event must be callable")
    resolved_clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    manifest = _ManifestStore(root / "product_composition.json").load_or_create(
        source_id=source_id,
        initial_bankroll=str(initial_bankroll),
    )

    lifecycle = ContinuousEventLifecycle(root / "catalog.json")
    market_store = SQLiteMarketStore(root / "market.db")
    mirror = MarketMirror()
    invalidations = BoundedMirrorInvalidationBuffer(mirror)
    # Rebuild volatile mirror truth from the canonical durable current projection.
    try:
        for event in market_store.current_by_source().values():
            invalidations.accept_persisted(event)
    except Exception:
        market_store.close()
        raise
    dependencies = FocusedMirrorDependencyIndex(mirror)
    collector_store = CollectorDeltaStore(root / "collector_deltas.json")
    collector = HeadlessCollectorService(
        delta_store=collector_store,
        lifecycle=lifecycle,
        source=source,
        state_path=root / "collector_state.json",
        run_id=f"product:{source_id}",
        clock=resolved_clock,
        sleep=sleep,
    )
    receipts = _MarketApplicationReceipts(
        root / "desktop_application_receipts.json",
        market_store=market_store,
        invalidations=invalidations,
        clock=resolved_clock,
    )
    desktop = DesktopDeltaConsumer(
        collector_store,
        DesktopDeltaCheckpointStore(root / "desktop_acks.json"),
        resolve_event=source.resolve_event,
        apply_event=receipts.apply,
        lookup_application_receipt=receipts.lookup,
    )
    coordinator = ContinuousSessionCoordinator(
        workspace=root,
        collector=collector,
        lifecycle=lifecycle,
        market_store=market_store,
        desktop_consumer=desktop,
        invalidation_buffer=invalidations,
        dependency_index=dependencies,
        outcome_authority=outcome_authority,
        settlement_learning_handoff=settlement_learning_handoff,
        clock=resolved_clock,
        initial_bankroll=manifest.initial_bankroll,
    )
    return AutonomousProductRuntime(
        workspace=root,
        manifest=manifest,
        coordinator=coordinator,
        collector=collector,
        market_store=market_store,
        lifecycle=lifecycle,
        mirror=mirror,
        invalidations=invalidations,
        dependencies=dependencies,
    )
