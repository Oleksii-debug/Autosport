from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .betfair_historical_import import (
    BetfairHistoricalImportReport,
    build_parser,
    import_betfair_historical,
)


_COPY_CHUNK_BYTES = 1024 * 1024


@contextmanager
def frozen_betfair_inputs(
    inputs: Iterable[str | Path],
) -> Iterator[tuple[Path, ...]]:
    """Freeze user-supplied Betfair files so hash and parse consume identical bytes.

    The legacy decoder is deliberately left unchanged. This boundary streams each external
    source path exactly once into a private temporary snapshot and passes only those snapshot
    paths to the decoder. The decoder therefore computes source hashes, source identity, byte
    sizes and parsed market/settlement records from the same frozen byte sequence even if the
    original path is replaced after capture. Streaming keeps memory bounded for large archives.
    """

    source_paths = tuple(Path(item) for item in inputs)
    if not source_paths:
        raise ValueError("at least one Betfair historical input file is required")

    with tempfile.TemporaryDirectory(prefix="autosport-betfair-read-once-") as temporary:
        root = Path(temporary)
        frozen: list[Path] = []
        for ordinal, source in enumerate(source_paths, start=1):
            if not source.is_file():
                raise ValueError(f"Betfair historical input does not exist: {source}")
            suffix = source.suffix.lower()
            snapshot = root / f"source-{ordinal:04d}{suffix}"
            # This is the one external content stream. All downstream verification/parsing
            # happens from the private snapshot written from these exact captured bytes.
            with source.open("rb") as source_handle, snapshot.open("xb") as snapshot_handle:
                shutil.copyfileobj(source_handle, snapshot_handle, length=_COPY_CHUNK_BYTES)
            frozen.append(snapshot)
        yield tuple(frozen)


def import_betfair_historical_read_once(
    inputs: Iterable[str | Path],
    output_dir: str | Path,
    *,
    acquired_at: str,
    terms_reference: str,
    retention_basis: str,
    redistribution_policy: str = "prohibited",
    dataset_name: str = "betfair-table-tennis-history",
    allowed_market_types: Iterable[str] = ("MATCH_ODDS",),
    imported_at: str | None = None,
) -> BetfairHistoricalImportReport:
    """Canonical product-path Betfair import with hash-to-parse byte binding."""

    with frozen_betfair_inputs(inputs) as frozen_inputs:
        return import_betfair_historical(
            frozen_inputs,
            output_dir,
            acquired_at=acquired_at,
            terms_reference=terms_reference,
            retention_basis=retention_basis,
            redistribution_policy=redistribution_policy,
            dataset_name=dataset_name,
            allowed_market_types=allowed_market_types,
            imported_at=imported_at,
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = import_betfair_historical_read_once(
            args.inputs,
            args.output_dir,
            acquired_at=args.acquired_at,
            terms_reference=args.terms_reference,
            retention_basis=args.retention_basis,
            redistribution_policy=args.redistribution_policy,
            dataset_name=args.dataset_name,
            allowed_market_types=args.market_types or ("MATCH_ODDS",),
        )
    except (OSError, ValueError) as exc:
        print(f"betfair_historical_import=FAIL_CLOSED error={exc}")
        return 3

    print(
        "betfair_historical_import=IMPORTED "
        f"events={report.market_event_count} quotes={report.quote_count} "
        f"settled_markets={report.settled_market_count}"
    )
    print(f"historical_import_identity={report.import_identity}")
    print(f"market_sha256={report.market_sha256}")
    print(f"sealed_results_sha256={report.results_sha256}")
    print(f"source_identity={report.source_identity}")
    print(
        "price_truth=available_back_observed quote_verified_requires_provider_image "
        "price_ladder=CLASSIC_verified runner_roster_verified=true "
        "paper_capacity_unit_bound=false actual_fill_verified=false"
    )
    print("licensing_retention_verified=false redistribution_verified=false")
    print("real_money_execution=false human_tested=false nvda_verified=false")
    print(f"dataset={report.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
