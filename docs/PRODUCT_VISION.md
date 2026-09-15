# Автоспорт — Product Vision

## 1. Кінцева продуктова мета

Autosport — незалежний Windows-продукт і професійна багатоагентна система спортивного беттінгу. Його зріла економічна мета — **довгострокове зростання банку в межах жорстких risk/execution limits**, а не максимізація точності одного прогнозу “хто виграє”.

Forecasting — один із можливих джерел edge. Рівноправними джерелами є:

- predictive probability edge;
- live odds/state movement;
- lead/lag та stale actionable quotes;
- cross-provider/cross-market discrepancy;
- arbitrage;
- dutching/full-outcome coverage;
- hedge/rebalance;
- багато singles/parlays/combinations, керованих як один портфель.

Для чистого arbitrage/dutching/hedging directional winner forecast може бути відсутнім. Ключовий об’єкт економічного рішення — **весь open + proposed portfolio**, а не одна ставка.

Canonical live program: GitHub #355. Generic opportunity decision contract: #356. Real bookmaker execution: #353. Mathematical intelligence: #213.

## 2. Один продукт, поетапна довіра

Autosport не ділиться на “іграшкову paper-версію” і окрему “справжню” програму. Це один продукт із поетапним підвищенням execution authority:

1. replay / paper / live observation;
2. professional paper + live qualification;
3. bookmaker/account read-only;
4. supervised execution;
5. separate real execution ledger/reconciliation;
6. bounded autonomous execution;
7. continuous causal learning/champion-challenger improvement.

`REAL_MONEY_EXECUTION=false` означає поточний стан реалізації, а не постійну межу продукту.

## 3. Live — first-class mature-product lane

Pre-match analysis і короткострокові прогнози за хвилини/години до події залишаються корисними. Але live є окремим першокласним контуром, тому що odds/market state можуть швидко змінюватися.

Цільовий live loop:

`market/state update -> canonical identity/provenance -> freshness/order -> incremental analysis -> opportunity candidates -> whole-portfolio delta -> min-P&L/risk -> execution feasibility -> supervised/bounded action -> bookmaker acknowledgement -> reconciliation -> repeat`.

Hot path не залежить від LLM. Дані, time ordering, odds, stake/payout, portfolio arithmetic, minimum terminal P&L, risk limits та execution identity визначаються детермінованим кодом.

## 4. Outcome-independent profit

Autosport може назвати набір позицій `OUTCOME_INDEPENDENT_POSITIVE` лише коли:

- доведено complete relevant terminal-state space;
- кожен terminal state порахований;
- settlement semantics сумісні;
- exact current quotes є actionable;
- stake vector проходить account/provider limits і granularity;
- враховані applicable fees/commission/tax;
- quote freshness/slippage не порушені;
- partial/rejected execution представлений;
- execution sequence/atomicity assumption реалістичний;
- `minimum terminal net P&L > 0`.

Інакше продукт повинен чесно показати `THEORETICAL_ARBITRAGE_ONLY`, `EXECUTION_RISK_PRESENT`, `PARTIAL_COVERAGE`, `HEDGED_BUT_NOT_GUARANTEED` або `RISKED_PORTFOLIO`.

Monte Carlo/sampling ніколи не є доказом guaranteed minimum P&L на неповному state space.

## 5. Market Mirror і causal data

Один canonical `MarketEventStream` використовується для historical replay і lawful live observation.

Кожна quote/state зміна повинна мати sport/event/market/selection/provider identity, odds/status, source/receive/ingest timestamps, sequence/version там де доступно, quality/freshness state і provenance.

Market Mirror зберігає current state та append-only history. Stale/suspended/ambiguous quote не може вважатися executable.

Historical replay фізично ізолює future quotes/results від strategy runtime. Всі learning/evaluation claims зберігають causal cutoffs.

## 6. Virtual Bank / PaperBook як proving ground

Користувач задає virtual bankroll, наприклад 10 000 UAH. PaperBook моделює singles, parlays/combinations, stake, locked decision-time odds, payout, result/settlement та повний audit trail.

Paper stage повинен довести не лише “чи виграли ставки”, а:

- bankroll growth;
- drawdown/risk-of-ruin;
- turnover;
- exact-vs-approximate portfolio truth;
- many-position session handling;
- hedge/rebalance/dutching/arbitrage mathematics;
- causal strategy/evaluation integrity;
- restart/recovery.

## 7. Portfolio / Exposure Engine

Portfolio Engine — центральне економічне ядро. Він моделює dependency graph між outcomes, selections, tickets, parlays, providers і scenario states.

Виходи:

- available/reserved bankroll;
- committed stake/capital at risk;
- event/market/provider concentration;
- best-case P&L;
- minimum/worst-case terminal P&L;
- expected P&L when valid probability evidence exists;
- exact/approximate/completeness label;
- outcome coverage;
- drawdown/risk metrics;
- hedge/rebalance alternatives;
- marginal effect of each proposed position.

При quote/state change перераховується affected dependency subgraph, якщо це не послаблює correctness.

## 8. Strategy / agent system

Agents працюють через typed contracts і shared canonical state. Ролі можуть включати:

- Coordinator;
- Research/Data Quality;
- Market Analyst;
- Forecast/Predictive Model;
- Live Opportunity Analyst;
- Strategy/Opportunity Planner;
- Portfolio Agent;
- Risk/Critic;
- Settlement;
- Learning/Evaluation.

Forecast не є глобально обов’язковим contract. Current V1 predictive pipeline може залишатися forecast-bound до release, але mature generic decision contract (#356) повинен підтримувати strategy classes `PREDICTIVE_EDGE`, `LIVE_PRICE_MOVEMENT`, `ARBITRAGE`, `DUTCHING`, `HEDGE_REBALANCE`, `HYBRID`.

## 9. Real bookmaker execution

Після доказового етапу той самий продукт переходить до #353:

- Bookmaker Capability Registry;
- account/balance/limits read-only;
- official API first, sanctioned integration second, permitted browser automation where applicable;
- bet-slip/action preparation;
- exact event/market/selection verification;
- current odds/freshness/slippage recheck;
- stake or stake-vector entry;
- acknowledgement/external bet IDs;
- open/settled position readback;
- balance reconciliation;
- duplicate-bet prevention;
- partial multi-leg recovery;
- bounded autonomous execution under external user limits and emergency STOP.

A multi-leg opportunity is re-evaluated after **every** acknowledgement. One accepted leg + rejected/repriced remaining legs is a P0 economic hazard.

## 10. Learning loop

Кожне decision фіксується ДО outcome разом із доступним evidence, quote/state snapshot, portfolio state, strategy/model/config identity та action/rejection reason.

Після authoritative outcome система оцінює:

- realized net P&L;
- bankroll growth;
- EV capture;
- calibration where predictive probabilities are used;
- drawdown/risk-of-ruin;
- turnover;
- execution slippage;
- rejected/partial execution rate;
- hedge cost;
- arbitrage detected-vs-captured;
- performance by sport/provider/market/live regime.

Champion/challenger promotion використовує causal walk-forward/holdout evidence. Model/agent не може сам збільшити stake limits або execution authority.

## 11. Windows / accessibility

V1 зберігає поточний Tk + tk-uia Windows path до physical acceptance. Не робити UI rewrite лише через абстрактну перевагу іншого framework.

Критичні analysis/risk/portfolio/execution-confirmation/status/reconciliation surfaces мають keyboard-first і NVDA-accessible textual semantics. Machine UIA не замінює physical Windows 11 + NVDA evidence.

## 12. Multi-sport architecture

Table tennis — лише перший vertical slice. Canonical domain залишається sport-generic; sport-specific rules/features підключаються як adapters/contracts, а не hard-coded product identity.

## 13. Release / development law

V1 — non-money-moving proof release, не фінальна бізнес-мета. Не роздувати V1 далекими post-V1 можливостями, якщо вони затримують release. Але всі V1 contracts повинні уникати тупикових рішень, що роблять live/portfolio/execution архітектуру неможливою.

Canonical current order:

`V1 exact release -> bug bash -> professional paper/live qualification -> #355 -> bookmaker read-only -> supervised execution -> real ledger/reconciliation -> bounded autonomy -> continuous improvement`.

`REAL_MONEY_EXECUTION=false`  
`HUMAN_TESTED=false`  
`NVDA_VERIFIED=false`  
`V1_READY=false`
