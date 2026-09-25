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
      --input BAO_VALUE=/path/to/value-file --inventory 2 \\
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from semaphore_seed import (  # noqa: E402
    API,
    Refusal,
    declaration,
    preflight,
    seed_target,
    stage_and_seed,
    submit,
    wait,
)


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


def verify_access(api, project, template_id, access_var, settings, *, check, timeout=600):
    """Run the template's read-only access check. Stages nothing.

    `check` is the same read-only preflight the seed runs (a callable), so this can never
    launch a template the seed itself would refuse.
    """
    check()
    what = "Read-only access check"
    task = submit(api, {"project_id": project, "template_id": template_id,
                        "message": "Read-only access check for an isolated seed environment",
                        "environment": json.dumps({**settings, access_var: "true"})}, what)
    task = wait(api, task, what, timeout)
    print(f"Access check task {task['id']}: {task['status']}", flush=True)
    if task["status"] != "success":
        raise Refusal("Access check failed; inspect the task output before seeding")
    return task["id"]


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
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", type=int, default=1)
    args = parser.parse_args()
    try:
        decl = declaration(args.template)
        if not decl.get("seed_inputs"):
            raise Refusal(f"{args.template!r} declares no seed_inputs for this CLI; see its own seed script")
        settings = read_settings(args.set, decl)
        values = {} if args.verify_only else read_inputs(args.input, set(decl["seed_inputs"]))
        if not args.verify_only and set(values) != set(decl["seed_inputs"]):
            raise Refusal(f"Supply every declared input: {', '.join(decl['seed_inputs'])}")
        api = API(args.url, args.project, sys.stdin.read().strip())
        t = seed_target(api, decl, args.variant, args.inventory)
        print(f"Template {t.template_name!r} in {t.environment_name!r}; inputs "
              f"{', '.join(sorted(values)) or 'none'}; settings {', '.join(sorted(settings))}", flush=True)

        def check():
            return preflight(api, args.project, t.template_id, t.environment_id,
                             playbook=t.playbook, template_name=t.template_name, bindings=t.bindings)
        # Each path runs the read-only preflight exactly once: here for a dry run, inside
        # verify_access and stage_and_seed otherwise.
        if not args.apply:
            check()
            print(f"Template {t.template_id}, environment {t.environment_id}: every preflight check passed. "
                  "Dry run: nothing changed.")
            return 0
        if args.verify_only:
            verify_access(api, args.project, t.template_id, decl["seed_access_check"], settings, check=check)
            return 0
        stage_and_seed(api, args.project, t.template_id, t.environment_id, values,
                       playbook=t.playbook, template_name=t.template_name, extra=settings,
                       bindings=t.bindings,
                       message=f"Seed via {t.environment_name} (encrypted, removed after the task)")
    except Refusal as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("Input or API shape refused; details suppressed to protect credentials", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
