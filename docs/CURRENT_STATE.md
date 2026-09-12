# Autosport current state

Last updated: 2026-09-12.

## Integrated main

`main` contains repository bootstrap and canonical `docs/PRODUCT_VISION.md`. It does not yet contain the first implementation slice.

## Active whole-product bootstrap

Branch: `work/whole-product-bootstrap-20260912`.

Implemented on the branch: autonomous development contract; detailed technical project; version roadmap; accessibility/copyability contract; reuse matrix; evidence-based progress model; full continuation prompt; Python packaging metadata; canonical normalized `MarketEvent`; idempotent `MarketState`; deterministic `CausalReplay`; exact-decimal `PaperBook`; semantic visible/copyable web UI; pywebview Windows launcher; core/accessibility regression tests; Ubuntu/Windows GitHub Actions workflow.

## Unproven / not ready

Branch tests have not yet been observed terminal-green in GitHub Actions at the time this file was authored. No real sports-data provider exists yet. No persistent high-rate event store benchmark, portfolio/exposure solver, model gateway/agent runtime, complete settlement rules, Windows packaged artifact or human NVDA acceptance exists yet.

## Nearest critical path

1. Open bootstrap PR and obtain exact-head Windows/Linux CI.
2. Repair any failures; integrate only after exact-head green/review.
3. Add exact-small-case Portfolio/Exposure oracle + dependency graph.
4. Benchmark persistent event store candidates (DuckDB/Parquet, analytical Polars path) with synthetic high-frequency events.
5. Add canonical provider/fixture interface and first table-tennis historical fixture without future leakage.
6. Connect durable experiment state and early packaged Windows candidate.
