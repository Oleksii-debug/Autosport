# Ingestion health and backpressure contract

Autosport keeps market history and provider operational health as separate truths.

`SQLiteMarketStore` remains append-only normalized market history/current projection. `SourceHealthStore` is a separate durable operational projection and must never be treated as market evidence.

## Bounded ingestion

`IngestionPolicy.max_batch_size` is the hard caller-side batch cap. `IngestionEngine.poll_once()` rejects a request above that cap before calling a provider.

A provider must also respect the requested `max_items`. Returning more quotes than requested is a provider-contract failure; the oversized batch is not persisted and source health becomes failed.

This is the current V1 backpressure boundary. It is synchronous and bounded; it does not claim an unbounded queue can safely absorb arbitrary producer throughput.

## Causal clock and source-time quality

The post-acquisition local poll clock is the causal ceiling for the batch. A quote's
`observed_ts` is local receipt evidence: it must be an exactly representable,
timezone-aware instant no later than that poll clock. A future local observation is
rejected with `FUTURE_OBSERVATION_TIMESTAMP`; malformed or unsupported-precision
local observation evidence is rejected as `INVALID_QUOTE`.

Freshness uses the same clock hierarchy as the decision mirror: `source_ts` when the
provider supplies it, otherwise `observed_ts`. Ingestion can emit:

- `STALE_SOURCE` when that freshness clock is older than the configured stale threshold;
- `FUTURE_CLOCK_SKEW` when a provider `source_ts` is too far ahead of local receive time;
- `INVALID_SOURCE_TIMESTAMP` when a supplied provider timestamp is malformed or cannot be represented without precision loss; that quote is rejected;
- `SOURCE_TIME_REGRESSION` when the newest accepted provider source timestamp moves behind the durable high-water mark.

Non-zero timestamp precision finer than microseconds is never silently rounded into
causal authority. Zero-only excess fractional digits remain admissible because they do
not change the represented instant.

The durable `latest_source_ts` is monotonic and is not moved backward by a regressed batch.

## Gap flags

A provider may attach explicit `ProviderBatch.quality_flags`, for example a gap detected from a vendor cursor/sequence contract. Core intentionally does not invent a universal `sequence + 1` rule because different providers can sequence snapshots, books, markets or connections differently.

Provider flags are carried into `IngestionStats` and the durable source-health projection.

## Source health

For each `source_id`, `SourceHealthStore` persists:

- status (`unknown`, `healthy`, `degraded`, `failed`);
- poll/received/accepted/rejected counters;
- total and consecutive failures;
- last success/error timestamps and last error;
- last cursor;
- monotonic latest source timestamp;
- current quality flags.

A provider exception is recorded as a failure and then re-raised. Health tracking does not hide acquisition or persistence errors.

This layer is deterministic and contains no LLM. It creates no real-money execution capability.
