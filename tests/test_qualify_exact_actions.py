from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.qualify_exact_actions import (
    _ADMISSION_STEPS, _CI, _CI_ADMISSION, _CI_REQUIRED_STEPS, _WINDOWS,
    _WINDOWS_ADMISSION, _WINDOWS_REQUIRED_STEPS, GateError, qualify,
)

SHA = "a" * 40


def fixture():
    def steps(names):
        return [{"name": n, "status": "completed", "conclusion": "success"}
                for n in sorted(names)]
    def job(name, jid, runid, required):
        return {"id": jid, "run_id": runid, "run_attempt": 2, "head_sha": SHA,
                "name": name, "status": "completed", "conclusion": "success",
                "steps": steps(required)}
    ci = {"id": 100, "run_attempt": 2, "event": "pull_request", "name": "CI",
          "head_sha": SHA, "status": "completed", "conclusion": "success"}
    win = {"id": 101, "run_attempt": 2, "event": "pull_request",
           "name": "Windows candidate", "head_sha": SHA,
           "status": "completed", "conclusion": "success"}
    cj = {"jobs": [job(n, i + 1, 100,
                       _ADMISSION_STEPS | frozenset({_CI_ADMISSION})
                       if n == "superseded_run_admission" else _CI_REQUIRED_STEPS)
                   for i, n in enumerate(sorted(_CI))]}
    wj = {"jobs": [job(n, i + 30, 101,
                       _ADMISSION_STEPS | frozenset({_WINDOWS_ADMISSION})
                       if n == "superseded_run_admission" else _WINDOWS_REQUIRED_STEPS)
                   for i, n in enumerate(sorted(_WINDOWS))]}
    return ci, cj, win, wj


class GateTests(unittest.TestCase):
    def setUp(self):
        self.ci, self.cj, self.win, self.wj = fixture()

    def check(self):
        return qualify(self.ci, self.cj, self.win, self.wj, SHA)

    def test_complete_matrix_and_steps_pass(self):
        self.assertEqual(self.check()["status"], "PASS")

    def test_workflow_success_but_build_skipped(self):
        for job in self.wj["jobs"]:
            if job["name"] == "build":
                job["conclusion"] = "skipped"
        with self.assertRaises(GateError): self.check()

    def test_job_success_but_external_uia_skipped(self):
        self._step("External UIA fresh-extraction gate")["conclusion"] = "skipped"
        with self.assertRaisesRegex(GateError, "External UIA"): self.check()

    def test_job_success_but_external_uia_missing(self):
        self._remove_step("External UIA fresh-extraction gate")
        with self.assertRaisesRegex(GateError, "missing required steps"): self.check()

    def test_job_success_but_release_extraction_skipped(self):
        self._step("Verify independent stage-neutral release extraction")["conclusion"] = "skipped"
        with self.assertRaises(GateError): self.check()

    def test_job_success_but_packaging_skipped(self):
        self._step("Run ./scripts/build_windows_candidate.ps1")["conclusion"] = "skipped"
        with self.assertRaises(GateError): self.check()

    def test_job_success_but_pytest_skipped(self):
        for j in self.cj["jobs"]:
            if j["name"].startswith("test ("):
                j["steps"] = [x for x in j["steps"] if x["name"] != "Run python -m pytest -v tests"]
                break
        with self.assertRaises(GateError): self.check()

    def test_job_success_but_demo_failed(self):
        for j in self.cj["jobs"]:
            if j["name"].startswith("test ("):
                next(x for x in j["steps"] if x["name"] == "Run python -m autosport demo")["conclusion"] = "failure"
                break
        with self.assertRaises(GateError): self.check()

    def test_duplicate_required_step(self):
        self._build()["steps"].append(copy.deepcopy(self._step("External UIA fresh-extraction gate")))
        with self.assertRaises(GateError): self.check()

    def test_wrong_job_attempt(self):
        self._build()["run_attempt"] = 1
        with self.assertRaisesRegex(GateError, "attempt mismatch"): self.check()

    def test_missing_job_attempt(self):
        self._build().pop("run_attempt")
        with self.assertRaises(GateError): self.check()

    def test_missing_job_sha(self):
        self._build().pop("head_sha")
        with self.assertRaises(GateError): self.check()

    def test_wrong_job_run_id(self):
        self._build()["run_id"] = 102
        with self.assertRaises(GateError): self.check()

    def test_missing_job_run_id(self):
        self._build().pop("run_id")
        with self.assertRaises(GateError): self.check()

    def test_duplicate_job_id(self):
        self.cj["jobs"][0]["id"] = self.cj["jobs"][1]["id"]
        with self.assertRaises(GateError): self.check()

    def test_run_sha_mismatch(self):
        self.win["head_sha"] = "b" * 40
        with self.assertRaises(GateError): self.check()

    def test_run_attempt_missing(self):
        self.win.pop("run_attempt")
        with self.assertRaises(GateError): self.check()

    def test_run_not_completed(self):
        self.win["status"] = "in_progress"
        with self.assertRaises(GateError): self.check()

    def test_wrong_workflow_name(self):
        self.win["name"] = "WebView2 control"
        with self.assertRaises(GateError): self.check()

    def test_non_sha(self):
        with self.assertRaises(GateError):
            qualify(self.ci, self.cj, self.win, self.wj, "main")

    def test_missing_matrix_job(self):
        self.cj["jobs"].pop()
        with self.assertRaises(GateError): self.check()

    def test_wrong_admission_step(self):
        for j in self.wj["jobs"]:
            if j["name"] == "superseded_run_admission":
                j["steps"] = [x for x in j["steps"] if x["name"] != _WINDOWS_ADMISSION]
        with self.assertRaises(GateError): self.check()

    def test_duplicate_run_id(self):
        self.win["id"] = self.ci["id"]
        for j in self.wj["jobs"]: j["run_id"] = self.ci["id"]
        with self.assertRaises(GateError): self.check()

    def test_real_skipped_gate_shape(self):
        self._step("External UIA fresh-extraction gate")["conclusion"] = "skipped"
        self._step("Verify independent stage-neutral release extraction")["conclusion"] = "skipped"
        with self.assertRaises(GateError): self.check()

    def test_cli_negative_nonzero(self):
        self._step("External UIA fresh-extraction gate")["conclusion"] = "skipped"
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, obj in enumerate((self.ci, self.cj, self.win, self.wj)):
                path = Path(tmp) / f"{i}.json"
                path.write_text(json.dumps(obj), encoding="utf-8")
                paths.append(path)
            script = Path(__file__).resolve().parents[1] / "scripts" / "qualify_exact_actions.py"
            p = subprocess.run([sys.executable, "-I", str(script), "--sha", SHA,
                                "--ci-run", str(paths[0]), "--ci-jobs", str(paths[1]),
                                "--windows-run", str(paths[2]), "--windows-jobs", str(paths[3])],
                               capture_output=True, text=True, check=False)
            self.assertEqual(p.returncode, 1)
            self.assertEqual(json.loads(p.stdout)["status"], "BLOCKED")

    def _build(self):
        return next(j for j in self.wj["jobs"] if j["name"] == "build")

    def _step(self, name):
        return next(s for s in self._build()["steps"] if s["name"] == name)

    def _remove_step(self, name):
        self._build()["steps"] = [s for s in self._build()["steps"] if s["name"] != name]


if __name__ == "__main__":
    unittest.main(verbosity=2)
