# Endurance and stress evidence

Autosport V1 has a deterministic bounded endurance harness for correctness under materially larger synthetic market streams. It is a qualification tool, not a profitability or hardware-independent speed claim.

## What the harness exercises

The harness creates a fresh dedicated workspace and performs the following sequence:

1. generate a deterministic provider stream with explicit source time and receive/observed time;
2. ingest the stream through `MarketProvider -> IngestionEngine -> MarketEventBus -> SQLiteMarketStore` in bounded batches;
3. verify exact history count and current-quote projection count;
4. construct and run a causal `ReplayEngine` from the persisted history;
5. ingest the exact same provider stream again and require zero newly accepted MarketEvents;
6. close and reopen SQLite/SourceHealth multiple times and require the same replay hash and current projection after every restart;
7. ingest the same source stream into an independent clean workspace and require the same replay dataset hash;
8. open bounded virtual one-leg PaperBook tickets, settle them once, require the second settlement pass to be a no-op, save/reload the PaperBook and verify balance/status persistence;
9. verify deliberately corrupted SourceHealth and PaperBook files fail closed;
10. write a JSON evidence report.

No bookmaker account, real-money execution or network source is used by the harness.

## Collector durability composition gate

The same Endurance workflow also runs a deterministic, no-sleep collector composition scenario on both Ubuntu and Windows. It exercises the already-canonical collector authorities together rather than introducing a second storage model:

- a sustained SQLite-backed `CollectorDeltaStore` history with duplicate idempotence;
- an explicit detected-gap -> recovered-revision sequence;
- restart/reopen through independent canonical store handles;
- product-owned stream-epoch rollover before old-epoch retention;
- durable desktop application acknowledgements and transport-anchor preservation;
- the durable native SQLite byte ceiling inherited by later handles;
- fail-closed `RETENTION_REQUIRED` backpressure when the ceiling is reached;
- pin-aware retention planning and physical compaction of the inactive epoch;
- restart after compaction with the same durable byte ceiling;
- successful retry of the exact pressure append only after compaction has created real capacity;
- existing desktop serialization-boundary regressions that prove a peer acknowledgement is re-read rather than overwritten.

This gate is correctness evidence for the composed collector lifecycle. It is not a wall-clock soak test, a provider-availability claim, or proof that any fixed hardware can sustain an arbitrary production event rate.

## Deterministic receive-time contract

`ProviderQuote.observed_ts` is the canonical local receive/observation time. `CanonicalNormalizer` copies it into `MarketEvent.observed_ts` and `MarketEvent.ingest_ts`; provider time remains separately represented as `source_ts`.

This prevents identical provider snapshots from receiving different replay hashes merely because normalization happened at different wall-clock moments.

## Stable vs measured fields

The report separates correctness invariants from machine-dependent measurements.

Stable evidence includes:

- history/current counts;
- first-pass and duplicate-pass accepted counts;
- replay dataset hash;
- independent clean-workspace re-ingest hash;
- restart hashes and projection counts;
- repeated-settlement behavior;
- corrupted-state rejection;
- a `stable_invariant_fingerprint` derived only from stable fields;
- `real_money_execution=false`.

Measured fields include elapsed times, accepted events per second and peak Python traced memory. These are recorded for comparison but are not treated as universal pass/fail thresholds because hosted runner performance varies.

## CLI

Use a fresh dedicated path:

```text
autosport endurance --workspace .autosport-endurance --events 20000 --quote-keys 200 --batch-size 500 --restart-cycles 3 --tickets 50 --output endurance-report.json
```

The harness fails closed if `primary` or `mirror` already exists in the requested endurance workspace. It never deletes or reuses an existing product workspace.

Bounded V1 limits are enforced by `EnduranceConfig`: at most 250,000 generated events, 5,000 quotes per batch, 20 restart cycles and 1,000 paper tickets.

## CI gate

`.github/workflows/endurance.yml` runs on pull requests and manual dispatch on both Ubuntu and Windows using a 20,000-event market profile, then runs the collector durability composition gate and its desktop serialization-boundary regressions on the same exact checkout. Each job uploads the market-harness JSON report; the collector gate is preserved in the exact-head workflow/job result and test log.

A green endurance workflow means the tested invariants held for that exact source revision and runner profile. It does not establish physical NVDA acceptance, real-provider legality/coverage, predictive profitability or a universal throughput guarantee.
