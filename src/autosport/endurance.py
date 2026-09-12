from __future__ import annotations

import hashlib
import json
import time
import tracemalloc
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .domain import MarketType, TicketLeg, TicketStatus
from .ingestion import IngestionEngine
from .ingestion_health import IngestionPolicy, SourceHealthStore
from .integrity import atomic_write_json
from .market_bus import MarketEventBus
from .paper import PaperBook
from .providers import InMemoryProvider, ProviderQuote
from .replay import ReplayEngine
from .settlement import SettlementEngine
from .storage import SQLiteMarketStore


@dataclass(frozen=True, slots=True)
class EnduranceConfig:
    event_count: int = 20_000
    quote_keys: int = 200
    batch_size: int = 500
    restart_cycles: int = 3
    paper_tickets: int = 50
    source_id: str = "endurance-fixture"

    def __post_init__(self) -> None:
        if self.event_count < 1 or self.event_count > 250_000:
            raise ValueError("event_count must be between 1 and 250000")
        if self.quote_keys < 1 or self.quote_keys > self.event_count:
            raise ValueError("quote_keys must be between 1 and event_count")
        if self.batch_size < 1 or self.batch_size > 5_000:
            raise ValueError("batch_size must be between 1 and 5000")
        if self.restart_cycles < 1 or self.restart_cycles > 20:
            raise ValueError("restart_cycles must be between 1 and 20")
        if self.paper_tickets < 1 or self.paper_tickets > min(1_000, self.quote_keys):
            raise ValueError("paper_tickets must be between 1 and min(1000, quote_keys)")
        if not self.source_id:
            raise ValueError("source_id must not be empty")


@dataclass(frozen=True, slots=True)
class EnduranceReport:
    status: str
    failures: tuple[str, ...]
    config: EnduranceConfig
    history_events: int
    current_quotes: int
    accepted_first_pass: int
    accepted_duplicate_pass: int
    rejected_first_pass: int
    replay_event_count: int
    replay_dataset_hash: str
    mirror_dataset_hash: str
    independent_reingest_hash_match: bool
    restart_hashes: tuple[str, ...]
    restart_projection_counts: tuple[int, ...]
    paper_tickets_opened: int
    paper_tickets_settled_first_pass: int
    paper_tickets_settled_second_pass: int
    paper_balance_after_restart: str
    corrupt_health_rejected: bool
    corrupt_paper_book_rejected: bool
    stable_invariant_fingerprint: str
    ingest_elapsed_seconds: float
    duplicate_elapsed_seconds: float
    replay_elapsed_seconds: float
    restart_elapsed_seconds: float
    mirror_ingest_elapsed_seconds: float
    accepted_events_per_second: float
    peak_traced_memory_bytes: int
    real_money_execution: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["failures"] = list(self.failures)
        payload["restart_hashes"] = list(self.restart_hashes)
        payload["restart_projection_counts"] = list(self.restart_projection_counts)
        return payload


def _fixture_quotes(config: EnduranceConfig) -> list[ProviderQuote]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    quotes: list[ProviderQuote] = []
    for index in range(config.event_count):
        key = index % config.quote_keys
        point = base + timedelta(milliseconds=index)
        timestamp = point.isoformat()
        quotes.append(
            ProviderQuote(
                provider_event_id=f"match-{key // 2}",
                provider_market_id="winner",
                provider_selection_id=f"selection-{key % 2}",
                decimal_odds=Decimal("1.50") + (Decimal(key % 50) / Decimal("100")),
                observed_ts=timestamp,
                sequence=index + 1,
                market_type=MarketType.WINNER,
                source_ts=timestamp,
                metadata={"endurance_key": key},
            )
        )
    return quotes


def _drain_provider(engine: IngestionEngine, provider: InMemoryProvider, batch_size: int) -> tuple[int, int, int]:
    received = 0
    accepted = 0
    rejected = 0
    while True:
        stats = engine.poll_once(provider, max_items=batch_size)
        received += stats.received
        accepted += stats.accepted
        rejected += stats.rejected
        if stats.received == 0:
            break
    return received, accepted, rejected


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_endurance(
    workspace: str | Path,
    config: EnduranceConfig | None = None,
    *,
    output_path: str | Path | None = None,
) -> EnduranceReport:
    """Exercise deterministic ingestion/replay/restart/settlement invariants under a bounded synthetic load."""

    cfg = config or EnduranceConfig()
    root = Path(workspace)
    primary = root / "primary"
    mirror = root / "mirror"
    if primary.exists() or mirror.exists():
        raise ValueError("endurance workspace must be fresh; primary/mirror already exist")
    primary.mkdir(parents=True, exist_ok=False)
    mirror.mkdir(parents=True, exist_ok=False)

    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()

    store: SQLiteMarketStore | None = None
    mirror_store: SQLiteMarketStore | None = None
    try:
        quotes = _fixture_quotes(cfg)
        clock_point = datetime.fromisoformat(quotes[-1].observed_ts) + timedelta(seconds=1)
        receive_clock = lambda: clock_point.isoformat()
        policy = IngestionPolicy(
            max_batch_size=cfg.batch_size,
            stale_after_seconds=3_600,
            max_future_skew_seconds=5,
        )

        store = SQLiteMarketStore(primary / "market.db")
        health = SourceHealthStore(primary / "source_health.json")
        engine = IngestionEngine(
            MarketEventBus(store),
            policy=policy,
            health_store=health,
            clock=receive_clock,
        )

        started = time.perf_counter()
        received_first, accepted_first, rejected_first = _drain_provider(
            engine,
            InMemoryProvider(cfg.source_id, quotes),
            cfg.batch_size,
        )
        ingest_elapsed = time.perf_counter() - started

        history = store.events()
        current = store.current()
        replay_started = time.perf_counter()
        replay_engine = ReplayEngine(history)
        replay_run = replay_engine.run(lambda _event: None, run_id="endurance-replay")
        replay_elapsed = time.perf_counter() - replay_started
        replay_hash = replay_engine.dataset_hash

        duplicate_started = time.perf_counter()
        received_duplicate, accepted_duplicate, rejected_duplicate = _drain_provider(
            engine,
            InMemoryProvider(cfg.source_id, quotes),
            cfg.batch_size,
        )
        duplicate_elapsed = time.perf_counter() - duplicate_started

        store.close()
        store = None

        restart_hashes: list[str] = []
        restart_projection_counts: list[int] = []
        restart_started = time.perf_counter()
        for _cycle in range(cfg.restart_cycles):
            restarted = SQLiteMarketStore(primary / "market.db")
            restarted_history = restarted.events()
            restart_hashes.append(ReplayEngine(restarted_history).dataset_hash)
            restart_projection_counts.append(len(restarted.current()))
            state = SourceHealthStore(primary / "source_health.json").get(cfg.source_id)
            if state.latest_source_ts != quotes[-1].source_ts:
                restart_hashes.append("SOURCE_HEALTH_HIGH_WATER_MISMATCH")
            restarted.close()
        restart_elapsed = time.perf_counter() - restart_started

        mirror_started = time.perf_counter()
        mirror_store = SQLiteMarketStore(mirror / "market.db")
        mirror_health = SourceHealthStore(mirror / "source_health.json")
        mirror_engine = IngestionEngine(
            MarketEventBus(mirror_store),
            policy=policy,
            health_store=mirror_health,
            clock=receive_clock,
        )
        mirror_received, mirror_accepted, mirror_rejected = _drain_provider(
            mirror_engine,
            InMemoryProvider(cfg.source_id, quotes),
            cfg.batch_size,
        )
        mirror_history = mirror_store.events()
        mirror_hash = ReplayEngine(mirror_history).dataset_hash
        mirror_elapsed = time.perf_counter() - mirror_started
        mirror_store.close()
        mirror_store = None

        store = SQLiteMarketStore(primary / "market.db")
        current_events = sorted(
            store.current().values(),
            key=lambda event: (event.event_id, event.market_id, event.selection_id),
        )
        ticket_events = current_events[: cfg.paper_tickets]
        book = PaperBook("100000")
        for event in ticket_events:
            book.open_ticket(
                [TicketLeg(event.event_id, event.market_id, event.selection_id, event.decimal_odds)],
                "1",
                reason="endurance-settlement",
                placed_at=event.observed_ts,
            )
        settlement = SettlementEngine()
        settlement.record({event.quote_key: "win" for event in ticket_events})
        settled_first = settlement.settle_ready(book)
        settled_second = settlement.settle_ready(book)
        paper_path = primary / "paper_book.json"
        book.save(paper_path)
        restored_book = PaperBook.load(paper_path)
        paper_balance = str(restored_book.balance)

        corrupt_health = root / "corrupt_source_health.json"
        corrupt_health.write_text('{"schema_version":999,"sources":{}}\n', encoding="utf-8")
        corrupt_health_rejected = False
        try:
            SourceHealthStore(corrupt_health).get("broken")
        except ValueError:
            corrupt_health_rejected = True

        corrupt_book = root / "corrupt_paper_book.json"
        corrupt_book.write_text('{"balance":"not-enough"}\n', encoding="utf-8")
        corrupt_paper_rejected = False
        try:
            PaperBook.load(corrupt_book)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            corrupt_paper_rejected = True

        failures: list[str] = []
        expected_current = cfg.quote_keys
        checks = (
            (received_first == cfg.event_count, f"first received={received_first} expected={cfg.event_count}"),
            (accepted_first == cfg.event_count, f"first accepted={accepted_first} expected={cfg.event_count}"),
            (rejected_first == 0, f"first rejected={rejected_first}"),
            (len(history) == cfg.event_count, f"history={len(history)} expected={cfg.event_count}"),
            (len(current) == expected_current, f"current={len(current)} expected={expected_current}"),
            (replay_run.event_count == cfg.event_count, f"replay count={replay_run.event_count}"),
            (received_duplicate == cfg.event_count, f"duplicate received={received_duplicate}"),
            (accepted_duplicate == 0, f"duplicate accepted={accepted_duplicate}"),
            (rejected_duplicate == 0, f"duplicate rejected={rejected_duplicate}"),
            (all(item == replay_hash for item in restart_hashes), "restart replay hash changed"),
            (all(item == expected_current for item in restart_projection_counts), "restart current projection changed"),
            (mirror_received == cfg.event_count, f"mirror received={mirror_received}"),
            (mirror_accepted == cfg.event_count, f"mirror accepted={mirror_accepted}"),
            (mirror_rejected == 0, f"mirror rejected={mirror_rejected}"),
            (mirror_hash == replay_hash, "independent re-ingest replay hash changed"),
            (len(settled_first) == cfg.paper_tickets, f"first settlement count={len(settled_first)}"),
            (len(settled_second) == 0, f"second settlement count={len(settled_second)}"),
            (
                all(ticket.status is not TicketStatus.OPEN for ticket in restored_book.tickets.values()),
                "restored PaperBook contains open endurance tickets",
            ),
            (restored_book.balance == book.balance, "PaperBook balance changed after restart"),
            (corrupt_health_rejected, "corrupt SourceHealthStore was accepted"),
            (corrupt_paper_rejected, "corrupt PaperBook was accepted"),
        )
        for passed, message in checks:
            if not passed:
                failures.append(message)

        stable_payload = {
            "config": asdict(cfg),
            "history_events": len(history),
            "current_quotes": len(current),
            "accepted_first_pass": accepted_first,
            "accepted_duplicate_pass": accepted_duplicate,
            "replay_dataset_hash": replay_hash,
            "mirror_dataset_hash": mirror_hash,
            "restart_hashes": restart_hashes,
            "restart_projection_counts": restart_projection_counts,
            "paper_tickets_opened": len(ticket_events),
            "paper_tickets_settled_first_pass": len(settled_first),
            "paper_tickets_settled_second_pass": len(settled_second),
            "paper_balance_after_restart": paper_balance,
            "corrupt_health_rejected": corrupt_health_rejected,
            "corrupt_paper_book_rejected": corrupt_paper_rejected,
            "real_money_execution": False,
        }
        fingerprint = _fingerprint(stable_payload)
        _current_memory, peak_memory = tracemalloc.get_traced_memory()
        report = EnduranceReport(
            status="PASS" if not failures else "FAIL",
            failures=tuple(failures),
            config=cfg,
            history_events=len(history),
            current_quotes=len(current),
            accepted_first_pass=accepted_first,
            accepted_duplicate_pass=accepted_duplicate,
            rejected_first_pass=rejected_first,
            replay_event_count=replay_run.event_count,
            replay_dataset_hash=replay_hash,
            mirror_dataset_hash=mirror_hash,
            independent_reingest_hash_match=mirror_hash == replay_hash,
            restart_hashes=tuple(restart_hashes),
            restart_projection_counts=tuple(restart_projection_counts),
            paper_tickets_opened=len(ticket_events),
            paper_tickets_settled_first_pass=len(settled_first),
            paper_tickets_settled_second_pass=len(settled_second),
            paper_balance_after_restart=paper_balance,
            corrupt_health_rejected=corrupt_health_rejected,
            corrupt_paper_book_rejected=corrupt_paper_rejected,
            stable_invariant_fingerprint=fingerprint,
            ingest_elapsed_seconds=ingest_elapsed,
            duplicate_elapsed_seconds=duplicate_elapsed,
            replay_elapsed_seconds=replay_elapsed,
            restart_elapsed_seconds=restart_elapsed,
            mirror_ingest_elapsed_seconds=mirror_elapsed,
            accepted_events_per_second=(accepted_first / ingest_elapsed) if ingest_elapsed > 0 else float("inf"),
            peak_traced_memory_bytes=peak_memory,
        )
        if output_path is not None:
            atomic_write_json(output_path, report.to_dict())
        return report
    finally:
        if store is not None:
            store.close()
        if mirror_store is not None:
            mirror_store.close()
        if not was_tracing and tracemalloc.is_tracing():
            tracemalloc.stop()
