# ParlayAPI table-tennis provider boundary

Status: V1 candidate read-only data source. This document does not assert redistribution rights or production coverage beyond what is verified at runtime.

## Purpose

Autosport integrates ParlayAPI only through `ParlayApiTableTennisProvider`. The adapter observes market data and emits typed `ProviderQuote` batches for the existing deterministic ingestion path. It has no bookmaker-account, wager-placement, funding, withdrawal, or real-money execution capability.

## Public provider documentation used for the adapter contract

- Table-tennis coverage page: https://parlay-api.com/sports/table_tennis
- Response shapes: https://parlay-api.com/docs/response-shapes
- Main API documentation: https://parlay-api.com/docs
- OpenAPI contract: https://parlay-api.com/openapi.json
- Error reference: https://parlay-api.com/errors

The documented table-tennis sport key is `table_tennis`. The game-line endpoint is documented as TOA-compatible and carries bookmaker -> market -> outcome trees. The adapter requests decimal odds and maps `h2h`, `spreads`, and `totals` into Autosport WINNER, HANDICAP, and TOTAL market types.

## Security and secrets

Authenticated calls pass the API key in the `X-API-Key` request header. Keys must come from runtime configuration/environment and must never be committed, logged, written into replay datasets, or included in release artifacts. Public preview mode requires no key and is suitable only for limited manual connectivity checks.

## Causal/time semantics

`markets[].last_update` is preferred as source time; `bookmakers[].last_update` is the fallback. Autosport records local observation time separately. The source timestamp is converted into a stable integer sequence so repeated delivery of an unchanged provider snapshot remains idempotent in the append-only Market Store.

If the provider omits or supplies an invalid source timestamp, the adapter fails closed rather than inventing causal precision for that quote. Provider availability, book coverage, market coverage and freshness must be treated as runtime facts, not static assumptions.

Historical schema-v2 imports are stricter: `source_ts`, `observed_ts`, and `ingest_ts` must all be explicitly present in the imported market row. A parser default or current wall-clock time is not acceptable historical evidence.

## Historical coverage preflight

The provider's current OpenAPI contract documents:

`GET /v1/historical/sports/{sport_key}/coverage?dateFrom=YYYY-MM-DD&dateTo=YYYY-MM-DD`

as a one-credit query that returns per-source row counts for the requested historical-match window before higher-cost historical filtering. The documented response is:

```json
{
  "sport_key": "table_tennis",
  "window": {"date_from": "2026-09-01", "date_to": "2026-09-12"},
  "by_source": {
    "example-source": {
      "rows": 100,
      "first_date": "2026-09-01",
      "last_date": "2026-09-12",
      "priced_rows": 94
    }
  }
}
```

The provider documentation also states that historical responses carry `x-historical-window-hours` and `x-historical-window-from`; requests older than the plan window fail with HTTP 403 / `HISTORICAL_LIMIT`.

Autosport exposes this as:

```text
autosport historical-coverage --from YYYY-MM-DD --to YYYY-MM-DD
```

The command requires `AUTOSPORT_PARLAYAPI_KEY`. It never puts the key in the URL or evidence file. On a successful provider response it fail-closed verifies:

- response `sport_key` is exactly `table_tennis`;
- response window exactly matches the requested dates;
- entitlement-window headers exist and the requested start does not precede the advertised oldest readable date;
- `by_source` is an object and every returned source has positive rows;
- `0 <= priced_rows <= rows`;
- source first/last dates are ordered and remain inside the requested window.

It writes an atomic JSON evidence record (default `.autosport-workspace/historical-coverage.json`) containing the requested window, runtime entitlement window, API version if provided, a SHA-256 of the canonical provider payload, per-source coverage, total/priced rows, and explicit truth labels.

A successful request with no sources is **not** promoted to corpus success: the CLI prints `historical_coverage=NO_DATA`, writes the evidence, and exits non-zero. A response with actual rows prints `DATA_AVAILABLE` and exits zero.

The evidence always records `licensing_or_retention_verified=false`. Authenticated access and observed provider coverage do not themselves prove that retention, redistribution, or long-term archive rights are permitted. A real schema-v2 corpus still requires truthful `terms_reference`, `retention_basis`, and `redistribution_policy` governance metadata.

## Historical product-shape boundary

Current provider documentation distinguishes historical results from historical prices. In particular:

- `/v1/historical/sports/{sport_key}/matches` is a match/results archive and rows may have `has_odds=false`;
- `/v1/historical/sports/{sport_key}/odds` is historical game-line pricing and supports `h2h`, `spreads`, and `totals`;
- `/v1/historical/sports/{sport_key}/closing-odds` is closing-line history.

Autosport must never treat a result-only row as price history or synthesize missing odds. Coverage preflight tells us where rows exist; a later importer must still select the correct endpoint and verify the actual market/source/date payload before creating a governed replay corpus.

## Reliability

The adapter uses bounded synchronous retries only for HTTP 429 and 5xx responses. Attempts are capped and Retry-After/backoff is capped; there is no unbounded retry loop. The provider remains outside the LLM path.

## Licensing / usage boundary

Provider documentation, pricing, historical windows, bookmaker availability and data licensing can change. Before a production data campaign, verify the current provider terms, plan, permitted storage/analysis use, table-tennis coverage, historical window and rate limits. Autosport stores provenance and dataset licensing/usage notes with any retained historical corpus; it does not infer those rights from API accessibility.

REAL_MONEY_EXECUTION=false.
