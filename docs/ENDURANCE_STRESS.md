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

`.github/workflows/endurance.yml` runs on pull requests and manual dispatch on both Ubuntu and Windows using a 20,000-event profile. Each job uploads its JSON report as endurance evidence.

A green endurance workflow means the tested invariants held for that exact source revision and runner profile. It does not establish physical NVDA acceptance, real-provider legality/coverage, predictive profitability or a universal throughput guarantee.
