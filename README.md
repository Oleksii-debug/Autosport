# Автоспорт / Autosport

**Professional agentic Windows sports-betting analysis, live-market intelligence, paper-proof and controlled execution platform.**

Автоспорт — окремий від Nika-Core продукт. Він розробляється, тестується, пакується та запускається незалежно від готовності Nika-Core.

## Product intent

Autosport **не є permanently paper-only продуктом** і **не є програмою «вгадай переможця»**. Його зріла економічна мета — максимізувати довгострокове зростання банку в межах жорстких risk/execution limits, використовуючи кілька strategy families:

- predictive probability edge;
- live odds/state movement, lead/lag і stale actionable quotes;
- cross-provider / cross-market discrepancies;
- arbitrage і dutching / full-outcome coverage;
- hedge / rebalance;
- багато singles/parlays/combinations, керованих як один портфель.

Forecasting є strategy-class dependent, а не глобально обов’язковим. Для чистого arbitrage/dutching/hedging directional forecast може бути відсутнім, якщо рішення спирається на causal executable quote structure та повну terminal-state economics.

Зрілий цикл продукту:

`lawful data -> pre-match/live analysis -> optional forecast / market-state intelligence -> opportunity classification -> bankroll/portfolio/min-P&L risk -> multiple positions -> paper/live-observation proof -> bookmaker capability/account read -> supervised real execution -> real execution ledger/reconciliation -> bounded autonomous execution -> causal learning/adaptation`.

`REAL_MONEY_EXECUTION=false` означає лише, що поточна реалізація ще не має кваліфікованого real-money execution path. Це не постійна заборона продукту.

### Outcome-independent profit rule

Autosport може використовувати truth label `OUTCOME_INDEPENDENT_POSITIVE` лише коли доведено повний релевантний terminal outcome space і точний executable position/stake plan має `minimum terminal net P&L > 0` після settlement rules, stake granularity, provider/account limits, quote freshness/slippage, applicable fees/commission/tax, partial acceptance та execution sequencing assumptions.

Якщо доказ неповний або sampled/approximate, потрібен слабший truth label, наприклад `THEORETICAL_ARBITRAGE_ONLY`, `EXECUTION_RISK_PRESENT`, `PARTIAL_COVERAGE`, `HEDGED_BUT_NOT_GUARANTEED` або `RISKED_PORTFOLIO`. Детальний live/outcome-independent contract — GitHub Issue #355; generic strategy-class decision contract — Issue #356.

## Current proof and qualification stage

Replay, Virtual Bank, paper betting, live observation, supervised execution scaffolding and release qualification are **safety/evidence stages inside the one final Autosport product**. They are not a separate product, version finish line or stopping target.

The first practical vertical slice is table tennis, while the canonical domain remains sport-generic. The current product tree already contains the shared Market Mirror/replay path, deterministic money/portfolio/risk authorities, agent/learning surfaces, bookmaker/account capability surfaces, recovery machinery and Windows packaging/accessibility machinery. Each capability still requires its own current evidence before it is treated as usable or integrated.

Money-moving authority remains fail-closed while its required provider/legal capability, exact receipt/reconciliation, duplicate-prevention, partial-execution recovery, owner-limit and emergency-STOP evidence is incomplete. `REAL_MONEY_EXECUTION=false` is current qualification truth, not a permanent product boundary.

Live observation uses the same canonical market path as replay. Decision-visible quotes are additionally fail-closed against durable provider health: the persistent live loop binds the exact source-local health replay horizon into crash recovery and immutable Decision Ledger evidence, so later equal-time health transitions cannot rewrite an earlier decision. A provider-health eligibility change can invalidate affected inputs even when no quote changed; repeated equally-healthy polls do not manufacture new economic decisions. Fast ingestion/storage/portfolio math remains deterministic and does not depend on an LLM. AI works over normalized structures and never replaces money arithmetic, quote-freshness, provider-health, risk or irreversible execution authority.

### Run

```powershell
python -m pip install -e .
python -m autosport demo
python -m autosport replay examples/table_tennis_replay.jsonl
python -m autosport dataset examples/tt_demo --workspace .autosport-workspace
python -m autosport gui
```

Tests:

```powershell
python -m unittest discover -s tests -v
```

Windows candidate:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1
```

## Development model

All work contributes to one whole finished product. Internal lanes are dependency/convergence aids only:

- `PRODUCT-WIDE` — canonical contracts, sport/market/event model, storage, replay, portfolio math, agents, learning/evaluation, observability, Windows/accessibility, provider and execution interfaces.
- `PROOF-QUALIFICATION` — causal replay/live-observation, paper economics, recovery, packaging, exact-head tests and physical accessibility evidence required before stronger authority is enabled.
- `LIVE-PORTFOLIO` — live price movement, arbitrage/dutching/hedging, coherent whole-portfolio minimum-P&L and exposure/risk.
- `BOOKMAKER-EXECUTION` — provider/account capability, supervised execution, real ledger/reconciliation and bounded autonomy under explicit owner limits.

## Work labels / lane tags

`PRODUCT-FOUNDATION`, `PROOF-QUALIFICATION`, `DATA`, `REPLAY`, `PORTFOLIO`, `AGENTS`, `LEARNING`, `PROVIDER`, `LIVE`, `ARBITRAGE`, `PERFORMANCE`, `WINDOWS`, `ACCESSIBILITY`, `RELEASE`, `BOOKMAKER`, `EXECUTION`, `DOCS`. Historical `V1-*` / `POST-V1` labels may remain on old issues or commits for archaeology only; they do not define a product target or landing order.

## Canonical control

- Repository: `Oleksii-debug/Autosport`
- Global coordination / product truth: GitHub Issue #1
- Continuous roadmap: GitHub Issue #198
- Mathematical intelligence: GitHub Issue #213
- Live market / arbitrage / dutching / outcome-independent portfolio program: GitHub Issue #355
- Generic opportunity decision contract: GitHub Issue #356
- Long-horizon bookmaker execution program: GitHub Issue #353
- Human-readable master specification: Google Drive folder `Автоспорт`, document `АВТОСПОРТ — MASTER TECHNICAL PROJECT`

## Capability progression

The dependency order below is sequencing inside one product, not a set of separate release goals:

- causal replay + paper/live-observation proof and reliability qualification;
- professional paper/live qualification with bankroll/risk/live-opportunity evidence;
- live portfolio intelligence: arbitrage/dutching/hedging/minimum-P&L;
- bookmaker read-only capability: account/balance, limits and open/settled positions;
- supervised execution with exact quote/plan/receipt authority;
- real execution ledger and reconciliation, including partial multi-leg safety;
- bounded autonomous execution only inside explicit owner limits and kill-switch policy;
- continual causal learning, multi-sport and multi-provider expansion.

Whole-product completion is defined by `docs/WHOLE_PRODUCT_COMPLETION_AUTHORITY.md`, not by a version label.

`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`  
`REAL_MONEY_EXECUTION=false`  
`WHOLE_PRODUCT_COMPLETE=false`

The legacy compatibility flag `V1_READY=false` may still appear in historical automation/evidence, but it is not the product-completion criterion.

## Binding Windows accessibility architecture

This is a product law for the final standalone Windows application.

The binding end-state for the primary Autosport Windows shell is **WebView2 + semantic HTML + a correctly exposed Windows UI Automation host**. The semantic surface must use real standard controls and expose accessible names, roles, state and deterministic focus so NVDA can navigate it by keyboard. Critical bankroll, risk, odds, portfolio, execution, reconciliation, errors and evidence must be real selectable/copyable text. No critical workflow or information may exist only in a canvas, chart, color, pointer position or mouse-only interaction.

The current Tk/Ttk + tk-uia shell is allowed to remain as a transitional delivery/qualification path while product work continues. Adopting this law does **not** restart Autosport and does not authorize rewriting the economic, market, provider, portfolio, learning, replay, persistence or execution core. Those must remain reusable behind a presentation boundary while the user-interface shell can be replaced incrementally.

Do not delay high-value product/domain work merely to rearrange visual layout or styling. However, new domain logic must not become tightly coupled to Tk/Ttk or any inaccessible presentation technology.

NVDA_VERIFIED=true still requires physical keyboard-only NVDA acceptance on the exact packaged Windows candidate. Automated Tk/UIA, DOM or accessibility-tree checks are supporting evidence only.
