# Autosport — Plan 5 Section 8 Windows-lab source-level contract

Contract: `PLAN5_SECTION8_LAB_V1`

## Role and authority

- The trusted controller is the default-branch-only, owner-manual, **nonexecuting** `.github/workflows/windows-lab-contract.yml`. It emits a bounded ticket; it does **not** run an interactive agent, install an application, or authorize actual bookmaker/financial execution.
- The separate interactive agent is a **future** dedicated ephemeral **non-admin** Windows VM process. It must be configured outside the repository with no host user-file mounts, no private user/browser profiles, no external bookmaker credentials, and no ability to check out arbitrary pull-request/fork code.
- Source-level separation is enforced by `WindowsLabTicket`: one exact repository, main-ref, dispatch event, source commit (40 lowercase hex), package SHA-256 (64 lowercase hex), ordered fixed keyboard/GUI/browser/emergency/restart scenarios, one immutable ticket ID.
- The SHA-256 passed to the manual workflow is a **declared digest**, not independent proof that a package was fetched or hashed. Independent package-byte verification and VM provenance authentication must occur before an external agent is trusted. This workflow cannot infer that acceptance from user input.
- `WindowsLabCampaign` stores only five bounded scalar observations and digests; no screenshots, environment variables, command-lines, tool output, user files, API tokens or raw exception strings. Agent status `PASS` is always `UNVERIFIED_AGENT_EVIDENCE`, not `NVDA_VERIFIED`, `HUMAN_TESTED`, hardware capacity or execution permission.

## Fixed lab scenarios

1. `desktop_start_stop`: keyboard-only bounded START/STOP without financial effects.
2. `keyboard_semantic_navigation`: semantic accessible labels, stable focus, escape/keyboard operation.
3. `native_emergency_stop`: native STOP reachable independent of web content.
4. `packaged_restart_recovery`: durable restart, checkpoint and duplicate-effect fencing.
5. `browser_semantic_readback`: independent screen-reader-readable status and errors.

Those names are **contracts for a later qualified interactive VM agent**, not claims that this source-only controller ran physical NVDA.

## Security, failure and restart

- No arbitrary task/command or repository-ref input; no public-fork execution; trusted manual workflow runs only `main` code with `contents:read`, `persist-credentials:false`, pristine exact-SHA checkout.
- Only an exact set of scenario names in canonical order is allowed. Invalid types, booleans, elevated flags, hostile JSON, duplicate keys, mixed source/package identity and wrong dispatch reject without printing private input.
- One active immutable ticket per restored campaign. At-least-once duplicate observation is idempotent; conflicting replay is rejected for independent reconciliation. Crash/restart can reconstruct the same digest from canonical JSON; no blind replay of an external GUI effect.
- Evidence is capped at 16 KiB per JSON document, maximum five observations, with deterministic canonical SHA-256. No persistence or network transport of raw operational logs.
- CLI `scripts/windows_lab_contract.py` mints/validates source-only tickets or validates untrusted returned campaigns on bounded standard input; it never performs the interactive tasks.
- This isolation contract is a **necessary source-level fence**, not proof of independently enforced OS permissions in a real user-owned machine. Actual private VM installation, interactive UIA/NVDA and package signature trust are downstream integration/final evidence classes.

## Test/qualification contract

- `tests/test_windows_lab_contract.py`: source/package identity, invalid owner/ref/fork, scenario authority, duplicate/malformed/oversized JSON, canary redaction, concurrency, bounded memory, crash/restart in fresh subprocess, conflicting receipt rejection, no self-promotion.
- `tests/test_windows_lab_controller_cli.py`: real CLI issuance/readback, malformed/oversized stdin, deliberately false PASS privilege claims, default-branch-only workflow static guarantees.
- Existing CI on Ubuntu and Windows (Python 3.11/3.12) and Windows package build qualify the exact source candidate. Pending/skipped jobs are not PASS. Closure requires actual CI results, safe integration and main readback.
- `REAL_MONEY_EXECUTION=false`, `HUMAN_TESTED=false`, `NVDA_VERIFIED=false`, `WHOLE_PRODUCT_COMPLETE=false`.
