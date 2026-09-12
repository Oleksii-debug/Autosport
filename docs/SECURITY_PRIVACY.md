# Security and privacy baseline

Provider credentials/session material, cookies, private account data and secrets must never enter Git history, fixtures, release packages or ordinary logs. Raw payload retention is opt-in and scrubbed of secrets. The paper laboratory must not bypass provider access controls or rate limits. UI diagnostics expose safe identifiers and bounded errors, not tokens/private paths. External AI providers receive only the minimum structured context authorized by configuration; future local-only modes must remain possible.
