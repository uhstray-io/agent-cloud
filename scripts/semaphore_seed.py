"""Shared core for staging secret inputs in a dedicated Semaphore environment.

One lifecycle, used by every seed CLI: verify the template and its isolated environment,
stage the values as ENCRYPTED environment inputs (Semaphore does not return their values
over its API), run exactly one task, then remove exactly the inputs this run created.
Never a survey field or an extra var: Semaphore persists both and serves them back.

Extracted from the reviewed Postiz stager (PR #170) so a second seed does not grow a
second, divergent copy of the trust-boundary checks.
"""

import http.client
import importlib.util
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# The one clean-environment rule, shared with publication and the provisioner (an Ansible
# filter module; loaded by path so there is a single definition).
_RULE = importlib.util.spec_from_file_location(
    "seed_environment_rule",
    Path(__file__).resolve().parents[1] / "platform/semaphore/filter_plugins/publication.py")
_rule = importlib.util.module_from_spec(_RULE)
_RULE.loader.exec_module(_rule)
seed_environment_problems = _rule.seed_environment_problems

# v2.17.31 pkg/task_logger/task_logger.go, IsFinished (rejected is not finished).
TERMINAL = {"success", "error", "stopped"}


class Refusal(Exception):
    """Safe, value-free error text."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, url, project, token):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise Refusal("Semaphore URL must be a plain HTTPS origin")
        if not token:
            raise Refusal("An existing authorized operator token is required on stdin")
        self.url = url.rstrip("/") + f"/api/project/{project}"
        self.token = token
        self.open = urllib.request.build_opener(NoRedirect).open

    def __call__(self, path, body=None):
        req = urllib.request.Request(self.url + path, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
        }, data=None if body is None else json.dumps(body).encode(),
            method="GET" if body is None else "PUT" if path.startswith("/environment/") else "POST")
        try:
            with self.open(req, timeout=30) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            raise Refusal(f"Semaphore HTTP {error.code}; response body suppressed") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise Refusal("Semaphore request outcome unavailable; do not retry a write blindly") from None


def environment_body(env, operations):
    # Preserve ALL fields returned by the server, replacing only operation data.
    return {**env, "secrets": operations}


def preflight(api, project, template_id, expected_env, *, playbook, template_names, endpoint,
              bindings=None):
    """Every read-only check a seed makes before its first write. Returns (template, env).

    Dry run, the read-only access check and the real seed all call this, so none of them
    can launch or stage against a template the others would refuse. `bindings` maps
    template fields (repository_id, inventory_id) to their APPROVED values: a template
    rebound after provisioning is refused before anything is staged or launched.
    """
    template = api(f"/templates/{template_id}")
    if (template.get("playbook") != playbook
            or template.get("name") not in set(template_names)
            or template.get("app") != "ansible"
            or template.get("arguments") not in (None, "[]", [])
            or template.get("environment_id") != expected_env):
        raise Refusal("Template name, playbook, app, arguments or environment binding differs")
    for field, approved in (bindings or {}).items():
        if template.get(field) != approved:
            raise Refusal(f"Template {field} differs from its approved binding")
    env_path = f"/environment/{expected_env}"
    before = api(env_path)
    if before.get("project_id") != project or before.get("id") != expected_env:
        raise Refusal("Environment identity differs")
    secrets = before.get("secrets")
    if not isinstance(secrets, list):
        raise Refusal("Environment response has no secrets array; absence cannot be established")
    # The seed task logs in to OpenBao and writes the value at THIS address. Pinned to the
    # operator-approved endpoint: an environment whose address changed after provisioning
    # would otherwise receive the AppRole login and the staged value (review of PR #205).
    # Required, with no default, so no caller can skip it.
    try:
        configured = (json.loads(before.get("json") or "{}") or {}).get("openbao_addr")
    except ValueError:
        configured = None
    if not endpoint or configured != endpoint:
        raise Refusal("Seed environment's OpenBao endpoint differs from the approved endpoint")
    # Before anything is staged the environment must be CLEAN by the one shared rule: only
    # the two AppRole inputs, no plaintext env vars, extra vars at most openbao_addr. A
    # leftover of ANY name (this seed's, another seed's) means an earlier run did not
    # finish, or something else writes here; its task may still need it (review of PR #205).
    problems = seed_environment_problems(before, endpoint)
    if problems:
        raise Refusal("Seed environment requires reconciliation before staging: " + "; ".join(problems))
    templates = {item["id"]: item for item in api("/templates")}
    if any(item.get("environment_id") == expected_env and item.get("id") != template_id
           for item in templates.values()):
        raise Refusal("Seed environment is bound to other templates; provision a dedicated environment through code")
    for task in api("/tasks/last"):
        if task.get("status") not in TERMINAL:
            owner = templates.get(task.get("template_id"), {})
            if owner.get("environment_id") == expected_env:
                raise Refusal("Another task is using the seed environment")
    return template, before


def stage_and_seed(api, project, template_id, expected_env, values, *, playbook, template_names,
                   endpoint, extra=None, bindings=None,
                   message="Seed declared inputs via encrypted inputs", timeout=600):
    """Stage `values` as encrypted inputs in a DEDICATED environment, run one task of the
    seed template, then remove exactly the inputs it created.

    Refuses before any write unless the environment is clean by the shared rule (preflight).
    `extra` is NON-SECRET launch configuration only: Semaphore persists it.
    """
    template, before = preflight(api, project, template_id, expected_env, playbook=playbook,
                                 template_names=template_names, endpoint=endpoint, bindings=bindings)
    env_path = f"/environment/{expected_env}"
    # A fresh equality check catches changes made during preflight. It is not CAS.
    if api(env_path) != before:
        raise Refusal("Environment changed during preflight")
    operations = [{"name": name, "type": "env", "secret": value, "operation": "create"}
                  for name, value in values.items()]
    # Print recovery metadata BEFORE the first write. The server may accept a
    # request whose response is lost, so absence of an acknowledgement proves nothing.
    print(f"Reconciliation scope: project {project}, environment {expected_env}; "
          f"inputs {', '.join(sorted(values))}", flush=True)
    api(env_path, environment_body(before, operations))
    staged = api(env_path)
    if {k: v for k, v in staged.items() if k != "secrets"} != {
            k: v for k, v in before.items() if k != "secrets"}:
        raise Refusal("Environment configuration changed; retain inputs for reconciliation")
    created = [item for item in staged.get("secrets", []) if item.get("name") in values]
    if (len(created) != len(values) or {item["name"] for item in created} != set(values)
            or any(item.get("type") != "env" or not isinstance(item.get("id"), int) for item in created)):
        raise Refusal("Encrypted input read-back differs; retain inputs for reconciliation")
    print("Created encrypted input IDs: " + ", ".join(str(item["id"]) for item in created), flush=True)
    now = api(f"/templates/{template_id}")
    bindings = ("id", "name", "playbook", "environment_id", "repository_id", "inventory_id", "arguments", "app")
    if any(now.get(key) != template.get(key) for key in bindings):
        raise Refusal("Seed template changed; retain inputs for reconciliation")
    # No retry/finally around submission: an uncertain response can mean a live
    # task still needs these inputs. Leave them encrypted and reconcile by task ID.
    submission = {"project_id": project, "template_id": template_id, "message": message}
    if extra:
        submission["environment"] = json.dumps(extra)
    task = api("/tasks", submission)
    task_id = task.get("id")
    if not isinstance(task_id, int):
        raise Refusal("Task identity unavailable; retain inputs and reconcile before another submission")
    print(f"Seed task {task_id} submitted", flush=True)
    deadline = time.monotonic() + timeout
    while task.get("status") not in TERMINAL:
        if time.monotonic() > deadline:
            raise Refusal(f"Task {task_id} still nonterminal; encrypted inputs retained for reconciliation")
        time.sleep(5)
        task = api(f"/tasks/{task_id}")
    current = api(env_path)
    current_secrets = current.get("secrets", [])
    for item in created:
        if not any(all(entry.get(key) == item.get(key) for key in ("id", "name", "type"))
                   for entry in current_secrets):
            raise Refusal(f"Task {task_id} terminal but input identity changed; cleanup refused")
    deletions = [{"id": item["id"], "name": item["name"], "type": "env", "operation": "delete"}
                 for item in created]
    api(env_path, environment_body(current, deletions))
    remaining = api(env_path)
    if any(item.get("id") in {entry["id"] for entry in created} for item in remaining.get("secrets", [])):
        raise Refusal(f"Task {task_id} terminal but encrypted input cleanup incomplete")
    print(f"Seed task {task_id}: {task['status']}; temporary encrypted inputs removed", flush=True)
    if task["status"] != "success":
        raise Refusal("Seed failed; inspect credential-redacted task output before retrying")
