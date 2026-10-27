# Account C blind live PAPER campaign — 2026-10-26

## Decision

The development campaign runs against ParlayAPI's documented no-auth table-tennis
sandbox. It is a synthetic, PAPER-only endurance exercise. It is not a bookmaker
feed, match-result feed, forward-evidence population, profitability test, or release
qualification. The decision policy receives only the market scope, quote count, and
snapshot identity. The audit and settlement authorities always retain the explicit
`SIMULATED` truth label and simulation-model identity.

The workflow starts two one-hour processes over one durable workspace. Session two
must load the exact PaperBook, causal-environment checkpoint, policy generation,
deduplication identity, and counters produced by session one. A fixed cutoff is
created before the first decision. Every synthetic settlement is derived only from a
later available snapshot, and the runner refuses a segment with fewer than two source
snapshots.

After session two, a third independent Python process reloads the final state before
artifact upload. The workflow exports the existing PaperBook authority's separate
append-only witness chain alongside the state and records SHA-256 for every exported
file. The witness remains deliberately bound to its original absolute path; the
export is audit evidence, not permission to transplant or rebind economic authority.

That independent reload also writes `paper_diagnostics.json`. The diagnostics are a
read-only reconstruction from authoritative settled PaperBook tickets: exact initial
and final equity, gross stake/payout, net reward, maximum drawdown and its equity peak,
maximum consecutive negative rewards, and separate SINGLE/PARLAY_2 settlement
cohorts. Ticket chronology and every aggregate counter are recomputed and compared;
any open stake, non-causal/overlapping settlement, unsupported leg count, balance
mismatch or counter disagreement fails closed. These metrics retain the SIMULATED
truth boundary and cannot authorize promotion or a profitability claim.

No API model, Drive credential, real-money account, bookmaker login, bet-placement
route, or user secret is used.

## Ten-platform Ukrainian market screen

This is a compliance screen, not a popularity ranking and not authorization to copy
site content. The current regulatory source of truth is the PlayCity open-data and
license-register page: <https://playcity.gov.ua/publichna-informatsiia/vidkryti-dani-playcity>.

| Candidate | Screen result | Automated collection decision |
|---|---|---|
| GGBET | Public live/table-tennis UI and Ukrainian licence disclosure found | UI diagnostic only; terms prohibit bot/copy use without permission |
| VBET | Candidate licensed Ukrainian operator | No collection without a machine-use licence or contracted API |
| BETON | Candidate licensed Ukrainian operator | No collection without a machine-use licence or contracted API |
| BETKING | Candidate licensed Ukrainian operator | No collection without a machine-use licence or contracted API |
| Favbet | Licence status requires fresh registry verification after reported 2026 termination decision | Rejected pending authoritative re-verification |
| Parimatch | No current admissible source contract established | Rejected |
| 1xBet | No current admissible Ukrainian licence/source contract established | Rejected |
| bet365 | No current admissible Ukrainian licence/source contract established | Rejected |
| Melbet | No current admissible Ukrainian licence/source contract established | Rejected |
| Pin-Up | No current admissible Ukrainian licence/source contract established | Rejected |

GGBET was selected only for public-page reachability diagnosis. It was not scraped.
The production path remains fail-closed until an operator or data vendor supplies
documented rights for automated in-play prices, provider-owned timestamps, stable
event identities, and authoritative results.

## Source diagnostics

ParlayAPI documents table tennis and a sandbox intended for integration testing:
<https://parlay-api.com/docs>. The no-key public preview currently rejects table
tennis, while the documented sandbox period-market endpoint returns synthetic rows.
The endpoint's use of “live” does not establish a bookmaker in-play state or an
authoritative match outcome. For that reason, later price movement is used only as
an explicitly named simulation model.

The stored evidence includes neither raw bookmaker content nor reusable source
pricing. Only bounded synthetic PaperBook state, causal hashes, aggregate counters,
and the transparent policy state are uploaded for 14 days.

## Frozen safety and scientific boundaries

- Initial virtual bankroll is exactly 10,000 UAH; maximum stake is 50 UAH and never
  more than 0.5% of current virtual balance.
- Seven of every ten curriculum slots are reserved for the two-event parlay family
  when two distinct events exist; two slots for singles; one lets the policy choose.
  Within the single and parlay families, the existing transparent bandit separately
  learns HIGH_ODDS and LOW_ODDS selection strategies. Each strategy receives a
  bounded eight-observation warmup; later family slots admit only strategies with
  positive historical mean reward and select the highest mean. If neither qualifies,
  the slot becomes a causal WAIT. A zero mean cannot win a lexical tie against WAIT.
  Ranking uses only decision-time odds; the future snapshot is unavailable until
  settlement. Eight samples are an engineering warmup, not statistical confidence.
  Once all strategies abstain, this campaign does not learn about a later recovery;
  a separately qualified fresh campaign is required to reassess that regime.
  Schema v3 binds the selection protocol and warmup to the immutable plan identity.
  Existing v1/v2 campaign files remain evidence for their original code and are not
  silently migrated or resumed under the changed selection rule.
- Decision evidence contains no execution-mode or synthetic-truth feature. Audit,
  outcome, reward, and summary records always contain that truth.
- The same decision snapshot cannot create a second action. A later snapshot and
  later availability timestamp are mandatory before settlement and learning.
- Positive and negative rewards are both retained. No observed result is inferred
  from price movement.
- `forward_evidence_eligible`, `promotion_authorized`, and `profitability_claim` are
  mechanically false.
- Existing forward-campaign adapters reject simulated witnesses, preventing this
  campaign from entering NEW, REPLAY, RETENTION, or HOLDOUT product populations.

## Reproduction

```bash
python -m pip install -e '.[test]'
python -m pytest -q tests/test_paper_live_sandbox_campaign.py
python scripts/run_account_c_blind_paper_campaign.py \
  --workspace account-c-paper-evidence \
  --segment-seconds 3600 --poll-seconds 30 \
  --cutoff-seconds 7800 --expect-new
python scripts/run_account_c_blind_paper_campaign.py \
  --workspace account-c-paper-evidence \
  --segment-seconds 3600 --poll-seconds 30 --expect-resume
python scripts/run_account_c_blind_paper_campaign.py \
  --workspace account-c-paper-evidence --verify-only
```

The GitHub workflow executes the same two-process sequence. A green workflow proves
only sandbox endurance, restart fidelity, and evidence-boundary enforcement. It does
not prove sporting prediction quality, an economic edge, or profitability.

## Existing research-to-learning integration

The production multi-leg path continues to use the existing
`ResearchDecisionPipeline`, `PaperBook`, `PaperSettlementLearningBridge`, and
`PaperCampaignRuntime`; no second settlement or learning engine is introduced.
`ResearchDecisionPipeline.decide_and_open` now accepts an optional exact causal
`Observation`. When supplied, its complete immutable identity is written into the
economic decision and the same binding enters the material-action intent hash. A
retry with another observation therefore conflicts instead of silently reusing the
ticket.

The settlement bridge admits `OPEN_PAPER_RESEARCH_TICKET` only for the established
`PAPER_PROPOSAL` AgentLoop action. It verifies the causal observation, availability
before decision, exact ticket identity, stake, and the complete unique set of
candidate quote keys before binding. A two-leg integration test then resolves the
ticket through the existing observed settlement authority and obtains one causal
reward plus one advanced environment checkpoint. Simulated sandbox outcomes remain
outside this observed path.
