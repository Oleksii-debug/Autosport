# Read-only inspection of simulated policy memory

`scripts/inspect_sandbox_policy_prior.py` builds a portable **description** of
a completed simulated policy, not a runnable policy or imported account. It
requires an externally supplied artifact SHA-256, verifies archive and manifest
integrity, checks simulation truth, reconstructs the canonical campaign plan
and policy, and verifies their environment, configuration, action set and policy
identity. It records simulated action counts and rewards with source identities.

The companion JSON was produced from artifact 12792792552 for GitHub run
40622520332, expected SHA-256
`6480ef8cf18a5cfef7a15524bad7f12327e52a4807b1212980605465f212e541`.
GitHub's reported digest was compared separately. The script itself cannot
authenticate a caller-provided expected hash or independently verify source
quotes; internal consistency is not proof of either. The recorded model uses
synthetic price-movement outcomes, not match results.

No new policy, economic authority, desktop import, forward-evidence eligibility
or profitability claim is created. The original actions and rewards are not
fully replayed by this inspection. Before reusing these aggregate estimates,
a separate carryover protocol must explicitly define compatibility, causal
lineage, new environment identity, decay and risk, duplicate-use prevention,
negative/restart tests, and why reuse helps on new data. The path-bound PAPER
book and its independent witness must not be forged or silently rebound.

Reproduce with `PYTHONPATH=src python scripts/inspect_sandbox_policy_prior.py
ARTIFACT.zip --expected-sha256 EXPECTED_SHA256` after obtaining the archive and
expected digest from independent trusted sources.
