#!/usr/bin/env python3
"""Emit safe metadata from the rendered and running agentgateway deployment.

No rendered config, environment values, request data, or container inspect output is
printed. This is the read-only first stage of observability-estate task 5.7.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

IMAGE = "cr.agentgateway.dev/agentgateway:v1.5.0"
FRONTEND_POLICIES = re.compile(r"^frontendPolicies:\s*(?:#.*)?$")
TRACING_BLOCK = re.compile(r"^  tracing:\s*(?:#.*)?$")
SAMPLING_KEY = re.compile(r"^([ \t]*)(randomSampling|clientSampling)\s*:\s*(.*)$")
SAMPLING_VALUE = re.compile(r"^(false|[0-9]+(?:\.[0-9]+)?)(?:\s+#.*)?$")
# The pinned v1.5.0 schema defines both sampling fields as StringBoolFloat, so
# clientSampling:false is a valid explicit way to disable parent-based sampling.
REVISION = re.compile(r"^[0-9a-f]{40}$")


def parse_sampling(config: str) -> tuple[dict[str, float | bool], str | None]:
    """Read exactly one sampling declaration per field from frontendPolicies.tracing."""
    lines = config.splitlines()
    policy_starts = [index for index, line in enumerate(lines) if FRONTEND_POLICIES.fullmatch(line)]
    if len(policy_starts) != 1:
        return {}, "rendered frontendPolicies block is missing or duplicated"

    policy_start = policy_starts[0] + 1
    policy_end = next((index for index in range(policy_start, len(lines))
                       if lines[index].strip() and not lines[index].lstrip().startswith("#")
                       and len(lines[index]) == len(lines[index].lstrip(" "))), len(lines))
    tracing_starts = [index for index in range(policy_start, policy_end)
                      if TRACING_BLOCK.fullmatch(lines[index])]
    if len(tracing_starts) != 1:
        return {}, "rendered frontendPolicies.tracing block is missing or duplicated"

    tracing_start = tracing_starts[0] + 1
    tracing_end = next((index for index in range(tracing_start, policy_end)
                        if lines[index].strip() and not lines[index].lstrip().startswith("#")
                        and len(lines[index]) - len(lines[index].lstrip(" ")) <= 2), policy_end)
    declarations: dict[str, list[tuple[str, str]]] = {"randomSampling": [], "clientSampling": []}
    for line in lines[tracing_start:tracing_end]:
        if line.lstrip().startswith("#"):
            continue
        match = SAMPLING_KEY.fullmatch(line)
        if match:
            declarations[match.group(2)].append((match.group(1), match.group(3)))

    duplicates = [key for key, values in declarations.items() if len(values) > 1]
    if duplicates:
        return {}, "rendered trace sampling fields are duplicated"
    if any(len(values) != 1 for values in declarations.values()):
        return {}, "rendered trace sampling fields are incomplete"

    samples: dict[str, float | bool] = {}
    for key, values in declarations.items():
        indent, raw_value = values[0]
        if indent != "    ":
            return {}, "rendered trace sampling field has invalid indentation"
        match = SAMPLING_VALUE.fullmatch(raw_value)
        if not match:
            return {}, "rendered trace sampling field has an invalid value"
        value = match.group(1)
        samples[key] = False if value == "false" else float(value)
    return samples, None


def main() -> int:
    try:
        data = json.load(sys.stdin)
        env = Path(data["env_path"]).read_text()
        config = Path(data["config_path"]).read_bytes()
        checkout_revision = str(data["checkout_revision"]).strip()
    except (KeyError, OSError, ValueError, TypeError):
        print(json.dumps({"status": "refused", "reason": "runtime inputs unavailable"}))
        return 2

    images = re.findall(r"^AGW_IMAGE=(.+)$", env, re.M)
    if len(images) != 1 or images[0] != IMAGE:
        print(json.dumps({"status": "refused", "reason": "rendered image is not the required v1.5.0 pin"}))
        return 2
    samples, sampling_error = parse_sampling(config.decode("utf-8", "replace"))
    if sampling_error:
        print(json.dumps({"status": "refused", "reason": sampling_error}))
        return 2
    random_sampling = samples["randomSampling"]
    client_sampling = samples["clientSampling"]
    if isinstance(random_sampling, bool) or not 0 < random_sampling <= 0.1:
        print(json.dumps({"status": "refused", "reason": "rendered random sampling is outside the reviewed range"}))
        return 2
    if client_sampling is not False and (
        isinstance(client_sampling, bool)
        or not 0 < client_sampling <= 0.1
        or client_sampling != random_sampling
    ):
        print(json.dumps({"status": "refused", "reason": "rendered trace sampling is outside the reviewed range"}))
        return 2
    if not REVISION.fullmatch(checkout_revision):
        print(json.dumps({"status": "refused", "reason": "checkout revision is unavailable"}))
        return 2

    print(json.dumps({
        "status": "pass",
        "image": IMAGE,
        "random_sampling": random_sampling,
        "client_sampling": client_sampling,
        "rendered_config_sha256": hashlib.sha256(config).hexdigest(),
        "checkout_revision": checkout_revision,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
