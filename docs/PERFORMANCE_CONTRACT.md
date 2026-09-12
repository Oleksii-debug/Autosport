# Performance contract

Autosport treats latency/throughput as product correctness because historical replay and live observation lose value if every odds update triggers global recomputation.

## Hot-path rules

Provider parsing and `MarketEvent` creation are deterministic and non-LLM. Capture must continue when agents are slow. State projection work is O(affected event) rather than O(all history). Portfolio recomputation uses dependency invalidation and may cancel/supersede stale expensive jobs when newer market events arrive.

## Benchmark families

1. Event normalization/validation events per second.
2. Append throughput and time-range/selection queries for candidate persistent stores.
3. Current-state projection latency under thousands of selections.
4. Event-driven historical replay speed relative to source wall-clock duration.
5. Dependency invalidation cost as ticket graph size grows.
6. Exact portfolio oracle scaling and crossover point where approximation/solver methods are required.
7. UI update latency while ingestion/replay runs in background.
8. Restart/checkpoint recovery time.

Thresholds must be derived from measured representative workloads, not invented. Benchmark datasets and environment metadata are versioned so performance changes can be compared honestly.
