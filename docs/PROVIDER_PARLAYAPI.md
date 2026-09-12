# ParlayAPI table-tennis provider boundary

Status: V1 candidate read-only data source. This document does not assert redistribution rights or production coverage beyond what is verified at runtime.

## Purpose

Autosport integrates ParlayAPI only through `ParlayApiTableTennisProvider`. The adapter observes market data and emits typed `ProviderQuote` batches for the existing deterministic ingestion path. It has no bookmaker-account, wager-placement, funding, withdrawal, or real-money execution capability.

## Public provider documentation used for the adapter contract

- Table-tennis coverage page: https://parlay-api.com/sports/table_tennis
- Response shapes: https://parlay-api.com/docs/response-shapes
- Getting started / public preview: https://api.parlay-api.com/getting-started
- Main API documentation: https://api.parlay-api.com/docs

The documented table-tennis sport key is `table_tennis`. The game-line endpoint is documented as TOA-compatible and carries bookmaker -> market -> outcome trees. The adapter requests decimal odds and maps `h2h`, `spreads`, and `totals` into Autosport WINNER, HANDICAP, and TOTAL market types.

## Security and secrets

Authenticated calls pass the API key in the `X-API-Key` request header. Keys must come from runtime configuration/environment and must never be committed, logged, written into replay datasets, or included in release artifacts. Public preview mode requires no key and is suitable only for limited manual connectivity checks.

## Causal/time semantics

`markets[].last_update` is preferred as source time; `bookmakers[].last_update` is the fallback. Autosport records local observation time separately. The source timestamp is converted into a stable integer sequence so repeated delivery of an unchanged provider snapshot remains idempotent in the append-only Market Store.

If the provider omits or supplies an invalid source timestamp, the adapter fails closed rather than inventing causal precision for that quote. Provider availability, book coverage, market coverage and freshness must be treated as runtime facts, not static assumptions.

## Reliability

The adapter uses bounded synchronous retries only for HTTP 429 and 5xx responses. Attempts are capped and Retry-After/backoff is capped; there is no unbounded retry loop. The provider remains outside the LLM path.

## Licensing / usage boundary

Provider documentation, pricing, historical windows, bookmaker availability and data licensing can change. Before a production data campaign, verify the current provider terms, plan, permitted storage/analysis use, table-tennis coverage, historical window and rate limits. Autosport should store provenance and dataset licensing/usage notes with any retained historical corpus.

REAL_MONEY_EXECUTION=false.
