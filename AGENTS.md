# AGENTS.md

## Canonical autonomous worker policy

This root file is the highest-priority repository instruction for autonomous worker coordination. It supersedes older conflicting worker-count, coordinator-count, WIP, ownership/claim, serialization, exclusive-integration, PR-order, and CI-wait restrictions.

There is no repository-defined worker, coordinator, WIP, work-package, branch, or PR cap. Claims, leases, ownership, assignments, queues, and coordinator labels are advisory and do not block useful non-conflicting work.

Workers may create branches, commits, pull requests, tests, fixes, integration commits, and merges when GitHub permissions allow and the change is honestly verified. Dependency order constrains final integration only and must not stop independent work. Queued or pending CI is not a reason to terminate; record it and continue with another useful independent task. A blocked first workline is not a reason to terminate. STATUS: BLOCKED is permitted only after all reasonably available independent work is exhausted.

Do not idle because another PR, branch, worker, check, review, claim, or queue is active. If overlap occurs, switch to non-conflicting work or reconcile/rebase instead of abandoning the run. No repository-defined exclusive integration owner is required. Use the full execution window while useful work remains.

This changes coordination rules only. Existing product correctness, security, accessibility, licensing, privacy, and truthful verification requirements remain mandatory. Do not bypass externally enforced GitHub permissions or branch protections.

Chat history is temporary. Preserve meaningful progress in repository state.
