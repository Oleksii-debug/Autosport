# Version policy

Autosport versions are integration/release checkpoints, not sequential development silos. Whole-product work is allowed at all times when ownership is disjoint and the work preserves current contracts. A feature may be architected or implemented before the milestone where it becomes release-blocking.

The default prioritization rule is: maintain a runnable current slice; remove nearest integration blocker; advance high-leverage shared foundations; parallelize independent long-lead work; package Windows early; keep accessibility/performance/persistence under continuous regression. Do not postpone all Windows, agent, portfolio or performance work merely because v0.1 is not yet tagged.

A milestone tag means the integrated repository satisfies that checkpoint's required evidence. It does not mean later-area code was forbidden before the tag.
