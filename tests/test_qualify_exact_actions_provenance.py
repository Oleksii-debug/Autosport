"""Regression: a successful same-name workflow from a foreign path/repository is not Autosport CI evidence."""
from __future__ import annotations

import unittest

from scripts.qualify_exact_actions import (
    _ADMISSION_STEPS, _CI, _CI_ADMISSION, _CI_REQUIRED_STEPS, _WINDOWS,
    _WINDOWS_ADMISSION, _WINDOWS_REQUIRED_STEPS, GateError, qualify,
)

SHA = "a" * 40
REPO = "Oleksii-debug/Autosport"


def fixture():
    def job(name, jid, runid, required):
        return {"id": jid, "run_id": runid, "run_attempt": 2, "head_sha": SHA,
                "name": name, "status": "completed", "conclusion": "success",
                "steps": [{"name": step, "status": "completed", "conclusion": "success"}
                          for step in sorted(required)]}
    def run(runid, name, path):
        return {"id": runid, "run_attempt": 2, "event": "pull_request",
                "name": name, "head_sha": SHA, "path": path,
                "repository": {"full_name": REPO},
                "head_repository": {"full_name": REPO},
                "status": "completed", "conclusion": "success"}
    ci = run(100, "CI", ".github/workflows/ci.yml")
    win = run(101, "Windows candidate", ".github/workflows/windows-build.yml")
    cj = {"jobs": [job(n, i + 1, 100,
                       _ADMISSION_STEPS | frozenset({_CI_ADMISSION})
                       if n == "superseded_run_admission" else _CI_REQUIRED_STEPS)
                   for i, n in enumerate(sorted(_CI))]}
    wj = {"jobs": [job(n, i + 30, 101,
                       _ADMISSION_STEPS | frozenset({_WINDOWS_ADMISSION})
                       if n == "superseded_run_admission" else _WINDOWS_REQUIRED_STEPS)
                   for i, n in enumerate(sorted(_WINDOWS))]}
    cj["total_count"] = len(cj["jobs"])
    wj["total_count"] = len(wj["jobs"])
    return ci, cj, win, wj


class WorkflowProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.ci, self.cj, self.win, self.wj = fixture()

    def check(self):
        return qualify(self.ci, self.cj, self.win, self.wj, SHA)

    def test_actual_canonical_workflow_paths_are_accepted(self):
        self.assertEqual(self.check()["status"], "PASS")

    def test_foreign_repository_ci_cannot_qualify(self):
        self.ci["repository"]["full_name"] = "other/Autosport"
        with self.assertRaisesRegex(GateError, "repository"):
            self.check()

    def test_foreign_repository_windows_cannot_qualify(self):
        self.win["repository"]["full_name"] = "other/Autosport"
        with self.assertRaisesRegex(GateError, "repository"):
            self.check()

    def test_foreign_pr_head_repository_cannot_qualify(self):
        self.win["head_repository"]["full_name"] = "other/Autosport"
        with self.assertRaisesRegex(GateError, "head repository"):
            self.check()

    def test_wrong_ci_workflow_path_cannot_qualify(self):
        self.ci["path"] = ".github/workflows/ci-easy-pass.yml"
        with self.assertRaisesRegex(GateError, "workflow path"):
            self.check()

    def test_wrong_windows_workflow_path_cannot_qualify(self):
        self.win["path"] = ".github/workflows/other-windows.yml"
        with self.assertRaisesRegex(GateError, "workflow path"):
            self.check()

    def test_missing_repository_cannot_qualify(self):
        self.ci.pop("repository")
        with self.assertRaisesRegex(GateError, "repository"):
            self.check()

    def test_missing_path_cannot_qualify(self):
        self.win.pop("path")
        with self.assertRaisesRegex(GateError, "workflow path"):
            self.check()


if __name__ == "__main__":
    unittest.main()
