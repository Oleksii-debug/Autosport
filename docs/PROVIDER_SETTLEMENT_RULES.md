# Provider settlement-rule version evidence

Settlement rules are provider-specific and time/version dependent. A recorded position outcome must not inherit void, push, cancellation, abandonment, dead-heat, overtime, or other settlement semantics merely because a provider or sport name is known.

`ProviderSettlementRuleVersion` binds one exact venue, rule-set id and provider rule version to an effective interval plus observation/source evidence. Its canonical `rule_id` commits the full immutable record. An exact version is `applicable` only when venue, rule-set and version match and the event time is inside the half-open effective interval `[effective_from, effective_until)`. Missing version or foreign identity is `unknown`; changed version is `version_mismatch`; time outside the interval is `not_effective`.

`ProviderSettlementRuleCatalog` forms a deterministic point-in-time evidence set, rejects future observations and duplicate version identity, and has an order-independent canonical `catalog_id`.

This contract does not encode or execute settlement outcomes. Both rule and catalog fix `settlement_authorized = false`. A downstream settlement authority must separately bind the exact applicable rule evidence to provider receipts and explicit outcome semantics before mutating any canonical ledger. Missing or mismatched rule evidence stays unproven rather than defaulting to a generic bookmaker rule.

The module performs no provider network calls, reads no credentials, writes no provider state, settles no position, and grants no real-money or release authority.
