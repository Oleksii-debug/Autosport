# Open-source / reusable technology research

## 2026-09-12 bootstrap pass

This research is decision support, not a lock-in. The canonical interfaces must stay replaceable until measured against Autosport workloads.

### DuckDB

Candidate role: embedded persistent historical analytics/event-history query layer and Parquet interoperability. Official documentation exposes an Appender API for efficient row insertion and timestamp types including nanosecond-precision naive timestamps. This is promising for bulk history and replay/evaluation queries, but real Autosport append/query/concurrency benchmarks are still required before choosing it as the canonical hot-store implementation.

### Polars

Candidate role: large historical/replay/evaluation transformations outside the per-event mutation hot path. Its lazy API enables query optimization and its streaming engine processes batches, including datasets that exceed memory. Candidate use: dataset preparation, walk-forward windows, aggregated evaluation, Parquet scans/sinks. Benchmark before placing Polars inside low-latency per-event loops.

### pywebview / WebView2

Candidate role: Windows shell around semantic HTML UI. pywebview supports local web content and JS/Python communication and uses Edge Chromium/WebView2 on Windows when available. This aligns with the owner's preference for webpage-like navigation and with lessons from Accessible Chess. Autosport adds a stricter copyability contract: primary speech content must remain normal visible/selectable DOM text.

### Solver layer

No solver is canonical yet. First implement a clean Portfolio Solver interface, exact small-case oracle and dependency/invalidation graph. Then benchmark one or more mature constraint/optimization packages against representative ticket/scenario problems. Avoid embedding solver-specific objects throughout the domain model.

## Next research actions

Benchmark candidate storage with generated high-frequency MarketEvents; inspect current Nika-Core model_gateway/multi_agent contracts against Autosport needs; inspect current Accessible Chess package/pywebview release path for reusable Windows packaging and WebView2 diagnostics; research mature exact/constraint solver options and licenses after the Portfolio API exists.
