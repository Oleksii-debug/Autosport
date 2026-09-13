# Historical corpus governance and import truth

Autosport treats a historical replay corpus as evidence, not as an unrestricted cache of provider output. A corpus may be used as a governed historical dataset only when its local package passes the schema-v2 verification path in `autosport.dataset.load_dataset`.

This contract does not grant a licence and does not infer provider rights. The operator remains responsible for obtaining lawful access and recording a truthful retention/licensing basis. Missing credentials, missing historical coverage, or missing rights are external blockers and must not be converted into synthetic success.

## Schema boundary

`schema_version=1` remains supported for bundled fixtures and legacy sealed demo datasets. It proves only local file integrity. It is not historical-source, licensing, or coverage evidence.

`schema_version=2` requires `dataset_kind=historical` and a governance object. Autosport fails closed unless all required governance and causal checks succeed.

A minimal v2 manifest has this shape:

```json
{
  "schema_version": 2,
  "dataset_kind": "historical",
  "name": "local corpus name",
  "sport": "table_tennis",
  "market_file": "market.jsonl",
  "results_file": "results.json",
  "market_sha256": "<sha256>",
  "results_sha256": "<sha256>",
  "governance": {
    "source_identity": "<provider/export/contract identity>",
    "terms_reference": "<licence/terms reference used for this import>",
    "retention_basis": "<truthful basis for keeping this corpus>",
    "redistribution_policy": "internal_only",
    "acquired_at": "2026-01-02T00:00:00+00:00",
    "imported_at": "2026-01-02T00:05:00+00:00",
    "coverage": {
      "start_ts": "2026-01-01T00:00:00+00:00",
      "end_ts": "2026-01-01T23:59:59+00:00",
      "source_ids": ["licensed-feed"],
      "market_types": ["winner", "total", "handicap"]
    },
    "causality": {
      "strategy_time_field": "observed_ts",
      "outcome_reveal_after": "2026-01-02T00:00:00+00:00"
    }
  }
}
```

`redistribution_policy` is an explicit truth label: `prohibited`, `internal_only`, or `permitted`. It is not inferred from the provider name.

## Machine-verifiable gates

For schema v2 Autosport verifies all of the following before a replay may consume the corpus:

- market and sealed-results files are separate, contained inside the dataset root, and match their declared SHA-256 digests;
- source identity, terms reference, retention basis, acquisition/import timestamps, coverage, market types and causal policy are present;
- acquisition/import and coverage timestamps are timezone-aware and ordered consistently;
- every market event belongs to a declared source and market type;
- every historical market event has a source timestamp, with `source_ts <= observed_ts <= ingest_ts`;
- every event falls inside the declared historical coverage window;
- duplicate canonical source-event identities are rejected;
- outcome/final-result/settlement metadata is forbidden in the strategy-visible market stream;
- sealed results may reference only quote keys that actually exist in the market corpus;
- the outcome reveal boundary cannot precede the end of strategy-visible market coverage.

The derived `historical_import_identity` is the SHA-256 of the canonical schema-v2 manifest identity, including the exact market/results hashes and governance record. If a manifest declares an `import_identity`, Autosport recomputes it and rejects any mismatch. This makes the import identity content-addressed rather than user-asserted.

## Verification without replay

Run:

```text
autosport verify-dataset PATH_TO_CORPUS
```

A valid historical package prints its exact file hashes, historical import identity, source identity, redistribution label, declared coverage, source/market sets and causal reveal boundary. A schema-v1 fixture is explicitly printed as `LEGACY_FIXTURE_ONLY historical_proof=false`.

`autosport dataset PATH_TO_CORPUS` uses the same loader, so a governed historical corpus cannot bypass these checks merely by entering the normal replay path.

## Truth boundary

Passing this verifier means only that the local package is internally consistent with its recorded governance contract. It does not prove that a provider actually granted a licence, that an API key has historical entitlement, that a desired date/source/market is available upstream, or that a strategy is profitable.

For a real table-tennis corpus, acquisition must still verify upstream coverage and the applicable terms/contract at runtime, then record that truth in the manifest. Secrets, cookies and API keys must never be committed into the corpus, Git history, logs or release artifacts.

`REAL_MONEY_EXECUTION=false` remains invariant.
