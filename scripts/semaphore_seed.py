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
from typing import NamedTuple

# The one clean-environment rule, shared with publication and the provisioner (an Ansible
# filter module; loaded by path so there is a single definition).
_RULE = importlib.util.spec_from_file_location(
    "seed_environment_rule",
    Path(__file__).resolve().parents[1] / "platform/semaphore/filter_plugins/publication.py")
_rule = importlib.util.module_from_spec(_RULE)
_RULE.loader.exec_module(_rule)
seed_environment_problems = _rule.seed_environment_problems

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "platform/semaphore/templates.yml"
REPOSITORIES = ROOT / "platform/semaphore/repositories.yml"
DEV_REPOSITORY = "agent-cloud dev"  # setup-templates.yml's semaphore_dev_repo_name default

# v2.17.31 pkg/task_logger/task_logger.go, IsFinished (rejected is not finished).
TERMINAL = {"success", "error", "stopped"}
POLL_SECONDS = 5


class Refusal(Exception):
    """Safe, value-free error text."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would copy the Authorization header to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    """The one Semaphore API client for the operator scripts (seed CLIs and the launcher)."""

    def __init__(self, url, project, token):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise Refusal("Semaphore URL must be a plain HTTPS origin")
        if not token:
            raise Refusal("An existing authorized operator token is required on stdin")
        self.base, self.project, self.token = url.rstrip("/"), project, token
        self.open = urllib.request.build_opener(NoRedirect).open

    def __call__(self, path, body=None, project_scoped=True):
        prefix = f"/api/project/{self.project}" if project_scoped else "/api"
        req = urllib.request.Request(self.base + prefix + path, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
        }, data=None if body is None else json.dumps(body).encode(),
            method="GET" if body is None else "PUT" if path.startswith("/environment/") else "POST")
        try:
            with self.open(req, timeout=30) as response:
                data = response.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            raise Refusal(f"Semaphore HTTP {error.code} on {path}; response body suppressed") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise Refusal(f"Semaphore request outcome unavailable on {path}; do not retry a write blindly") from None


def submit(api, body, what):
    """POST one task and return it. A failed POST means the outcome is UNKNOWN, not "nothing
    started": the server may have accepted it and lost the response, so this never retries."""
    try:
        task = api("/tasks", body)
    except Refusal as error:
        raise Refusal(f"{what}: submission outcome uncertain ({error}); a task may be running, "
                      "check the template's tasks before launching again") from None
    if not isinstance((task or {}).get("id"), int):
        raise Refusal(f"{what}: submission outcome uncertain (no task id returned); a task may be running")
    return task


def wait(api, task, what, timeout=600):
    """Poll a submitted task until it is terminal; the final task row."""
    task_id = task["id"]
    deadline = time.monotonic() + timeout
    while task.get("status") not in TERMINAL:
        if time.monotonic() > deadline:
            raise Refusal(f"{what}: task {task_id} still nonterminal after {timeout}s")
        time.sleep(POLL_SECONDS)
        task = api(f"/tasks/{task_id}")
        if not isinstance(task, dict):
            # Name the task: the caller may not have printed its id yet (review of #249).
            raise Refusal(f"{what}: task {task_id} status unreadable; inspect it in Semaphore")
    return task


def declaration(base_name, catalog=CATALOG):
    import yaml  # here, not at the top: the launcher imports this module and stays stdlib-only
    templates = yaml.safe_load(catalog.read_text())["templates"]
    found = [t for t in templates if t.get("name") == base_name]
    if len(found) != 1:
        raise Refusal(f"No single declared template named {base_name!r}")
    decl = found[0]
    if not decl.get("isolated_environment"):
        raise Refusal(f"{base_name!r} declares no isolated_environment")
    return decl


def resolve_names(decl, variant):
    if variant not in ("dev", "main"):
        raise Refusal("variant must be dev or main")
    if variant == "dev" and not decl.get("dev_variant"):
        raise Refusal("This template has no dev variant")
    suffix = " (Dev)" if variant == "dev" else ""
    return decl["name"] + suffix, decl["isolated_environment"] + suffix


def repository_name(decl, variant):
    """The repository record this variant must run from, as setup-templates binds it."""
    return DEV_REPOSITORY if variant == "dev" else decl.get("repository", "agent-cloud")


def resolve_repository(api, name, declarations=REPOSITORIES):
    """The live record's id, after proving its URL and branch match the declaration."""
    import yaml  # see declaration()
    declared = [r for r in yaml.safe_load(declarations.read_text())["repositories"] if r.get("name") == name]
    live = [r for r in api("/repositories") if r.get("name") == name]
    if len(declared) != 1 or len(live) != 1:
        raise Refusal(f"Expected one declared and one live repository named {name!r}")
    if (live[0].get("git_url"), live[0].get("git_branch")) != (declared[0]["git_url"], declared[0]["git_branch"]):
        raise Refusal(f"Repository {name!r} differs from its declaration")
    return live[0]["id"]


def locate(api, template_name, environment_name):
    """Name to id only. Whether the template is bound to this environment, with the approved
    playbook, is preflight's to decide: one checker, not two that can drift."""
    templates = [t for t in api("/templates") if t.get("name") == template_name]
    if len(templates) != 1:
        raise Refusal(f"Expected one live template named {template_name!r}")
    envs = [e for e in api("/environment") if e.get("name") == environment_name]
    if len(envs) != 1:
        raise Refusal(f"Expected one environment named {environment_name!r}; run Provision Seed Environment")
    return templates[0]["id"], envs[0]["id"]


class Target(NamedTuple):
    """Where one seed runs: resolved by name, with the bindings its preflight must still see."""
    template_id: int
    environment_id: int
    template_name: str
    environment_name: str
    playbook: str
    bindings: dict


def seed_target(api, decl, variant, inventory):
    """The template and environment for this declaration and variant, and the APPROVED
    repository and inventory the template must still be bound to. Every seed CLI resolves
    its target here, so none of them can skip or reorder a binding check."""
    template_name, environment_name = resolve_names(decl, variant)
    template_id, environment_id = locate(api, template_name, environment_name)
    bindings = {"repository_id": resolve_repository(api, repository_name(decl, variant)),
                "inventory_id": inventory}
    return Target(template_id, environment_id, template_name, environment_name, decl["playbook"], bindings)


def environment_body(env, operations):
    # Preserve ALL fields returned by the server, replacing only operation data.
    return {**env, "secrets": operations}


def preflight(api, project, template_id, expected_env, *, playbook, template_name, bindings=None):
    """Every read-only check a seed makes before its first write. Returns (template, env).

    Dry run, the read-only access check and the real seed all call this, so none of them
    can launch or stage against a template the others would refuse. `bindings` maps
    template fields (repository_id, inventory_id) to their APPROVED values: a template
    rebound after provisioning is refused before anything is staged or launched. The
    approved inventory is also where the seed run's OpenBao address comes from (its
    all.vars); the run refuses an address that differs from it.
    """
    template = api(f"/templates/{template_id}")
    if (template.get("playbook") != playbook
            or template.get("name") != template_name
            or template.get("app") != "ansible"
            or template.get("arguments") not in (None, "[]", [])
            or template.get("environment_id") != expected_env):
        raise Refusal("Template name, playbook, app, arguments or environment binding differs; "
                      "run Provision Seed Environment")
    for field, approved in (bindings or {}).items():
        if template.get(field) != approved:
            raise Refusal(f"Template {field} differs from its approved binding")
    templates = {item["id"]: item for item in api("/templates")}
    if any(item.get("environment_id") == expected_env and item.get("id") != template_id
           for item in templates.values()):
        raise Refusal("Seed environment is bound to other templates; provision a dedicated environment through code")
    for task in api("/tasks/last"):
        if task.get("status") not in TERMINAL:
            owner = templates.get(task.get("template_id"), {})
            if owner.get("environment_id") == expected_env:
                raise Refusal("Another task is using the seed environment")
    # Read LAST, so the environment the caller stages into is the one every check above saw.
    before = api(f"/environment/{expected_env}")
    if before.get("project_id") != project or before.get("id") != expected_env:
        raise Refusal("Environment identity differs")
    # Before anything is staged the environment must be CLEAN by the one shared rule: only
    # the two AppRole inputs, no plaintext env vars, no extra vars (an OpenBao address pin
    # would override the inventory's). A leftover of ANY name means an earlier run did not
    # finish, or something else writes here; its task may still need it (review of PR #205).
    problems = seed_environment_problems(before)
    if problems:
        raise Refusal("Seed environment requires reconciliation before staging: " + "; ".join(problems))
    # The rule allows an environment with no AppRole yet (a fresh provision); a seed needs it.
    if {item["name"] for item in before.get("secrets", [])} != set(_rule.SEED_AUTH_INPUTS):
        raise Refusal("Provision both AppRole inputs in the isolated environment before staging")
    return template, before


def stage_and_seed(api, project, template_id, expected_env, values, *, playbook, template_name,
                   extra=None, bindings=None,
                   message="Seed declared inputs via encrypted inputs", timeout=600):
    """Stage `values` as encrypted inputs in a DEDICATED environment, run one task of the
    seed template, then remove exactly the inputs it created.

    Refuses before any write unless the environment is clean by the shared rule (preflight).
    `extra` is NON-SECRET launch configuration only: Semaphore persists it.
    """
    template, before = preflight(api, project, template_id, expected_env, playbook=playbook,
                                 template_name=template_name, bindings=bindings)
    env_path = f"/environment/{expected_env}"
    # A fresh equality check immediately before the first write. It is not CAS (v2.17 has
    # none); it refuses a change made since preflight read the environment (reviews of #249).
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
    pinned = ("id", "name", "playbook", "environment_id", "repository_id", "inventory_id", "arguments", "app")
    if any(now.get(key) != template.get(key) for key in pinned):
        raise Refusal("Seed template changed; retain inputs for reconciliation")
    # No retry/finally around submission: an uncertain response can mean a live
    # task still needs these inputs. Leave them encrypted and reconcile by task ID.
    submission = {"project_id": project, "template_id": template_id, "message": message}
    if extra:
        submission["environment"] = json.dumps(extra)
    retained = "Seed (encrypted inputs retained for reconciliation)"
    task = submit(api, submission, retained)
    task_id = task["id"]
    print(f"Seed task {task_id} submitted", flush=True)
    task = wait(api, task, retained, timeout)
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
