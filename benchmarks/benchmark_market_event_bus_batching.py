from __future__ import annotations

import argparse
import json
import math
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter_ns
from typing import Sequence

from autosport.domain import MarketEvent
from autosport.market_bus import MarketEventBus
from autosport.providers import CanonicalNormalizer, ProviderQuote
from autosport.storage import SQLiteMarketStore


@dataclass(frozen=True, slots=True)
class MarketEventBusBatchingProfile:
    sample_count: int
    batch_size: int
    warmup_batches: int
    quote_keys: int
    accepted_single: int
    accepted_batch: int
    single_samples_ns: tuple[int, ...]
    batch_samples_ns: tuple[int, ...]
    single_p50_ms_per_event: float
    single_p95_ms_per_event: float
    single_p99_ms_per_event: float
    batch_p50_ms_per_event: float
    batch_p95_ms_per_event: float
    batch_p99_ms_per_event: float
    p50_single_over_batch_ratio: float


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _nearest_rank_ns(samples_ns: Sequence[int], percentile: float) -> int:
    if not samples_ns:
        raise ValueError("latency samples must not be empty")
    if isinstance(percentile, bool) or not isinstance(percentile, (int, float)):
        raise ValueError("percentile must be a finite number in (0, 100]")
    value = float(percentile)
    if not math.isfinite(value) or value <= 0 or value > 100:
        raise ValueError("percentile must be a finite number in (0, 100]")

    normalized: list[int] = []
    for sample in samples_ns:
        if isinstance(sample, bool) or not isinstance(sample, int) or sample <= 0:
            raise ValueError("latency samples must be positive integer nanoseconds")
        normalized.append(sample)

    ordered = sorted(normalized)
    rank = math.ceil((value / 100.0) * len(ordered))
    return ordered[rank - 1]


def _build_quote(index: int, quote_keys: int) -> ProviderQuote:
    quote_keys = _positive_int("quote_keys", quote_keys)
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("index must be a non-negative integer")
    key_index = index % quote_keys
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ProviderQuote(
        provider_event_id=f"event-{key_index}",
        provider_market_id="winner",
        provider_selection_id="home",
        decimal_odds=Decimal("1.80"),
        observed_ts=(origin + timedelta(milliseconds=index)).isoformat(),
        sequence=index,
    )


def _publish_batch(
    bus: MarketEventBus,
    events: tuple[MarketEvent, ...],
    *,
    mode: str,
    measure: bool,
) -> tuple[int, int | None]:
    if mode not in {"single", "batch"}:
        raise ValueError("mode must be 'single' or 'batch'")

    started_ns = perf_counter_ns() if measure else 0
    if mode == "single":
        accepted = sum(1 for event in events if bus.publish(event))
    else:
        accepted = bus.publish_many(events)
    elapsed_ns = perf_counter_ns() - started_ns if measure else None

    if accepted != len(events):
        raise RuntimeError(
            f"{mode} path accepted wrong batch cardinality: "
            f"expected={len(events)} accepted={accepted}"
        )
    if measure and (elapsed_ns is None or elapsed_ns <= 0):
        raise RuntimeError(f"{mode} latency clock did not advance")
    return accepted, elapsed_ns


def _verify_store_truth(
    store: SQLiteMarketStore,
    *,
    mode: str,
    expected_history: int,
    expected_current: int,
) -> None:
    history = store.events()
    current = store.current_by_source()
    if len(history) != expected_history:
        raise RuntimeError(
            f"{mode} durable history cardinality mismatch: "
            f"expected={expected_history} actual={len(history)}"
        )
    if len(current) != expected_current:
        raise RuntimeError(
            f"{mode} current projection cardinality mismatch: "
            f"expected={expected_current} actual={len(current)}"
        )

    latest_by_projection: dict[tuple[str, str], int] = {}
    for event in history:
        key = (event.source_id, event.quote_key)
        previous = latest_by_projection.get(key)
        if previous is None or event.sequence > previous:
            latest_by_projection[key] = event.sequence
    if set(current) != set(latest_by_projection):
        raise RuntimeError(f"{mode} current projection key set mismatch")
    for key, latest_sequence in latest_by_projection.items():
        if current[key].sequence != latest_sequence:
            raise RuntimeError(f"{mode} current projection latest sequence mismatch")


def run_batching_profile(
    *,
    sample_count: int = 40,
    batch_size: int = 25,
    warmup_batches: int = 4,
    quote_keys: int = 100,
) -> MarketEventBusBatchingProfile:
    """Compare per-event publish versus publish_many durable transaction cost.

    The measured boundary starts immediately before MarketEventBus publication and ends
    immediately after persistence/delivery returns. Provider/network time, normalization,
    fixture construction, and post-run truth verification are excluded. Each mode writes
    the exact same pre-built workload to an independent SQLite store, and measured pair
    order alternates to reduce systematic first/second-call bias.

    This is descriptive machine-local evidence only. It defines no performance acceptance
    threshold and authorizes no change to live ingestion semantics.
    """

    sample_count = _positive_int("sample_count", sample_count)
    batch_size = _positive_int("batch_size", batch_size)
    warmup_batches = _nonnegative_int("warmup_batches", warmup_batches)
    quote_keys = _positive_int("quote_keys", quote_keys)

    total_batches = warmup_batches + sample_count
    normalizer = CanonicalNormalizer()
    events_by_batch: list[tuple[MarketEvent, ...]] = []
    event_index = 0
    for _batch_index in range(total_batches):
        events: list[MarketEvent] = []
        for _ in range(batch_size):
            events.append(normalizer.normalize("benchmark", _build_quote(event_index, quote_keys)))
            event_index += 1
        events_by_batch.append(tuple(events))

    single_samples: list[int] = []
    batch_samples: list[int] = []
    accepted_single = 0
    accepted_batch = 0

    with tempfile.TemporaryDirectory() as tmp:
        single_store = SQLiteMarketStore(Path(tmp) / "single.db")
        batch_store = SQLiteMarketStore(Path(tmp) / "batch.db")
        single_bus = MarketEventBus(single_store)
        batch_bus = MarketEventBus(batch_store)
        try:
            for batch_index, events in enumerate(events_by_batch):
                is_warmup = batch_index < warmup_batches
                measured_index = batch_index - warmup_batches

                modes = ("single", "batch")
                if not is_warmup and measured_index % 2:
                    modes = ("batch", "single")

                for mode in modes:
                    if mode == "single":
                        accepted, elapsed_ns = _publish_batch(
                            single_bus,
                            events,
                            mode=mode,
                            measure=not is_warmup,
                        )
                        if not is_warmup:
                            accepted_single += accepted
                            if elapsed_ns is None:
                                raise RuntimeError("single measured latency missing")
                            single_samples.append(elapsed_ns)
                    else:
                        accepted, elapsed_ns = _publish_batch(
                            batch_bus,
                            events,
                            mode=mode,
                            measure=not is_warmup,
                        )
                        if not is_warmup:
                            accepted_batch += accepted
                            if elapsed_ns is None:
                                raise RuntimeError("batch measured latency missing")
                            batch_samples.append(elapsed_ns)

            expected_total = total_batches * batch_size
            expected_current = min(quote_keys, expected_total)
            _verify_store_truth(
                single_store,
                mode="single",
                expected_history=expected_total,
                expected_current=expected_current,
            )
            _verify_store_truth(
                batch_store,
                mode="batch",
                expected_history=expected_total,
                expected_current=expected_current,
            )
        finally:
            single_store.close()
            batch_store.close()

    expected_measured = sample_count * batch_size
    if accepted_single != expected_measured or accepted_batch != expected_measured:
        raise RuntimeError("measured accepted cardinality does not match requested workload")
    if len(single_samples) != sample_count or len(batch_samples) != sample_count:
        raise RuntimeError("timing sample cardinality does not match requested workload")

    single_samples_tuple = tuple(single_samples)
    batch_samples_tuple = tuple(batch_samples)

    def per_event_ms(samples: tuple[int, ...], percentile: float) -> float:
        return (_nearest_rank_ns(samples, percentile) / batch_size) / 1_000_000.0

    single_p50 = per_event_ms(single_samples_tuple, 50)
    batch_p50 = per_event_ms(batch_samples_tuple, 50)
    if single_p50 <= 0 or batch_p50 <= 0:
        raise RuntimeError("per-event p50 latency must be positive")

    ratio = single_p50 / batch_p50
    if not math.isfinite(ratio) or ratio <= 0:
        raise RuntimeError("descriptive p50 ratio must be finite and positive")

    return MarketEventBusBatchingProfile(
        sample_count=sample_count,
        batch_size=batch_size,
        warmup_batches=warmup_batches,
        quote_keys=quote_keys,
        accepted_single=accepted_single,
        accepted_batch=accepted_batch,
        single_samples_ns=single_samples_tuple,
        batch_samples_ns=batch_samples_tuple,
        single_p50_ms_per_event=single_p50,
        single_p95_ms_per_event=per_event_ms(single_samples_tuple, 95),
        single_p99_ms_per_event=per_event_ms(single_samples_tuple, 99),
        batch_p50_ms_per_event=batch_p50,
        batch_p95_ms_per_event=per_event_ms(batch_samples_tuple, 95),
        batch_p99_ms_per_event=per_event_ms(batch_samples_tuple, 99),
        p50_single_over_batch_ratio=ratio,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure canonical MarketEventBus per-event publication versus publish_many "
            "transaction amortization. Output is descriptive machine-local evidence only."
        )
    )
    parser.add_argument("--samples", type=int, default=40, help="measured batch samples per mode")
    parser.add_argument("--batch-size", type=int, default=25, help="events in each measured batch")
    parser.add_argument("--warmup-batches", type=int, default=4, help="unmeasured batches per mode")
    parser.add_argument("--quote-keys", type=int, default=100, help="rotating canonical quote keys")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = run_batching_profile(
        sample_count=args.samples,
        batch_size=args.batch_size,
        warmup_batches=args.warmup_batches,
        quote_keys=args.quote_keys,
    )
    payload = asdict(result)
    payload.update(
        {
            "scope": "market_event_bus_durable_publication_only",
            "provider_network_included": False,
            "normalization_included": False,
            "post_run_truth_verification_included": False,
            "pair_order_alternates": True,
            "performance_threshold_defined": False,
            "target_claim": False,
        }
    )
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
