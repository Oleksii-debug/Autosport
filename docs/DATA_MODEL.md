# Canonical data model — bootstrap

Autosport stores provider-specific labels only at adapter boundaries. Canonical logic uses stable identities and normalized records.

## Identity hierarchy

`provider -> sport -> competition -> match -> market -> selection`.

A provider may expose several provider ids for the same real-world participant/match. Cross-provider entity reconciliation is a separate mapping/evidence problem and must not silently merge ambiguous entities.

## MarketEvent

Required bootstrap fields: unique `event_id`; monotonic/provider-derived or adapter-derived `sequence`; UTC `observed_at`; provider/match/market/selection key; `kind`; optional exact `decimal_odds`; optional value/status; optional provider/source timestamp; bounded metadata.

The event store is append-only. A correction produces a new event. Duplicate event id is idempotent. Current projection rejects stale sequence rewinds for a selection unless a future explicit reconciliation contract handles provider resets/epochs.

## Ticket

A ticket contains immutable id, exact decimal stake, one or more legs, quoted decimal odds per leg, decision provenance and creation timestamp once persistence layer lands. Derived parlay quoted odds are the exact Decimal product. Settlement adds terminal status/payout without rewriting the quoted decision.

## StrategyDecision

Planned required fields: decision id/time; experiment/replay id; causal market horizon/event id; agent/strategy/model versions; normalized feature/probability references; bankroll/portfolio snapshot identity; generated candidates; selected/rejected action and rationale/evidence references. Outcome is not part of the pre-outcome decision payload.

## Scenario universe

Portfolio claims bind the exact set/factorization of unresolved outcomes considered, market-rule assumptions and calculation mode (`exact`, `bounded`, `approximate`). A scenario result without this provenance cannot support a guarantee claim.
