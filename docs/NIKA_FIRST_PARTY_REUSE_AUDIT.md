# Nika-Core first-party reuse audit for Autosport

Status: active strategic reuse lane under Issue #362.

## Source truth reviewed

- Nika-Core repository: `Oleksii-debug/Nika-Core`
- Nika main reviewed: `2f7be3389109d7dd6fb3bae40540fe0cf2eba695`
- Autosport main at clean-rebase checkpoint: `d9fdae2c137a01a581aa84b03ceec6b553291c6a`

These SHAs are evidence checkpoints, not permanent source truth. Refresh live GitHub before any new adoption.

## Reuse doctrine

`SEARCH AUTOSPORT -> SEARCH NIKA -> REUSE/ADAPT IF IT SHORTENS AUTOSPORT -> THIN PROJECT ADAPTER -> GENERIC TESTS -> EXTRACT ONLY AFTER A SECOND REAL CONSUMER`

Current Autosport delivery always wins over abstract portability. No duplicate first-party engine may be created merely because Nika names the concept differently.

Every reused/adapted slice must preserve provenance and distinguish first-party Nika code from third-party engines/dependencies used by Nika.

## Adopt now

### NIKA-REUSE-01 — causal feature-engineering guards

Donor:
- `src/nika_core/trading_research/causality.py`
- `tests/test_trading_research_causality.py`

Autosport adaptation:
- `src/autosport/causal_features.py`
- `tests/test_causal_features.py`

Why this is additive rather than duplicate:
Autosport already has strong causal replay, dataset sealing, decision evidence and future-result firewalls, but it did not have a dedicated fail-closed feature-transform layer for Strategy/Model Factory work.

Imported/adapted semantics:
- feature values carry `available_at`;
- feature lineage cannot claim availability before its latest input;
- negative shift is forbidden;
- centered rolling window is forbidden;
- backward/non-causal fill is forbidden;
- scaler/statistical fit is TRAIN-only;
- future cache entries cannot be consumed at an earlier decision time.

Autosport dataset/replay/evidence authorities remain unchanged.

## Adapt later when the dependency becomes active

### Nika experiments -> Autosport Strategy/Model Factory

Potential donor areas:
- `src/nika_core/experiments/contracts.py`
- `engine.py`
- `repository.py`

Useful concepts:
- explicit champion/challenger identity;
- replay-set identity;
- promotion policy/guardrails;
- promoted/rolled-back states;
- permission fingerprint invariant preventing a challenger from widening authority.

Do not copy wholesale until compared against Autosport `strategy_comparison.py`, `evaluation_bundle.py`, `run_registry.py` and existing Issue #213 work. Autosport already owns sealed economic evidence and betting-specific promotion truth.

### Nika semantic interaction -> future Bookmaker adapter

Potential donor areas:
- `src/nika_core/interaction/domain.py`
- `orchestration.py`
- `resolver.py`
- `playwright_adapter.py`
- interaction Playwright lifecycle/TOCTOU/download tests.

Adapt semantic locator/state/postcondition/drift/recovery patterns when Stage K activates. Do not import Nika browser scheduler or permission authority into Autosport. The final Autosport browser path remains a deterministic semantic `BrowserBookmakerAdapter` behind `BookmakerCapabilityProfile`.

### Nika effect/idempotency/recovery patterns -> real execution saga

Potential donor areas:
- `src/nika_core/runtime/idempotency.py`
- `runtime_effect_journal.py`
- runtime recovery/retry contracts and adversarial tests.

Use as design/conformance donors for `ExecutionPlan/Attempt/Acknowledgement/Reconciliation/ExecutionSaga`, especially UNKNOWN external acknowledgement and no-blind-retry behavior. Do not create Nika Runtime #2 inside Autosport.

### Nika scheduler adapter -> 24/7 research trigger

Nika's `SchedulerPort`/APScheduler adapter is a useful implementation donor when Autosport reaches its persistent Research Supervisor. APScheduler may trigger Autosport Runs; it must not become the durable truth or create a second run/recovery authority.

### Nika Capability Registry / Tool Broker pattern

Useful donor areas:
- `tools.py`
- `kernel/action_registry.py`
- `mcp_boundary.py`
- `plugins/`
- `toolsmith/`

Potential future use: one capability profile/registry for scientific tools, browser execution and external providers. Do not import Nika's whole Toolsmith/Product Factory.

### Nika ModelGateway pattern

Nika's model routing/privacy/budget/effect-safe fallback architecture is a useful first-party design donor for future LLM research agents. It is not a current replacement for Autosport's numerical/statistical intelligence, and must not become economic authority.

## Do not copy wholesale

Autosport already has stronger or domain-authoritative implementations for:
- sealed sports-market datasets and historical corpus governance;
- replay and no-future-leakage at market/decision level;
- run transactions and recovery;
- decision/evidence ledgers;
- paper bankroll and settlement;
- price truth;
- portfolio/scenario economics;
- Windows packaging and current accessibility/NVDA evidence;
- betting-specific risk and strategy evidence.

Nika runtime/coordinator/session store, generic scheduler authority, Product Factory, permission authority, generic dataset, UI shell and packaging system must not be copied as parallel authorities.

## Provenance rule

Nika-Core is first-party source owned in the same GitHub account, but Nika itself wraps third-party libraries. Never move third-party source or imply independent first-party authorship without checking original provenance/license. Each adopted module records the donor repository and exact reviewed source checkpoint. Dependency/SBOM/notices are re-evaluated in Autosport independently.

## Next exact order

1. Land/qualify `NIKA-REUSE-01` causal feature guards.
2. Compare Nika experiment/promotion contracts against Autosport current strategy/evaluation/run registries; adapt only the missing semantics.
3. Compare Nika semantic interaction contracts/tests and draft the minimum Autosport bookmaker interaction port without opening a second browser authority.
4. Map Nika effect-journal/idempotency adversaries to Autosport future real execution saga.
5. Reassess scheduler/capability/model-gateway reuse only when those Autosport stages become live dependencies.

Before each source adoption: refresh `main`, open PRs, Issue #1, Issue #362 and relevant ownership. One semantic slice = one lineage.
