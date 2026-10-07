# AGENTS.md

## Canonical autonomous worker policy

This root file is the highest-priority repository instruction for autonomous worker coordination. It supersedes older conflicting worker-count, coordinator-count, WIP, ownership/claim, serialization, exclusive-integration, PR-order, and CI-wait restrictions.

There is no repository-defined worker, coordinator, WIP, work-package, branch, or PR cap. Claims, leases, ownership, assignments, queues, and coordinator labels are advisory and do not block useful non-conflicting work.

Workers may create branches, commits, pull requests, tests, fixes, integration commits, and merges when GitHub permissions allow and the change is honestly verified. Parallel work must obey the Sequential closure authority below. Queued or pending CI is not by itself a reason to terminate; continue non-conflicting work on the current closure front or dependency-safe preparation for the next front. A blocked primary front may be left only after all safe internally controllable residual work is exhausted and the blocker is durably recorded.

Do not idle because another PR, branch, worker, check, review, claim, or queue is active. If overlap occurs, switch to non-conflicting work or reconcile/rebase instead of abandoning the run. No repository-defined exclusive integration owner is required. Use the full execution window while useful work remains.

This changes coordination rules only. Existing product correctness, security, accessibility, licensing, privacy, and truthful verification requirements remain mandatory. Do not bypass externally enforced GitHub permissions or branch protections.

Chat history is temporary. Preserve meaningful progress in repository state.

## Sequential closure authority — owner directive 2026-10-07

This section is the controlling coordination rule if any older repository text, worker prompt, issue, roadmap note, swarm rule, claim/ownership rule, or “parallel lane” instruction conflicts with it.

- Read the current canonical ordered Section plan and the live repository state before choosing work.
- The numerically earliest Section that is not honestly closed is the primary closure front. The next unfinished Section may be prepared only when this is dependency-safe, directly removes a dependency, or the primary front is genuinely non-actionable after all safe internal residual work is exhausted.
- Parallel workers are allowed, but parallelism does not authorize skipping the ordered closure front. Workers should take non-overlapping residuals of the same current front or dependency-safe preparation for the next front.
- A Section or Subsection may be marked DONE only after its required implementation/integration and applicable tests, negative/failure/recovery evidence, accessibility/security/performance/packaging evidence, and exact durable source state are satisfied.
- Every DONE Section/Subsection must be recorded durably in GitHub before the worker moves on. Use `SEQUENTIAL_CLOSURE_STATE.md` plus the repository’s existing canonical issue/status/control records when applicable.
- Once a Section/Subsection is durably recorded DONE, workers MUST NOT routinely re-enter it for reimplementation, polishing, re-auditing, or repeat verification. Skip it and work on the earliest unfinished Section.
- A closed Section/Subsection may be reopened only for a demonstrated regression, invalidated closure evidence, changed acceptance contract, or a later integration change that demonstrably broke it. Record `REOPENED` and the exact reason before new work begins.
- If two workers close the same scope concurrently, keep one canonical closure lineage/evidence set. Converge any unique necessary changes, then close/supersede the duplicate PR/branch/issue; delete the duplicate branch when safe. Never count one Section twice.
- Before creating a new PR/branch for the current Section, inspect existing active lineages and reuse/repair/converge them when possible.
- External blockers do not justify false DONE. Finish all internally controllable work, record the blocker precisely, and proceed only to dependency-safe work.

Chat history is not closure authority. Durable GitHub state is.
