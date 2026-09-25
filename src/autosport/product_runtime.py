from __future__ import annotations

import hashlib
import json
from types import FunctionType
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .causal_collector import (
    CanonicalDesktopApplication,
    CollectorDelta,
    CollectorDeltaStore,
    DesktopDeltaCheckpointStore,
    DesktopDeltaConsumer,
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
from .ingestion_health import SourceHealthStore
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .market_bus import MarketEventBus
from .market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
    MarketMirror,
)
from .paper import PaperBook
from .resolver_semantics import ResolverSemanticIdentityError, function_semantic_sha256
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
    settlement_authority_identity: str | None = None


class _ManifestStore:
    _SCHEMA = "autosport.autonomous_product_composition"
    _VERSION = 2
    _V1_FIELDS = {"schema", "schema_version", "source_id", "initial_bankroll"}
    _FIELDS = {
        "schema",
        "schema_version",
        "source_id",
        "initial_bankroll",
        "settlement_authority_identity",
    }

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
        if type(raw) is not dict or raw.get("schema") != self._SCHEMA:
            raise ProductCompositionError("product composition manifest schema mismatch")
        version = raw.get("schema_version")
        if version == 1 and set(raw) == self._V1_FIELDS:
            self._text(raw.get("source_id"), "source_id")
            self._text(raw.get("initial_bankroll"), "initial_bankroll")
            return {
                **raw,
                "settlement_authority_identity": None,
            }
        if version != self._VERSION or set(raw) != self._FIELDS:
            raise ProductCompositionError("product composition manifest schema mismatch")
        self._text(raw.get("source_id"), "source_id")
        self._text(raw.get("initial_bankroll"), "initial_bankroll")
        authority_identity = raw.get("settlement_authority_identity")
        if authority_identity is not None:
            identity = self._text(
                authority_identity,
                "settlement_authority_identity",
            )
            if (
                len(identity) != 64
                or identity != identity.lower()
                or any(character not in "0123456789abcdef" for character in identity)
            ):
                raise ProductCompositionError(
                    "settlement_authority_identity must be lowercase SHA-256 hex"
                )
        return raw

    def load_or_create(
        self,
        *,
        source_id: str,
        initial_bankroll: str,
        settlement_authority_identity: str | None,
    ) -> ProductCompositionManifest:
        source_id = self._text(source_id, "source_id")
        initial_bankroll = self._text(initial_bankroll, "initial_bankroll")
        if settlement_authority_identity is not None:
            settlement_authority_identity = self._text(
                settlement_authority_identity,
                "settlement_authority_identity",
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            atomic_write_json(
                self.path,
                {
                    "schema": self._SCHEMA,
                    "schema_version": self._VERSION,
                    "source_id": source_id,
                    "initial_bankroll": initial_bankroll,
                    "settlement_authority_identity": settlement_authority_identity,
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
        if raw["settlement_authority_identity"] != settlement_authority_identity:
            raise ProductCompositionError(
                "settlement authority identity conflicts with durable product composition"
            )
        return ProductCompositionManifest(
            source_id=source_id,
            initial_bankroll=initial_bankroll,
            settlement_authority_identity=settlement_authority_identity,
        )


def _settlement_authority_identity(
    *,
    source: ProductCollectorSource,
    source_id: str,
    outcome_authority: SettlementOutcomeAuthority | None,
) -> str | None:
    if outcome_authority is None:
        return None
    if outcome_authority is not source:
        raise ProductCompositionError(
            "settlement outcome authority must be owned by the configured product source"
        )
    instance_dict = getattr(source, "__dict__", None)
    if type(instance_dict) is dict and "resolve" in instance_dict:
        raise ProductCompositionError(
            "source-owned settlement authority forbids per-instance resolve shadowing"
        )
    resolver = getattr(type(source), "resolve", None)
    if type(resolver) is not FunctionType:
        raise ProductCompositionError(
            "source-owned settlement authority must use a concrete class resolve method"
        )
    if resolver.__defaults__ is not None or resolver.__kwdefaults__ not in (None, {}):
        raise ProductCompositionError(
            "source-owned settlement resolve method cannot use mutable call defaults"
        )
    if resolver.__closure__ is not None:
        raise ProductCompositionError(
            "source-owned settlement resolve method cannot close over mutable authority"
        )
    try:
        resolver_semantic_sha256 = function_semantic_sha256(
            resolver,
            runtime_owner=type(source),
        )
    except ResolverSemanticIdentityError as exc:
        raise ProductCompositionError(
            "source-owned settlement resolve semantics cannot be fingerprinted safely"
        ) from exc
    resolver_owner = _ManifestStore._text(
        f"{resolver.__module__}.{resolver.__qualname__}",
        "settlement resolver owner",
    )
    declared_implementation_id = getattr(
        type(source),
        "settlement_resolver_implementation_id",
        None,
    )
    if declared_implementation_id is None:
        raise ProductCompositionError(
            "source-owned settlement authority must declare stable settlement_resolver_implementation_id"
        )
    if (
        type(instance_dict) is dict
        and "settlement_resolver_implementation_id" in instance_dict
    ):
        raise ProductCompositionError(
            "source-owned settlement authority forbids per-instance implementation identity shadowing"
        )
    resolver_implementation_id = _ManifestStore._text(
        declared_implementation_id,
        "settlement_resolver_implementation_id",
    )
    authority_id = _ManifestStore._text(
        getattr(source, "settlement_authority_id", None),
        "settlement_authority_id",
    )
    configuration_sha256 = _ManifestStore._text(
        getattr(source, "settlement_configuration_sha256", None),
        "settlement_configuration_sha256",
    )
    if (
        len(configuration_sha256) != 64
        or configuration_sha256 != configuration_sha256.lower()
        or any(
            character not in "0123456789abcdef"
            for character in configuration_sha256
        )
    ):
        raise ProductCompositionError(
            "settlement_configuration_sha256 must be lowercase SHA-256 hex"
        )
    implementation = (
        f"{type(source).__module__}.{type(source).__qualname__}"
    )
    payload = {
        "source_id": source_id,
        "authority_id": authority_id,
        "configuration_sha256": configuration_sha256,
        "implementation": implementation,
        "resolver_owner": resolver_owner,
        "resolver_implementation_id": resolver_implementation_id,
        "resolver_semantic_sha256": resolver_semantic_sha256,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    source_id = getattr(source, "source_id", None)
    if type(source_id) is not str or not source_id or source_id.strip() != source_id:
        raise ProductCompositionError("source.source_id must be a non-empty trimmed string")
    if not callable(getattr(source, "resolve_event", None)):
        raise ProductCompositionError("source.resolve_event must be callable")

    try:
        normalized_bankroll = str(initial_bankroll)
        PaperBook(normalized_bankroll)
    except Exception as exc:
        raise ValueError("initial_bankroll must construct a valid PaperBook") from exc

    root.mkdir(parents=True, exist_ok=True)
    resolved_clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    settlement_authority_identity = _settlement_authority_identity(
        source=source,
        source_id=source_id,
        outcome_authority=outcome_authority,
    )
    manifest = _ManifestStore(root / "product_composition.json").load_or_create(
        source_id=source_id,
        initial_bankroll=normalized_bankroll,
        settlement_authority_identity=settlement_authority_identity,
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

    # Future mirror updates are downstream of the canonical market bus so they are
    # delivered only after SQLite persistence. If a subscriber fails after persistence,
    # canonical desktop application recovery can safely replay from durable truth.
    market_bus = MarketEventBus(market_store)
    market_bus.subscribe(invalidations.accept_persisted)
    source_health = SourceHealthStore(root / "source_health.json")
    canonical_application = CanonicalDesktopApplication(
        market_bus,
        source_health,
        root / "desktop_application.json",
        clock=resolved_clock,
    )

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
    desktop = DesktopDeltaConsumer(
        collector_store,
        DesktopDeltaCheckpointStore(root / "desktop_acks.json"),
        resolve_event=source.resolve_event,
        apply_event=canonical_application.apply,
        lookup_application_receipt=canonical_application.lookup_receipt,
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
