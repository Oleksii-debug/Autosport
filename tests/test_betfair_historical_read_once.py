from __future__ import annotations

import bz2
import gzip
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autosport import betfair_historical_read_once as read_once
from autosport import data_tools_entry
from autosport.betfair_historical_import import BetfairHistoricalImportReport


def _report(root: str | Path) -> BetfairHistoricalImportReport:
    return BetfairHistoricalImportReport(
        root=str(root),
        import_identity="import-id",
        market_sha256="1" * 64,
        results_sha256="2" * 64,
        source_identity="source-id",
        market_event_count=1,
        quote_count=1,
        settled_market_count=1,
    )


class BetfairHistoricalReadOnceTests(unittest.TestCase):
    def test_product_wrapper_parses_frozen_bytes_after_original_path_is_replaced(self) -> None:
        with TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            source = tmp_path / "market.bz2"
            original_bytes = bz2.compress(b'{"op":"mcm","pt":1789372800000}\n')
            source.write_bytes(original_bytes)
            frozen_paths: tuple[Path, ...] = ()

            def fake_import(inputs, output_dir, **_kwargs):
                nonlocal frozen_paths
                frozen_paths = tuple(inputs)
                source.write_bytes(b"replacement-bytes")
                self.assertEqual(
                    [path.read_bytes() for path in frozen_paths],
                    [original_bytes],
                )
                self.assertTrue(all(path != source for path in frozen_paths))
                return _report(output_dir)

            output = tmp_path / "dataset"
            with patch.object(read_once, "import_betfair_historical", side_effect=fake_import):
                report = read_once.import_betfair_historical_read_once(
                    [source],
                    output,
                    acquired_at="2026-09-13T08:00:00Z",
                    terms_reference="https://historicdata.betfair.com/",
                    retention_basis="user-supplied lawful local-use evidence required",
                )

            self.assertEqual(report.root, str(output))
            self.assertEqual(source.read_bytes(), b"replacement-bytes")
            self.assertTrue(frozen_paths)
            self.assertTrue(all(not path.exists() for path in frozen_paths))

    def test_product_wrapper_rejects_duplicate_json_members_before_legacy_parse(self) -> None:
        with TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            source = tmp_path / "market.jsonl"
            source.write_text(
                '{"op":"mcm","mc":[{"id":"1.1","id":"1.2"}]}\n',
                encoding="utf-8",
            )
            output = tmp_path / "dataset"

            with patch.object(read_once, "import_betfair_historical") as legacy_import:
                with self.assertRaisesRegex(ValueError, "duplicate JSON object member 'id'"):
                    read_once.import_betfair_historical_read_once(
                        [source],
                        output,
                        acquired_at="2026-09-13T08:00:00Z",
                        terms_reference="terms",
                        retention_basis="basis",
                    )

            legacy_import.assert_not_called()
            self.assertFalse(output.exists())

    def test_product_wrapper_rejects_nonstandard_json_numeric_constants(self) -> None:
        with TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            source = tmp_path / "market.jsonl"
            source.write_text('{"op":"mcm","pt":NaN}\n', encoding="utf-8")
            output = tmp_path / "dataset"

            with patch.object(read_once, "import_betfair_historical") as legacy_import:
                with self.assertRaisesRegex(ValueError, "non-standard JSON numeric constant NaN"):
                    read_once.import_betfair_historical_read_once(
                        [source],
                        output,
                        acquired_at="2026-09-13T08:00:00Z",
                        terms_reference="terms",
                        retention_basis="basis",
                    )

            legacy_import.assert_not_called()
            self.assertFalse(output.exists())

    def test_product_wrapper_rejects_standard_json_float_overflow_before_legacy_parse(self) -> None:
        with TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            source = tmp_path / "market.jsonl"
            source.write_text(
                '{"op":"mcm","mc":[{"id":1e999999}]}\n',
                encoding="utf-8",
            )
            output = tmp_path / "dataset"

            with patch.object(read_once, "import_betfair_historical") as legacy_import:
                with self.assertRaisesRegex(ValueError, "outside finite float range"):
                    read_once.import_betfair_historical_read_once(
                        [source],
                        output,
                        acquired_at="2026-09-13T08:00:00Z",
                        terms_reference="terms",
                        retention_basis="basis",
                    )

            legacy_import.assert_not_called()
            self.assertFalse(output.exists())

    def test_truncated_compressed_inputs_fail_closed_at_user_facing_boundary(self) -> None:
        payload = b'{"op":"mcm","pt":1789372800000}\n'
        for suffix, compress in ((".bz2", bz2.compress), (".gz", gzip.compress)):
            with self.subTest(suffix=suffix), TemporaryDirectory() as temporary:
                tmp_path = Path(temporary)
                source = tmp_path / f"market{suffix}"
                source.write_bytes(compress(payload)[:-1])
                output = tmp_path / "dataset"

                with patch.object(read_once, "import_betfair_historical") as legacy_import:
                    result = read_once.main(
                        [
                            str(source),
                            "--output-dir",
                            str(output),
                            "--acquired-at",
                            "2026-09-13T08:00:00Z",
                            "--terms-reference",
                            "terms",
                            "--retention-basis",
                            "basis",
                        ]
                    )

                self.assertEqual(result, 3)
                legacy_import.assert_not_called()
                self.assertFalse(output.exists())

    def test_windows_data_tool_routes_betfair_import_through_read_once_boundary(self) -> None:
        seen: list[list[str]] = []

        def fake_main(argv):
            seen.append(list(argv))
            return 17

        forwarded = [
            "history.bz2",
            "--output-dir",
            "dataset",
            "--acquired-at",
            "2026-09-13T08:00:00Z",
            "--terms-reference",
            "terms",
            "--retention-basis",
            "basis",
        ]
        with patch.object(read_once, "main", side_effect=fake_main):
            result = data_tools_entry.main(["import-betfair-historical", *forwarded])

        self.assertEqual(result, 17)
        self.assertEqual(seen, [forwarded])


if __name__ == "__main__":
    unittest.main()
