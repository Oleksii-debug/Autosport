from __future__ import annotations

import sys


_USAGE = """Autosport-Data — portable Windows historical-data tools

Usage:
  Autosport-Data.exe acquire [autosport-acquire-historical-evidence arguments]
  Autosport-Data.exe build-corpus [autosport-build-historical-corpus arguments]
  Autosport-Data.exe build-corpus-from-bundle [bundle-adapter arguments]
  Autosport-Data.exe verify-dataset <dataset-path>

Commands:
  acquire                   Capture immutable authenticated historical odds + match/result evidence.
  build-corpus              Assemble selected snapshots, sealed outcomes, and rights proof into a governed corpus.
  build-corpus-from-bundle  Verify an acquisition bundle and reuse the canonical governed corpus assembler.
  verify-dataset            Verify sealed hashes and historical governance without replay.

Truth boundaries remain fail closed: credentials, lawful retention/licensing, real sealed outcomes,
coverage, profitability, human testing, and NVDA verification are never inferred by this wrapper.
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
    if command == "build-corpus":
        from autosport.historical_corpus import main as corpus_main

        return corpus_main(forwarded)
    if command == "build-corpus-from-bundle":
        from autosport.historical_bundle_corpus import main as bundle_corpus_main

        return bundle_corpus_main(forwarded)
    if command == "verify-dataset":
        from autosport.cli import main as cli_main

        return cli_main(["verify-dataset", *forwarded])

    print(f"Autosport-Data: unknown command {command!r}\n")
    print(_USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
