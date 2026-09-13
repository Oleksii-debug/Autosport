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

## Rights/retention authority binding for corpus builds

The production corpus-build entrypoints do not accept a bare JSON file that merely sets `licensing_or_retention_verified=true`. The governance proof must name a direct sibling authority-evidence record and bind that record by exact SHA-256. The claims copied into the governance proof must exactly match the hashed authority record.

The governance proof therefore includes:

```json
{
  "schema_version": 1,
  "kind": "historical_corpus_governance_proof",
  "source_identity": "<provider/account/export identity>",
  "source_ids": ["parlayapi:table_tennis"],
  "terms_reference": "<specific terms/contract reference>",
  "retention_basis": "<truthful retention basis>",
  "authority_reference": "<entitlement/contract/decision reference>",
  "verified_at": "2026-09-13T06:00:00Z",
  "redistribution_policy": "internal_only",
  "redistribution_verified": false,
  "licensing_or_retention_verified": true,
  "authority_record_file": "authority-record.json",
  "authority_record_sha256": "<sha256 of authority-record.json>"
}
```

The sibling `authority-record.json` uses `kind=historical_corpus_governance_authority_record`, repeats the exact rights-bearing fields above, and additionally records non-empty `evidence_reference`, `verification_method`, and `recorded_by` fields. The build fails closed if the file is missing, nested outside the direct sibling boundary, hash-mismatched, or disagrees with the proof on source, terms, retention, authority, verification time, licensing flag, or redistribution truth.

This is an integrity/provenance gate, not independent legal certification. A fabricated authority record can still have a valid checksum. The record must refer to real evidence and an actual lawful basis. Autosport never turns a checksum, API credential, read-only provider response, or operator assertion into legal rights.

The canonical installed commands `autosport-build-historical-corpus` and `autosport-build-historical-corpus-from-bundle`, and the packaged `Autosport-Data.exe build-corpus*` commands, route through this binding gate before the corpus assembler runs.

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

The derived `historical_import_identity` is the SHA-256 of the canonical schema-v2 manifest identity, including the exact market/results hashes and governance record. If a manifest declares an `import_identity`, Autosport recomputes it and rejects any mismatch. This makes the import identity content-addressed rather than user-asserted. Because the governance proof itself contains the authority-record SHA-256 and the assembled manifest stores the governance-proof SHA-256, the final import identity transitively binds the accepted authority evidence record without redistributing that external evidence artifact.

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