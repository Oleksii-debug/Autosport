# Read-only table-tennis observation

Autosport can acquire one bounded table-tennis odds snapshot without a replay dataset and persist it through the same canonical Market Store used by downstream research.

This path is observation-only. It has no bookmaker account, wager placement, funding, withdrawal or real-money execution capability.

## Session API

`AutosportSession.observe_provider_once(provider, max_items=...)` performs:

`MarketProvider -> IngestionEngine -> normalization/quality -> MarketEventBus -> SQLiteMarketStore -> current quote projection`

and returns:

- ingestion statistics;
- persistent source-health state;
- sorted current quotes for that provider source.

It does not invoke paper strategy agents and does not open PaperBook tickets.

## CLI

Public preview, no secret required:

```text
autosport observe-table-tennis --public-preview
```

Authenticated provider mode reads the key only from the process environment:

```text
AUTOSPORT_PARLAYAPI_KEY=<runtime secret>
autosport observe-table-tennis
```

The key is not accepted as a CLI argument so it does not need to appear in command history. It is not printed in observation output, persisted in MarketEvent metadata, stored in SourceHealthStore or written into release artifacts.

Useful bounded controls:

```text
--workspace PATH
--max-items N
--show N
```

`--max-items` is still subject to `IngestionPolicy.max_batch_size`; a caller cannot use this command to bypass core backpressure limits.

The textual output includes source id, health state, received/accepted/rejected counts, quality flags, current quote count, latest source timestamp, cursor and a bounded set of current quote lines.

## Windows GUI boundary

This change intentionally does not call live HTTP acquisition synchronously from the Tk main thread. A later GUI live-observation slice must use a worker/message boundary so a slow provider cannot freeze keyboard navigation or screen-reader interaction.
