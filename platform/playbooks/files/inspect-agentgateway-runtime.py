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
SAMPLE = re.compile(r"^\s+(randomSampling|clientSampling):\s*(false|[0-9]+(?:\.[0-9]+)?)\s*$", re.M)
# The pinned v1.5.0 schema defines both sampling fields as StringBoolFloat, so
# clientSampling:false is a valid explicit way to disable parent-based sampling.
REVISION = re.compile(r"^[0-9a-f]{40}$")


def main() -> int:
    try:
        data = json.load(sys.stdin)
        env = Path(data["env_path"]).read_text()
        config = Path(data["config_path"]).read_bytes()
        revision = str(data["revision"]).strip()
    except (KeyError, OSError, ValueError, TypeError):
        print(json.dumps({"status": "refused", "reason": "runtime inputs unavailable"}))
        return 2

    images = re.findall(r"^AGW_IMAGE=(.+)$", env, re.M)
    samples = {
        key: (False if value == "false" else float(value))
        for key, value in SAMPLE.findall(config.decode("utf-8", "replace"))
    }
    if len(images) != 1 or images[0] != IMAGE:
        print(json.dumps({"status": "refused", "reason": "rendered image is not the required v1.5.0 pin"}))
        return 2
    if set(samples) != {"randomSampling", "clientSampling"}:
        print(json.dumps({"status": "refused", "reason": "rendered trace sampling fields are incomplete"}))
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
    if not REVISION.fullmatch(revision):
        print(json.dumps({"status": "refused", "reason": "deployed revision is unavailable"}))
        return 2

    print(json.dumps({
        "status": "pass",
        "image": IMAGE,
        "random_sampling": random_sampling,
        "client_sampling": client_sampling,
        "rendered_config_sha256": hashlib.sha256(config).hexdigest(),
        "repository_revision": revision,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
