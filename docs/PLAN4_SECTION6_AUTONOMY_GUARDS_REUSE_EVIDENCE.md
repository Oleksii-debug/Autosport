# Plan 4 Section 6 — Bounded autonomy guard engineering: terminal offline evidence

**Closure date:** 2026-10-09. **Scope:** provider/execution component engineering with recorded/synthetic inputs; no bookmaker credentials, real account, money movement, or autonomous live authorization. **Method:** REUSE / QUALIFY / INTEGRATED READBACK. No duplicate execution authority introduced.

## Acceptance mapping
- **6.1 hard envelope / STOP / token and revalidation.** Canonical `execution_stop_authority.py` provides durable STOP/ARM journal, locked admission lease, explicit operator command/revision, integrity and monotonic workspace binding. `execution_scope_admission.py` binds action/provider/account/environment/market/settlement/strategy/time evidence and never issues a positive provider write token. `supervised_execution.py` binds exact plan, quote, provider profile, approval and readback; `risk.py` and `owner_economic_authority.py` maintain owner-backed Decimal stake, capital-at-risk, simultaneous-position, loss, day and drawdown ceilings.
- **6.2 never expand owner/Risk permissions.** `tests/test_economic_goal_risk_binding.py` checks strictest ceiling wins, ZERO limits block positive stake, historical/current exposure and emergency STOP. `tests/test_execution_scope_admission.py` checks even apparently favorable caller evidence cannot mint write entitlement, undocumented/unknown provider action fails closed, and changed jurisdiction/sport/mode/semantics/identity produce explicit blockers.
- **6.3 external autonomy activation.** Positive autonomous real-money authority is NOT exposed/qualified by this section; actual owner approval, external account/entitlement and real effects are Plan 8. PAPER evidence is not real authorization.

## Frozen executed gates / byte-identical integrated source
Fully executed frozen PR #2274 **`4ea79138873b37f016162b69cde24196e5258c80`**:
- CI https://github.com/Oleksii-debug/Autosport/actions/runs/37904783985 — SUCCESS, full test jobs Ubuntu/Windows Python 3.11/3.12.
- Windows Candidate https://github.com/Oleksii-debug/Autosport/actions/runs/37904784003 — SUCCESS.
- Endurance https://github.com/Oleksii-debug/Autosport/actions/runs/37904784053 — SUCCESS.
- Tested source to main `f51dab5c63e3649352064e4b20ec760c62c16ff5`: compare ahead=163, behind=0, no Section-6 production/test file changes. Independently fetched identical blob SHAs on BOTH refs:
  - `execution_stop_authority.py` `630defbae9eaf6ddbff7ba0bde849109e275c131`
  - `execution_scope_admission.py` `f5e181c7eece3961a4f4be6bf45fe2ec3d3af9f9`
  - `supervised_execution.py` `a440f31f9313811e482acc0e0d8bcfd99dba7f3f`
  - `risk.py` `9f092f2e0f8e38a8deb574949ee309cad831f671`
  - `owner_economic_authority.py` `67eae17b35041e2586184fa54f33639dcff53380`.

## Executed negative, idempotence and recovery battery (byte-identical)
- `test_execution_stop_authority.py` `dd67143ba2d680501be7cdaf292bb1406e52b7ed`: default STOP, stale revision, repeated commands/confirmation replay, corrupt/torn journal, duplicate JSON keys, tampered anchor, safe reopen.
- `test_execution_stop_admission_lease.py` `22fd77259d6215d9cc5b6ff06dcc1122f8dde9f9`: one lease/lock fence, integrity failure and same-file replacement.
- `test_execution_stop_process_kill_recovery.py` `f13f14f0a43960978e37311fd0f9c076e3757397` and `test_execution_stop_torn_commit_recovery.py` `d6057e4936efc16da706a9fa55bc15cf4b3cbbcc`: process kill/partial commit, safe STOP persistence and no unsafe auto-reARM.
- `test_execution_stop_monotonic_authority_loss.py` `eb8d0a76d42d4ad8e438eaf9e27d53fa3746bee2`, `test_execution_stop_valid_old_rollback.py` `4c2115eddbb92b2ee31fa47ddc9fc16e7ad5774c`: rollback and binding-falsification fail closed.
- `test_execution_scope_admission.py` `37de6fc50f70774b6fac08e50129e010654d96ce`: no caller-minted write, exact scope and temporal evidence.
- `test_economic_goal_risk_binding.py` `11553900af9eeb8d5775cecc34c977163ae95f8b`: Risk/owner limits tighten but never expand.
- `test_supervised_execution.py` `dd9f4d32372b04d41a37aa3869a5481edceb9485`: approval, confirmation/readback semantics.

**Outcome:** ALL repository-controllable offline Section-6 acceptance covered by existing integrated canonical implementations with executed exact-source CI. No new code, privilege path, real-money action or live-key material. **REAL_MONEY_EXECUTION=false**. Human NVDA and external authenticated/real-account qualification explicitly remain Plan 8 and are not claimed. Section 6: **DONE** under AGENTS.md v3.
