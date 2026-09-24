# Provider degradation evidence contract

Provider failure is not the same fact as provider capability. This contract records a point-in-time degraded observation for one exact venue/account/adapter/capability scope without silently converting absence, failure, or stale data into `unsupported`, `supported`, zero, or execution authority.

## Reason vocabulary

Every event has exactly one explicit reason:

- `transient_provider_failure`
- `stale_data`
- `malformed_evidence`
- `rate_limited`
- `authentication_or_permission_failure`
- `transport_unavailable`
- `unknown`

Each reason maps deterministically to a diagnostic recovery class (`no_automatic_retry`, `retry_with_backoff`, `refresh_evidence`, `reauthorize`, or `operator_review`). The recovery class describes the kind of follow-up that may be needed; it does not perform or authorize that follow-up.

`capability_unsupported` is deliberately not a degradation reason. Technical SUPPORTED/UNSUPPORTED/UNKNOWN truth remains solely in the canonical versioned `BookmakerCapabilityProfile`; a degradation event cannot mint or contradict that authority.

## Evidence binding

`ProviderDegradationEvent` binds the exact provider scope, requested canonical `BookmakerCapability`, observation timestamp, evidence reference, source-payload SHA-256, and a provider/adapter detail code. Its `event_id` is the SHA-256 of canonical JSON.

`ProviderDegradationReport` is a deterministic point-in-time set. It rejects empty reports, future observations, duplicate event identity, and multiple different outcomes for the same venue/account/adapter/capability scope in one report. Equivalent event sets produce the same canonical representation and `report_id` regardless of input order.

## Fail-closed fallback boundary

A degraded provider does not prove that another provider is a semantically equivalent source for the same market/account state. Therefore degradation evidence always fixes:

- `automatic_fallback_authorized = false`
- `provider_write_authorized = false`
- `real_money_execution = false`

A consumer may use `recovery_class` as diagnostic evidence when choosing a separately authorized recovery workflow. Cross-provider substitution requires an independent market/source-equivalence authority; provider writes and real-money execution require their own explicit gates and evidence.

The module performs no network calls, reads no credentials, dispatches no retry, selects no fallback provider, and exposes no provider-write operation.
