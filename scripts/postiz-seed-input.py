#!/usr/bin/env python3
"""Stage declared Postiz provider inputs in Semaphore; never source an env file.

Run without --apply for a provider-presence report. With --apply, read the existing operator
token from stdin, find Seed Postiz Secrets and its isolated environment by name, check them
the way every seed CLI does (template bindings, approved repository and inventory, approved
OpenBao endpoint, a clean environment), stage encrypted inputs, run one task, then remove
only the created inputs after a terminal result.

  scripts/postiz-seed-input.py --env-file providers.env --apply --inventory 2 \\
      --openbao-addr https://bao.example:8200 --url https://semaphore.example <token-file
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED_PLAYBOOK = "platform/playbooks/seed-postiz-secrets.yml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from semaphore_seed import (  # noqa: E402
    API,
    Refusal,
    declaration,
    locate,
    repository_name,
    resolve_names,
    resolve_repository,
    stage_and_seed,
)

TEMPLATE = "Seed Postiz Secrets"


def provider_fields():
    """Derive the allow-list from the actual seed declaration and app template."""
    seed = (ROOT / SEED_PLAYBOOK).read_text()
    declaration = seed.split("    _seedable:\n", 1)[1].split("\n  tasks:", 1)[0]
    names = set(re.findall(r"^      - ([a-z_]+)$", declaration, re.M))
    template = (ROOT / "platform/services/postiz/deployment/templates/postiz.env.j2").read_text()
    fields = re.findall(r"^([A-Z_]+)=\{\{ secrets\.postiz_([a-z_]+)", template, re.M)
    return {env: "SEED_" + name.upper() for env, name in fields if name in names}


def parse_inputs(text, fields):
    selected = {}
    seen = set()
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not match or match[1] not in fields:
            continue
        name, value = match.groups()
        if name in seen:
            raise Refusal(f"Duplicate selected field: {name}")
        seen.add(name)
        if value.startswith(("'", '"')):
            quote = value[0]
            # Deliberately literal; ambiguous escaped/multiline forms are refused.
            end = value.find(quote, 1)
            tail = value[end + 1:].strip()
            if end < 0 or "\\" in value[1:end] or (tail and not tail.startswith("#")):
                raise Refusal(f"Unsupported quoting in selected field: {name}")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if "\x00" in value or "\r" in value:
            raise Refusal(f"Unsupported control character in selected field: {name}")
        if value:
            selected[fields[name]] = value
    groups = {}
    for name, target in fields.items():
        groups.setdefault(name.split("_")[0], []).append(target)
    for provider, keys in groups.items():
        if any(key in selected for key in keys):
            missing = [key.removeprefix("SEED_") for key in keys if key not in selected]
            if missing:
                raise Refusal(f"Incomplete {provider} fields: {', '.join(missing)}")
    if not selected:
        raise Refusal("No complete provider credentials supplied")
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--variant", default="dev", choices=["dev", "main"])
    parser.add_argument("--inventory", type=int,
                        help="the APPROVED inventory id the template must still be bound to")
    parser.add_argument("--openbao-addr", help="the APPROVED OpenBao endpoint the seed environment must point at")
    parser.add_argument("--url")
    parser.add_argument("--project", type=int, default=1)
    args = parser.parse_args()
    try:
        values = parse_inputs(args.env_file.read_text(), provider_fields())
        providers = sorted({name.removeprefix("SEED_").split("_")[0] for name in values})
        print("Configured provider fields: " + ", ".join(providers))
        if args.apply:
            if not all((args.url, args.inventory, args.openbao_addr)):
                raise Refusal("Apply requires --url, --inventory and --openbao-addr")
            decl = declaration(TEMPLATE)
            template_name, environment_name = resolve_names(decl, args.variant)
            api = API(args.url, args.project, sys.stdin.read().strip())
            template_id, environment_id = locate(api, template_name, environment_name)
            bindings = {"repository_id": resolve_repository(api, repository_name(decl, args.variant)),
                        "inventory_id": args.inventory}
            stage_and_seed(api, args.project, template_id, environment_id, values, playbook=SEED_PLAYBOOK,
                           template_names={template_name}, endpoint=args.openbao_addr, bindings=bindings,
                           message="Seed declared Postiz provider credentials via encrypted inputs")
    except Refusal as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("Input or API shape refused; details suppressed to protect credentials", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
