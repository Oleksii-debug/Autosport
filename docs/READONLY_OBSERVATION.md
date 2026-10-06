# Read-only table-tennis observation

Autosport can acquire a bounded table-tennis odds snapshot without a replay dataset and persist it through the same canonical Market Store used by downstream research.

This path is observation-only. It has no bookmaker account, wager placement, funding, withdrawal or real-money execution capability.

## Session API

`AutosportSession.observe_provider_once(provider, max_items=...)` performs:

`MarketProvider -> IngestionEngine -> normalization/quality -> MarketEventBus -> SQLiteMarketStore -> current quote projection`

and returns:

- validated ingestion statistics;
- persistent source-health state bound to the same source/cursor/quality evidence;
- decision-causal current quotes for that provider source, ordered by the full canonical `quote_key`.

Sealed generation-zero migration rows remain audit/sequence evidence in durable history but are excluded from the returned live result until positive product-issued append provenance exists.

Forward-observed replay cutoffs form a monotonic decision-time frontier. Once product issuance has advanced to a later normalized `as_of`, a previously unseen earlier `as_of` is rejected rather than being bound to a newer append generation. Already-issued earlier cutoffs remain readable and immutable. This prevents a late backdated append from being retroactively admitted into a newly minted older decision slice. It does not claim that the first post-facto issuance of an arbitrary historical instant proves what the product knew at that old wall-clock time.

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

Accessible live quote rows expose sport and exchange side in addition to event/market/selection identity, odds and source time. Missing optional identity dimensions are announced explicitly rather than silently omitted.

## Continuous observation safety boundary

The bounded continuous collector preserves the same persist-first market authority as one-shot observation. Its retry and polling waits are causal timing boundaries: a non-stopping waiter must consume the requested monotonic delay before another provider acquisition is allowed. This applies to ordinary polling intervals, same-process provider-unavailable backoff and restart-earned provider backoff. A stop signal may terminate a wait immediately; it never authorizes another provider call.

The operator status JSON is non-authoritative projection only. Its path must be disjoint from the workspace root, Market Store files and SQLite sidecars, SourceHealthStore files/locks/temporary paths, and the independent monotonic-authority tree. Ancestor, descendant and symlink aliases are treated as collisions so status publication cannot create a directory or file that poisons a durable authority path.

Provider/source/run identifiers, operational cursors and quality flags used by this path must remain canonical text without control characters. Malformed operational metadata fails closed before it can become durable health evidence or forge additional lines in operator-facing status output.

A provider-unavailable retry is permitted only when durable SourceHealthStore evidence confirms the typed failure streak. If that confirmation read fails, collection terminates as a local health-read failure, preserves the provider outage as the primary diagnostic, and performs no retry.

## Authenticated Betfair Stream durable bridge

The reserved source ID `betfair_exchange_stream` is accepted by generic observation only through `BetfairAuthenticatedMarketProvider`. A provider that merely exposes that source ID is rejected before acquisition or persistence.

The bridge must bind to the target Market Store's exact current projection before its first read. That binding restores the durable sequence floor and any previously persisted open Betfair identities. On subsequent polls the in-memory consumed-stream state is re-proved against SQLite before another frame is read; divergence fails closed and requires a fresh authenticated runtime rather than guessing across an uncertain persistence boundary.

Only publications that remain decision-eligible under the canonical authenticated subscription/freshness runtime are emitted as `status="open"` provider quotes. Loss of authority after provider 503, market suspension/closure, runner deactivation/removal, image replacement, timing/ladder/semantic epoch change, or quote removal is materialized as a strictly higher-sequence `status="closed"` tombstone using the prior durable quote identity. The same transition carries `BETFAIR_AUTHORITY_REVOKED`, so SourceHealth is degraded instead of falsely reporting a healthy successful poll while authenticated actionability has been revoked. This lets the append-only Market Store and MarketMirror revoke stale actionability without deleting audit history.

Bounded transitions use `TRUNCATED_BATCH` pages when needed. Each page cursor names only the highest sequence actually returned in that page; it never advertises later transition entries that have not crossed the persistence boundary yet. Bridge sequence/open-state advancement is page-causal as well: planning a multi-page authenticated-frame transition does not install later pages into in-memory authority before those pages are exposed to the persistence seam.

If a page exhausts the bounded SQLite retry budget without committing, the live replay wrapper invokes the bridge reset hook. The bridge rolls back only that last exposed page and restores the already-consumed authenticated-frame transition as pending work, so a retry does not reread the socket, skip an uncommitted prefix, or pretend later pages were durable. Post-commit health/subscriber failures are not rolled back; the durable page remains the exact continuation point.

The bridge serializes durable binding, durable re-proof, paging, rollback and sequence/open-state mutation under one bridge-local re-entrant lock. The authenticated stream runtime retains its own transport/state locks; these layers protect different authorities.

This bridge remains observation-only. It grants no order placement, account, settlement, funding, withdrawal, or real-money execution authority.

## Windows GUI live snapshot

The Windows GUI exposes one manual read-only refresh at a time. The mode is explicitly selected as either:

- `Public preview — без ключа`;
- `API key з environment`.

Authenticated mode reads only `AUTOSPORT_PARLAYAPI_KEY` from the process environment. No secret entry field exists in the GUI.

`OneShotObservationWorker` performs acquisition on a non-daemon worker thread. That worker never calls Tk. Non-daemon ownership is intentional: interpreter shutdown must not kill a live observation while it may be crossing a durable persistence boundary. `observe_workspace_once()` opens its own short-lived SQLite WAL connection and SourceHealthStore, performs the bounded snapshot, closes the market connection and returns an ownership-isolated `ObservationResult` through a thread-safe queue.

The Tk main thread polls the queue with `after()`, then updates accessible status text and the live quote Listbox. The refresh control stays disabled until the terminal worker message is consumed, preventing overlapping manual snapshots from one GUI instance.

Before publication, the worker revalidates the exact `ObservationResult` and snapshots ingestion statistics, source-health state and current quotes so caller-owned mutable references cannot change already-published live truth. Current quote ordering uses the full canonical `quote_key`, including sport/exchange-side identity where present.

Keyboard navigation:

- `Ctrl+L` — start one live refresh;
- `F7` — focus the live quote list;
- existing replay shortcuts remain unchanged.

The packaged accessibility audit treats live mode, live refresh and live quote list as critical controls. It requires the mode Value pattern, refresh Invoke pattern and exposed UIA rows for the live Listbox. This remains an in-process machine prerequisite only; it does not change `HUMAN_TESTED=false` or `NVDA_VERIFIED=false`.

## Thread and persistence boundary

The GUI's long-lived `AutosportSession` owns a SQLite connection created on the Tk thread. That connection is never passed to the worker. The worker uses separate short-lived store connections to the same WAL-backed workspace, avoiding cross-thread sqlite3 use.

The worker path does not instantiate or save PaperBook. A live snapshot cannot create tickets or modify virtual bankroll.
