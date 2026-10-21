# Autosport — повний реєстр самостійних розділів у незавершених планах

**Дата:** 2026-10-13. План 6 (включно із закритим раніше S1), План 7 і План 8 повністю розбиті на рівноправні самостійні розділи. Нумерованих підпунктів немає. Плани 1–5 terminal DONE залишені без змін.

**Робочий контракт:** NА/NБ/NВ — самостійні розділи, НЕ підрозділи. Старе N у PR/CI/issue — агрегат нових, завершення лише після всіх окремих DONE, реальних перевірок та інтеграції. Підтвердження тестами не є реальним букмекерським виконанням, фінансовим допуском чи фізичною NVDA-атестацією. Чинні PR, source lineage, DONE та WAITING залишаються.

**Разом:** 66, раніше DONE: 3, не DONE: 63.

## План 6: 21 самостійних розділів

[Актуальний Google Doc](https://docs.google.com/document/d/1qWTi8eG9SDrNWvtUoYmJyIl92ySviTBRympJ3jvvJVo/edit)

| Розділ | Зміст | Стан |
| --- | --- | --- |
| 1А | uk-UA mandatory/default catalog for visible text, UIA Name/Description/status/errors/confirmations. | Статус: DONE — 2026-10-09; PR #1303 merged into main 8d0cca786ff5472e1ce4399084a9a67b5c781b7f; exact-head CI 37840613258 |
| 1Б | CI rejects missing critical Ukrainian keys/English leakage and semantic-label regressions. | Статус: DONE — успадковано з раніше прийнятого батьківського розділу 1; не відкривати. |
| 1В | Keyboard/NVDA semantics are source contracts, not final physical acceptance. | Статус: DONE — успадковано з раніше прийнятого батьківського розділу 1; не відкривати. |
| 2А | Reuse current windows_gui/windows_surface_contract/gui and PR #1598 WebView2 semantic shell lineage. | Статус: IN_PROGRESS / NOT DONE — 2026-10-09. Canonical WebView2 PR #1598 (head 5c5ea0bc67545b88ba31d13ee68f503cfbb5fa14) |
| 2Б | One predictable navigation model, headings/landmarks/controls/status, keyboard-first operation and text alternatives. | Статус: IN_PROGRESS / NOT DONE |
| 2В | Native emergency controls remain reachable if WebView/browser surface fails. | Статус: IN_PROGRESS / NOT DONE |
| 3А | Cover launch/config/source status/research/paper portfolio/risk/agent/models/STOP/recovery/settings/export/update flows. | Статус міграції: PARTIAL_EXISTING |
| 3Б | Unsupported external bookmaker actions visibly unavailable, not blockers for PAPER/SHADOW product. | **DONE — CLOSED_FOR_DEVELOPMENT / TERMINAL (2026-10-21)**. Canonical Windows shell shows unavailable bookmaker/opportunities with reason and disabled Open; PAPER/SHADOW available. 11/11 source/test Git blobs byte-identical to qualified `d93d203e867c71aad880f1b2e3d144549e166030` (CI #38148320018, Windows Candidate #38148320047 SUCCESS). Integrated source main `ae8376d1f35da65313fac45fe7efbca214575c35`. [Evidence](docs/PLAN6_SECTION3B_BOOKMAKER_UNAVAILABLE_REUSE_EVIDENCE.md). 3А/3В/WebView2 still open. |
| 3В | Focus restoration, error recovery and actionable status are deterministic. | Статус: PARTIAL_EXISTING |
| 4А | Reuse accessibility_audit/announcements/keyboard_audit/UIA provenance/evidence export. | Статус міграції: PARTIAL_EXISTING |
| 4Б | Automate labels/roles/focus/live-region/duplicate-id/malformed-state checks and selectable reports. | Статус: PARTIAL_EXISTING |
| 4В | Automated evidence never falsely claims physical NVDA verification. | **DONE — CLOSED_FOR_DEVELOPMENT / TERMINAL (2026-10-21)**. Automatic PASS/FAIL audit never claims human/NVDA verification. 11/11 exact Git source/test blob matches with qualified head `1ed86090b04dd5f3fcc6bfd83b63b791ed992c25` (CI #39542347381 + Windows Candidate #39542347333 SUCCESS), main source readback `48d3d3ea68da0ef5b3f5c14eb87f846e9f6602a9`. [Evidence](docs/PLAN6_SECTION4V_MACHINE_NVDA_TRUTH_REUSE_EVIDENCE.md). 4А/4Б & Plan 8 final physical acceptance stay open. |
| 5А | Reuse release_package/windows entry/start guide/build scripts and supply-chain metadata. | **DONE — CLOSED_FOR_DEVELOPMENT / TERMINAL (2026-10-21)**. 17/17 exact source/test/build Git blob identities against qualified head `d93d203e867c71aad880f1b2e3d144549e166030`; CI #38148320018 + Windows Candidate #38148320047 SUCCESS; integrated main readback `64542242c4002693f4310bcf25c78966444d4228`; [terminal evidence](docs/PLAN6_SECTION5A_WINDOWS_PACKAGING_REUSE_EVIDENCE.md). Remaining implementation for 5А = 0. 5Б/5В remain open. |
| 5Б | Deterministic package binds source/dependencies/config/migrations/licenses/SBOM without secrets/test state. | Статус: PARTIAL_EXISTING |
| 5В | Signing/update primitives qualify on fixture certificate/key material; final delivered signature Plan 8. | Статус: PARTIAL_EXISTING |
| 6А | Clean install, state migration, update, rollback, uninstall/reinstall and corruption/interruption recovery. | Статус міграції: PARTIAL_EXISTING |
| 6Б | Package/update never bypasses integrity policy or silently downgrades schema/state. | Статус: PARTIAL_EXISTING |
| 6В | Exact user data backup/restore interfaces coordinate Plan 5. | Статус: PARTIAL_EXISTING |
| 7А | Run localization/UI/accessibility/package/install/update tests on exact component candidate. | Статус міграції: OPEN |
| 7Б | Prove keyboard-only source workflow and emergency fallback without real bookmaker credentials. | Статус: OPEN |
| 7В | Record terminal DONE. | Статус: OPEN |

## План 7: 21 самостійних розділів

[Актуальний Google Doc](https://docs.google.com/document/d/19ifIaWknBedZBodtwjs3dG7uqVA_39hduUzox0A6P_Q/edit)

| Розділ | Зміст | Стан |
| --- | --- | --- |
| 1А | Перевірити terminal Plans 1,2,3,5,6 та terminal Plan 4 Sections 1,2,7 (capability registry, read-only/provider path, public web-lab) і exact contracts/evidence; інші Plan-4 execution Sections не блокують M1. | Статус міграції: WAITING_UPSTREAM |
| 1Б | Select one exact source/package candidate and freeze moving component set. | Статус: WAITING_UPSTREAM |
| 1В | Bookmaker/account states can be NOT_CONFIGURED/UNAVAILABLE without product failure. | Статус: WAITING_UPSTREAM |
| 2А | Compose lawful/public/historical collection, replay/live-like mirror, financial/PaperBook, intelligence, runtime, Windows UI and verification. | Статус міграції: WAITING_UPSTREAM |
| 2Б | One authority for chronology/storage/risk/portfolio/decision/evidence/learning; no duplicate schedulers or ledgers. | Статус: WAITING_UPSTREAM |
| 2В | Use public permitted sources/recorded fixtures where live site unavailable. | Статус: WAITING_UPSTREAM |
| 3А | Start from declared minimal dynamic knowledge and let Autosport collect/learn through its own causal loop. | Статус міграції: WAITING_UPSTREAM |
| 3Б | Run hours/days: collect→normalize→analyze→portfolio→PAPER decision→settle→learn→research→continue. | Статус: WAITING_UPSTREAM |
| 3В | No worker outcome hints or hand-picked betting advice injected into active campaign. | Статус: WAITING_UPSTREAM |
| 4А | Freeze predictions/policies before outcomes; track accepted-paper analogue, latency/costs, settlement, bankroll/drawdown and baseline comparisons. | Статус міграції: WAITING_UPSTREAM |
| 4Б | Strategy-specific evidence for predictive/live/discrepancy/arbitrage/dutching/hedging; no universal CLV shortcut. | Статус: WAITING_UPSTREAM |
| 4В | Result may PASS/FAIL/INCONCLUSIVE; implementation completion does not fabricate economic edge. | Статус: WAITING_UPSTREAM |
| 5А | Inject restarts/source gaps/model failures/browser failures/storage interruptions during same campaign identity. | Статус міграції: WAITING_UPSTREAM |
| 5Б | Resume from durable checkpoints without future leakage/duplicate actions and preserve memory/experiment lineage. | Статус: WAITING_UPSTREAM |
| 5В | Campaign dashboard/report shows uptime/restarts/data coverage/PAPER actions/bank/drawdown/errors/learning/costs. | Статус: WAITING_UPSTREAM |
| 6А | Run exact packaged candidate through keyboard-first start/status/research/paper/risk/STOP/recovery/export/update/rollback workflows. | Статус міграції: WAITING_UPSTREAM |
| 6Б | Automated accessibility and interactive test-agent evidence included; physical owner NVDA not yet required. | Статус: WAITING_UPSTREAM |
| 6В | No external real-money capability is advertised. | Статус: WAITING_UPSTREAM |
| 7А | Freeze exact source/package/campaign/evidence set, integrate and perform post-merge readback. | Статус міграції: WAITING_UPSTREAM |
| 7Б | No critical provider-free blocker remains; unresolved external/real execution scope explicitly deferred to Plan 8. | Статус: WAITING_UPSTREAM |
| 7В | Preserve learned memory/checkpoint only if causally/lawfully accumulated and provenance-valid. | Статус: WAITING_UPSTREAM |

## План 8: 24 самостійних розділів

[Актуальний Google Doc](https://docs.google.com/document/d/1DmEXcaIF4nff8iNLKFsV6CxexX1v7cEq8E8FCRq5bX8/edit)

| Розділ | Зміст | Стан |
| --- | --- | --- |
| 1А | Bind exact supported bookmaker/account/environment/capability profile to Plan-4 adapters. | Статус міграції: WAITING_EXTERNAL_PLAN4 |
| 1Б | Authenticated balance/market/open-position/read evidence, freshness, corrections and restart behavior; secrets protected. | Статус: WAITING_EXTERNAL_PLAN4 |
| 1В | If provider unavailable/forbidden, mark unsupported; no CAPTCHA/geo/security bypass. | Статус: WAITING_EXTERNAL_PLAN4 |
| 2А | Requires Plan 2 financial engine, Plan 4 execution/supervised machinery, M1 and explicit owner/account authorization. | Статус міграції: WAITING_OWNER_UPSTREAM |
| 2Б | Human confirmation revalidates exact current price/account/portfolio/risk; external receipt/accepted odds become truth. | Статус: WAITING_OWNER_UPSTREAM |
| 2В | Emergency STOP/cancel/reconcile and stale/partial/reject paths proven. | Статус: WAITING_OWNER_UPSTREAM |
| 3А | Bind actual external IDs/accepted odds/stakes/fills/partials/rejects/cancels/settlement/balance to Plan-2 financial truth. | Статус міграції: WAITING_SECTION2 |
| 3Б | Restart/reconciliation resolves UNKNOWN without blind retry; partial multi-leg exposure safe. | Статус: WAITING_SECTION2 |
| 3В | Corrections propagate causally into realized utility/learning. | Статус: WAITING_SECTION2 |
| 4А | Separate explicit owner authorization and tiny hard envelope required; not implied by general continue instruction. | Статус міграції: WAITING_OWNER_SECTION3 |
| 4Б | Model/learning cannot expand limits; independent risk/STOP and provider capability revalidation each effect. | Статус: WAITING_OWNER_SECTION3 |
| 4В | Long-enough bounded campaign proves safe recovery/reconciliation before scope expansion. | Статус: WAITING_OWNER_SECTION3 |
| 5А | Assemble exact claimed-scope package with final signatures/SBOM/provenance/licenses/migrations and provider capability matrix. | Статус міграції: WAITING_PLAN6_M1_EXTERNAL_SCOPE |
| 5Б | Clean install/update/rollback/uninstall/reinstall on release bytes; no unsigned fallback. | Статус: WAITING_PLAN6_M1_EXTERNAL_SCOPE |
| 5В | Package claims only capabilities whose evidence class is actually satisfied. | Статус: WAITING_PLAN6_M1_EXTERNAL_SCOPE |
| 6А | On exact signed artifact perform real keyboard-only/NVDA complete workflows, error/live-region/focus/zoom/high-contrast/emergency paths. | Статус міграції: WAITING_SECTION5 |
| 6Б | Use dedicated safe test session; manual finding repairs owning engineering plan then rebuild/requalify. | Статус: WAITING_SECTION5 |
| 6В | No source-level automation is substituted for this physical evidence. | Статус: WAITING_SECTION5 |
| 7А | Map every claimed capability to source/replay/PAPER/real/physical evidence, exact SHAs/packages/providers/accounts/sports/modes. | Статус міграції: WAITING_CLAIMED_SCOPE |
| 7Б | No unresolved critical blocker, hidden unsupported claim, duplicate authority or future leakage. | Статус: WAITING_CLAIMED_SCOPE |
| 7В | Operational docs, backup/recovery, diagnostics, support/repro packet complete. | Статус: WAITING_CLAIMED_SCOPE |
| 8А | Freeze exact source/package/evidence/memory/checkpoint/support bundle after all claimed required gates pass. | Статус міграції: WAITING_SECTION7 |
| 8Б | Perform clean-machine/readback and archive rollback predecessor. | Статус: WAITING_SECTION7 |
| 8В | Anything not authorized/available is explicitly NOT_AUTHORIZED/UNSUPPORTED rather than false DONE. | Статус: WAITING_SECTION7 |
