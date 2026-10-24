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
    _WINDOWS_ADMISSION, _WINDOWS_REQUIRED_STEPS, _NON_PR_ADMISSION,
    GateError, qualify,
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

    def _non_pr_admission(self, jobs):
        for job in jobs["jobs"]:
            if job["name"] != "superseded_run_admission":
                continue
            for step in job["steps"]:
                step["conclusion"] = "skipped"
                step["status"] = "completed"
            job["steps"].append({"name": _NON_PR_ADMISSION,
                                 "status": "completed", "conclusion": "success"})
            return job
        raise AssertionError("admission job missing")

    def test_push_main_uses_non_pr_admission(self):
        self.ci["event"] = "push"
        self.win["event"] = "push"
        self._non_pr_admission(self.cj)
        self._non_pr_admission(self.wj)
        self.assertEqual(self.check()["status"], "PASS")

    def test_dispatch_windows_uses_non_pr_admission(self):
        self.win["event"] = "workflow_dispatch"
        self._non_pr_admission(self.wj)
        self.assertEqual(self.check()["status"], "PASS")

    def test_non_pr_admission_must_execute(self):
        self.ci["event"] = "push"
        job = self._non_pr_admission(self.cj)
        job["steps"][-1]["conclusion"] = "skipped"
        with self.assertRaisesRegex(GateError, "Admit non-PR"):
            self.check()

    def test_non_pr_admission_cannot_be_missing(self):
        self.win["event"] = "push"
        job = self._non_pr_admission(self.wj)
        job["steps"].pop()
        with self.assertRaisesRegex(GateError, "missing required steps"):
            self.check()

    def test_non_pr_event_rejects_pr_admission_success(self):
        self.win["event"] = "workflow_dispatch"
        job = self._non_pr_admission(self.wj)
        job["steps"][0]["conclusion"] = "success"
        with self.assertRaisesRegex(GateError, "wrong event admission path"):
            self.check()

    def test_pr_event_rejects_non_pr_admission_success(self):
        admission = next(j for j in self.cj["jobs"]
                         if j["name"] == "superseded_run_admission")
        admission["steps"].append({"name": _NON_PR_ADMISSION,
                                    "status": "completed", "conclusion": "success"})
        with self.assertRaisesRegex(GateError, "wrong event admission path"):
            self.check()

    def test_ci_workflow_dispatch_is_not_supported(self):
        self.ci["event"] = "workflow_dispatch"
        with self.assertRaisesRegex(GateError, "unsupported workflow event"):
            self.check()

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
        with self.assertRaisesRegex(GateError, "rerun job attempt not proven"): self.check()

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

    def test_rest_first_attempt_without_job_attempt_passes(self):
        # GitHub's documented REST workflow-job response omits run_attempt.
        for run, jobs in ((self.ci, self.cj), (self.win, self.wj)):
            run["run_attempt"] = 1
            for job in jobs["jobs"]:
                job.pop("run_attempt")
        self.assertEqual(self.check()["status"], "PASS")

    def test_rest_rerun_without_attempt_provenance_blocks(self):
        for job in self.wj["jobs"]:
            job.pop("run_attempt")
        with self.assertRaisesRegex(GateError, "rerun job attempt not proven"):
            self.check()

    def test_attempt_scoped_response_rerun_passes(self):
        # Caller supplies the attempt number of the /attempts/N/jobs URL.
        for jobs in (self.cj, self.wj):
            jobs["run_attempt"] = 2
            for job in jobs["jobs"]:
                job.pop("run_attempt")
        self.assertEqual(self.check()["status"], "PASS")

    def test_attempt_scoped_response_mismatch_blocks(self):
        self.wj["run_attempt"] = 1
        with self.assertRaisesRegex(GateError, "jobs response attempt mismatch"):
            self.check()

    def test_boolean_attempt_provenance_blocks(self):
        self.wj["run_attempt"] = True
        with self.assertRaisesRegex(GateError, "jobs response attempt mismatch"):
            self.check()

    def test_wrong_job_attempt_blocks_even_with_matching_envelope(self):
        self.wj["run_attempt"] = 2
        self._build()["run_attempt"] = 1
        with self.assertRaisesRegex(GateError, "job attempt mismatch"):
            self.check()

    def test_duplicate_run_conclusion_cannot_override_failure(self):
        from scripts.qualify_exact_actions import _load
        raw = json.dumps(self.ci).replace(
            '"conclusion": "success"',
            '"conclusion": "failure", "conclusion": "success"', 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ambiguous.json"
            path.write_text(raw, encoding="utf-8")
            with self.assertRaisesRegex(GateError, "duplicate object member"):
                _load(str(path))

    def test_duplicate_nested_job_step_cannot_override_failure(self):
        from scripts.qualify_exact_actions import _load
        raw = json.dumps(self.wj).replace(
            '"conclusion": "success"',
            '"conclusion": "failure", "conclusion": "success"', 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ambiguous-jobs.json"
            path.write_text(raw, encoding="utf-8")
            with self.assertRaisesRegex(GateError, "duplicate object member"):
                _load(str(path))

    def test_nonfinite_json_evidence_is_rejected(self):
        from scripts.qualify_exact_actions import _load
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nan.json"
            path.write_text('{"unused": NaN}', encoding="utf-8")
            with self.assertRaisesRegex(GateError, "non-finite number"):
                _load(str(path))

    def test_oversized_json_evidence_is_rejected(self):
        from scripts.qualify_exact_actions import _load
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huge.json"
            path.write_bytes(b" " * 2_000_001)
            with self.assertRaisesRegex(GateError, "exceeds 2 MB"):
                _load(str(path))

    def _build(self):
        return next(j for j in self.wj["jobs"] if j["name"] == "build")

    def _step(self, name):
        return next(s for s in self._build()["steps"] if s["name"] == name)

    def _remove_step(self, name):
        self._build()["steps"] = [s for s in self._build()["steps"] if s["name"] != name]


if __name__ == "__main__":
    unittest.main(verbosity=2)
