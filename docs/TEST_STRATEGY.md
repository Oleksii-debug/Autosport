# Autosport test strategy

Core tests are layered by claim. Pure unit tests prove validation and exact arithmetic. Deterministic integration tests prove event ordering, current-state projection, replay release, paper ledger and later portfolio relationships. Adversarial tests explicitly attempt future leakage, duplicate/stale events, conflicting settlements and incorrect guarantee classification. Fixture-provider tests prove normalization against frozen inputs. Performance tests prove measured throughput/latency only on named environments. Windows package tests prove the exact packaged application. Physical NVDA tests remain human-only.

A green synthetic fixture test may not be described as proof of a real sportsbook/provider. An HTML static test may not be described as physical NVDA verification. A Monte Carlo estimate may not be described as an exact worst-case proof.
