"""Exact-head Dev merge guard tests; mutations are counted at the fake boundary."""

import base64
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/merge-when-green.py"
SPEC = importlib.util.spec_from_file_location("merge_when_green", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

HEAD = "a" * 40
PR = 532


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.merge_calls = []
        self.pr = {
            "number": PR, "state": "open", "draft": False, "merged": False,
            "base": {"ref": "dev", "repo": {"full_name": guard.REPO}},
            "head": {"sha": HEAD, "repo": {"full_name": guard.REPO}},
            "user": {"login": "author"},
        }
        self.files = ["docs/change.md"]
        self.reviews = [{"user": {"login": "reviewer"}, "state": "APPROVED",
                         "commit_id": HEAD, "submitted_at": "2026-10-10T12:00:00Z",
                         "html_url": "https://github.com/uhstray-io/agent-cloud/pull/532#pullrequestreview-1"}]
        self.threads = []
        self.comments = [{"id": 1, "body": "Addressed in this review."}]
        self.jobs = {}
        self.runs = [{"id": 900, "workflow_id": 7, "run_number": 4, "run_attempt": 1,
                      "head_sha": HEAD, "event": "pull_request", "path": guard.WORKFLOW + "@refs/pull/532/merge",
                      "pull_requests": [{"number": PR}],
                      "status": "completed", "conclusion": "success"}]
        for index, name in enumerate(sorted(guard.EXPECTED)):
            expected = guard.required_jobs(self.files)[name]
            conclusion = "success" if expected else "skipped"
            self.jobs[name] = {"name": name, "status": "completed", "conclusion": conclusion,
                               "check_run_url": f"https://api.github.com/repos/{guard.REPO}/check-runs/{index + 1}"}
        self.checks = [
            {"id": i + 1, "name": name, "head_sha": HEAD, "status": "completed",
             "conclusion": job["conclusion"], "html_url": f"https://github.com/checks/{i + 1}",
             "app": {"slug": "github-actions"}}
            for i, (name, job) in enumerate(sorted(self.jobs.items()))
        ]
        self.file_pages = None
        self.check_pages = None
        self.workflow_source = (SCRIPT.parents[1] / guard.WORKFLOW).read_bytes()

    def api(self, endpoint, fields=()):
        self.calls.append((endpoint, tuple(fields)))
        if endpoint == f"repos/{guard.REPO}/contents/{guard.WORKFLOW}?ref=dev":
            return {"content": base64.b64encode(self.workflow_source).decode()}
        if endpoint == f"repos/{guard.REPO}/pulls/{PR}":
            return self.pr
        if endpoint == f"repos/{guard.REPO}/pulls/{PR}/files":
            page = int(dict(fields)["page"])
            return self.file_pages[page - 1] if self.file_pages is not None else [
                {"filename": filename} for filename in self.files
            ]
        if endpoint == f"repos/{guard.REPO}/pulls/{PR}/reviews":
            return self.reviews
        if endpoint == f"repos/{guard.REPO}/issues/{PR}/comments":
            return self.comments
        if endpoint == f"repos/{guard.REPO}/commits/{HEAD}/check-runs":
            page = int(dict(fields)["page"])
            batch = self.check_pages[page - 1] if self.check_pages is not None else self.checks
            total = sum(map(len, self.check_pages)) if self.check_pages is not None else len(self.checks)
            return {"total_count": total, "check_runs": batch}
        if endpoint.startswith(f"repos/{guard.REPO}/actions/runs?"):
            page = int(dict(fields)["page"])
            return {"total_count": len(self.runs), "workflow_runs": self.runs if page == 1 else []}
        if endpoint == f"repos/{guard.REPO}/actions/runs/900/jobs?filter=latest":
            return {"total_count": len(self.jobs), "jobs": list(self.jobs.values())}
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": self.threads, "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}}
        raise AssertionError(endpoint)

    def merge(self, pr, sha):
        self.merge_calls.append(["gh", "pr", "merge", str(pr), "--repo", guard.REPO,
                                 "--merge", "--match-head-commit", sha])
        self.pr["merged"] = True


def run_refusal(test, api, expected="", *extra):
    with contextlib.redirect_stderr(io.StringIO()) as stderr:
        result = guard.main(["--pr", str(PR), "--head", HEAD, "--merge", "--feedback-addressed", *extra], api)
    test.assertEqual(result, 1)
    test.assertEqual(api.merge_calls, [])
    if expected:
        test.assertIn(expected, stderr.getvalue())


class MergeGuardTests(unittest.TestCase):
    def test_read_only_validation_passes_without_merging(self):
        api = FakeGitHub()
        self.assertEqual(guard.main(["--pr", str(PR), "--head", HEAD], api), 0)
        self.assertEqual(api.merge_calls, [])

    def test_explicit_merge_revalidates_and_uses_one_expected_sha(self):
        api = FakeGitHub()
        self.assertEqual(guard.main(["--pr", str(PR), "--head", HEAD, "--merge", "--feedback-addressed"], api), 0)
        self.assertEqual(api.merge_calls, [["gh", "pr", "merge", str(PR), "--repo", guard.REPO,
                                            "--merge", "--match-head-commit", HEAD]])
        self.assertEqual(sum(endpoint == f"repos/{guard.REPO}/pulls/{PR}" for endpoint, _ in api.calls), 3)

    def test_stale_head_refuses_without_merge(self):
        api = FakeGitHub()
        api.pr["head"]["sha"] = "b" * 40
        run_refusal(self, api, "match --head")

    def test_missing_running_failing_and_unconditional_skipped_jobs_refuse(self):
        changes = [
            (lambda api: api.jobs.pop("Python core"), "Missing expected Actions jobs"),
            (lambda api: api.jobs["Bash Tests"].update(status="in_progress", conclusion=None), "incomplete"),
            (lambda api: api.jobs["Security Scan"].update(conclusion="failure"), "failure"),
            (lambda api: api.jobs["Static Analysis"].update(conclusion="skipped"), "was skipped"),
        ]
        for change, message in changes:
            with self.subTest(message=message):
                api = FakeGitHub()
                change(api)
                by_name = {check["name"]: check for check in api.checks}
                for name, job in api.jobs.items():
                    by_name[name]["conclusion"] = job["conclusion"]
                    by_name[name]["status"] = job["status"]
                run_refusal(self, api, message)

    def test_path_gated_skips_pass_only_when_files_prove_the_paths_absent(self):
        api = FakeGitHub()
        self.assertTrue(guard.validate(api, PR, HEAD, None))
        api.files = ["platform/services/uhhcraft/deployment/internal/main.go"]
        run_refusal(self, api, "was skipped")

    def test_changes_to_the_dev_workflow_refuse_before_merge(self):
        api = FakeGitHub()
        api.files = [guard.WORKFLOW]
        run_refusal(self, api, "PR changes the protected lint-and-test workflow")

    def test_paginated_changed_files_are_used_for_path_evidence(self):
        api = FakeGitHub()
        api.file_pages = [[{"filename": f"docs/{n}.md"} for n in range(100)],
                          [{"filename": "platform/services/uhhcraft/deployment/main.go"}]]
        run_refusal(self, api, "was skipped")
        self.assertTrue(any(dict(fields).get("page") == "2" for endpoint, fields in api.calls
                            if endpoint.endswith("/files")))

    def test_exact_head_check_runs_are_paginated_and_unknown_runs_refuse(self):
        api = FakeGitHub()
        api.check_pages = [api.checks + [dict(api.checks[0], id=i + 1000) for i in range(88)],
                           [dict(api.checks[0], id=2000)]]
        run_refusal(self, api, "Unknown or stale")
        self.assertTrue(any(dict(fields).get("page") == "2" for endpoint, fields in api.calls
                            if endpoint.endswith("check-runs")))

    def test_queued_rerun_supersedes_an_older_successful_workflow_run(self):
        api = FakeGitHub()
        api.runs.append({**api.runs[0], "id": 901, "run_number": 4, "run_attempt": 2,
                         "status": "queued", "conclusion": None})
        run_refusal(self, api, "Latest workflow run 901 is queued")

    def test_unassociated_workflow_run_is_not_used_as_pr_evidence(self):
        api = FakeGitHub()
        api.runs[0]["pull_requests"] = []
        run_refusal(self, api, "Unknown or non-PR workflow")

    def test_stale_review_cannot_approve_the_expected_head(self):
        api = FakeGitHub()
        api.reviews[0]["commit_id"] = "b" * 40
        run_refusal(self, api, "No independent approval")

    def test_structured_sol_receipt_can_supply_final_head_approval(self):
        api = FakeGitHub()
        api.reviews = []
        receipt = {"repository": guard.REPO, "pr": PR, "head_sha": HEAD,
                   "reviewer": "GPT-6.1 Sol", "session": "review-session-1",
                   "verdict": "APPROVED", "evidence": "review-notes.md"}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as source:
            json.dump(receipt, source)
            source.flush()
            self.assertTrue(guard.validate(api, PR, HEAD, source.name)["receipt"])
        self.assertEqual(api.merge_calls, [])

    def test_changes_requested_review_and_unresolved_outdated_threads_refuse(self):
        api = FakeGitHub()
        api.reviews.append({"user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED",
                            "commit_id": HEAD, "submitted_at": "2026-10-10T13:00:00Z"})
        run_refusal(self, api, "changes-requested")
        api = FakeGitHub()
        api.threads = [{"id": "thread-1", "isResolved": False, "isOutdated": True}]
        run_refusal(self, api, "unresolved review thread")

    def test_workflow_policy_drift_refuses_before_merge(self):
        api = FakeGitHub()
        original = api.api

        def drift(endpoint, fields=()):
            if endpoint.startswith(f"repos/{guard.REPO}/contents/"):
                changed = b"name: Changed\non:\n  pull_request:\n    branches: [dev]\n"
                return {"content": base64.b64encode(changed).decode()}
            return original(endpoint, fields)

        api.api = drift
        run_refusal(self, api, "workflow changed")

    def test_path_gate_drift_refuses_before_merge(self):
        api = FakeGitHub()
        api.workflow_source = api.workflow_source.replace(
            b"              - '.github/workflows/lint-and-test.yml'\n",
            b"              - '.github/workflows/lint-and-test.yml'\n              - 'platform/services/other/**'\n",
            1,
        )
        run_refusal(self, api, "workflow changed")

    def test_ambiguous_merge_response_is_read_back_and_never_retried(self):
        api = FakeGitHub()

        def uncertain(_pr, _sha):
            api.merge_calls.append(["gh", "pr", "merge", str(PR), "--repo", guard.REPO,
                                    "--merge", "--match-head-commit", HEAD])
            raise guard.Refusal("merge outcome unavailable")

        api.merge = uncertain
        self.assertEqual(guard.main(["--pr", str(PR), "--head", HEAD, "--merge", "--feedback-addressed"], api), 1)
        self.assertEqual(api.merge_calls, [["gh", "pr", "merge", str(PR), "--repo", guard.REPO,
                                            "--merge", "--match-head-commit", HEAD]])

if __name__ == "__main__":
    unittest.main()
