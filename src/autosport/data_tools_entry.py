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

Commands:
  acquire                    Capture immutable authenticated historical odds + match/result evidence.
  import-betfair-historical Import user-supplied local Betfair Historical Data stream files into the canonical governed table-tennis dataset format; source files are never redistributed by this command.
  build-corpus               Assemble selected snapshots, sealed outcomes, and rights proof into a governed corpus.
  build-corpus-from-bundle   Verify an acquisition bundle and reuse the canonical governed corpus assembler.
  verify-dataset             Verify sealed hashes and historical governance without replay.
  walk-forward-evaluate      Run the canonical strict causal walk-forward evaluator and emit machine-readable evidence.
  compare-strategies         Compare compatible completed paper strategy runs on the same sealed replay identity.
  repair-workspace           Reconcile a late-crashed economic run using the canonical fail-closed recovery path.

Truth boundaries remain fail closed: credentials, lawful retention/licensing, real sealed outcomes,
coverage, profitability or predictive superiority, human testing, and NVDA verification are never
inferred by this wrapper. Local source-data rights remain the user's responsibility and are recorded,
not independently certified, by import commands. Recovery never fabricates completion: an ambiguous
workspace remains unresolved unless canonical transaction/base hashes prove the disposition.
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(_USAGE)
        return 0

    command, forwarded = args[0], args[1:]
    if command == "acquire":
        from autosport.historical_acquisition import main as acquisition_main

        return acquisition_main(forwarded)
    if command == "import-betfair-historical":
        from autosport.betfair_historical_import import main as betfair_import_main

        return betfair_import_main(forwarded)
    if command == "build-corpus":
        from autosport.historical_corpus import main as corpus_main

        return corpus_main(forwarded)
    if command == "build-corpus-from-bundle":
        from autosport.historical_bundle_corpus import main as bundle_corpus_main

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

    print(f"Autosport-Data: unknown command {command!r}\n")
    print(_USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
