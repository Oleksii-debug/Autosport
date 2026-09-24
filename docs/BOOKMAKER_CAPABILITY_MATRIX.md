# Bookmaker / exchange technical capability matrix

This matrix is a deterministic evidence view over the canonical `BookmakerCapabilityProfile` contract. It exists to compare provider/account/adapter scopes without assuming that every bookmaker or exchange exposes the same functions.

## Evidence rules

Each row is one exact `(venue_id, account_id, adapter_id)` scope and carries the source profile identity, adapter version, profile revision, observation timestamp, source reference, and source payload SHA-256. The matrix expands every current `BookmakerCapability` value to one explicit state: `supported`, `unsupported`, or `unknown`.

Missing evidence is always `unknown`. It is never promoted to `supported`, and an omitted capability cannot disappear from the matrix. A matrix rejects duplicate provider/account/adapter scopes and rejects a profile observed after the matrix `as_of` time.

Input row order does not affect the canonical representation or `matrix_id`, so equivalent point-in-time evidence has one deterministic identity.

## Safety boundary

Technical capability is not execution permission. Even when a source profile explicitly reports `place_bet`, `cashout`, or `cancel_bet` as technically supported, the matrix fixes:

- `provider_write_authorized = false`
- `real_money_execution = false`

Those facts can only describe provider technology. Separate product authority, legal/provider permission, supervised execution controls, risk/economic gates, and real execution evidence remain required before any write or real-money claim.

## Canonical artifact shape

`BookmakerCapabilityMatrix.to_canonical_dict()` emits:

- `schema_version`
- `as_of`
- the complete `capability_vocabulary`
- deterministic profile rows with source evidence and state for every capability
- the two explicit false authority flags above

`matrix_id` is the SHA-256 of that canonical JSON representation. Consumers should persist the canonical artifact plus its `matrix_id` when using a capability comparison as release, provider-selection, or technical due-diligence evidence.

This module performs no network calls, reads no credentials, and exposes no provider-write operation.
