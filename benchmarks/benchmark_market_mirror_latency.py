from __future__ import annotations

import argparse
import math
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter_ns
from typing import Sequence

from autosport.market_bus import MarketEventBus
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_runtime import BoundedMirrorInvalidationBuffer
from autosport.providers import CanonicalNormalizer, ProviderQuote
from autosport.storage import SQLiteMarketStore


@dataclass(frozen=True, slots=True)
class MarketMirrorLatencyResult:
    measured_count: int
    warmup_count: int
    quote_keys: int
    accepted: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _nearest_rank_percentile_ms(samples_ns: Sequence[int], percentile: float) -> float:
    if not samples_ns:
        raise ValueError("latency samples must not be empty")
    if isinstance(percentile, bool) or not isinstance(percentile, (int, float)):
        raise ValueError("percentile must be a finite number in (0, 100]")
    percentile_value = float(percentile)
    if not math.isfinite(percentile_value) or percentile_value <= 0 or percentile_value > 100:
        raise ValueError("percentile must be a finite number in (0, 100]")

    normalized: list[int] = []
    for sample in samples_ns:
        if isinstance(sample, bool) or not isinstance(sample, int) or sample <= 0:
            raise ValueError("latency samples must be positive integer nanoseconds")
        normalized.append(sample)

    ordered = sorted(normalized)
    rank = math.ceil((percentile_value / 100.0) * len(ordered))
    return ordered[rank - 1] / 1_000_000.0


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


def run_latency_benchmark(
    *,
    count: int = 5_000,
    quote_keys: int = 100,
    warmup: int = 200,
) -> MarketMirrorLatencyResult:
    """Measure normalize + durable Market Mirror update latency without provider/network time.

    This is a measurement harness, not a release claim. Target-laptop acceptance must run
    the harness on the actual target machine and bind the observed result to that build.
    """

    count = _positive_int("count", count)
    quote_keys = _positive_int("quote_keys", quote_keys)
    warmup = _nonnegative_int("warmup", warmup)

    samples_ns: list[int] = []
    accepted = 0
    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteMarketStore(Path(tmp) / "market-mirror-latency.db")
        try:
            normalizer = CanonicalNormalizer()
            mirror = MarketMirror()
            invalidations = BoundedMirrorInvalidationBuffer(mirror)
            bus = MarketEventBus(store)
            bus.subscribe(invalidations.accept_persisted)
            total = warmup + count
            for index in range(total):
                quote = _build_quote(index, quote_keys)
                if index < warmup:
                    event = normalizer.normalize("benchmark", quote)
                    if not bus.publish(event):
                        raise RuntimeError("warmup event was not accepted by the local Market Mirror")
                    continue

                started_ns = perf_counter_ns()
                event = normalizer.normalize("benchmark", quote)
                published = bus.publish(event)
                elapsed_ns = perf_counter_ns() - started_ns
                if not published:
                    raise RuntimeError("measured event was not accepted by the local Market Mirror")
                if elapsed_ns <= 0:
                    raise RuntimeError("latency clock did not advance for a measured event")
                samples_ns.append(elapsed_ns)
                accepted += 1

            mirror_view = mirror.view()
            expected_quote_keys = min(total, quote_keys)
            if mirror_view.revision != total or len(mirror_view.events) != expected_quote_keys:
                raise RuntimeError(
                    "market mirror benchmark workload did not fully apply: "
                    f"expected_revision={total} actual_revision={mirror_view.revision} "
                    f"expected_quote_keys={expected_quote_keys} "
                    f"actual_quote_keys={len(mirror_view.events)}"
                )
        finally:
            store.close()

    if accepted != count or len(samples_ns) != count:
        raise RuntimeError(
            "latency workload did not complete exactly: "
            f"requested={count} accepted={accepted} samples={len(samples_ns)}"
        )

    p50_ms = _nearest_rank_percentile_ms(samples_ns, 50)
    p95_ms = _nearest_rank_percentile_ms(samples_ns, 95)
    p99_ms = _nearest_rank_percentile_ms(samples_ns, 99)
    max_ms = max(samples_ns) / 1_000_000.0
    if not (0 < p50_ms <= p95_ms <= p99_ms <= max_ms):
        raise RuntimeError("latency summary ordering is invalid")

    return MarketMirrorLatencyResult(
        measured_count=count,
        warmup_count=warmup,
        quote_keys=quote_keys,
        accepted=accepted,
        p50_ms=p50_ms,
        p95_ms=p95_ms,
        p99_ms=p99_ms,
        max_ms=max_ms,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Autosport CanonicalNormalizer + durable local Market Mirror update latency. "
            "The output is machine-specific evidence, not a release claim by itself."
        )
    )
    parser.add_argument("--count", type=int, default=5_000, help="measured event count")
    parser.add_argument("--quote-keys", type=int, default=100, help="rotating canonical quote keys")
    parser.add_argument("--warmup", type=int, default=200, help="unmeasured warmup event count")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = run_latency_benchmark(
        count=args.count,
        quote_keys=args.quote_keys,
        warmup=args.warmup,
    )
    print(
        "scope=normalizer_plus_durable_local_market_mirror "
        "provider_network_included=false target_claim=false "
        f"measured={result.measured_count} warmup={result.warmup_count} "
        f"quote_keys={result.quote_keys} accepted={result.accepted} "
        f"p50_ms={result.p50_ms:.3f} p95_ms={result.p95_ms:.3f} "
        f"p99_ms={result.p99_ms:.3f} max_ms={result.max_ms:.3f}"
    )


if __name__ == "__main__":
    main()
