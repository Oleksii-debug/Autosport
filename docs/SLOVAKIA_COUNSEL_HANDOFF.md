# Slovakia outside-counsel handoff

**Work package:** WP-L06  
**Prepared:** 2026-09-21  
**Purpose:** turn the Slovakia legal evidence package into explicit written launch decisions without asking counsel to reconstruct the product or source inventory from scratch.

This is a legal-review handoff, not legal advice. Engineering must treat every unanswered launch-blocking question below as **deny/disabled**. WP-L07 may close only on written outside-counsel evidence that either (a) states a scoped `NO_BLOCKER` conclusion, or (b) enumerates every blocker with a named mitigation owner and activation condition.

## 1. Decision requested from counsel

Please assess the intended **Slovakia** deployment of Autosport, a Windows sports-analysis/portfolio-risk application that:

- consumes sports/market data only from sources for which lawful access/use is evidenced;
- may link to a user's account at a Slovak-licensed bookmaker for account read/reconciliation where contractually permitted;
- may later submit supervised or bounded automated betting actions through an operator-approved integration, but only after legal and safety activation gates;
- does not intend to become the sportsbook/operator of record, custody pooled customer stakes, or pay gambling winnings itself;
- can display analytical recommendations, `WAIT`/`ZERO`, proposed stake vectors and risk/portfolio information;
- may be sold as a paid digital product/service to consumers;
- is intended to launch territorially, Slovakia first, rather than assuming one EU-wide gambling permission.

For each decision below, please return one of:

- `NO_BLOCKER` — scoped written conclusion for the described mechanics;
- `NO_BLOCKER_IF` — allowed only with listed binding conditions/controls;
- `BLOCKER` — capability cannot launch until the stated change/evidence is complete;
- `OUT_OF_SCOPE` — requires another specialist/authority; identify the owner where possible.

## 2. Product facts counsel should rely on

These facts are intentionally narrow. If any is wrong, the answer should flag the dependency rather than assume a different product.

| Fact | Current engineering position |
| --- | --- |
| Gambling operator | External licensed sportsbook remains the operator/counterparty. |
| User funds | Autosport does not pool or custody customer betting funds. |
| Money-moving execution | Disabled by default; future activation requires separate authority. |
| Bookmaker credentials | Must not be stored/used unless an approved integration and security design require it. |
| Data acquisition | No right is inferred from browser/API reachability. Production source needs licence/terms provenance. |
| Reverse-engineered endpoints | Not an accepted production integration basis without written permission/legal clearance. |
| Jurisdiction | Slovakia only for this review. Other states require separate evidence. |
| Marketing | Default is neutral analytics/product copy; affiliate/bonus/gambling inducement surfaces remain off pending review. |
| Consumer sale | Assume a paid digital service/content offering to Slovak consumers may be used. |
| Personal data | May include Autosport account data, coarse/precise location evidence, telemetry, and permitted linked-bookmaker account data. |
| Final authority | Product owner defines risk/automation ceilings; software must not self-expand them. |

Binding engineering context: `docs/MASTER_TECHNICAL_PROJECT.md`.  
Legal evidence map: `docs/SLOVAKIA_LEGAL_RISK_MATRIX.md`.

## 3. Primary-source bundle already resolved

Counsel should cite the exact temporal/versioned authority used in the memo.

### Slovak Gambling Act

**Act No. 30/2019 Coll. on Gambling**, Slov-Lex current page; evidence cut showed temporal version effective from 01.03.2026:  
https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2019/30/

Review anchors already identified:

- §§ 1-4 — scope, definitions, operating/promotion baseline;
- § 30 — internet games;
- § 35 — licence categories;
- § 39 — individual-licence baseline;
- §§ 63-65 — application requirements for internet betting/casino categories.

### ÚRHH regulator evidence

- Individual-licence register (page valid-to 20.09.2026 at evidence cut):  
  https://www.urhh.sk/licencie-2/registre-zoznamy-a-ciselniky/zoznam-udelenych-individualnych-licencii/
- Central operator register (page valid-to 20.09.2026 at evidence cut):  
  https://www.urhh.sk/licencie-2/registre-zoznamy-a-ciselniky/centralny-register-prevadzkovatelov-hazardnych-hier/
- Legal internet websites:  
  https://www.urhh.sk/licencie-2/legalne-webove-stranky/

### FORTUNA SK documents reviewed for the first provider slice

- General gambling terms, effective 15.04.2026:  
  https://www.ifortuna.sk/file/69cbc98bb87c125e6c5b430c
- Public game-plan material for `www.ifortuna.sk`:  
  https://www.ifortuna.sk/file/695ba1e87a17ba1f5b6ac2a6

The public game-plan material describes API/HTTPS/VPN connectivity for the regulator/system context. **Autosport does not treat that passage as third-party/public API authorization.**

### Consumer law

**Act No. 108/2024 Coll. on Consumer Protection**, Slov-Lex; evidence cut was the version effective 31.07.2026-26.09.2026:  
https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2024/108

Review anchors include §§ 4-5, unfair-commercial-practice provisions, and distance/digital-contract duties in §§ 15-20.

### EU privacy/data law

- GDPR, Regulation (EU) 2016/679:  
  https://eur-lex.europa.eu/eli/reg/2016/679
- Directive 96/9/EC on databases, especially Art. 7:  
  https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:01996L0009-20190606

## 4. Counsel decision queue

### Q1 — Regulatory classification of Autosport

**Launch blocker:** YES  
**Owner:** Outside Counsel / Legal  
**Question:** Under Act No. 30/2019 Coll., does the described analytics + user-account-linked supervised/bounded execution model cause Autosport or its supplier to be treated as an operator of gambling, intermediary/agent, supervised subject, gambling-service supplier, or another licensed/registered role?

Please separately classify:

1. analytics-only/read-only mode;
2. bookmaker account read/reconciliation;
3. user-confirmed bet submission through an operator-approved interface;
4. bounded automated bet submission under user-defined limits;
5. hedge/rebalance/multi-leg execution across more than one provider.

**Engineering default until answer:** modes 3-5 stay disabled; modes 1-2 require lawful data/operator terms and privacy clearance.

### Q2 — Reliance on a licensed Slovak sportsbook

**Launch blocker:** YES for execution  
**Owner:** Legal + Compliance  
**Question:** When the sportsbook is itself licensed in Slovakia and the user bets from that user's own account, what legal duties remain with Autosport? Is any notification, registration, outsourcing approval, contract structure or operator-side authorization required for the software supplier?

Please identify the statutory/licence provisions relied on, not only a general conclusion.

**Engineering default:** current provider status must be verified from ÚRHH before activation and periodically revalidated.

### Q3 — Machine access and bet placement under operator terms

**Launch blocker:** YES  
**Owner:** Legal + Data Partnerships  
**Question:** What documentary permission is sufficient for automated account read, odds/market access, bet submission, retries/reconciliation and production-scale use? Do the reviewed FORTUNA public documents authorize any of those uses by Autosport, or is a separate written agreement/API licence required?

Please address whether using undocumented or reverse-engineered `ifortuna.sk` / `api.ifortuna.sk` endpoints is permissible even where an end user can access the same information interactively.

**Engineering default:** no production automation permission is evidenced; undocumented/reverse-engineered access remains blocked.

### Q4 — Market-data/database rights

**Launch blocker:** YES for each unlicensed source  
**Owner:** Legal + Data Partnerships  
**Question:** For an odds/market dataset used to power live/pre-match analysis, what rights must Autosport obtain for:

- machine collection;
- repeated refresh;
- local storage and append-only history;
- replay/research;
- model/strategy training;
- creation of derived signals;
- display to users;
- commercial use;
- cross-provider comparison;
- retention after termination?

Please apply contractual terms plus Directive 96/9/EC/database-right rules to the actual source and cadence.

**Engineering default:** source capability is disabled when permitted-use provenance is absent or expired.

### Q5 — Promotion/affiliate/inducement boundary

**Launch blocker:** YES for marketing surfaces  
**Owner:** Legal + Product/Marketing  
**Question:** Which Autosport features/messages constitute `propagovanie hazardnej hry` under Act No. 30/2019 Coll.? Review at minimum:

- sportsbook names/logos;
- links/deep links to bet slips;
- provider ranking by price/odds;
- push notifications that a betting opportunity exists;
- expected-profit or win messaging;
- affiliate links/revenue share;
- bonuses/promotions;
- screenshots of bookmaker odds.

Specify prohibited content and any mandatory age/responsible-gambling/risk wording or placement requirements.

**Engineering default:** affiliate/bonus inducements off; neutral functional provider identification only.

### Q6 — Consumer-law classification and checkout

**Launch blocker:** YES before paid Slovak consumer launch  
**Owner:** Legal + Product  
**Question:** Classify Autosport under Act No. 108/2024 Coll. for a paid subscription/licence: service, digital content, digital service, mixed contract, or other. Specify required pre-contract information, order-button/checkout wording, contract confirmation, complaints/ADR, renewal/cancellation and withdrawal/early-performance mechanics.

Also confirm whether any gambling-specific exclusion applies to Autosport itself; do not assume that an analytics product inherits the sportsbook's legal category.

**Engineering default:** full consumer-information/distance-contract flow required until counsel narrows it.

### Q7 — Marketing/performance claims

**Launch blocker:** YES before external paid marketing  
**Owner:** Legal + Compliance + Product  
**Question:** Approve a claims policy for ROI, win rate, expected value, model accuracy, drawdown, arbitrage and expressions such as "risk-free"/"guaranteed". What evidence window and risk disclosures are required for a claim to be defensible under Slovak unfair-commercial-practice rules?

**Engineering default:** no guaranteed-profit/risk-free claims; every quantitative claim needs reproducible evidence and scope/period labels.

### Q8 — Privacy roles and lawful bases

**Launch blocker:** YES before production personal-data processing  
**Owner:** Privacy Counsel + Security  
**Question:** For Autosport account data, geolocation evidence, telemetry and linked-bookmaker account data, identify:

- controller/processor/joint-controller roles;
- Art. 6 lawful basis per purpose;
- transparency notices;
- data minimisation and retention;
- DPIA requirement;
- processor/subprocessor and transfer controls;
- rights handling;
- whether Art. 22 applies to any user-facing automated decision in this product design.

**Engineering default:** collect the minimum; do not treat consent as a universal lawful basis; no unnecessary bookmaker credential retention.

### Q9 — Territorial control / geofence

**Launch blocker:** YES for automated execution  
**Owner:** Legal + Compliance + Engineering  
**Question:** What user/location/account-domicile evidence is sufficient to allow Slovakia-only functionality, particularly for travel, VPN/proxy use, cross-border residence and remote access? Must controls operate at installation, session, recommendation, account-link and/or execution time?

**Engineering default:** uncertain territory fails closed; a Slovakia legal package is not exported to another jurisdiction.

### Q10 — Audit/evidence retention

**Launch blocker:** NO for analysis-only, YES for execution activation package  
**Owner:** Legal + Compliance + Engineering  
**Question:** What must be retained to prove the basis for activation and consumer/operator consent/authorization, and for how long? Please specify treatment of:

- counsel memo/version;
- regulator register evidence;
- operator/data terms snapshot;
- operator integration approval;
- user terms/privacy acceptance;
- geofence evidence;
- configuration/authority limits;
- execution acknowledgement/reconciliation records.

**Engineering default:** version/timestamp all launch evidence and make expiry/change revoke activation.

## 5. Provider-specific documentary request: FORTUNA SK

Before FORTUNA production automation can be enabled, the project needs a governing document that answers all of the following in writing:

1. exact contracting legal entity and approved hostname/API base URL;
2. permitted authentication method and whether credential automation is allowed;
3. read endpoints/data categories permitted;
4. bet placement/cancel/edit capability permitted, if any;
5. rate/volume/concurrency limits;
6. permitted storage/history/replay period;
7. model training/derived analytics rights;
8. user display/commercial use rights;
9. redistribution/export restrictions;
10. security/incident obligations;
11. account suspension/termination effects;
12. retry/idempotency/reconciliation expectations for money-moving requests;
13. change-notification/versioning process;
14. whether operator consent/approval is needed for each production release.

A working endpoint, browser traffic, robots behavior, sportsbook footer, or a game-plan reference to regulator API access is **not** a substitute for this permission.

## 6. Required format of outside-counsel response

The fastest mergeable answer is a short memo or signed issue attachment using this table for each question:

| Field | Required value |
| --- | --- |
| Decision ID | `Q1` ... `Q10` |
| Decision | `NO_BLOCKER`, `NO_BLOCKER_IF`, `BLOCKER`, or `OUT_OF_SCOPE` |
| Product mode | analysis / read integration / supervised execution / bounded automation / marketing / consumer sale |
| Jurisdiction | Slovakia |
| Authority | statute/regulation/operator term/case/guidance with section/article and URL/document ID |
| Reasoning | concise application to the stated product facts |
| Conditions | technical/product/contract controls required |
| Owner | named role/person for every remaining condition |
| Evidence expiry | date/event that forces re-review |
| Counsel identity/date | reviewer and date |

A generic statement that the product is "legal" is insufficient because it cannot be wired to capability gates.

## 7. WP-L07 no-blocker acceptance gate

WP-L07 is ready to close only when all of these are true:

- Q1-Q10 each have a written scoped decision from qualified outside counsel or are explicitly marked non-applicable with reasoning;
- every `NO_BLOCKER_IF`/`BLOCKER` has an implementation/contract owner and objective exit evidence;
- FORTUNA or any first provider has explicit machine/data permission for the exact production actions being enabled;
- the memo identifies the statute/terms temporal versions relied on;
- no conclusion relies on endpoint reachability as permission;
- launch configuration can point to the counsel memo and provider/data evidence versions;
- any unresolved launch blocker keeps the affected capability disabled.

## 8. Counsel sign-off template

```text
AUTOSPORT — SLOVAKIA LAUNCH LEGAL CLEARANCE
Date:
Counsel / firm:
Product facts version: docs/SLOVAKIA_COUNSEL_HANDOFF.md @ <commit>
Legal matrix version: docs/SLOVAKIA_LEGAL_RISK_MATRIX.md @ <commit>
Jurisdiction: Slovakia
Provider(s):
Provider/data agreement version(s):

Q1 Regulatory classification: <decision + citation>
Q2 Licensed-provider reliance: <decision + citation>
Q3 Machine access/execution terms: <decision + citation>
Q4 Data/database rights: <decision + citation>
Q5 Promotion/affiliate boundary: <decision + citation>
Q6 Consumer contract: <decision + citation>
Q7 Performance claims: <decision + citation>
Q8 Privacy: <decision + citation>
Q9 Territory/geofence: <decision + citation>
Q10 Audit retention: <decision + citation>

OVERALL:
[ ] NO_BLOCKER for the explicitly listed enabled capabilities
[ ] NO_BLOCKER_IF — conditions below
[ ] BLOCKED — blockers below

Conditions/blockers with owner and objective closure evidence:
- ...

Re-review triggers / expiry:
- ...
```

No checkbox in this template is self-executing. Engineering activation must separately verify the referenced evidence and product/safety gates.