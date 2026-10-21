# Plan 6 exact-HEAD source qualification runner

This is an integrated **source-test execution tool**, NOT a terminal section
closure, exact Windows build, NVDA attestation, licensed bundled-component SBOM
or approval for real-money execution.

On a full clean checkout with its test dependencies and pytest installed:

    python scripts/qualify_plan6_source.py --output /absolute/private/path/plan6-source.json

The runner executes four sets of existing canonical tests: Ukrainian locale,
machine accessibility, package integrity, PAPER install/update and recovery.
It records Git HEAD, platform, executable pytest pass counts and exit codes.
If source changes, files are absent, tests fail or all are skipped, the result
is FAIL_OR_INCOMPLETE rather than an invented successful qualification.

Even PASS_SOURCE_ONLY has mandatory false values for Windows installed release,
complete packaged SBOM, license clearance, cross-schema migration, owner/NVDA,
real-money execution and whole-product completion. Plan 6 atomic sections
5Б, 6А, 7А and 7В remain unfinished until their full requirements actually
pass independently.
