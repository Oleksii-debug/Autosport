# Fixed synthetic regime-change robustness experiment

The 37- and 40-step recovery probes were selected after prior development
fixtures. This is exploratory synthetic stress, not an untouched holdout.

Compare the current rule and both research probes on identical 180-decision
synthetic quote streams. Use fixed seeds 211, 223, 227, 229 and two families:
continuing unfavorable movement and a single transition to favorable movement.
The first 60–90 steps are unfavorable, with small fixed-seed variations and
unchanged observations; recovery occurs only in the second family. Report every
seed, arm, loss, drawdown, tickets and WAIT. Preserve the common snapshot hash.
No live code, policy memory, risk cap or executed ticket is modified.

The simulation determines win/loss from subsequent price direction, not verified
match outcomes. Cost assumptions, real fill and live edge remain unverified.
The output cannot authorize promotion or a claim of profitability.
