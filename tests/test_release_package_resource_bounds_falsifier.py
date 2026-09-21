from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autosport.release_package import verify_windows_package


class ReleasePackageResourceBoundsFalsifierTests(unittest.TestCase):
    """Require resource preflight before any release ZIP payload is decompressed."""

    SOURCE_SHA = "a" * 40
    MEMBER = "Autosport-V1/payload.bin"
    _CENTRAL_SIGNATURE = b"PK\\x01\\x02"

    @staticmethod
    def _write_candidate(path: Path) -> None:
        info = zipfile.ZipInfo(
            ReleasePackageResourceBoundsFalsifierTests.MEMBER,
            (1980, 1, 1, 0, 0, 0),
        )
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.create_version = 20
        info.extract_version = 20
        info.reserved = 0
        info.flag_bits = 0
        info.volume = 0
        info.internal_attr = 0
        info.external_attr = 0o644 << 16
        with zipfile.ZipFile(
            path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            archive.writestr(
                info,
                b"small trusted-looking payload",
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )

    @classmethod
    def _patch_first_central_sizes(
        cls,
        path: Path,
        *,
        compressed_size: int | None = None,
        uncompressed_size: int | None = None,
    ) -> None:
        payload = bytearray(path.read_bytes())
        central = payload.find(cls._CENTRAL_SIGNATURE)
        if central < 0:
            raise AssertionError("expected ZIP central-directory entry")
        if compressed_size is not None:
            payload[central + 20 : central + 24] = compressed_size.to_bytes(
                4,
                "little",
            )
        if uncompressed_size is not None:
            payload[central + 24 : central + 28] = uncompressed_size.to_bytes(
                4,
                "little",
            )
        path.write_bytes(payload)

    def _assert_rejected_before_payload_read(self, package: Path) -> None:
        with patch.object(
            zipfile.ZipFile,
            "read",
            side_effect=AssertionError(
                "release payload decompression happened before resource preflight"
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "resource|size|limit|large|compression",
            ):
                verify_windows_package(
                    package,
                    expected_source_sha=self.SOURCE_SHA,
                )

    def test_absurd_declared_member_size_is_rejected_before_payload_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "candidate.zip"
            self._write_candidate(package)
            self._patch_first_central_sizes(
                package,
                uncompressed_size=0xFFFFFFFF,
            )
            self._assert_rejected_before_payload_read(package)

    def test_extreme_declared_compression_ratio_is_rejected_before_payload_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "candidate.zip"
            self._write_candidate(package)
            self._patch_first_central_sizes(
                package,
                compressed_size=1,
                uncompressed_size=64 * 1024 * 1024,
            )
            self._assert_rejected_before_payload_read(package)


if __name__ == "__main__":
    unittest.main()
