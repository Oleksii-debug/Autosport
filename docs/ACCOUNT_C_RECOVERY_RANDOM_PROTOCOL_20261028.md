# Synthetic recovery robustness: fixed random walks

After observing the first deterministic recovery fixtures, evaluate the existing
v3 policy and the two experimental recovery schedules on eight preselected random
walk seeds: 101, 103, 107, 109, 113, 127, 131, 137. Each case has 120 decisions,
uses the existing snapshot generator `compare_account_c_selection.snapshots`, and
feeds the same snapshots to all arms. All seeds and arms are reported, including
losses, ties and increased drawdowns. The baseline policy and stakes remain intact.

This is an exploratory stress test; the schedules were already selected on other
development fixtures. Synthetic quote movement is not a verified match result or
bookmaker execution. The output cannot substantiate real-world profitability,
independent holdout success, statistical significance or promotion. No experimental
policy is deployed as a result.
