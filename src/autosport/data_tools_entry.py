from __future__ import annotations

import sys


_USAGE = """Autosport-Data — portable Windows historical-data tools + research + recovery

Usage:
  Autosport-Data.exe acquire [autosport-acquire-historical-evidence arguments]
  Autosport-Data.exe import-betfair-historical <local-file> [...] [import arguments]
  Autosport-Data.exe build-corpus [autosport-build-historical-corpus arguments]
  Autosport-Data.exe build-corpus-from-bundle [bundle-adapter arguments]
  Autosport-Data.exe verify-dataset <dataset-path>
  Autosport-Data.exe walk-forward-evaluate <bundle.json> [--output report.json]
  Autosport-Data.exe compare-strategies <run-summary> <run-summary> [...] [strategy-comparison arguments]
  Autosport-Data.exe repair-workspace [--workspace <workspace-path>]
  Autosport-Data.exe export-evidence <workspace-path> --output <manifest.json>
  Autosport-Data.exe verify-evidence <manifest.json> --workspace <workspace-path>
  Autosport-Data.exe nvda-evidence-template --release-zip <zip> --expected-source-sha <canonical-github-sha> --expected-package-sha256 <published-zip-sha256> --output <evidence.json>
  Autosport-Data.exe verify-nvda-evidence --release-zip <zip> --expected-source-sha <canonical-github-sha> --expected-package-sha256 <published-zip-sha256> --evidence <evidence.json> [--output <report.json>]

Commands:
  acquire                    Capture immutable authenticated historical odds + match/result evidence.
  import-betfair-historical Import user-supplied local Betfair Historical Data stream files into the canonical governed table-tennis dataset format; source files are never redistributed by this command.
  build-corpus               Assemble selected snapshots, sealed outcomes, and checksum-bound rights/retention evidence into a governed corpus.
  build-corpus-from-bundle   Verify an acquisition bundle and reuse the same checksum-bound governance gate and canonical corpus assembler.
  verify-dataset             Verify sealed hashes and historical governance without replay.
  walk-forward-evaluate      Run the canonical strict causal walk-forward evaluator and emit machine-readable evidence.
  compare-strategies         Compare compatible completed paper strategy runs on the same sealed replay identity.
  repair-workspace           Reconcile a late-crashed economic run using the canonical fail-closed recovery path.
  export-evidence            Export the canonical deterministic metadata-only workspace evidence manifest.
  verify-evidence            Verify an exported evidence manifest against the current canonical workspace evidence.
  nvda-evidence-template     Create a physical NVDA test record bound to one exact packaged release candidate and independently supplied source/package trust anchors.
  verify-nvda-evidence      Fail closed if human-supplied NVDA evidence drifts from the exact release ZIP, external trust anchors, or required checks.

Truth boundaries remain fail closed: credentials, lawful retention/licensing, real sealed outcomes,
coverage, profitability or predictive superiority, human testing, and NVDA verification are never
inferred by this wrapper. Corpus build commands reject a bare self-declared rights proof: the proof
must name a direct sibling authority-evidence record, bind its SHA-256, and exactly match its rights,
retention, source and redistribution claims. That checksum binding proves provenance/integrity only;
it is not an independent legal opinion and cannot create rights that the evidence does not actually
grant. Local source-data rights remain the user's responsibility. Recovery never fabricates
completion: an ambiguous workspace remains unresolved unless canonical transaction/base hashes prove
the disposition. Evidence export and verification reuse the canonical metadata-only manifest path;
they do not include file contents, market databases, raw historical/provider bytes, environment or
credential values, or arbitrary workspace files. NVDA evidence validation checks a human-supplied
record against independently obtained source/package trust anchors and exact candidate identity; it
never proves that a physical test happened and never changes BUILD_INFO human/NVDA truth labels.
"""


def _dispatch(command: str, forwarded: list[str]) -> int:
    if command == "acquire":
        from autosport.historical_acquisition import main as acquisition_main

        return acquisition_main(forwarded)
    if command == "import-betfair-historical":
        from autosport.betfair_historical_read_once import main as betfair_import_main

        return betfair_import_main(forwarded)
    if command == "build-corpus":
        from autosport.historical_governance import corpus_main

        return corpus_main(forwarded)
    if command == "build-corpus-from-bundle":
        from autosport.historical_governance import bundle_corpus_main

        return bundle_corpus_main(forwarded)
    if command == "verify-dataset":
        from autosport.cli import main as cli_main

        return cli_main(["verify-dataset", *forwarded])
    if command == "walk-forward-evaluate":
        from autosport.cli import main as cli_main

        return cli_main(["walk-forward-evaluate", *forwarded])
    if command == "compare-strategies":
        from autosport.strategy_comparison import main as strategy_comparison_main

        return strategy_comparison_main(forwarded)
    if command == "repair-workspace":
        from autosport.cli import main as cli_main

        return cli_main(["repair-workspace", *forwarded])
    if command == "export-evidence":
        from autosport.evidence_export import main as evidence_export_main

        return evidence_export_main(forwarded)
    if command == "verify-evidence":
        from autosport.evidence_export import verify_main as evidence_verify_main

        return evidence_verify_main(forwarded)
    if command == "nvda-evidence-template":
        from autosport.nvda_acceptance import template_main

        return template_main(forwarded)
    if command == "verify-nvda-evidence":
        from autosport.nvda_acceptance import verify_main

        return verify_main(forwarded)

    print(f"Autosport-Data: unknown command {command!r}\n")
    print(_USAGE)
    return 2


def _expected_failure_message(command: str, exc: OSError | ValueError) -> str:
    # Formatting belongs to the same packaged fail-closed boundary as dispatch.
    # Exception subclasses are caller/library supplied: neither custom type metadata,
    # __str__(), nor methods on a returned str subclass may recreate a traceback.
    try:
        exception_type = type.__getattribute__(type(exc), "__name__")
    except BaseException:
        exception_type = "Exception"
    try:
        rendered = str.__str__(str(exc))
    except BaseException:
        rendered = exception_type
    detail = " ".join(rendered.splitlines()).strip()
    if not detail:
        detail = exception_type
    return f"Autosport-Data: {command}=FAIL_CLOSED error={exception_type}: {detail}"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(_USAGE)
        return 0

    command, forwarded = args[0], args[1:]
    try:
        return _dispatch(command, forwarded)
    except (OSError, ValueError) as exc:
        # This executable is a packaged user-facing boundary. Expected malformed
        # local input and filesystem failures must be recoverable without a Python
        # traceback. Deliberately do not catch RuntimeError, SystemExit or
        # BaseException so programming failures and argparse exit semantics remain
        # visible to qualification instead of being mislabeled as user-input errors.
        print(_expected_failure_message(command, exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
