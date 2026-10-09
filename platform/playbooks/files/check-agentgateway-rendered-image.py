#!/usr/bin/env python3
"""Report only the reviewed agentgateway image value from a rendered env file."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

IMAGE = "cr.agentgateway.dev/agentgateway:v1.5.0"


def main() -> int:
    try:
        env = Path(sys.argv[1]).read_text()
    except (IndexError, OSError):
        print(json.dumps({"status": "refused", "reason": "rendered image input unavailable"}))
        return 2

    images = re.findall(r"^AGW_IMAGE=(.*)$", env, re.M)
    if images != [IMAGE]:
        print(json.dumps({"status": "refused", "reason": "rendered image does not match the reviewed pin"}))
        return 2
    print(json.dumps({"status": "pass", "image": IMAGE}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
