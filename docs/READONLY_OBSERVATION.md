# Read-only table-tennis observation

Autosport can acquire a bounded table-tennis odds snapshot without a replay dataset and persist it through the same canonical Market Store used by downstream research.

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

## Windows GUI live snapshot

The Windows GUI exposes one manual read-only refresh at a time. The mode is explicitly selected as either:

- `Public preview — без ключа`;
- `API key з environment`.

Authenticated mode reads only `AUTOSPORT_PARLAYAPI_KEY` from the process environment. No secret entry field exists in the GUI.

`OneShotObservationWorker` performs acquisition on a daemon worker thread. That worker never calls Tk. `observe_workspace_once()` opens its own short-lived SQLite WAL connection and SourceHealthStore, performs the bounded snapshot, closes the market connection and returns an immutable result through a thread-safe queue.

The Tk main thread polls the queue with `after()`, then updates accessible status text and the live quote Listbox. The refresh control stays disabled until the terminal worker message is consumed, preventing overlapping manual snapshots from one GUI instance.

Keyboard navigation:

- `Ctrl+L` — start one live refresh;
- `F7` — focus the live quote list;
- existing replay shortcuts remain unchanged.

The packaged accessibility audit treats live mode, live refresh and live quote list as critical controls. It requires the mode Value pattern, refresh Invoke pattern and exposed UIA rows for the live Listbox. This remains an in-process machine prerequisite only; it does not change `HUMAN_TESTED=false` or `NVDA_VERIFIED=false`.

## Thread and persistence boundary

The GUI's long-lived `AutosportSession` owns a SQLite connection created on the Tk thread. That connection is never passed to the worker. The worker uses separate short-lived store connections to the same WAL-backed workspace, avoiding cross-thread sqlite3 use.

The worker path does not instantiate or save PaperBook. A live snapshot cannot create tickets or modify virtual bankroll.
