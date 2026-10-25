"""Reject impossible reuse of a GitHub job ID across independent workflow runs."""
import unittest

from test_qualify_exact_actions import SHA, fixture
from scripts.qualify_exact_actions import GateError, qualify


class GlobalJobIdentityTests(unittest.TestCase):
    def setUp(self):
        self.ci, self.cj, self.win, self.wj = fixture()

    def qualify(self):
        return qualify(self.ci, self.cj, self.win, self.wj, SHA)

    def test_distinct_workflow_job_ids_are_accepted(self):
        self.assertEqual(self.qualify()["status"], "PASS")

    def test_ci_test_id_cannot_reappear_as_windows_build_id(self):
        ci_test = next(job for job in self.cj["jobs"] if job["name"].startswith("test ("))
        windows_build = next(job for job in self.wj["jobs"] if job["name"] == "build")
        windows_build["id"] = ci_test["id"]
        with self.assertRaisesRegex(GateError, "share a GitHub job id"):
            self.qualify()

    def test_admission_id_cannot_reappear_across_workflows(self):
        ci_admission = next(job for job in self.cj["jobs"] if job["name"] == "superseded_run_admission")
        win_admission = next(job for job in self.wj["jobs"] if job["name"] == "superseded_run_admission")
        win_admission["id"] = ci_admission["id"]
        with self.assertRaisesRegex(GateError, "share a GitHub job id"):
            self.qualify()


if __name__ == "__main__":
    unittest.main()
