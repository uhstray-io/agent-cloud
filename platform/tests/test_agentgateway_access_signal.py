"""The OTLP access-log stream carries a bounded signal discriminator."""

import hashlib
import json
from pathlib import Path

from jinja2 import Environment
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "platform/services/agentgateway/deployment/templates/config.yaml.j2"


def _render(**overrides):
    environment = Environment(trim_blocks=True, lstrip_blocks=True)
    environment.filters.update(
        bool=bool,
        flatten=lambda values: [item for group in values for item in group],
        unique=lambda values: list(dict.fromkeys(values)),
        hash=lambda value, algorithm="sha256": hashlib.new(algorithm, str(value).encode()).hexdigest(),
        to_json=lambda value: json.dumps(value),
    )
    host_vars = {
        "agw_ui_enabled": False,
        "agw_listener_tls": False,
        "internal_leaves": [],
        "agw_clients": ["workstation"],
        "agw_client_policies": {},
        "agw_plaintext_keys": False,
        "local_mode": False,
        "agw_models": [{"name": "test-model"}],
        "agw_upstream_base_url": "http://upstream.invalid/v1",
        "secrets": {"client_workstation": "synthetic-client-key", "vllm_api_key": "synthetic-upstream-key"},
        "agw_otlp_host": "alloy.invalid:4317",
        "agw_trace_sampling": 0.05,
        "agw_rate_requests_per_minute_total": 120,
        **overrides,
    }
    rendered = environment.from_string(CONFIG.read_text()).render(**host_vars)
    return yaml.safe_load(rendered)


def test_signal_discriminator_is_scoped_to_otlp_access_logs():
    rendered = _render()
    access_log = rendered["frontendPolicies"]["accessLog"]
    otlp_fields = access_log["otlp"]["fields"]["add"]

    assert otlp_fields["signal"] == '"access-log"'
    assert otlp_fields["service"] == '"agentgateway"'
    assert otlp_fields["identity"] == "apiKey.name"
    assert otlp_fields["http.path"] == "request.path"
    assert "request.uri" not in otlp_fields
    assert "signal" not in access_log["add"]
    assert "signal" not in access_log["otlp"]
    assert "signal" not in rendered["frontendPolicies"]["tracing"]["attributes"]
