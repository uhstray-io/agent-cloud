#!/usr/bin/env python3
"""Read-only Dev PR merge gate; --merge performs one explicit, SHA-bound merge."""

import argparse
import base64
import json
import re
import subprocess
import sys
from urllib.parse import urlencode

REPO = "uhstray-io/agent-cloud"
WORKFLOW = ".github/workflows/lint-and-test.yml"
EXPECTED = {
    "Detect changes", "Static Analysis", "Security Scan", "Python core",
    "Python Semaphore", "Python NetBox", "Bash Tests", "Rego Tests", "Unit Tests",
    "Go lint (uhhcraft)", "Go test (uhhcraft)", "Go build (uhhcraft image)",
}
GO_PATH = "platform/services/uhhcraft/deployment/"
GO_WORKFLOW = WORKFLOW
DOCKERFILES = {
    "platform/services/uhhcraft/deployment/Dockerfile",
    "platform/services/inference-comfyui/deployment/Dockerfile",
    "platform/services/inference-hunyuan3d/deployment/Dockerfile",
}
SHA = re.compile(r"^[0-9a-f]{40}$")


class Refusal(Exception):
    pass


class GitHub:
    def __init__(self):
        self.calls = []

    def api(self, endpoint, fields=()):
        command = ["gh", "api", endpoint]
        if fields and endpoint == "graphql":
            for key, value in fields:
                command.extend(["-F", f"{key}={value}"])
        elif fields:
            command[2] += ("&" if "?" in endpoint else "?") + urlencode(fields)
        self.calls.append(command)
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
            raise Refusal(f"GitHub read failed for {endpoint}") from error

    def merge(self, pr, sha):
        command = ["gh", "pr", "merge", str(pr), "--repo", REPO,
                   "--merge", "--match-head-commit", sha]
        self.calls.append(command)
        try:
            return subprocess.run(command, capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise Refusal("SHA-bound merge command returned an error; read PR state before retrying") from error


def paged(api, endpoint, key):
    values = []
    expected_total = None
    for page in range(1, 101):
        data = api.api(endpoint, (("per_page", "100"), ("page", str(page))))
        batch = data.get(key) if key and isinstance(data, dict) else data if not key else None
        if not isinstance(batch, list):
            raise Refusal(f"Incomplete {key} pagination for {endpoint}")
        if key and isinstance(data, dict) and "total_count" not in data:
            raise Refusal(f"Pagination count is missing for {endpoint}")
        if isinstance(data, dict) and "total_count" in data:
            count = data["total_count"]
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise Refusal(f"Invalid pagination count for {endpoint}")
            if expected_total is not None and count != expected_total:
                raise Refusal(f"Pagination count changed for {endpoint}")
            expected_total = count
        values.extend(batch)
        if key == "" and len(values) >= 3000:
            raise Refusal(f"GitHub's changed-file limit prevents complete evidence for {endpoint}")
        if expected_total is not None and len(values) > expected_total:
            raise Refusal(f"Pagination exceeded the reported count for {endpoint}")
        if len(batch) < 100 or expected_total is not None and len(values) == expected_total:
            if expected_total is not None and len(values) != expected_total:
                raise Refusal(f"Pagination is incomplete for {endpoint}")
            return values
    raise Refusal(f"Pagination limit reached for {endpoint}")


def get_pr(api, number):
    return api.api(f"repos/{REPO}/pulls/{number}")


def check_pr(pr, number, sha):
    if not isinstance(pr, dict):
        raise Refusal("PR response is incomplete")
    base, head, author = pr.get("base"), pr.get("head"), pr.get("user")
    if (not isinstance(author, dict) or not isinstance(author.get("login"), str)
            or not isinstance(base, dict) or not isinstance(base.get("repo"), dict)
            or not isinstance(head, dict) or not isinstance(head.get("repo"), dict)
            or pr.get("number") != number or pr.get("state") != "open" or pr.get("draft") is not False
            or base["repo"].get("full_name") != REPO or base.get("ref") != "dev"
            or head["repo"].get("full_name") != REPO or head.get("sha") != sha):
        raise Refusal("PR must be open, ready, same-repository, target dev, and match --head")


def changed_files(api, number):
    files = paged(api, f"repos/{REPO}/pulls/{number}/files", "")
    if any(not isinstance(f, dict) or not isinstance(f.get("filename"), str) for f in files):
        raise Refusal("Changed-file evidence is incomplete")
    if any(f.get("previous_filename") is not None and not isinstance(f.get("previous_filename"), str)
           for f in files):
        raise Refusal("Changed-file rename evidence is incomplete")
    if any(f["filename"] == WORKFLOW or f.get("previous_filename") == WORKFLOW for f in files):
        raise Refusal("PR changes the protected lint-and-test workflow")
    return [path for f in files for path in (f.get("previous_filename"), f["filename"]) if path]


def required_jobs(files):
    go = any(p.startswith(GO_PATH) or p == GO_WORKFLOW for p in files)
    go_build = go or any(p in DOCKERFILES for p in files)
    return {
        "Detect changes": True, "Static Analysis": True, "Security Scan": True,
        "Python core": True, "Python Semaphore": True, "Python NetBox": True,
        "Bash Tests": True, "Rego Tests": True, "Unit Tests": True,
        "Go lint (uhhcraft)": go, "Go test (uhhcraft)": go,
        "Go build (uhhcraft image)": go_build,
    }


def check_workflow_policy(api):
    data = api.api(f"repos/{REPO}/contents/{WORKFLOW}?ref=dev")
    try:
        source = base64.b64decode(data["content"], validate=False).decode()
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as error:
        raise Refusal("Cannot verify Dev PR workflow policy") from error
    expected = {
        "changes": "Detect changes", "lint": "Static Analysis", "security": "Security Scan",
        "python-core": "Python core", "python-semaphore": "Python Semaphore",
        "python-netbox": "Python NetBox", "bats": "Bash Tests", "rego": "Rego Tests",
        "test": "Unit Tests", "go-lint": "Go lint (uhhcraft)",
        "go-test": "Go test (uhhcraft)", "go-build": "Go build (uhhcraft image)",
    }
    jobs = {}
    current = None
    in_jobs = False
    for line in source.splitlines():
        if line == "jobs:":
            in_jobs = True
            continue
        if in_jobs and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*:", line):
            break
        if not in_jobs:
            continue
        start = re.fullmatch(r"  ([a-zA-Z0-9_-]+):", line)
        if start:
            current = start.group(1)
            jobs[current] = {}
        elif current and (name := re.fullmatch(r"    name: (.+)", line)):
            jobs[current]["name"] = name.group(1)
        elif current and (condition := re.fullmatch(r"    if: (.+)", line)):
            jobs[current]["if"] = condition.group(1)
    conditions = {
        "test": "always()",
        "go-lint": "needs.changes.outputs.go == 'true'",
        "go-test": "needs.changes.outputs.go == 'true'",
        "go-build": "needs.changes.outputs.go == 'true' || needs.changes.outputs.dockerfiles == 'true'",
    }
    filters = {}
    inside_filters = False
    current_filter = None
    for line in source.splitlines():
        indent = len(line) - len(line.lstrip())
        if line.strip() == "filters: |":
            inside_filters = True
            continue
        if not inside_filters:
            continue
        if line.strip() and indent < 12:
            break
        content = line.strip()
        if indent == 12 and content.endswith(":"):
            current_filter = content[:-1]
            filters[current_filter] = []
        elif indent >= 14 and content.startswith("- ") and current_filter:
            path = re.fullmatch(r"- ['\"](.+)['\"]", content)
            if not path:
                raise Refusal("Dev PR workflow path filters changed; review the merge gate policy first")
            filters[current_filter].append(path.group(1))
    expected_filters = {
        "go": [GO_PATH + "**", GO_WORKFLOW],
        "python_inference": ["platform/services/inference-comfyui/deployment/**",
                             "platform/services/inference-hunyuan3d/deployment/**", WORKFLOW],
        "dockerfiles": ["platform/services/uhhcraft/deployment/Dockerfile",
                        "platform/services/inference-comfyui/deployment/Dockerfile",
                        "platform/services/inference-hunyuan3d/deployment/Dockerfile"],
    }
    required = [
        "name: Lint and Test", "pull_request:", "branches: [main, dev]",
        "platform/services/uhhcraft/deployment/**",
        GO_WORKFLOW,
        "platform/services/uhhcraft/deployment/Dockerfile",
        "platform/services/inference-comfyui/deployment/Dockerfile",
        "platform/services/inference-hunyuan3d/deployment/Dockerfile",
    ]
    if (jobs.keys() != expected.keys()
            or any(jobs[job].get("name") != name for job, name in expected.items())
            or any(jobs[job].get("if") != condition for job, condition in conditions.items())
            or filters != expected_filters
            or any(item not in source for item in required)):
        raise Refusal("Dev PR workflow changed; review and update the merge gate policy first")


def check_runs(api, sha):
    runs, total = [], None
    for page in range(1, 101):
        data = api.api(f"repos/{REPO}/commits/{sha}/check-runs",
                       (("per_page", "100"), ("page", str(page))))
        batch = data.get("check_runs") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise Refusal("Incomplete exact-head check-run response")
        total = data.get("total_count")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise Refusal("Exact-head check-run count is invalid")
        runs.extend(batch)
        if len(runs) >= total or len(batch) < 100:
            break
    else:
        raise Refusal("Exact-head check-run pagination limit reached")
    if total is None or len(runs) != total:
        raise Refusal("Exact-head check-run pagination is incomplete")
    return runs


def check_commit_statuses(api, sha):
    statuses = paged(api, f"repos/{REPO}/commits/{sha}/statuses", "")
    latest = {}
    for status in statuses:
        if (not isinstance(status, dict) or not isinstance(status.get("context"), str)
                or not status["context"] or not isinstance(status.get("state"), str)
                or status["state"] not in ("error", "failure", "pending", "success")):
            raise Refusal("Exact-head commit status is incomplete")
        latest.setdefault(status["context"].casefold(), status)
    failed = [status["context"] for context, status in latest.items()
              if context != "coderabbit" and status["state"] != "success"]
    if failed:
        raise Refusal(f"Latest commit status is not successful: {', '.join(sorted(failed))}")


def workflow_runs(api, sha, number):
    runs = paged(api, f"repos/{REPO}/actions/runs?head_sha={sha}&event=pull_request", "workflow_runs")
    if not runs:
        raise Refusal("No pull_request workflow execution exists for this head")
    by_workflow = {}
    for run in runs:
        if not isinstance(run, dict):
            raise Refusal("Workflow execution response contains an invalid item")
        associated = run.get("pull_requests")
        run_path = run.get("path")
        path = run_path.split("@", 1)[0] if isinstance(run_path, str) else ""
        if (run.get("head_sha") != sha or run.get("event") != "pull_request" or path != WORKFLOW
                or not isinstance(associated, list)
                or not any(isinstance(pr, dict) and pr.get("number") == number for pr in associated)):
            raise Refusal("Unknown or non-PR workflow execution applies to this head")
        workflow_id = run.get("workflow_id")
        rank_values = (run.get("run_number"), run.get("run_attempt"), run.get("id"))
        if (not isinstance(workflow_id, int) or isinstance(workflow_id, bool)
                or any(not isinstance(value, int) or isinstance(value, bool) for value in rank_values)):
            raise Refusal("Workflow execution has no stable identity")
        previous = by_workflow.get(workflow_id)
        rank = rank_values
        previous_rank = ((previous["run_number"], previous["run_attempt"], previous["id"])
                         if previous else None)
        if previous_rank is None or rank > previous_rank:
            by_workflow[workflow_id] = run
    if len(by_workflow) != 1:
        raise Refusal("Unexpected Dev PR workflow policy or multiple workflow identities")
    return list(by_workflow.values())


def check_job_evidence(api, number, sha, files):
    check_commit_statuses(api, sha)
    runs = check_runs(api, sha)
    by_id = {}
    external = []
    for run in runs:
        if (not isinstance(run, dict) or not isinstance(run.get("app"), dict)
                or run["app"].get("slug") != "github-actions"):
            external.append(run)
            continue
        if (not isinstance(run.get("id"), int) or isinstance(run.get("id"), bool)
                or not isinstance(run.get("name"), str)):
            raise Refusal("Exact-head check run has an invalid identity")
        by_id[str(run["id"])] = run
    for run in external:
        name = run.get("name", "<unnamed>") if isinstance(run, dict) else "<unnamed>"
        raise Refusal(f"Unknown external check policy: {name}")
    latest = workflow_runs(api, sha, number)
    evidence = {}
    matched_ids = set()
    for workflow in latest:
        if workflow.get("status") != "completed" or workflow.get("conclusion") != "success":
            raise Refusal(f"Latest workflow run {workflow.get('id')} is "
                          f"{workflow.get('status')}/{workflow.get('conclusion')}")
        jobs = paged(api, f"repos/{REPO}/actions/runs/{workflow['id']}/jobs?filter=latest", "jobs")
        expected = required_jobs(files)
        found = set()
        for job in jobs:
            if not isinstance(job, dict):
                raise Refusal("Workflow job response contains an invalid item")
            name = job.get("name")
            if name not in EXPECTED or name in found:
                raise Refusal(f"Unknown or duplicate Actions job: {name!r}")
            found.add(name)
            check_url = job.get("check_run_url")
            match = re.search(r"/(\d+)$", check_url) if isinstance(check_url, str) else None
            check = by_id.get(match.group(1)) if match else None
            if (not check or check.get("head_sha") != sha or check.get("name") != name
                    or check.get("status") != "completed" or check.get("conclusion") != job.get("conclusion")
                    or job.get("status") != "completed"):
                raise Refusal(f"Check run for {name} is missing, stale, or incomplete")
            matched_ids.add(match.group(1))
            if job.get("conclusion") == "skipped" and expected[name]:
                raise Refusal(f"Required job {name} was skipped")
            if expected[name] and job.get("conclusion") != "success":
                raise Refusal(f"Required job {name} is {job.get('conclusion')}")
            if not expected[name] and job.get("conclusion") not in ("success", "skipped"):
                raise Refusal(f"Path-gated job {name} is {job.get('conclusion')}")
            evidence[name] = check.get("html_url", "")
        missing = EXPECTED - found
        if missing:
            raise Refusal(f"Missing expected Actions jobs: {', '.join(sorted(missing))}")
    if matched_ids != set(by_id):
        raise Refusal("Unknown or stale GitHub Actions check run exists on --head")
    return evidence


def valid_receipt(path, number, sha):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as source:
            receipt = json.load(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Refusal("Review receipt is unreadable JSON") from error
    required = {"repository", "pr", "head_sha", "reviewer", "session", "verdict", "evidence"}
    if (not isinstance(receipt, dict) or set(receipt) != required or receipt.get("repository") != REPO
            or receipt.get("pr") != number or receipt.get("head_sha") != sha
            or receipt.get("verdict") != "APPROVED"
            or any(not isinstance(receipt.get(k), str) or not receipt[k].strip()
                   for k in ("reviewer", "session", "evidence"))
            or not receipt["reviewer"].lower().startswith(("gpt-6.1 sol", "claude"))):
        raise Refusal("Review receipt must attest this repository, PR, head, reviewer, session, and approval")
    return receipt


def review_evidence(api, number, sha, author, receipt_path):
    reviews = paged(api, f"repos/{REPO}/pulls/{number}/reviews", "")
    latest = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise Refusal("Review response contains an invalid item")
        reviewer = review.get("user")
        if reviewer is not None and not isinstance(reviewer, dict):
            raise Refusal("Review response contains an invalid reviewer")
        login = (reviewer or {}).get("login")
        if login is not None and not isinstance(login, str):
            raise Refusal("Review response contains an invalid reviewer")
        if not login or login == author or review.get("state") not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        submitted = review.get("submitted_at") or ""
        if not isinstance(submitted, str):
            raise Refusal("Review response has an invalid submission time")
        # GitHub returns reviews chronologically, so the later item wins timestamp ties.
        if login not in latest or submitted >= (latest[login].get("submitted_at") or ""):
            latest[login] = review
    if any(r.get("state") == "CHANGES_REQUESTED" for r in latest.values()):
        raise Refusal("An outstanding changes-requested review remains")
    approvals = [r for r in latest.values() if r.get("state") == "APPROVED" and r.get("commit_id") == sha]
    receipt = valid_receipt(receipt_path, number, sha)
    if not approvals and not receipt:
        raise Refusal("No independent approval is bound to --head")
    query = ("query($owner:String!, $name:String!, $number:Int!, $endCursor:String) {"
             "repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$endCursor){"
             "nodes{id isResolved isOutdated} pageInfo{hasNextPage endCursor}}}}}")
    cursor, unresolved = None, []
    for _ in range(100):
        fields = (("query", query), ("owner", "uhstray-io"), ("name", "agent-cloud"),
                  ("number", str(number)), ("endCursor", cursor or "null"))
        data = api.api("graphql", fields)
        try:
            threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        except (KeyError, TypeError) as error:
            raise Refusal("Review-thread response is incomplete") from error
        nodes, page_info = threads.get("nodes"), threads.get("pageInfo")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise Refusal("Review-thread response is incomplete")
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("isResolved"), bool):
                raise Refusal("Review-thread resolution evidence is incomplete")
            if not node["isResolved"]:
                unresolved.append(node)
        has_next = page_info.get("hasNextPage")
        if not isinstance(has_next, bool):
            raise Refusal("Review-thread pagination metadata is incomplete")
        if not has_next:
            break
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            raise Refusal("Review-thread pagination is incomplete")
    else:
        raise Refusal("Review-thread pagination limit reached")
    if unresolved:
        raise Refusal(f"{len(unresolved)} unresolved review thread(s) remain")
    return [r.get("html_url", "") for r in approvals], receipt


def validate(api, number, sha, receipt_path):
    check_workflow_policy(api)
    pr = get_pr(api, number)
    check_pr(pr, number, sha)
    files = changed_files(api, number)
    comments = paged(api, f"repos/{REPO}/issues/{number}/comments", "")
    if any(not isinstance(comment, dict) or not isinstance(comment.get("body"), str) for comment in comments):
        raise Refusal("Conversation comments are incomplete")
    checks = check_job_evidence(api, number, sha, files)
    approvals, receipt = review_evidence(api, number, sha, pr["user"]["login"], receipt_path)
    return {"checks": checks, "approvals": approvals, "receipt": receipt, "comments": len(comments)}


def main(argv=None, api=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, type=int, help="pull request number")
    parser.add_argument("--head", required=True, help="full expected head commit SHA")
    parser.add_argument("--merge", action="store_true", help="perform one SHA-bound merge after all gates pass")
    parser.add_argument("--feedback-addressed", action="store_true",
                        help="attest actionable review and ordinary-comment feedback is addressed")
    parser.add_argument("--review-receipt", help="JSON receipt for an independent Sol/Claude review")
    args = parser.parse_args(argv)
    if not SHA.fullmatch(args.head):
        parser.error("--head must be a full 40-character lowercase SHA")
    if args.merge and not args.feedback_addressed:
        parser.error("--merge requires --feedback-addressed attestation")
    api = api or GitHub()
    try:
        evidence = validate(api, args.pr, args.head, args.review_receipt)
        print(f"PASS: PR {args.pr} head {args.head}; {len(evidence['checks'])} expected checks; "
              f"approval={'receipt' if evidence['receipt'] else 'GitHub'}; "
              f"{evidence['comments']} conversation comments read")
        if not args.merge:
            print("Read-only validation only; no feedback attestation supplied and no merge attempted.")
            return 0
        # Re-read every gate immediately before the single mutating command.
        validate(api, args.pr, args.head, args.review_receipt)
        try:
            api.merge(args.pr, args.head)
        except Refusal:
            pr = get_pr(api, args.pr)
            if (isinstance(pr, dict) and pr.get("merged")
                    and isinstance(pr.get("head"), dict) and pr["head"].get("sha") == args.head):
                print(f"PASS: PR {args.pr} is merged at expected head {args.head}")
                return 0
            raise
        pr = get_pr(api, args.pr)
        if (not isinstance(pr, dict) or not pr.get("merged")
                or not isinstance(pr.get("head"), dict) or pr["head"].get("sha") != args.head):
            raise Refusal("Merge command returned; read-back does not confirm this PR/head merged")
        print(f"PASS: PR {args.pr} merged at expected head {args.head}")
        return 0
    except Refusal as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
