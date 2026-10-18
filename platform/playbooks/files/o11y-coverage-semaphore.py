#!/usr/bin/env python3
"""Strict, bounded readback and acceptance for one declared Semaphore receipt."""

import http.client
import importlib.util
import io
import json
import pathlib
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta

MAX_BINDINGS = 64
MAX_METADATA_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
REQUEST_TIMEOUT = 10
TOTAL_TIMEOUT = 60
EXPECTED_TEMPLATE = "Verify o11y Target Receipt"
EXPECTED_PLAYBOOK = "platform/playbooks/verify-o11y-service.yml"
EXPECTED_REPOSITORY_NAME = "agent-cloud dev"
EXPECTED_REPOSITORY_URL = "https://github.com/uhstray-io/agent-cloud.git"
EXPECTED_REPOSITORY_BRANCH = "dev"
LAUNCH_FIELDS = {
    "o11y_verification_mode",
    "expected_repository_sha",
    "expected_inventory_revision",
    "expected_target_id",
    "expected_signal",
    "signal_selector",
    "freshness_seconds",
    "observation_window_seconds",
}
OBSERVATION_FIELDS = {
    "status", "target_id", "signal", "repository_sha", "inventory_revision",
    "observation_window_seconds", "freshness_seconds", "query_ended_at", "observed_at",
    "task_reference_unattributed",
}
CONTROLLER_FIELDS = {
    "semaphore_url", "api_token", "project_id", "repository_id", "expected_repository_sha", "inventory",
}
CONTROLLER_REQUIRED_FIELDS = {"expected_repository_sha", "inventory"}
SAFE_REFUSAL_CODES = frozenset({
    "binding_limit", "conflicting_stats", "deadline", "http_status", "invalid_binding",
    "invalid_controller_input", "invalid_credentials", "invalid_declaration", "invalid_endpoint",
    "invalid_template_repository", "invalid_utc_time", "launch_inputs", "malformed_json",
    "observation_binding", "observation_shape", "observation_time_order",
    "output_rows", "output_shape", "output_time_order", "readback_shape", "repository_mismatch",
    "repository_revision_mismatch", "response_encoding", "response_framing", "response_size",
    "route_refused", "task_drift", "task_identity_mismatch", "task_inventory_override",
    "task_not_accepted", "task_override", "task_params", "task_time_order", "template_drift",
    "template_mismatch", "terminal_stats", "transport", "truncated_response",
})


class Refusal(Exception):
    """A safe fixed refusal code; never includes response content or credentials."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _positive_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-standard JSON constant")


def _json(data):
    try:
        return json.loads(
            data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise Refusal("malformed_json") from None


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc(value):
    pattern = r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|\+00:00)"
    match = re.fullmatch(pattern, value) if isinstance(value, str) else None
    if match is None:
        raise Refusal("invalid_utc_time")
    fraction = match.group(2)
    normalized = match.group(1) + ("." + fraction[:6] if fraction else "") + match.group(3)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        raise Refusal("invalid_utc_time") from None
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise Refusal("invalid_utc_time")
    return parsed.astimezone(UTC)


def _int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def accept_receipt(*, binding, target, signal, project_id, repository_id,
                   repository_sha, inventory_revision, task, output_rows, template, repository, now):
    """Return bounded attribution only when one exact task/output pair proves the declaration."""
    if (not isinstance(binding, dict) or set(binding) != {"task_id", "template_id", "observation_window_seconds"}
            or not _positive_id(binding.get("task_id")) or not _positive_id(binding.get("template_id"))
            or not _int(binding.get("observation_window_seconds"))):
        raise Refusal("invalid_binding")
    task_id, template_id, window = (
        binding["task_id"], binding["template_id"], binding["observation_window_seconds"]
    )
    if not all(_positive_id(value) for value in (project_id, repository_id)):
        raise Refusal("invalid_binding")
    if (not isinstance(target, dict) or not isinstance(target.get("signals"), dict)
            or not isinstance(signal, str) or not isinstance(target.get("target_id"), str)):
        raise Refusal("invalid_declaration")
    declaration = target.get("signals", {}).get(signal)
    if not isinstance(declaration, dict) or declaration.get("applicable") is not True:
        raise Refusal("invalid_declaration")
    selector = declaration.get("selector")
    freshness = declaration.get("freshness_seconds")
    if (not isinstance(selector, str) or not selector or not _int(freshness)
            or not 60 <= window <= 3600 or not window < freshness <= 86400):
        raise Refusal("invalid_declaration")
    if (not isinstance(repository_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", repository_sha)
            or not isinstance(inventory_revision, str)
            or not re.fullmatch(r"[0-9a-f]{64}", inventory_revision)):
        raise Refusal("invalid_declaration")
    now = _utc(now) if isinstance(now, str) else now
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
        raise Refusal("invalid_utc_time")
    now = now.astimezone(UTC)

    if (not isinstance(task, dict) or not _positive_id(task.get("id")) or task.get("id") != task_id
            or not _positive_id(task.get("project_id")) or task.get("project_id") != project_id):
        raise Refusal("task_identity_mismatch")
    if (not _positive_id(task.get("template_id")) or task.get("template_id") != template_id
            or task.get("status") != "success"):
        raise Refusal("task_not_accepted")
    task_repository_sha = task.get("commit_hash")
    if not isinstance(task_repository_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", task_repository_sha):
        raise Refusal("repository_revision_mismatch")
    if (task.get("playbook") != "" or task.get("arguments") not in (None, "")
            or task.get("git_branch") not in (None, "") or task.get("limit", "") != ""
            or task.get("secret", "") not in (None, "") or task.get("schedule_id") is not None
            or task.get("workflow_run_id") is not None):
        raise Refusal("task_override")
    if task.get("params") != {"dry_run": False, "diff": False}:
        raise Refusal("task_params")
    task_inventory = task.get("inventory_id")
    template_inventory = template.get("inventory_id") if isinstance(template, dict) else None
    if (not _positive_id(template_inventory)
            or (task_inventory is not None and task_inventory != template_inventory)
            or (task_inventory is not None and not _positive_id(task_inventory))):
        raise Refusal("task_inventory_override")
    environment = task.get("environment")
    if not isinstance(environment, str):
        raise Refusal("launch_inputs")
    try:
        launch = json.loads(environment, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError):
        raise Refusal("launch_inputs") from None
    expected_launch = {
        "o11y_verification_mode": "strict",
        "expected_repository_sha": task_repository_sha,
        "expected_inventory_revision": inventory_revision,
        "expected_target_id": target.get("target_id"),
        "expected_signal": signal,
        "signal_selector": selector,
        "freshness_seconds": freshness,
        "observation_window_seconds": window,
    }
    if (not isinstance(launch, dict) or set(launch) != LAUNCH_FIELDS or launch != expected_launch):
        raise Refusal("launch_inputs")

    if (not isinstance(template, dict) or not _positive_id(template.get("id")) or template.get("id") != template_id
            or not _positive_id(template.get("project_id")) or template.get("project_id") != project_id
            or template.get("name") != EXPECTED_TEMPLATE or template.get("playbook") != EXPECTED_PLAYBOOK
            or not _positive_id(template.get("repository_id")) or template.get("repository_id") != repository_id
            or template.get("app") != "ansible" or template.get("arguments") not in (None, "")
            or template.get("allow_override_args_in_task", False) is not False
            or template.get("task_params", {}) != {}
            or template.get("git_branch") not in (None, "")):
        raise Refusal("template_mismatch")
    if (not isinstance(repository, dict) or not _positive_id(repository.get("id"))
            or repository.get("id") != repository_id or not _positive_id(repository.get("project_id"))
            or repository.get("project_id") != project_id or repository.get("name") != EXPECTED_REPOSITORY_NAME
            or repository.get("git_url") != EXPECTED_REPOSITORY_URL
            or repository.get("git_branch") != EXPECTED_REPOSITORY_BRANCH):
        raise Refusal("repository_mismatch")

    created = _utc(task.get("created"))
    started = _utc(task.get("start"))
    ended = _utc(task.get("end"))
    if not created <= started <= ended <= now:
        raise Refusal("task_time_order")
    observation = _terminal_observation(task_id, output_rows, created, started, ended, now)
    if (not isinstance(observation, dict) or observation.get("status") != "observed"
            or observation.get("task_reference_unattributed") is not True
            or set(observation) != OBSERVATION_FIELDS):
        raise Refusal("observation_shape")
    if (observation.get("target_id") != target.get("target_id") or observation.get("signal") != signal
            or observation.get("repository_sha") != task_repository_sha
            or observation.get("inventory_revision") != inventory_revision
            or observation.get("observation_window_seconds") != window
            or observation.get("freshness_seconds") != freshness):
        raise Refusal("observation_binding")
    query_end = _utc(observation.get("query_ended_at"))
    observed = _utc(observation.get("observed_at"))
    if not started <= query_end <= observed <= ended <= now:
        raise Refusal("observation_time_order")
    expires = query_end + timedelta(seconds=freshness - window)
    return {
        "status": "incomplete" if task_repository_sha != repository_sha or now >= expires else "verified",
        "target_id": target["target_id"],
        "signal": signal,
        "task_id": task_id,
        "template_id": template_id,
        "repository_sha": task_repository_sha,
        "inventory_revision": inventory_revision,
        "observed_at": observation["observed_at"],
        "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _terminal_observation(task_id, rows, created, started, ended, now):
    if not isinstance(rows, list) or len(rows) < 2:
        raise Refusal("output_rows")
    row_ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
    if not all(_int(value) and value >= 0 for value in row_ids):
        raise Refusal("output_rows")
    zero_ids = all(value == 0 for value in row_ids)
    if not zero_ids and (any(value == 0 for value in row_ids)
                         or any(left >= right for left, right in zip(row_ids, row_ids[1:], strict=False))):
        raise Refusal("output_rows")
    previous_time = None
    texts = []
    stamps = []
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"id", "task_id", "stage_id", "time", "output"}
                or not _positive_id(row.get("task_id")) or row.get("task_id") != task_id
                or row.get("stage_id") is not None or not isinstance(row.get("output"), str)):
            raise Refusal("output_rows")
        stamp = _utc(row.get("time"))
        if not created <= stamp <= ended <= now or (previous_time is not None and stamp < previous_time):
            raise Refusal("output_time_order")
        previous_time = stamp
        output = row["output"]
        if not output.strip():
            continue
        banner_suffix = output[len("CUSTOM STATS:"):].strip() if output.startswith("CUSTOM STATS:") else ""
        if banner_suffix and set(banner_suffix) == {"*"}:
            output = "CUSTOM STATS:"
        if output.startswith("\tRUN: "):
            output = output[1:]
        texts.append(output)
        stamps.append(stamp)
    if len(texts) < 2:
        raise Refusal("terminal_stats")
    if texts[-2] != "CUSTOM STATS:" or not texts[-1].startswith("RUN: "):
        raise Refusal("terminal_stats")
    if any(stamp < started for stamp in stamps[-2:]):
        raise Refusal("output_time_order")
    if any(value.startswith(("CUSTOM STATS:", "RUN:")) for value in texts[:-2]):
        raise Refusal("conflicting_stats")
    payload = _json(texts[-1][5:].encode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != {"o11y_coverage_observation"}:
        raise Refusal("terminal_stats")
    return payload["o11y_coverage_observation"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise urllib.error.HTTPError(request.full_url, code, "redirect refused", headers, response)


def _endpoint_base(url):
    if not isinstance(url, str):
        raise Refusal("invalid_endpoint")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise Refusal("invalid_endpoint") from None
    if (parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/") or port == 0):
        raise Refusal("invalid_endpoint")
    return f"https://{parsed.netloc}"


class SemaphoreReader:
    """HTTPS-only GET client. URL and token are supplied in memory by its caller."""

    def __init__(self, url, token, project_id, *, opener=None, clock=time.monotonic):
        self.base = _endpoint_base(url)
        if not isinstance(token, str) or not token or not _positive_id(project_id):
            raise Refusal("invalid_credentials")
        self.token = token
        self.project_id = project_id
        self.clock = clock
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context)
        )

    def _get(self, path, limit, deadline):
        route = (
            r"/api/project/[1-9][0-9]*/(?:tasks/[1-9][0-9]*(?:/output)?|"
            r"templates/[1-9][0-9]*|repositories/[1-9][0-9]*)"
        )
        if not re.fullmatch(route, path):
            raise Refusal("route_refused")
        request = urllib.request.Request(
            self.base + path,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"},
            method="GET",
        )
        try:
            remaining_time = deadline - self.clock()
            if remaining_time <= 0:
                raise Refusal("deadline")
            response = self.opener.open(request, timeout=min(REQUEST_TIMEOUT, remaining_time))
            with response:
                if response.status != 200:
                    raise Refusal("http_status")
                lengths = response.headers.get_all("Content-Length", [])
                if len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or int(lengths[0]) > limit)):
                    raise Refusal("response_size")
                transfers = response.headers.get_all("Transfer-Encoding", [])
                if len(transfers) > 1 or (transfers and (lengths or transfers[0].strip().lower() != "chunked")):
                    raise Refusal("response_framing")
                if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                    raise Refusal("response_encoding")
                body = bytearray()
                while True:
                    remaining_time = deadline - self.clock()
                    if remaining_time <= 0:
                        raise Refusal("deadline")
                    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        sock.settimeout(min(REQUEST_TIMEOUT, remaining_time))
                    chunk = response.read1(min(16384, limit + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > limit:
                        raise Refusal("response_size")
                if lengths and len(body) != int(lengths[0]):
                    raise Refusal("truncated_response")
                return _json(bytes(body))
        except Refusal:
            raise
        except urllib.error.HTTPError:
            raise Refusal("http_status") from None
        except (OSError, ValueError, http.client.HTTPException, TimeoutError, ssl.SSLError):
            raise Refusal("transport") from None

    def read_bindings(self, bindings):
        if not isinstance(bindings, list) or len(bindings) > MAX_BINDINGS:
            raise Refusal("binding_limit")
        if not bindings:
            return []
        deadline = self.clock() + TOTAL_TIMEOUT
        cache = {}

        def get(kind, object_id, limit):
            key = kind, object_id
            if key not in cache:
                path = f"/api/project/{self.project_id}/{kind}/{object_id}"
                cache[key] = self._get(path, limit, deadline)
            return cache[key]

        results = []
        output_cache = {}
        for binding in bindings:
            if (not isinstance(binding, dict) or set(binding) != {"task_id", "template_id"}
                    or not all(_positive_id(binding.get(name)) for name in binding)):
                raise Refusal("invalid_binding")
            task_id, template_id = binding["task_id"], binding["template_id"]
            task = get("tasks", task_id, MAX_METADATA_BYTES)
            if task_id not in output_cache:
                output_cache[task_id] = self._get(
                    f"/api/project/{self.project_id}/tasks/{task_id}/output", MAX_OUTPUT_BYTES, deadline
                )
            output = output_cache[task_id]
            template = get("templates", template_id, MAX_METADATA_BYTES)
            repository_id = template.get("repository_id") if isinstance(template, dict) else None
            if not _positive_id(repository_id):
                raise Refusal("invalid_template_repository")
            repository = get("repositories", repository_id, MAX_METADATA_BYTES)
            if not isinstance(output, list):
                raise Refusal("output_shape")
            results.append({"task": task, "output": output, "template": template, "repository": repository})

        checked = set()
        for binding, result in zip(bindings, results, strict=True):
            task_id, template_id = binding["task_id"], binding["template_id"]
            task_again = result["task"]
            if ("task", task_id) not in checked:
                task_again = self._get(
                    f"/api/project/{self.project_id}/tasks/{task_id}", MAX_METADATA_BYTES, deadline
                )
                checked.add(("task", task_id))
            template_again = result["template"]
            if ("template", template_id) not in checked:
                template_again = self._get(
                    f"/api/project/{self.project_id}/templates/{template_id}", MAX_METADATA_BYTES, deadline
                )
                checked.add(("template", template_id))
            if _canonical(task_again) != _canonical(result["task"]):
                raise Refusal("task_drift")
            if _canonical(template_again) != _canonical(result["template"]):
                raise Refusal("template_drift")
        return results


def controller_report(payload, *, reader_factory=None, clock=None, census_module=None):
    """Read declared receipts and build a report without serializing acceptance evidence."""
    if (not isinstance(payload, dict) or payload.keys() - CONTROLLER_FIELDS
            or not payload.keys() >= CONTROLLER_REQUIRED_FIELDS):
        raise Refusal("invalid_controller_input")
    project_id, repository_id = payload.get("project_id"), payload.get("repository_id")
    repository_sha = payload["expected_repository_sha"]
    inventory = payload["inventory"]
    if (not isinstance(repository_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", repository_sha)
            or not isinstance(inventory, dict) or "verified_receipts" in inventory):
        raise Refusal("invalid_controller_input")

    if census_module is None:
        path = pathlib.Path(__file__).with_name("o11y-coverage-census.py")
        spec = importlib.util.spec_from_file_location("o11y_coverage_census", path)
        census_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(census_module)
    targets = inventory.get("targets")
    inventory_revision = inventory.get("inventory_revision")
    has_inventory_revision = "inventory_revision" in inventory
    if (not isinstance(targets, list) or (has_inventory_revision and (
            not isinstance(inventory_revision, str)
            or not re.fullmatch(r"[0-9a-f]{64}", inventory_revision)
            or inventory_revision != census_module.canonical_inventory_revision(targets)))):
        raise Refusal("invalid_controller_input")
    if inventory.get("repository_sha", repository_sha) != repository_sha:
        raise Refusal("invalid_controller_input")
    with redirect_stdout(io.StringIO()):
        census_module.validate(targets, inventory.get("receipt_bindings", {}))

    receipt_bindings = inventory.get("receipt_bindings", {})
    declarations = []
    for target in targets:
        for signal, binding in receipt_bindings.get(target["target_id"], {}).items():
            freshness = target["signals"][signal]["freshness_seconds"]
            window = binding["observation_window_seconds"]
            if not 60 <= window <= 3600 or not window < freshness <= 86400:
                raise Refusal("invalid_binding")
            declarations.append((target, signal, binding))
    if declarations and not has_inventory_revision:
        raise Refusal("invalid_controller_input")
    read_bindings = [
        {"task_id": binding["task_id"], "template_id": binding["template_id"]}
        for _target, _signal, binding in declarations
    ]
    if clock is None:
        def clock():
            return datetime.now(UTC)

    if declarations:
        if not _positive_id(project_id) or not _positive_id(repository_id):
            raise Refusal("invalid_controller_input")
        url = payload.get("semaphore_url")
        token = payload.get("api_token")
        _endpoint_base(url)
        if not isinstance(token, str) or not token:
            raise Refusal("invalid_credentials")
        reader_factory = SemaphoreReader if reader_factory is None else reader_factory
        results = reader_factory(url, token, project_id).read_bindings(read_bindings)
    else:
        results = []
    if not isinstance(results, list) or len(results) != len(declarations):
        raise Refusal("readback_shape")

    verified_receipts = {}
    for (target, signal, binding), result in zip(declarations, results, strict=True):
        if not isinstance(result, dict) or set(result) != {"task", "output", "template", "repository"}:
            raise Refusal("readback_shape")
        accepted = accept_receipt(
            binding=binding, target=target, signal=signal,
            project_id=project_id, repository_id=repository_id,
            repository_sha=repository_sha, inventory_revision=inventory_revision,
            task=result["task"], output_rows=result["output"], template=result["template"],
            repository=result["repository"], now=clock(),
        )
        if accepted["status"] == "verified":
            verified_receipts.setdefault(target["target_id"], {})[signal] = accepted

    with redirect_stdout(io.StringIO()):
        report_now = clock()
        if isinstance(report_now, str):
            report_now = _utc(report_now)
        return census_module.build_report(
            pathlib.Path(__file__).parents[3].resolve(), inventory, repository_sha,
            report_now, verified_receipts=verified_receipts,
        )


def main():
    generic_refusal = {"status": "refused", "reason": "receipt_verification_failed"}
    try:
        if sys.argv[1:] != ["--controller"]:
            raise Refusal("invalid_controller_input")
        payload = json.load(sys.stdin, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
        report = controller_report(payload)
        print(json.dumps(report, sort_keys=True))
        return 0
    except Refusal as exc:
        # Only internal, reviewed literal categories may cross the controller boundary.
        refusal = {"status": "refused", "reason": exc.code if isinstance(exc.code, str)
                   and exc.code in SAFE_REFUSAL_CODES
                   else generic_refusal["reason"]}
        print(json.dumps(refusal, sort_keys=True))
        return 2
    except (Exception, SystemExit):
        print(json.dumps(generic_refusal, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
