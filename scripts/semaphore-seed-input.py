#!/usr/bin/env python3
"""Seed a secret through a declared seed template's ISOLATED Semaphore environment.

Works for any template in platform/semaphore/templates.yml that declares
`isolated_environment` and `seed_inputs` (e.g. Seed OpenBao Key). The value is staged
as an encrypted environment input, one task runs, and exactly that input is removed.
It never becomes a survey field or an extra var, which Semaphore persists and serves
back over its API.

  # dry run: resolve and check everything, change nothing
  scripts/semaphore-seed-input.py --template "Seed OpenBao Key" \\
      --set bao_path=services/agentgateway --set bao_key=vllm_api_key \\
      --input BAO_VALUE=/path/to/value-file --inventory 2 --openbao-addr https://bao.example:8200 \\
      --url https://semaphore.example <token-file

  # after Provision Seed Environment: prove the environment's AppRole, no write
  ... --verify-only --apply <token-file
  # the real seed
  ... --apply <token-file

The operator token arrives on stdin. Values are read from files, never argv.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from semaphore_seed import API, TERMINAL, Refusal, preflight, stage_and_seed  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "platform/semaphore/templates.yml"
REPOSITORIES = ROOT / "platform/semaphore/repositories.yml"
DEV_REPOSITORY = "agent-cloud dev"  # setup-templates.yml's semaphore_dev_repo_name default


def declaration(base_name, catalog=CATALOG):
    templates = yaml.safe_load(catalog.read_text())["templates"]
    found = [t for t in templates if t.get("name") == base_name]
    if len(found) != 1:
        raise Refusal(f"No single declared template named {base_name!r}")
    decl = found[0]
    if not decl.get("isolated_environment") or not decl.get("seed_inputs"):
        raise Refusal(f"{base_name!r} declares no isolated_environment and seed_inputs")
    return decl


def resolve_names(decl, variant):
    if variant not in ("dev", "main"):
        raise Refusal("variant must be dev or main")
    if variant == "dev" and not decl.get("dev_variant"):
        raise Refusal("This template has no dev variant")
    suffix = " (Dev)" if variant == "dev" else ""
    return decl["name"] + suffix, decl["isolated_environment"] + suffix


def read_inputs(pairs, allowed):
    values = {}
    for pair in pairs:
        name, sep, path = pair.partition("=")
        if not sep or name not in allowed:
            raise Refusal(f"Input must be NAME=FILE with NAME in {sorted(allowed)}")
        if name in values:
            raise Refusal(f"Duplicate input: {name}")
        text = Path(path).read_text()
        value = text[:-1] if text.endswith("\n") else text
        if not value or "\n" in value or "\r" in value or "\x00" in value:
            raise Refusal(f"Input {name} must be one non-empty line")
        values[name] = value
    return values


def read_settings(pairs, decl):
    access = decl.get("seed_access_check")
    allowed = {v["name"] for v in decl.get("survey_vars") or []} - {access}
    settings = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or key not in allowed:
            raise Refusal(f"Setting must be KEY=VALUE with KEY in {sorted(allowed)}")
        settings[key] = value
    missing = [v["name"] for v in decl.get("survey_vars") or []
               if v.get("required") and v["name"] not in settings]
    if missing:
        raise Refusal(f"Missing required settings: {', '.join(missing)}")
    return settings


def repository_name(decl, variant):
    """The repository record this variant must run from, as setup-templates binds it."""
    return DEV_REPOSITORY if variant == "dev" else decl.get("repository", "agent-cloud")


def resolve_repository(api, name, declarations=REPOSITORIES):
    """The live record's id, after proving its URL and branch match the declaration."""
    declared = [r for r in yaml.safe_load(declarations.read_text())["repositories"] if r.get("name") == name]
    live = [r for r in api("/repositories") if r.get("name") == name]
    if len(declared) != 1 or len(live) != 1:
        raise Refusal(f"Expected one declared and one live repository named {name!r}")
    if (live[0].get("git_url"), live[0].get("git_branch")) != (declared[0]["git_url"], declared[0]["git_branch"]):
        raise Refusal(f"Repository {name!r} differs from its declaration")
    return live[0]["id"]


def locate(api, template_name, environment_name, playbook):
    templates = [t for t in api("/templates") if t.get("name") == template_name]
    if len(templates) != 1:
        raise Refusal(f"Expected one live template named {template_name!r}")
    template = templates[0]
    envs = [e for e in api("/environment") if e.get("name") == environment_name]
    if len(envs) != 1:
        raise Refusal(f"Expected one environment named {environment_name!r}; run Provision Seed Environment")
    if template.get("environment_id") != envs[0]["id"] or template.get("playbook") != playbook:
        raise Refusal("Template is not bound to its isolated environment; run Provision Seed Environment")
    return template["id"], envs[0]["id"]


def verify_access(api, project, template_id, access_var, settings, *, check, timeout=600):
    """Run the template's read-only access check. Stages nothing.

    `check` is the same read-only preflight the seed runs (a callable), so this can never
    launch a template the seed itself would refuse.
    """
    check()
    task = api("/tasks", {"project_id": project, "template_id": template_id,
                          "message": "Read-only access check for an isolated seed environment",
                          "environment": json.dumps({**settings, access_var: "true"})})
    task_id = task.get("id")
    if not isinstance(task_id, int):
        raise Refusal("Task identity unavailable")
    deadline = time.monotonic() + timeout
    while task.get("status") not in TERMINAL:
        if time.monotonic() > deadline:
            raise Refusal(f"Access check task {task_id} still nonterminal")
        time.sleep(5)
        task = api(f"/tasks/{task_id}")
    print(f"Access check task {task_id}: {task['status']}", flush=True)
    if task["status"] != "success":
        raise Refusal("Access check failed; inspect the task output before seeding")
    return task_id


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--template", required=True, help="declared base name, e.g. 'Seed OpenBao Key'")
    parser.add_argument("--variant", default="dev", choices=["dev", "main"])
    parser.add_argument("--input", action="append", default=[], help="NAME=FILE, NAME from seed_inputs")
    parser.add_argument("--set", action="append", default=[], help="KEY=VALUE, a declared survey var")
    parser.add_argument("--verify-only", action="store_true", help="run the read-only access check only")
    parser.add_argument("--apply", action="store_true", help="without it, resolve and check only")
    parser.add_argument("--inventory", type=int, required=True,
                        help="the APPROVED inventory id the template must still be bound to")
    parser.add_argument("--openbao-addr", required=True,
                        help="the APPROVED OpenBao endpoint the seed environment must still point at")
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", type=int, default=1)
    args = parser.parse_args()
    try:
        decl = declaration(args.template)
        template_name, environment_name = resolve_names(decl, args.variant)
        settings = read_settings(args.set, decl)
        values = {} if args.verify_only else read_inputs(args.input, set(decl["seed_inputs"]))
        if not args.verify_only and set(values) != set(decl["seed_inputs"]):
            raise Refusal(f"Supply every declared input: {', '.join(decl['seed_inputs'])}")
        print(f"Template {template_name!r} in {environment_name!r}; inputs "
              f"{', '.join(sorted(values)) or 'none'}; settings {', '.join(sorted(settings))}", flush=True)
        api = API(args.url, args.project, sys.stdin.read().strip())
        template_id, environment_id = locate(api, template_name, environment_name, decl["playbook"])
        bindings = {"repository_id": resolve_repository(api, repository_name(decl, args.variant)),
                    "inventory_id": args.inventory}

        def check():
            return preflight(api, args.project, template_id, environment_id, decl["seed_inputs"],
                             playbook=decl["playbook"], template_names={template_name},
                             endpoint=args.openbao_addr, bindings=bindings)
        check()
        if not args.apply:
            print(f"Template {template_id}, environment {environment_id}: every preflight check passed. "
                  "Dry run: nothing changed.")
            return 0
        if args.verify_only:
            verify_access(api, args.project, template_id, decl["seed_access_check"], settings, check=check)
            return 0
        stage_and_seed(api, args.project, template_id, environment_id, values,
                       playbook=decl["playbook"], template_names={template_name}, extra=settings,
                       endpoint=args.openbao_addr, bindings=bindings, message=f"Seed via {environment_name} (encrypted, removed after the task)")
    except Refusal as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("Input or API shape refused; details suppressed to protect credentials", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
