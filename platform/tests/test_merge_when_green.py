"""Exact-head Dev merge guard tests; mutations are counted at the fake boundary."""

import base64
import contextlib
import hashlib
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

WORKFLOW_PATH = SCRIPT.parents[1] / guard.WORKFLOW
CURRENT_WORKFLOW = WORKFLOW_PATH.read_bytes()
PRE_PARTITION_WORKFLOW = Path(__file__).with_name("fixtures").joinpath(
    "lint-and-test-pre-partition.txt").read_bytes()
INITIAL_PARTITIONED_WORKFLOW = Path(__file__).with_name("fixtures").joinpath(
    "lint-and-test-python-partition.txt").read_bytes()


def _blob_sha(source):
    return hashlib.sha1(b"blob " + str(len(source)).encode() + b"\0" + source).hexdigest()


assert _blob_sha(PRE_PARTITION_WORKFLOW) == guard.PRE_PARTITION_WORKFLOW_BLOB_SHA
assert _blob_sha(INITIAL_PARTITIONED_WORKFLOW) == guard.INITIAL_PARTITIONED_WORKFLOW_BLOB_SHA
assert _blob_sha(CURRENT_WORKFLOW) == guard.PARTITIONED_WORKFLOW_BLOB_SHA

HEAD = "a" * 40
BASE = "b" * 40
PR = 532


class FakeGitHub:
    def __init__(self):
        self.calls = []
        self.merge_calls = []
        self.pr = {
            "number": PR, "state": "open", "draft": False, "merged": False,
            "base": {"ref": "dev", "sha": BASE, "repo": {"full_name": guard.REPO}},
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
        self.extra_jobs = []
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
        self.statuses = []
        self.status_pages = None
        self.thread_page_info = {"hasNextPage": False, "endCursor": None}
        self.workflow_sources = {
            BASE: CURRENT_WORKFLOW,
            HEAD: CURRENT_WORKFLOW,
        }
        self.tree_overrides = {}

    def api(self, endpoint, fields=()):
        self.calls.append((endpoint, tuple(fields)))
        if endpoint.startswith(f"repos/{guard.REPO}/contents/{guard.WORKFLOW}?ref="):
            sha = endpoint.rsplit("=", 1)[1]
            source = self.workflow_sources[sha]
            blob_sha = hashlib.sha1(b"blob " + str(len(source)).encode() + b"\0" + source).hexdigest()
            return {"type": "file", "path": guard.WORKFLOW, "encoding": "base64", "size": len(source),
                    "sha": blob_sha, "content": base64.encodebytes(source).decode()}
        if endpoint.startswith(f"repos/{guard.REPO}/git/commits/"):
            sha = endpoint.rsplit("/", 1)[1]
            return {"sha": sha, "tree": {"sha": "c" * 40 if sha == BASE else "d" * 40}}
        if endpoint.startswith(f"repos/{guard.REPO}/git/trees/"):
            tree_sha = endpoint.rsplit("/", 1)[1]
            source = self.workflow_sources[BASE if tree_sha == "c" * 40 else HEAD]
            blob_sha = hashlib.sha1(b"blob " + str(len(source)).encode() + b"\0" + source).hexdigest()
            return self.tree_overrides.get(tree_sha, {
                "sha": tree_sha, "truncated": False,
                "tree": [{"path": guard.WORKFLOW, "mode": "100644", "type": "blob",
                          "sha": blob_sha, "size": len(source)}],
            })
        if endpoint == f"repos/{guard.REPO}/pulls/{PR}":
            return self.pr
        if endpoint == f"repos/{guard.REPO}/pulls/{PR}/files":
            page = int(dict(fields)["page"])
            return self.file_pages[page - 1] if self.file_pages is not None else [
                {"filename": filename, "status": "modified"} for filename in self.files
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
        if endpoint == f"repos/{guard.REPO}/commits/{HEAD}/statuses":
            page = int(dict(fields)["page"])
            pages = self.status_pages or [self.statuses]
            return pages[page - 1] if page <= len(pages) else []
        if endpoint.startswith(f"repos/{guard.REPO}/actions/runs?"):
            page = int(dict(fields)["page"])
            return {"total_count": len(self.runs), "workflow_runs": self.runs if page == 1 else []}
        if endpoint == f"repos/{guard.REPO}/actions/runs/900/jobs?filter=latest":
            jobs = list(self.jobs.values()) + self.extra_jobs
            return {"total_count": len(jobs), "jobs": jobs}
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "nodes": self.threads, "pageInfo": self.thread_page_info,
            }}}}}
        raise AssertionError(endpoint)

    @property
    def workflow_source(self):
        return self.workflow_sources[BASE]

    @workflow_source.setter
    def workflow_source(self, value):
        self.workflow_sources[BASE] = value
        self.workflow_sources[HEAD] = value

    def set_partition_transition(self):
        self.workflow_sources[BASE] = PRE_PARTITION_WORKFLOW
        self.workflow_sources[HEAD] = INITIAL_PARTITIONED_WORKFLOW
        self.files = [guard.WORKFLOW]
        for name in ("Go lint (uhhcraft)", "Go test (uhhcraft)", "Go build (uhhcraft image)"):
            self.jobs[name]["conclusion"] = "success"
            next(check for check in self.checks if check["name"] == name)["conclusion"] = "success"

    def set_partition_followup(self):
        self.workflow_sources[BASE] = INITIAL_PARTITIONED_WORKFLOW
        self.workflow_sources[HEAD] = CURRENT_WORKFLOW
        self.files = [guard.WORKFLOW]
        for name in ("Go lint (uhhcraft)", "Go test (uhhcraft)", "Go build (uhhcraft image)"):
            self.jobs[name]["conclusion"] = "success"
            next(check for check in self.checks if check["name"] == name)["conclusion"] = "success"

    def corrupt_blob(self, sha):
        original = self.api

        def changed(endpoint, fields=()):
            result = original(endpoint, fields)
            if endpoint.startswith(f"repos/{guard.REPO}/contents/"):
                result["sha"] = sha
            return result

        self.api = changed

    def corrupt_content(self, field, value):
        original = self.api

        def changed(endpoint, fields=()):
            result = original(endpoint, fields)
            if endpoint.startswith(f"repos/{guard.REPO}/contents/"):
                result[field] = value
            return result

        self.api = changed

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
        self.assertEqual(sum(endpoint == f"repos/{guard.REPO}/pulls/{PR}" for endpoint, _ in api.calls), 5)

    def test_stale_head_refuses_without_merge(self):
        api = FakeGitHub()
        api.pr["head"]["sha"] = "b" * 40
        run_refusal(self, api, "match --head")

    def test_missing_running_failing_and_unconditional_skipped_jobs_refuse(self):
        changes = [
            (lambda api: api.jobs.pop("Python core"), "Missing expected Actions jobs"),
            (lambda api: api.jobs.pop("Python Playbook Safety"), "Missing expected Actions jobs"),
            (lambda api: api.jobs["Python Playbook Safety"].update(
                status="in_progress", conclusion=None), "incomplete"),
            (lambda api: api.jobs["Python Playbook Safety"].update(
                conclusion="cancelled"), "is cancelled"),
            (lambda api: api.jobs["Python Playbook Safety"].update(
                conclusion="skipped"), "was skipped"),
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

    def test_exact_partition_transition_is_allowed(self):
        api = FakeGitHub()
        api.set_partition_transition()
        self.assertEqual(guard.main(["--pr", str(PR), "--head", HEAD], api), 0)
        self.assertEqual(api.merge_calls, [])

    def test_exact_partition_followup_is_allowed(self):
        api = FakeGitHub()
        api.set_partition_followup()
        self.assertEqual(guard.main(["--pr", str(PR), "--head", HEAD], api), 0)
        self.assertEqual(api.merge_calls, [])

    def test_partitioned_workflow_is_accepted_for_later_prs(self):
        api = FakeGitHub()
        self.assertEqual(guard.main(["--pr", str(PR), "--head", HEAD], api), 0)
        self.assertEqual(api.merge_calls, [])

    def test_only_exact_transition_or_partitioned_baseline_is_accepted(self):
        cases = [
            ("unsupported base", lambda api: api.workflow_sources.__setitem__(BASE, b"other")),
            ("extra candidate byte", lambda api: api.workflow_sources.__setitem__(
                HEAD, CURRENT_WORKFLOW + b"# extra\n")),
            ("reverse transition", lambda api: (api.workflow_sources.__setitem__(BASE, CURRENT_WORKFLOW),
                                                  api.workflow_sources.__setitem__(HEAD, PRE_PARTITION_WORKFLOW))),
            ("old baseline unchanged", lambda api: (api.workflow_sources.__setitem__(BASE, PRE_PARTITION_WORKFLOW),
                                                      api.workflow_sources.__setitem__(HEAD, PRE_PARTITION_WORKFLOW))),
        ]
        for label, change in cases:
            with self.subTest(label=label):
                api = FakeGitHub()
                api.set_partition_transition()
                change(api)
                run_refusal(self, api)

    def test_original_partition_transition_sha_remains_required(self):
        api = FakeGitHub()
        api.set_partition_transition()
        api.workflow_sources[HEAD] = CURRENT_WORKFLOW
        run_refusal(self, api, "exact original Python Playbook Safety partition")

    def test_partition_transition_requires_one_modified_workflow_record(self):
        for status, previous in (("renamed", "old-workflow.yml"), ("added", None),
                                 ("removed", None), ("copied", None), ("modified", "old-workflow.yml")):
            with self.subTest(status=status, previous=previous):
                api = FakeGitHub()
                api.set_partition_followup()
                api.file_pages = [[{"filename": guard.WORKFLOW, "status": status,
                                    **({"previous_filename": previous} if previous else {})}]]
                run_refusal(self, api, "one modified record")
        api = FakeGitHub()
        api.set_partition_followup()
        api.files = []
        run_refusal(self, api, "one modified record")

    def test_partitioned_baseline_rejects_misleading_workflow_change_record(self):
        api = FakeGitHub()
        api.file_pages = [[{"filename": guard.WORKFLOW, "status": "modified"}]]
        run_refusal(self, api, "byte-identical on subsequent PRs")

    def test_partition_job_must_succeed_even_when_the_aggregate_run_is_green(self):
        api = FakeGitHub()
        api.jobs["Python Playbook Safety"].update(conclusion="failure")
        next(check for check in api.checks if check["name"] == "Python Playbook Safety")["conclusion"] = "failure"
        run_refusal(self, api, "Python Playbook Safety is failure")

    def test_duplicate_partition_job_refuses_even_when_both_are_green(self):
        api = FakeGitHub()
        api.extra_jobs = [dict(api.jobs["Python Playbook Safety"])]
        run_refusal(self, api, "Unknown or duplicate Actions job")

    def test_stale_partition_check_run_refuses_even_when_job_is_green(self):
        api = FakeGitHub()
        next(check for check in api.checks if check["name"] == "Python Playbook Safety")["head_sha"] = "f" * 40
        run_refusal(self, api, "missing, stale, or incomplete")

    def test_missing_go_check_refuses_when_workflow_is_touched(self):
        api = FakeGitHub()
        api.set_partition_transition()
        api.jobs.pop("Go lint (uhhcraft)")
        run_refusal(self, api, "Missing expected Actions jobs")

    def test_other_workflow_path_changes_refuse(self):
        api = FakeGitHub()
        api.files = [".github/workflows/other.yml"]
        run_refusal(self, api, "another protected workflow path")

    def test_pinned_workflow_blob_tree_and_mode_evidence_fail_closed(self):
        cases = [
            ("blob SHA", lambda api: api.corrupt_blob("e" * 40)),
            ("size", lambda api: api.corrupt_content("size", 1)),
            ("path", lambda api: api.corrupt_content("path", "elsewhere.yml")),
            ("encoding", lambda api: api.corrupt_content("encoding", "utf-8")),
            ("base64", lambda api: api.corrupt_content("content", "%%%")),
            ("truncated", lambda api: api.tree_overrides.update({"d" * 40: {
                "sha": "d" * 40, "truncated": True, "tree": []}})),
            ("mode", lambda api: api.tree_overrides.update({"d" * 40: {
                "sha": "d" * 40, "truncated": False,
                "tree": [{"path": guard.WORKFLOW, "mode": "100755", "type": "blob",
                          "sha": hashlib.sha1(
                              b"blob " + str(len(api.workflow_sources[HEAD])).encode() + b"\0"
                              + api.workflow_sources[HEAD]).hexdigest(),
                          "size": len(api.workflow_sources[HEAD])}]}})),
        ]
        for label, corrupt in cases:
            with self.subTest(label=label):
                api = FakeGitHub()
                corrupt(api)
                run_refusal(self, api)

    def test_workflow_status_is_required_and_rename_evidence_is_rejected(self):
        for record in ({"filename": guard.WORKFLOW},
                       {"filename": guard.WORKFLOW, "status": "renamed"},
                       {"filename": "other.yml", "status": "renamed", "previous_filename": guard.WORKFLOW}):
            with self.subTest(record=record):
                api = FakeGitHub()
                api.set_partition_transition()
                api.file_pages = [[record]]
                run_refusal(self, api)

    def test_workflow_path_keeps_all_go_jobs_required(self):
        self.assertEqual(guard.required_jobs([guard.WORKFLOW])["Go lint (uhhcraft)"], True)
        self.assertEqual(guard.required_jobs([guard.WORKFLOW])["Go test (uhhcraft)"], True)
        self.assertEqual(guard.required_jobs([guard.WORKFLOW])["Go build (uhhcraft image)"], True)

    def test_pr_base_and_head_movement_during_validation_refuse(self):
        def moving_api(field, api):
            original = api.api
            reads = 0

            def moving(endpoint, fields=()):
                nonlocal reads
                if endpoint == f"repos/{guard.REPO}/pulls/{PR}":
                    reads += 1
                    if reads == 2:
                        if field == "base":
                            api.pr["base"]["sha"] = "e" * 40
                        else:
                            api.pr["head"]["sha"] = "f" * 40
                return original(endpoint, fields)

            return moving

        for field in ("base", "head"):
            with self.subTest(field=field):
                api = FakeGitHub()
                api.api = moving_api(field, api)
                run_refusal(self, api, "match --head" if field == "head"
                            else "moved while collecting")

    def test_pr_base_movement_between_merge_validations_refuses(self):
        api = FakeGitHub()
        api.workflow_sources["e" * 40] = api.workflow_sources[BASE]
        original = api.api
        reads = 0

        def moving(endpoint, fields=()):
            nonlocal reads
            if endpoint == f"repos/{guard.REPO}/pulls/{PR}":
                reads += 1
                if reads == 3:
                    api.pr["base"]["sha"] = "e" * 40
            return original(endpoint, fields)

        api.api = moving
        run_refusal(self, api, "moved between merge-gate validations")

    def test_paginated_changed_files_are_used_for_path_evidence(self):
        api = FakeGitHub()
        api.file_pages = [[{"filename": f"docs/{n}.md", "status": "modified"} for n in range(100)],
                          [{"filename": "platform/services/uhhcraft/deployment/main.go", "status": "modified"}]]
        run_refusal(self, api, "was skipped")
        self.assertTrue(any(dict(fields).get("page") == "2" for endpoint, fields in api.calls
                            if endpoint.endswith("/files")))

    def test_renamed_go_file_remains_in_path_skip_evidence(self):
        api = FakeGitHub()
        api.file_pages = [[{"filename": "docs/moved.md", "status": "renamed",
                            "previous_filename": "platform/services/uhhcraft/deployment/main.go"}]]
        run_refusal(self, api, "was skipped")

    def test_renamed_files_require_a_valid_previous_filename(self):
        for previous_filename in ("missing", None, "", 42):
            with self.subTest(previous_filename=previous_filename):
                api = FakeGitHub()
                file = {"filename": "docs/moved.md", "status": "renamed"}
                if previous_filename != "missing":
                    file["previous_filename"] = previous_filename
                api.file_pages = [[file]]
                run_refusal(self, api, "Changed-file rename evidence is incomplete")

    def test_exact_head_commit_status_failure_refuses_and_coderabbit_is_optional(self):
        api = FakeGitHub()
        api.statuses = [
            {"context": "ci/build", "state": "failure", "created_at": "2026-10-10T12:00:00Z"},
            {"context": "CodeRabbit", "state": "error", "created_at": "2026-10-10T12:01:00Z"},
        ]
        run_refusal(self, api, "Latest commit status is not successful")
        api.statuses[0]["state"] = "success"
        self.assertTrue(guard.validate(api, PR, HEAD, None))

    def test_commit_statuses_are_paginated_and_latest_state_wins(self):
        api = FakeGitHub()
        api.status_pages = [
            [{"context": "ci/build", "state": "success", "created_at": "2026-10-10T12:01:00Z"}
             for _ in range(100)],
            [{"context": "ci/build", "state": "failure", "created_at": "2026-10-10T12:00:00Z"}],
        ]
        self.assertTrue(guard.validate(api, PR, HEAD, None))
        self.assertTrue(any(dict(fields).get("page") == "2" for endpoint, fields in api.calls
                            if endpoint.endswith("/statuses")))

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

    def test_equal_timestamp_latest_review_verdict_wins_in_api_order(self):
        api = FakeGitHub()
        api.reviews.append({"user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED",
                            "commit_id": HEAD, "submitted_at": api.reviews[0]["submitted_at"]})
        run_refusal(self, api, "changes-requested")

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

    def test_review_threads_require_boolean_pagination_and_resolution_fields(self):
        invalid = [
            ({}, []),
            ({"hasNextPage": None, "endCursor": None}, []),
            ({"hasNextPage": True, "endCursor": None}, []),
            ({"hasNextPage": False, "endCursor": None}, [{"id": "thread-1"}]),
            ({"hasNextPage": False, "endCursor": None}, [{"id": "thread-1", "isResolved": "false"}]),
        ]
        for page_info, threads in invalid:
            with self.subTest(page_info=page_info, threads=threads):
                api = FakeGitHub()
                api.thread_page_info = page_info
                api.threads = threads
                run_refusal(self, api, "Review-thread")

    def test_workflow_policy_drift_refuses_before_merge(self):
        changed = b"name: Changed\non:\n  pull_request:\n    branches: [dev]\n"
        with self.assertRaisesRegex(guard.Refusal, "Dev PR workflow changed"):
            guard.check_workflow_policy(changed, partitioned=True)

    def test_path_gate_drift_refuses_before_merge(self):
        changed = CURRENT_WORKFLOW.replace(
            b"              - '.github/workflows/lint-and-test.yml'\n",
            b"              - '.github/workflows/lint-and-test.yml'\n              - 'platform/services/other/**'\n",
            1,
        )
        with self.assertRaisesRegex(guard.Refusal, "Dev PR workflow changed"):
            guard.check_workflow_policy(changed, partitioned=True)

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
