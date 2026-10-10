#!/usr/bin/env python3
"""Bounded readback for this controller task; output contains no task logs."""

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import signal
import sys
import threading
import time
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MARKER_PREFIX = "AGENT_CLOUD_SEMAPHORE_ATTRIBUTION_V1:"
PLAYBOOK_ID = "platform/playbooks/diagnose-o11y-semaphore-attribution.yml"
TEMPLATE_ID = "Diagnose o11y Semaphore Task Attribution (Dev)"
MAX_INPUT = 16 * 1024
MAX_RESPONSE = 64 * 1024
MAX_ROWS = 2048
ATTEMPTS = 8
POLL_SECONDS = 1
VERIFY_DEADLINE_SECONDS = 30
MAX_VERIFY_DEADLINE_SECONDS = 120


class Refusal(Exception):
    """A fixed reason code safe to return to task output."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refusal("redirect_refused")


def _record_id(value, reason):
    if type(value) is int and 0 < value < 10_000_000_000:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value):
        return value
    raise Refusal(reason)


def _frame(fields):
    framed = bytearray(b"agent-cloud-semaphore-attribution\x00v1\x00")
    for field in fields:
        value = field if isinstance(field, bytes) else field.encode("ascii")
        framed.extend(len(value).to_bytes(4, "big"))
        framed.extend(value)
    return bytes(framed)


def prepare(data):
    task_id = _record_id(data.get("task_id"), "invalid_task_id")
    project_id = _record_id(data.get("project_id"), "invalid_project_id")
    revision = data.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise Refusal("invalid_reviewed_sha")
    nonce = secrets.token_bytes(32)
    digest = hashlib.sha256(
        _frame((task_id, project_id, revision, PLAYBOOK_ID, TEMPLATE_ID, nonce))
    ).hexdigest()
    return {
        "status": "prepared",
        "marker": MARKER_PREFIX + digest,
        "nonce_hex": nonce.hex(),
    }


def _origin(url):
    parts = urlsplit(url)
    if (parts.username is not None or parts.password is not None or parts.query or parts.fragment
            or parts.path not in ("", "/")):
        raise Refusal("invalid_api_origin")
    if parts.scheme == "https" and parts.hostname:
        return url.rstrip("/")
    if parts.scheme == "http" and parts.hostname:
        host = parts.hostname.lower()
        try:
            loopback = ipaddress.ip_address(host.strip("[]")).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if loopback:
            return url.rstrip("/")
    raise Refusal("insecure_api_origin")


def _client_read(origin, token, path):
    client = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(
        origin + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with client.open(request, timeout=5) as response:
            if response.status != 200:
                raise Refusal("api_read_failed")
            body = response.read(MAX_RESPONSE + 1)
    except Refusal:
        raise
    except HTTPError as error:
        if 300 <= error.code < 400:
            raise Refusal("redirect_refused") from None
        raise Refusal("api_read_failed") from None
    except (URLError, TimeoutError, OSError):
        raise Refusal("transport_failed") from None
    if len(body) > MAX_RESPONSE:
        raise Refusal("response_too_large")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Refusal("malformed_api_json") from None


def _matches_id(row, key, expected, reason):
    if not isinstance(row, dict):
        raise Refusal(reason)
    value = row.get(key)
    try:
        return _record_id(value, reason) == expected
    except Refusal:
        raise Refusal(reason) from None


def _readback_context(data, read):
    project_id = _record_id(data.get("project_id"), "invalid_project_id")
    task_id = _record_id(data.get("task_id"), "invalid_task_id")
    task = read(f"/api/project/{project_id}/tasks/{task_id}")
    if not _matches_id(task, "id", task_id, "task_metadata_invalid"):
        raise Refusal("task_metadata_mismatch")
    if not _matches_id(task, "project_id", project_id, "task_metadata_invalid"):
        raise Refusal("task_metadata_mismatch")
    template_id = _record_id(task.get("template_id"), "task_metadata_invalid")
    if task.get("status") not in {"running", "waiting"}:
        if task.get("status") in {"success", "error", "failed", "stopped", "canceled"}:
            raise Refusal("terminal_task")
        raise Refusal("task_context_unverifiable")

    template = read(f"/api/project/{project_id}/templates/{template_id}")
    if (not _matches_id(template, "id", template_id, "template_metadata_invalid")
            or not _matches_id(template, "project_id", project_id, "template_metadata_invalid")
            or template.get("name") != TEMPLATE_ID
            or template.get("playbook") != PLAYBOOK_ID):
        raise Refusal("template_context_mismatch")
    repository_id = _record_id(template.get("repository_id"), "template_metadata_invalid")
    inventory_id = _record_id(template.get("inventory_id"), "template_metadata_invalid")
    repository = read(f"/api/project/{project_id}/repositories/{repository_id}")
    if (not _matches_id(repository, "id", repository_id, "repository_metadata_invalid")
            or not _matches_id(repository, "project_id", project_id, "repository_metadata_invalid")
            or repository.get("git_branch") != "dev"):
        raise Refusal("repository_context_mismatch")
    overrides = (
        ("playbook", PLAYBOOK_ID),
        ("git_branch", "dev"),
        ("repository_id", repository_id),
        ("inventory_id", inventory_id),
    )
    for key, expected in overrides:
        if task.get(key) is not None:
            try:
                matches = (_record_id(task[key], "task_context_mismatch") == expected
                           if key in {"repository_id", "inventory_id"}
                           else task[key] == expected)
            except Refusal:
                raise Refusal("task_context_mismatch") from None
            if not matches:
                raise Refusal("task_context_mismatch")
    return (task_id, project_id, template_id, repository_id, inventory_id,
            PLAYBOOK_ID, "dev")


def _output_rows(value, task_id):
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise Refusal("output_rows_invalid")
    outputs = []
    for row in value:
        if not isinstance(row, dict):
            raise Refusal("output_rows_invalid")
        if not _matches_id(row, "task_id", task_id, "output_rows_invalid"):
            raise Refusal("output_task_mismatch")
        if not isinstance(row.get("output"), str) or not isinstance(row.get("time"), str):
            raise Refusal("output_rows_invalid")
        outputs.append(row["output"])
    return outputs


@contextmanager
def _verification_deadline(seconds):
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not 0 < seconds <= MAX_VERIFY_DEADLINE_SECONDS):
        raise Refusal("deadline_invalid")
    if threading.current_thread() is not threading.main_thread():
        raise Refusal("deadline_unavailable")
    try:
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        previous_handler = signal.getsignal(signal.SIGALRM)
    except (AttributeError, OSError, ValueError):
        raise Refusal("deadline_unavailable") from None
    if previous_timer != (0.0, 0.0):
        raise Refusal("deadline_unavailable")

    installed = False

    def expire(_signum, _frame):
        raise Refusal("verification_deadline_exceeded")

    try:
        signal.signal(signal.SIGALRM, expire)
        installed = True
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    except Refusal:
        raise
    except (OSError, ValueError, RuntimeError):
        raise Refusal("deadline_unavailable") from None
    finally:
        if installed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)


def _unverified(reason):
    return {
        "status": "unverified",
        "reason": reason,
        "metadata_context_matches": False,
        "template_context_matches": False,
        "repository_branch_dev": False,
        "marker_count": 0,
        "output_row_count": 0,
    }


def verify(data, read=None, sleep=time.sleep,
           deadline_seconds=VERIFY_DEADLINE_SECONDS):
    try:
        with _verification_deadline(deadline_seconds):
            return _verify_operation(data, read, sleep)
    except Refusal as error:
        return _unverified(str(error))
    except Exception:
        return _unverified("readback_unavailable")


def _verify_operation(data, read=None, sleep=time.sleep):
    base = {
        "status": "unverified",
        "reason": "attribution_unverified",
        "metadata_context_matches": False,
        "template_context_matches": False,
        "repository_branch_dev": False,
        "marker_count": 0,
        "output_row_count": 0,
    }
    try:
        origin = _origin(data.get("api_origin", ""))
        project_id = _record_id(data.get("project_id"), "invalid_project_id")
        task_id = _record_id(data.get("task_id"), "invalid_task_id")
        token = data.get("token")
        marker = data.get("marker")
        nonce_hex = data.get("nonce_hex")
        revision = data.get("revision")
        if (not isinstance(token, str) or not token or len(token) > 8192
                or any(ord(char) < 33 or ord(char) > 126 for char in token)):
            raise Refusal("runtime_token_unavailable")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise Refusal("invalid_reviewed_sha")
        if (not isinstance(marker, str) or not re.fullmatch(
                re.escape(MARKER_PREFIX) + r"[0-9a-f]{64}", marker)
                or not isinstance(nonce_hex, str)
                or not re.fullmatch(r"[0-9a-f]{64}", nonce_hex)):
            raise Refusal("marker_invalid")
        expected_marker = MARKER_PREFIX + hashlib.sha256(
            _frame((task_id, project_id, revision, PLAYBOOK_ID, TEMPLATE_ID,
                    bytes.fromhex(nonce_hex)))
        ).hexdigest()
        if not hmac.compare_digest(marker, expected_marker):
            raise Refusal("marker_invalid")
        if read is None:
            def read(path):
                return _client_read(origin, token, path)

        context = _readback_context(data, read)
        base.update({
            "metadata_context_matches": True,
            "template_context_matches": True,
            "repository_branch_dev": True,
        })
        for attempt in range(ATTEMPTS):
            rows = _output_rows(
                read(f"/api/project/{project_id}/tasks/{task_id}/output"), task_id
            )
            count = sum(output.count(marker) for output in rows)
            base["output_row_count"] = len(rows)
            base["marker_count"] = count
            if count > 1:
                raise Refusal("duplicate_marker")
            if count == 1:
                if _readback_context(data, read) != context:
                    raise Refusal("task_context_changed")
                base.update({"status": "partial", "reason": "marker_observed"})
                return base
            if attempt + 1 < ATTEMPTS:
                sleep(POLL_SECONDS)
        raise Refusal("marker_not_observed")
    except Refusal as error:
        base["reason"] = str(error)
        return base
    except Exception:
        base["reason"] = "readback_unavailable"
        return base


def main():
    try:
        raw = sys.stdin.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise Refusal("input_too_large")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise Refusal("invalid_input")
        mode = data.get("mode")
        result = prepare(data) if mode == "prepare" else verify(data) if mode == "verify" else None
        if result is None:
            raise Refusal("invalid_mode")
    except Refusal as error:
        result = {
            "status": "unverified",
            "reason": str(error),
            "metadata_context_matches": False,
            "template_context_matches": False,
            "repository_branch_dev": False,
            "marker_count": 0,
            "output_row_count": 0,
        }
    except Exception:
        result = {
            "status": "unverified",
            "reason": "invalid_input",
            "metadata_context_matches": False,
            "template_context_matches": False,
            "repository_branch_dev": False,
            "marker_count": 0,
            "output_row_count": 0,
        }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
