#!/usr/bin/env python3
"""Stage declared Postiz provider inputs in Semaphore; never source an env file.

Run without --apply for a provider-presence report. With --apply, read the existing operator
token from stdin, find Seed Postiz Secrets and its isolated environment by name, check them
the way every seed CLI does (template bindings, the approved repository and inventory, a
clean environment), stage encrypted inputs, run one task, then remove only the created
inputs after a terminal result. The approved inventory is also where the seed run takes its
OpenBao address.

  scripts/postiz-seed-input.py --env-file providers.env --apply --inventory <inventory-id> \\
      --url https://semaphore.example <token-file
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
from semaphore_seed import API, Refusal, declaration, seed_target, stage_and_seed  # noqa: E402

TEMPLATE = "Seed Postiz Secrets"


def provider_fields():
    """Derive the allow-list from the actual seed declaration and app template."""
    # The playbook the template declares (templates.yml), not a second copy of its path.
    seed = (ROOT / declaration(TEMPLATE)["playbook"]).read_text()
    seedable = seed.split("    _seedable:\n", 1)[1].split("\n  tasks:", 1)[0]
    names = set(re.findall(r"^      - ([a-z_]+)$", seedable, re.M))
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
    parser.add_argument("--url")
    parser.add_argument("--project", type=int, default=1)
    args = parser.parse_args()
    try:
        values = parse_inputs(args.env_file.read_text(), provider_fields())
        providers = sorted({name.removeprefix("SEED_").split("_")[0] for name in values})
        print("Configured provider fields: " + ", ".join(providers))
        if args.apply:
            if not all((args.url, args.inventory)):
                raise Refusal("Apply requires --url and --inventory")
            api = API(args.url, args.project, sys.stdin.read().strip())
            t = seed_target(api, declaration(TEMPLATE), args.variant, args.inventory)
            stage_and_seed(api, args.project, t, values,
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
