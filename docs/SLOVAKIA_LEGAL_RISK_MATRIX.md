# Slovakia legal-risk matrix

**Work package:** WP-L06  
**Evidence cut:** 2026-09-21  
**Launch market:** Slovakia (EU/EEA first-market slice)  
**Status:** counsel-ready evidence package; unresolved legal questions remain fail-closed  

This document is an engineering launch-control artifact, not legal advice. It records primary-source evidence, the product interpretation that engineering must currently enforce, and the questions that require written outside-counsel confirmation before real-money activation. A row marked `OPEN_COUNSEL` is not permission to ship that capability.

## Product boundary used for this review

The binding product authority describes Autosport as a Windows sports-analysis and portfolio/risk system that may eventually perform supervised and then bounded real-money execution, but only after legal, safety, and evidence gates. The current legal review therefore distinguishes:

1. **analysis/read-only** — ingest lawful market data, analyse pre-match/live state, and produce `WAIT`/`ZERO`/candidate decisions;
2. **account/read integration** — authenticate to a bookmaker account and reconcile balances/open/settled positions;
3. **execution** — submit or modify money-moving betting actions;
4. **promotion/affiliate activity** — communications that may encourage participation in gambling.

No conclusion below converts one category into another. Real-money execution remains disabled unless a separate launch authority explicitly proves every required gate.

## Status legend

- `RESOLVED_SOURCE` — primary authority/source is identified and the engineering default is unambiguous enough to implement fail-closed.
- `OPEN_COUNSEL` — primary sources are identified, but legal classification/permission must be confirmed in writing by Slovak/EU counsel.
- `OPEN_CONTRACT` — operator/data-owner permission is not evidenced by a public source; do not infer it from technical reachability.
- `BLOCK_LAUNCH` — capability must remain disabled until the cited evidence is supplied.

## Launch-gate matrix

| ID | Domain | Primary authority / section | Evidence | Engineering interpretation / default | Status | Mitigation / control | Owner | Counsel decision needed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SK-LIC-01 | Gambling licensing boundary | Act No. 30/2019 Coll. on Gambling, §§ 1, 4, 35, 39 | Slov-Lex current version: https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2019/30/ | The Act regulates operating and promoting gambling; gambling may be operated only under the statutory licence framework. Autosport must not represent itself as the gambling operator or independently accept/hold stakes or pay winnings unless counsel concludes that the actual design is licensed/permitted. | `OPEN_COUNSEL` | Keep sportsbook as external regulated counterparty; no pooled customer money; no operator-of-record language; real-money path disabled pending written classification. | Legal + Product | Does the planned account-linked execution model make Autosport an operator, agent/intermediary, supervised subject, or other regulated participant under Slovak law? |
| SK-REM-01 | Remote/internet gambling | Act No. 30/2019 Coll., § 30; licence categories in § 35(b)(13)-(15); application requirements §§ 63-65 | Slov-Lex: https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2019/30/ | Internet betting is specifically regulated. Integration must target only an operator/website whose current Slovak authority is verified for the intended product at launch time. | `RESOLVED_SOURCE` + `OPEN_COUNSEL` | Provider allowlist; hostname + legal-entity + licence evidence pin; fail closed if evidence expires/changes; no fallback to an unverified provider. | Compliance + Engineering | Confirm whether using a licensed operator through the user's own account changes Autosport's regulatory classification and whether any additional authorization/registration is needed. |
| SK-REM-02 | Licensed-provider verification | ÚRHH current registers and legal-web list | Individual-licence register (valid-to 20.09.2026 at evidence cut): https://www.urhh.sk/licencie-2/registre-zoznamy-a-ciselniky/zoznam-udelenych-individualnych-licencii/ ; central operator register: https://www.urhh.sk/licencie-2/registre-zoznamy-a-ciselniky/centralny-register-prevadzkovatelov-hazardnych-hier/ ; legal websites: https://www.urhh.sk/licencie-2/legalne-webove-stranky/ | A remembered brand name is not sufficient evidence. The exact legal entity, web hostname and licence scope must be checked against regulator evidence close to activation. | `RESOLVED_SOURCE` | Store evidence URL, retrieval timestamp, legal entity, hostname, licence identifier/scope and review expiry in provider capability profile. | Compliance + Data/Engineering | What refresh interval and evidence-retention period does counsel require for licence/website verification? |
| SK-PROMO-01 | Gambling promotion / affiliate surface | Act No. 30/2019 Coll., § 4(3)-(4) and definition of `propagovanie hazardnej hry` in § 2 | Slov-Lex: https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2019/30/ | Product copy, provider rankings, affiliate links, bonus messaging, win messaging and calls to bet can cross from neutral analytics into regulated gambling promotion. No such surface is launch-safe by assumption. | `OPEN_COUNSEL` | Default: no affiliate/bonus inducements; neutral provider identity only where necessary for functionality; separate marketing review before any promotional content. | Legal + Product/Marketing | Which Autosport UI/notifications/links constitute gambling promotion, and what Slovak content/placement restrictions apply? |
| SK-AUTO-01 | Automated account access / bet placement | Operator contract + approved integration terms are required in addition to public law | FORTUNA SK current general gambling terms (effective 15.04.2026): https://www.ifortuna.sk/file/69cbc98bb87c125e6c5b430c ; current public game-plan material: https://www.ifortuna.sk/file/695ba1e87a17ba1f5b6ac2a6 | Technical access is not contractual permission. Public game-plan text mentions API connectivity for the regulator's terminal; that is **not evidence of a public or third-party betting API licence** for Autosport. | `OPEN_CONTRACT` + `BLOCK_LAUNCH` | No reverse-engineered placement; no credential automation against undocumented endpoints; allow real execution only for a documented operator-approved integration mode whose terms permit the exact read/write actions. | Partnerships/Data + Legal + Engineering | Obtain and cite operator permission covering authentication, automated reads, rate/volume, storage, derived analytics, placement, retries/reconciliation and production use. |
| SK-DATA-01 | Market/data terms and database rights | Directive 96/9/EC, Art. 7; operator/data-vendor contract | EUR-Lex consolidated Directive 96/9/EC: https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:01996L0009-20190606 | Odds/market feeds can be subject to contractual limits and EU database rights. Repeated/systematic extraction cannot be treated as free merely because a web/API endpoint is reachable. | `OPEN_CONTRACT` + `OPEN_COUNSEL` | Prefer licensed feed/API; retain source licence/terms version and permitted-use scope; rate-limit and collect only allowed fields; block production ingest when rights provenance is missing. | Data Partnerships + Legal | For each source, are collection, storage, replay, model training, derived signals, redistribution/display and commercial use permitted? Does the planned cadence implicate database-right restrictions? |
| SK-DATA-02 | Fortuna source-specific data permission | FORTUNA SK terms/game plan; any separate written API/data agreement | General terms: https://www.ifortuna.sk/file/69cbc98bb87c125e6c5b430c ; game-plan source: https://www.ifortuna.sk/file/695ba1e87a17ba1f5b6ac2a6 | The reviewed public documents establish operator/player rules and describe internal/regulator API use, but do not establish Autosport's right to scrape or use undocumented `api.ifortuna.sk` endpoints as a production data feed. | `OPEN_CONTRACT` + `BLOCK_LAUNCH` | Treat `ifortuna.sk`/`api.ifortuna.sk` as **no production automation permission evidenced** until a written agreement or clearly applicable published API licence is attached. | Partnerships + Legal | Does FORTUNA SK authorize Autosport's intended machine access and downstream uses? If yes, obtain the governing document/version and technical limits. |
| SK-CONS-01 | Consumer information / digital-service sale | Act No. 108/2024 Coll. on Consumer Protection, current version at evidence cut; especially §§ 4-5 and distance-contract duties in §§ 15-20 | Slov-Lex current version (31.07.2026-26.09.2026 at evidence cut): https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2024/108 | If Autosport is sold to Slovak consumers, trader identity, main service characteristics, total pricing/fees and required pre-contract/distance-contract information must be clear before purchase/order. | `RESOLVED_SOURCE` + `OPEN_COUNSEL` | Pre-purchase disclosure checklist; durable contract confirmation where required; no hidden fees; versioned terms/privacy copy; explicit treatment of digital-content/service withdrawal rules. | Product + Legal | Classify Autosport's paid offering (service/digital content/mixed), confirm withdrawal/early-performance flow, complaints/ADR duties, and any gambling-related exclusions that do or do not apply to this analytics service. |
| SK-CONS-02 | Marketing claims / expected returns | Act No. 108/2024 Coll., unfair commercial-practice rules including §§ 9-12 | Slov-Lex: https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2024/108 | Profitability, win-rate, ROI, "risk-free", "guaranteed", model quality and comparative claims must be evidence-backed and must not omit material risk/limitations. | `RESOLVED_SOURCE` | Claims registry with evidence link + evaluation period; forbid guaranteed-profit wording; show uncertainty, costs/slippage/data limitations and historical-vs-live distinction. | Product + Compliance | Counsel to approve claim taxonomy and mandatory risk disclosures before paid marketing. |
| SK-PRIV-01 | Personal data | GDPR Arts. 5, 6, 13 (and Art. 22 only if its conditions are actually met) | EUR-Lex Regulation (EU) 2016/679: https://eur-lex.europa.eu/eli/reg/2016/679 | Account identifiers, location/geofence evidence, usage telemetry and linked-bookmaker data can be personal data. A lawful basis, transparent notice, minimisation, retention and security are required; consent is not a universal default lawful basis. | `OPEN_COUNSEL` | Data inventory/DPIA screening; purpose/retention mapping; least-privilege secrets; do not store bookmaker credentials unless integration design requires and protects them; versioned privacy notice. | Privacy + Security + Engineering | Confirm controller/processor roles, lawful basis per purpose, DPIA need, cross-border processors/transfers, retention and whether any user-facing automated-decision safeguards apply. |
| SK-GEO-01 | Territorial launch control | Act No. 30/2019 Coll., § 1(3) scope applies to gambling available in Slovakia; provider legal status is territorial | Slov-Lex: https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2019/30/ | A Slovakia launch decision cannot be generalized to other states. A provider legal in Slovakia does not establish legality elsewhere. | `RESOLVED_SOURCE` | Market allowlist defaults to Slovakia only for this package; fail closed on uncertain territory; every new jurisdiction requires its own evidence package. | Compliance + Engineering | Confirm acceptable geolocation/re-residency evidence and edge cases (travel, VPN, cross-border users, account domicile). |
| SK-AUDIT-01 | Evidence / auditability | Engineering governance + legal evidence-control requirement (not itself a claim that statute mandates Autosport's internal ledger) | Binding project authority: `docs/MASTER_TECHNICAL_PROJECT.md`; source authorities above | Every irreversible activation decision must be reproducible from the legal/provider evidence it relied on. | `RESOLVED_SOURCE` | Persist evidence IDs/versions/timestamps with provider capability and activation decision; evidence expiry causes `WAIT`/disable, not silent fallback. | Engineering + Compliance | Counsel to set minimum retention for launch approvals, provider terms snapshots and user consents. |

## Source inventory and verification notes

### A. Slovak gambling statute

- **Act No. 30/2019 Coll. on Gambling** — Slov-Lex currently exposes a version effective from **01.03.2026** at the evidence cut. Relevant review anchors are §§ 1, 2, 4, 30, 35, 39 and 63-65.  
  https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2019/30/
- The statute defines regulated gambling/promotion, requires licensing for operating gambling, and separately regulates internet games and licence categories. This matrix intentionally does **not** decide by itself whether Autosport's planned execution mechanics cross the operator/intermediary boundary; that is a counsel question.

### B. Slovak regulator evidence

Úrad pre reguláciu hazardných hier (ÚRHH) publishes the authoritative operational registers used by this package:

- current individual-licence register — page states data updated/valid to **20.09.2026** at evidence cut:  
  https://www.urhh.sk/licencie-2/registre-zoznamy-a-ciselniky/zoznam-udelenych-individualnych-licencii/
- central operator register — page states data updated/valid to **20.09.2026** at evidence cut:  
  https://www.urhh.sk/licencie-2/registre-zoznamy-a-ciselniky/centralny-register-prevadzkovatelov-hazardnych-hier/
- legal internet websites list:  
  https://www.urhh.sk/licencie-2/legalne-webove-stranky/

The regulator list is the launch source of truth; a stale historical PDF or a sportsbook footer is corroboration, not a substitute for the current register.

### C. FORTUNA SK / ifortuna.sk documents reviewed

- General gambling terms, marked effective **15.04.2026**:  
  https://www.ifortuna.sk/file/69cbc98bb87c125e6c5b430c
- Public game-plan material describing `www.ifortuna.sk` and the betting system; it states that online access between the regulator's endpoint and the system uses an exposed API/HTTPS/VPN path:  
  https://www.ifortuna.sk/file/695ba1e87a17ba1f5b6ac2a6

**Important evidence rule:** the second item cannot be cited as permission for Autosport to automate the operator website/API. It describes a specific regulator/system connection. Until a governing public API licence or written operator agreement is attached, automated production reads/writes remain blocked.

### D. Consumer protection

- **Act No. 108/2024 Coll. on Consumer Protection**, Slov-Lex version effective **31.07.2026-26.09.2026** at this evidence cut:  
  https://www.slov-lex.sk/ezbierky/pravne-predpisy/SK/ZZ/2024/108

The launch implementation must re-check the effective temporal version before release; the law page already shows another temporal version beginning 27.09.2026.

### E. EU data/privacy law

- GDPR, Regulation (EU) 2016/679:  
  https://eur-lex.europa.eu/eli/reg/2016/679
- Directive 96/9/EC on the legal protection of databases, consolidated text; Art. 7 is the relevant first-pass database extraction/re-utilisation anchor:  
  https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:01996L0009-20190606

## Launch rule derived from the matrix

`REAL_MONEY_EXECUTION` for Slovakia must remain `false` unless **all** of the following evidence is attached to a release decision:

1. current ÚRHH evidence proves the exact bookmaker legal entity/hostname/licence scope is valid for the intended interaction;
2. written outside counsel answers the regulatory-classification, promotion, consumer, privacy and territorial questions in `SLOVAKIA_COUNSEL_HANDOFF.md` with either `NO_BLOCKER` or explicit owned mitigations;
3. operator/data-owner terms or written permission authorize the intended machine access and data uses; mere endpoint availability does not count;
4. product disclosures/privacy/claims are approved for the actual paid offering;
5. provider capability configuration carries evidence identities and expiry; missing/stale/ambiguous evidence fails closed to no activation.

## Evidence maintenance

This matrix is time-sensitive. Before any launch candidate:

- re-open the **current temporal version** of Slov-Lex statutes;
- re-check ÚRHH registers and legal website list;
- hash/archive or otherwise durably identify the exact operator terms/data agreement relied on;
- record counsel memo identity/date/scope;
- invalidate approval if operator, product mechanics, monetisation, jurisdictions or data flows materially change.
