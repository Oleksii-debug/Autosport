# Development-only bounded recovery probe

The completed second a1869a94 synthetic campaign spent 198 of 230 decisions in
WAIT after negative initial rewards, preventing any further on-policy learning.
Test one predeclared option without changing the running candidate or its state.

Compare the existing v3 abstention rule against an experimental subclass on the
same 181 synthetic snapshots (180 decisions). Add only one exploratory PAPER ticket
when the base rule already selects WAIT and the generation is a positive multiple
of 40. Select the least-observed strategy within the currently eligible family;
lexical tie-break. The existing PaperBook and hard stake limits still apply. The
schedule bounds additional attempts to at most four on this horizon. No hidden
outcomes or unexecuted/counterfactual rewards are fed to the policy.

Use three fixed deterministic fixtures: continually losing, continually favorable,
and a regime that begins losing and becomes favorable halfway through. Report
both outcomes, drawdowns, ticket/WAIT counts and all observations; do not select
parameters or erase regressions. This is synthetic line-movement settlement and
only establishes behavior of this experimental sandbox design. The latest
historical 2-hour runs use a different sample and are not a matched control.
Economic profitability, statistical significance, promotion, real outcomes and
real money remain unproved and disabled. Future changes require separate scientific
and policy identity plus restart verification; this experiment does not deploy them.
