# AUTOSPORT — BINDING WHOLE-PRODUCT COMPLETION AUTHORITY

## Owner correction

This document records the latest explicit owner instruction and overrides any older wording in repository documents, issues, task templates, worker prompts or reports that treats `V1`, `V1.1`, `V2`, `V3`, `V4`, `post-V1`, `first version`, `proof release`, `paper-only first release`, or any similar label as a separate product goal or as a stopping/optimization target.

There is **one product, one target, one completion definition**:

> **Finish the complete final Autosport product from A to Z.**

Workers must optimize only `TIME_TO_WHOLE_FINISHED_PRODUCT`.

## Forbidden interpretation

The following is forbidden:

- treating a “first version” as a separate target;
- reporting readiness/progress as “V1 readiness” versus “whole product readiness”;
- stopping work because a paper/replay Windows milestone is packaged;
- prioritizing a smaller version at the expense of the whole finished product;
- using historical version labels to redefine owner scope;
- presenting `V1_READY` or any similar legacy flag as the product-completion definition;
- using a stale whole-product percentage merely because an earlier audit recorded it.

Historical `V1/V2/V3/V4/post-V1` wording may remain in old commits/issues only as historical evidence. It has no authority to split the product or to create a separate completion target.

## Allowed sequencing

Development may still use internal stages, dependencies, gates and safe rollout order where technically necessary (for example paper validation before real-money execution, supervised execution before bounded autonomy, machine accessibility checks before physical NVDA acceptance). These are **safety/dependency stages inside the same final product**, not separate versions or products.

A stage is never a finish line. Completion means the entire final Autosport capability set required by the owner is implemented, integrated, tested, accessible and productized.

## Progress reporting

Any future progress estimate must:

1. refer only to the **whole final Autosport product**;
2. be recomputed from current live capability truth rather than copied from an older percentage;
3. explain what materially changed since the previous estimate;
4. not infer progress from raw PR/commit/test counts alone;
5. never invent or report a separate “V1 percentage”.

## Canonical product direction

The single final Autosport product includes the full intended chain:

`lawful data -> pre-match/live market state -> research/forecast/other opportunity intelligence -> multi-agent decision -> whole-portfolio economics/risk -> endogenous stake/stake-vector/WAIT/ZERO -> live opportunity search -> bookmaker capability/read -> supervised execution -> real execution ledger/reconciliation -> bounded autonomous execution under owner limits -> settlement -> evaluation -> continual causal learning/research -> accessible Windows product`

This same final product must preserve Ukrainian-first accessibility, keyboard/NVDA usability, restart/recovery, causal no-future-leakage, exact money semantics, provenance, lawful data/automation boundaries and fail-closed financial authority.

## Worker law

Before selecting work, every worker/auditor must interpret Issue #1, #198, #362, #368 and technical documents through this correction. If older text conflicts, **this owner correction wins**.

Do not create a second architecture or rewrite working components merely to rename historical labels. Continue live useful lineages, but evaluate every next action by contribution to the one whole finished product.
