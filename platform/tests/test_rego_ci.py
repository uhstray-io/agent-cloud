"""The Rego CI job installs only the reviewed OPA release and remains gated."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/lint-and-test.yml").read_text())


def test_rego_job_verifies_opa_before_use_and_keeps_the_gate():
    job = WORKFLOW["jobs"]["rego"]
    assert job["timeout-minutes"] == 10

    install = next(step for step in job["steps"] if step.get("name") == "Install verified OPA 1.0.0")
    script = install["run"]
    assert "https://openpolicyagent.org/downloads/v1.0.0/opa_linux_amd64_static" in script
    assert install["env"]["OPA_SHA256"] == "3985fb4814b3860511beac516f59c5509eee9fdc83dbe910b67f3999f743b901"
    assert "--retry 3 --retry-all-errors" in script
    assert "--connect-timeout 10 --max-time 60" in script
    assert script.index("sha256sum --check") < script.index("chmod 755")

    run = next(step for step in job["steps"] if step.get("name") == "Run Rego tests")["run"]
    assert "opa check --strict platform/services/opa/deployment/policies" in run
    assert "opa test platform/services/opa/deployment/policies -v" in run

    gate = WORKFLOW["jobs"]["test"]
    assert gate["if"] == "always()"
    assert "rego" in gate["needs"]
    assert 'test "$REGO_RESULT" = success' in gate["steps"][0]["run"]
