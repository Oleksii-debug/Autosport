# Autosport reuse matrix

This file is a living engineering decision record. Nothing is copied merely because it exists elsewhere; every candidate must have compatible semantics, understandable ownership and acceptable licensing/provenance.

## Current candidates

| Source | Candidate | Initial decision | Reason / next proof |
| --- | --- | --- | --- |
| Nika-Core | `model_gateway` | ADAPT later | Useful provider-agnostic local/API model routing; audit live API and dependencies before transfer. Autosport must not runtime-depend on unfinished Nika. |
| Nika-Core | `multi_agent` | ADAPT later | Likely useful durable role/task orchestration; first define Autosport typed task/decision boundaries, then selectively port only compatible primitives. |
| Nika-Core | `memory` / persistence primitives | REVIEW | Could reduce persistence work but sports event store/ledger semantics are specialized; do not reuse generic memory as market truth. |
| Nika-Core | `interaction` / Windows interaction | REVIEW | Potentially useful neutral UI/tool bridge. No bookmaker real-money execution path is permitted in Autosport. |
| Nika-Core | builder/Product Factory | DO NOT COPY now | Not needed for first product path and would import large unrelated complexity. |
| Accessible Chess | semantic HTML/WebView shell patterns | ADAPT NOW | Proven direction for keyboard/NVDA Windows app. Autosport strengthens it with copyability-visible-text contract. |
| Accessible Chess | native packaging/Windows diagnostics patterns | ADAPT later | Valuable for early packaged build and exact-artifact tests; audit current canonical release line before copying. |
| Accessible Chess | chess-domain state/parsers | DO NOT COPY | Wrong domain. |
| DuckDB | embedded analytical/event history store candidate | BENCHMARK | Efficient Appender, Parquet integration and timestamp support; benchmark append/query/concurrency before locking storage. |
| Polars | historical analytical pipeline | BENCHMARK/ADAPT | Lazy optimizer and streaming engine fit large replay/evaluation data. Keep outside per-event mutation hot path until measured. |
| pywebview/WebView2 | Windows web-style shell | ADAPT NOW | Matches required web-page navigation model and existing Accessible Chess direction; package/physical NVDA proof still required. |
| Optimization libraries | CP/MIP/constraint solver adapter | RESEARCH | Do not couple canonical portfolio API to a solver yet. Establish exact-small-case oracle and dependency graph first, then benchmark adapters. |

## Mandatory research rule

Before implementing a non-trivial new subsystem, update this matrix with at least the current Autosport incumbent and relevant proven reuse candidates. Reuse work must preserve source/license attribution where required, pin the consumed source version/commit where practical, and add Autosport-owned tests around the adopted behavior.
