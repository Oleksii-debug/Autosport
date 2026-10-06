from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import package_windows


class PackageWindowsSourceBindingTests(unittest.TestCase):
    SOURCE_SHA = "d3d0e64496732411d46cc84ae6c29778215c73f4"
    OTHER_SHA = "48ccaed3a7ad88634db83034ea0cd889f500e2cf"
    SOURCE_TREE = "1" * 40

    @staticmethod
    def _write_pull_request_event(
        root: Path,
        *,
        source_sha: str,
        repository: str = "Oleksii-debug/Autosport",
        head_repository: str = "Oleksii-debug/Autosport",
    ) -> Path:
        event_path = root / "event.json"
        event_path.write_text(
            json.dumps(
                {
                    "repository": {"full_name": repository},
                    "pull_request": {
                        "head": {
                            "sha": source_sha,
                            "repo": {"full_name": head_repository},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return event_path

    def test_github_event_head_is_authoritative_not_arbitrary_hex(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_path = self._write_pull_request_event(
                Path(temp_dir),
                source_sha=self.SOURCE_SHA,
            )
            with patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_PATH": str(event_path),
                    "GITHUB_REPOSITORY": "Oleksii-debug/Autosport",
                    "GITHUB_SHA": self.OTHER_SHA,
                },
                clear=True,
            ):
                self.assertEqual(
                    package_windows._github_authoritative_source_sha(),
                    self.SOURCE_SHA,
                )
                with patch.object(package_windows, "_fetch_source_commit") as fetch_commit:
                    with patch.object(
                        package_windows,
                        "_git_output",
                        return_value=self.SOURCE_TREE,
                    ):
                        with self.assertRaisesRegex(
                            ValueError,
                            "does not match the authoritative GitHub candidate head",
                        ):
                            package_windows._bind_source_sha_to_checkout(
                                "a" * 40,
                                repo_root=Path(temp_dir),
                            )
                    fetch_commit.assert_not_called()

    def test_github_source_is_fetched_from_origin_and_tree_bound_to_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            event_path = self._write_pull_request_event(
                root,
                source_sha=self.SOURCE_SHA,
            )
            with patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_PATH": str(event_path),
                    "GITHUB_REPOSITORY": "Oleksii-debug/Autosport",
                    "GITHUB_SHA": self.OTHER_SHA,
                },
                clear=True,
            ):
                with patch.object(package_windows, "_fetch_source_commit") as fetch_commit:
                    with patch.object(
                        package_windows,
                        "_git_output",
                        side_effect=[self.SOURCE_TREE, self.SOURCE_TREE],
                    ):
                        package_windows._bind_source_sha_to_checkout(
                            self.SOURCE_SHA,
                            repo_root=root,
                        )
                fetch_commit.assert_called_once_with(root, self.SOURCE_SHA)

    def test_github_checkout_tree_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            event_path = self._write_pull_request_event(
                root,
                source_sha=self.SOURCE_SHA,
            )
            with patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_PATH": str(event_path),
                    "GITHUB_REPOSITORY": "Oleksii-debug/Autosport",
                },
                clear=True,
            ):
                with patch.object(package_windows, "_fetch_source_commit"):
                    with patch.object(
                        package_windows,
                        "_git_output",
                        side_effect=[self.SOURCE_TREE, "2" * 40],
                    ):
                        with self.assertRaisesRegex(
                            ValueError,
                            "checked-out source tree does not match",
                        ):
                            package_windows._bind_source_sha_to_checkout(
                                self.SOURCE_SHA,
                                repo_root=root,
                            )

    def test_fork_head_cannot_anchor_official_origin_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            event_path = self._write_pull_request_event(
                Path(temp_dir),
                source_sha=self.SOURCE_SHA,
                head_repository="someone-else/Autosport",
            )
            with patch.dict(
                os.environ,
                {
                    "GITHUB_EVENT_PATH": str(event_path),
                    "GITHUB_REPOSITORY": "Oleksii-debug/Autosport",
                },
                clear=True,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "authoritative repository",
                ):
                    package_windows._github_authoritative_source_sha()

    def test_local_packaging_requires_exact_checkout_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict(os.environ, {}, clear=True):
                with patch.object(
                    package_windows,
                    "_git_output",
                    side_effect=[self.SOURCE_SHA, self.SOURCE_TREE, self.SOURCE_TREE],
                ):
                    package_windows._bind_source_sha_to_checkout(
                        self.SOURCE_SHA,
                        repo_root=root,
                    )

                with patch.object(
                    package_windows,
                    "_git_output",
                    return_value=self.OTHER_SHA,
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "does not match the exact local checkout HEAD",
                    ):
                        package_windows._bind_source_sha_to_checkout(
                            self.SOURCE_SHA,
                            repo_root=root,
                        )


if __name__ == "__main__":
    unittest.main()
