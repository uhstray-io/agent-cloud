"""Validation core of the launch gate: the rules an agent-initiated Semaphore launch must pass.

The gate is a stdlib HTTP service between the agents (skynet, the Dispatch Console) and Semaphore.
It authenticates the caller, checks the request against its allowlist and the template's declared
survey fields, asks OPA for a decision, and only then creates the task. The rules are pure functions
that do no network I/O: config loading, bearer authentication, request parsing, agent and template
authorisation, the survey merge, and the OPA input and Semaphore task body. Below them sit the
Semaphore and OPA clients and the request flow (Gate), which add the I/O and the check order but
no rule of their own. The HTTPS listener (GateServer, GateHandler) and the entry point (main) sit
on top: TLS with certificate reload, HTTP framing and the body cap, with no rule of their own.
"""

import hmac
import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

TERMINAL = frozenset({"success", "error", "stopped"})

# Exact Semaphore releases whose task body and check-mode flag were verified. Mirrors
# scripts/semaphore-launch.py:45 (VERIFIED_DRY_RUN_VERSIONS). Exact, not a series: another patch
# release may read params.dry_run differently, and a mis-read flag means a real run.
VERIFIED_DRY_RUN_VERSIONS = frozenset({"v2.17.31", "v2.18.12", "v2.19.11"})

MAX_VALUE = 4096
MAX_BODY = 65536
MAX_TEMPLATE = 200
MIN_TOKEN = 32

# Matched with fullmatch, never with a trailing "$": "$" also matches before a final newline,
# which would let "infra-agent\n" through.
_CALLER = re.compile(r"[a-z][a-z0-9-]{0,31}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SURVEY_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_INTEGER = re.compile(r"-?[0-9]+")
_TEMPLATE_MARKERS = ("{{", "{%", "{#")
_REQUEST_KEYS = frozenset({"template", "survey", "agent", "workflow_id", "step", "dry_run", "proposal", "context"})
_REQUIRED_CONFIG = frozenset(
    {
        "semaphore_url",
        "semaphore_project",
        "opa_url",
        "allowlist",
        "callers",
        "listen_host",
        "listen_port",
        "tls_cert",
        "tls_key",
    }
)
_OPTIONAL_CONFIG = frozenset({"opa_ca_bundle", "local_mode"})
_BEARER = "Bearer "


class ConfigError(Exception):
    """The gate's configuration is unusable. The message names a field or variable, never a value."""


class Refusal(Exception):
    """A request the gate refuses. ``str()`` is the detail, which never echoes a survey value."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Config:
    semaphore_url: str
    semaphore_project: int
    semaphore_token: str
    opa_url: str
    opa_ca_bundle: str | None
    allowlist: frozenset[str]
    callers: dict[str, frozenset[str]]
    caller_tokens: dict[str, str]
    listen_host: str
    listen_port: int
    tls_cert: str
    tls_key: str
    local_mode: bool

    def __repr__(self) -> str:
        # Tokens never print. Field names and non-secret values stay, so a config dump is still readable.
        callers = {name: sorted(agents) for name, agents in self.callers.items()}
        return (
            f"Config(semaphore_url={self.semaphore_url!r}, semaphore_project={self.semaphore_project!r}, "
            "semaphore_token='<redacted>', "
            f"opa_url={self.opa_url!r}, opa_ca_bundle={self.opa_ca_bundle!r}, "
            f"allowlist={sorted(self.allowlist)!r}, callers={callers!r}, caller_tokens='<redacted>', "
            f"listen_host={self.listen_host!r}, listen_port={self.listen_port!r}, "
            f"tls_cert={self.tls_cert!r}, tls_key={self.tls_key!r}, local_mode={self.local_mode!r})"
        )


@dataclass(frozen=True)
class LaunchRequest:
    template: str
    survey: dict[str, str]
    agent: str
    workflow_id: str
    step: str
    dry_run: bool
    proposal: dict[str, Any] | None
    context: dict[str, Any] | None


def load_config(path: str, environ: Mapping[str, str]) -> Config:
    """Read the config file and take the tokens from the environment. Raises ConfigError, never prints a value."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"config file {path} is unreadable or is not JSON") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config file must hold a JSON object")
    unknown = sorted(set(raw) - _REQUIRED_CONFIG - _OPTIONAL_CONFIG)
    if unknown:
        raise ConfigError(f"unknown config key(s): {', '.join(unknown)}")
    missing = sorted(_REQUIRED_CONFIG - set(raw))
    if missing:
        raise ConfigError(f"missing config key(s): {', '.join(missing)}")

    local_mode = raw.get("local_mode", False)
    if not isinstance(local_mode, bool):
        raise ConfigError("local_mode must be true or false")
    opa_ca_bundle = raw.get("opa_ca_bundle")
    if opa_ca_bundle is not None:
        opa_ca_bundle = _text(opa_ca_bundle, "opa_ca_bundle")

    callers = _callers(raw["callers"])
    caller_tokens: dict[str, str] = {}
    seen: dict[str, str] = {}
    for name in callers:
        var = _caller_token_var(name)
        token = _token(environ, var)
        if token in seen:
            raise ConfigError(f"{var} repeats the token of {_caller_token_var(seen[token])}")
        seen[token] = name
        caller_tokens[name] = token

    return Config(
        semaphore_url=_base_url(raw["semaphore_url"], "semaphore_url", local_mode),
        semaphore_project=_positive_int(raw["semaphore_project"], "semaphore_project"),
        semaphore_token=_token(environ, "LAUNCH_GATE_SEMAPHORE_TOKEN"),
        opa_url=_base_url(raw["opa_url"], "opa_url", local_mode),
        opa_ca_bundle=opa_ca_bundle,
        allowlist=frozenset(_str_list(raw["allowlist"], "allowlist")),
        callers=callers,
        caller_tokens=caller_tokens,
        listen_host=_text(raw["listen_host"], "listen_host"),
        listen_port=_port(raw["listen_port"]),
        tls_cert=_text(raw["tls_cert"], "tls_cert"),
        tls_key=_text(raw["tls_key"], "tls_key"),
        local_mode=local_mode,
    )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _port(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError("listen_port must be an integer from 1 to 65535")
    return value


def _str_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{name} must be a list of non-empty strings")
    return value


def _base_url(value: object, name: str, local_mode: bool) -> str:
    url = _text(value, name).rstrip("/")
    schemes = {"https", "http"} if local_mode else {"https"}
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as exc:
        raise ConfigError(f"{name} is not a valid URL") from exc
    if parts.scheme not in schemes or not host:
        raise ConfigError(f"{name} must be an https:// URL (http:// only when local_mode is true)")
    return url


def _callers(value: object) -> dict[str, frozenset[str]]:
    if not isinstance(value, dict):
        raise ConfigError("callers must be an object of caller name to agent list")
    callers: dict[str, frozenset[str]] = {}
    for name, agents in value.items():
        if not _CALLER.fullmatch(name):
            raise ConfigError(
                "caller names are 1-32 characters: a lowercase letter, then lowercase letters, digits or hyphens"
            )
        callers[name] = frozenset(_str_list(agents, f"callers.{name}"))
    return callers


def _caller_token_var(name: str) -> str:
    return "LAUNCH_GATE_CALLER_TOKEN_" + name.upper().replace("-", "_")


def _token(environ: Mapping[str, str], var: str) -> str:
    value = environ.get(var)
    if not value:
        raise ConfigError(f"{var} is missing or empty")
    if len(value) < MIN_TOKEN:
        raise ConfigError(f"{var} is shorter than {MIN_TOKEN} characters")
    return value


def _unauthorized() -> Refusal:
    return Refusal(401, "unauthorized", "missing or invalid bearer token")


def authenticate(header: str | None, config: Config) -> str:
    """Return the caller whose token the Bearer header presents. Every token is compared; none is skipped."""
    if not isinstance(header, str) or not header.startswith(_BEARER):
        raise _unauthorized()
    presented = header[len(_BEARER) :].encode("utf-8")
    matched: str | None = None
    for name, token in config.caller_tokens.items():
        # Bytes on both sides: compare_digest raises TypeError on a non-ASCII str.
        if hmac.compare_digest(presented, token.encode("utf-8")) and matched is None:
            matched = name
    if matched is None:
        raise _unauthorized()
    return matched


def parse_request(body: bytes) -> LaunchRequest:
    """Parse and check the request's shape and its literal survey values. No outbound call happens here."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise Refusal(400, "bad_request", "body is not UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise Refusal(400, "bad_request", "body must be a JSON object")
    unknown = sorted(set(data) - _REQUEST_KEYS)
    if unknown:
        raise Refusal(400, "bad_request", f"unknown request key(s): {', '.join(unknown)}")

    template = _required_str(data, "template")
    agent = _required_str(data, "agent")
    workflow_id = _required_str(data, "workflow_id")
    step = _required_str(data, "step")
    if not template or len(template) > MAX_TEMPLATE:
        raise Refusal(400, "bad_request", f"template must be 1 to {MAX_TEMPLATE} characters")
    for key, value in (("agent", agent), ("workflow_id", workflow_id), ("step", step)):
        if not _IDENTIFIER.fullmatch(value):
            raise Refusal(400, "bad_request", f"{key} is not a valid identifier")

    survey = _survey(data.get("survey", {}))

    dry_run = data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise Refusal(400, "bad_request", "dry_run must be true or false")

    optional: dict[str, dict[str, Any] | None] = {}
    for key in ("proposal", "context"):
        value = data.get(key)
        if key in data and not isinstance(value, dict):
            raise Refusal(400, "bad_request", f"{key} must be a JSON object when present")
        optional[key] = value

    return LaunchRequest(
        template=template,
        survey=survey,
        agent=agent,
        workflow_id=workflow_id,
        step=step,
        dry_run=dry_run,
        proposal=optional["proposal"],
        context=optional["context"],
    )


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise Refusal(400, "bad_request", f"{key} is missing or not a string")
    return value


def _survey(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise Refusal(400, "bad_request", "survey must be a JSON object")
    survey: dict[str, str] = {}
    for key, item in value.items():
        if not _SURVEY_NAME.fullmatch(key):
            raise Refusal(400, "bad_request", "a survey field name is not a valid identifier")
        if not isinstance(item, str):
            raise Refusal(400, "bad_request", f"survey field {key} must be a string")
        if len(item) > MAX_VALUE:
            raise Refusal(400, "bad_request", f"survey field {key} is longer than {MAX_VALUE} characters")
        if any(marker in item for marker in _TEMPLATE_MARKERS):
            raise Refusal(400, "bad_request", f"survey field {key} contains a template marker")
        survey[key] = item
    return survey


def authorize_local(caller: str, req: LaunchRequest, config: Config) -> None:
    """Check the caller may act as the request's agent, then that the template is allowlisted. Agent first."""
    if req.agent not in config.callers.get(caller, frozenset()):
        raise Refusal(403, "agent_not_permitted", f"caller {caller} may not act as agent {req.agent}")
    if req.template not in config.allowlist:
        raise Refusal(403, "template_not_allowed", f"template {req.template} is not on the launch allowlist")


def merge_survey(template: dict[str, Any], survey: dict[str, str]) -> dict[str, str]:
    """Check the request against the template's declared survey fields and return the values to launch.

    Semaphore fills declared defaults only in its web form, never for an API-created task, so the
    gate fills them here, as the form would. An explicit value always wins.
    """
    name = template.get("name")
    survey_vars = template.get("survey_vars")
    if survey_vars is None:
        survey_vars = []
    if not isinstance(survey_vars, list):
        raise Refusal(502, "semaphore_unreadable", f"survey fields of {name} are unreadable")
    declared: list[dict[str, Any]] = []
    for var in survey_vars:
        # A field of unknown shape is refused, not skipped: skipping could hide a default.
        if not isinstance(var, dict) or not isinstance(var.get("name"), str):
            raise Refusal(502, "semaphore_unreadable", f"a survey field of {name} is unreadable")
        declared.append(var)

    names = {var["name"] for var in declared}
    undeclared = sorted(set(survey) - names)
    if undeclared:
        raise Refusal(400, "undeclared_field", f"not survey fields of {name}: {', '.join(undeclared)}")

    merged: dict[str, str] = {}
    for var in declared:
        var_name, default = var["name"], var.get("default_value")
        if var_name in survey or default in (None, ""):
            continue
        if not isinstance(default, str):
            raise Refusal(502, "bad_default", f"survey field {var_name} of {name} has a non-string default")
        merged[var_name] = default
    values = {**merged, **survey}

    for var in declared:
        if var["name"] in values:
            _check_value(var, values[var["name"]], name)

    for var in declared:
        if var.get("required") is True and not values.get(var["name"]):
            raise Refusal(400, "missing_required", f"required survey field {var['name']} of {name} has no value")
    return values


_STRING_TYPES = frozenset({"", "string", "text"})
_INTEGER_TYPES = frozenset({"int", "integer"})


def _check_value(var: dict[str, Any], value: str, template: str) -> None:
    """Refuse a value its field's type cannot hold. A refusal names the field, never the value.

    Semaphore runs an enum or integer field as the literal it is given, so the gate checks the value
    against the declared type. An unknown type is refused rather than passed through unchecked.
    """
    var_name = var["name"]
    kind = var.get("type")
    if kind is None:
        kind = ""
    if not isinstance(kind, str):
        raise Refusal(502, "unsupported_field_type", f"survey field {var_name} of {template} has an unsupported type")
    if kind in _STRING_TYPES:
        return
    if kind in _INTEGER_TYPES:
        if not _INTEGER.fullmatch(value):
            raise Refusal(400, "bad_value", f"survey field {var_name} must be an integer")
        return
    if kind == "enum":
        options = var.get("values")
        if not isinstance(options, list) or not all(
            isinstance(option, dict) and isinstance(option.get("value"), str) for option in options
        ):
            raise Refusal(502, "semaphore_unreadable", f"survey field {var_name} of {template} has unreadable values")
        if value not in {option["value"] for option in options}:
            raise Refusal(400, "bad_value", f"survey field {var_name} is not one of its declared values")
        return
    raise Refusal(502, "unsupported_field_type", f"survey field {var_name} of {template} has an unsupported type")


def pin_branch(
    template: dict[str, Any], survey: dict[str, str], values: dict[str, str], repo_branch: str
) -> tuple[str, dict[str, str]]:
    """Pin the launch to the branch of the template's repository record, and return it with the values.

    OPA is asked about that branch, so the code that runs must be that branch. A template that carries
    its own branch is refused when the two differ, since the gate cannot tell which one a run would take.
    A template that declares ``service_branch`` clones that branch onto the target, so the gate sets the
    field to the record's branch (overriding any default) and refuses a caller who names another.
    """
    name = template.get("name")
    own = template.get("git_branch")
    if own is not None and not isinstance(own, str):
        raise Refusal(502, "semaphore_unreadable", f"the branch of template {name} is unreadable")
    if own and own != repo_branch:
        raise Refusal(
            502, "branch_ambiguous", f"template {name} carries its own branch, different from its repository record"
        )
    declared = {var.get("name") for var in template.get("survey_vars") or [] if isinstance(var, dict)}
    if "service_branch" not in declared:
        return repo_branch, values
    if "service_branch" in survey and survey["service_branch"] != repo_branch:
        raise Refusal(400, "branch_mismatch", f"service_branch must be the template's branch {repo_branch}")
    return repo_branch, {**values, "service_branch": repo_branch}


def opa_input(req: LaunchRequest, git_branch: str, values: dict[str, str]) -> dict[str, Any]:
    """The OPA input for one launch, built from scratch. Only proposal and context come from the request."""
    body: dict[str, Any] = {
        "agent": req.agent,
        "service": "semaphore",
        "action": "run_task",
        "template_name": req.template,
        "human_approved": False,
        "step": req.step,
        "git_branch": git_branch,
        "settings": dict(values),
    }
    if req.proposal is not None:
        body["proposal"] = req.proposal
    if req.context is not None:
        body["context"] = req.context
    return body


def task_message(caller: str, workflow_id: str, step: str) -> str:
    """The Semaphore task message. Skynet's tasks keep the ``skynet <workflow_id> <step_id>`` form."""
    return f"{caller} {workflow_id} {step}"


def task_body(
    template_id: int,
    project: int,
    values: dict[str, str],
    *,
    dry_run: bool,
    message: str,
) -> dict[str, Any]:
    """The exact POST body for a launch. Check mode lives in ``params``, never at the top."""
    # No git_branch in this body, on purpose. agent_actions.rego reads a missing git_branch as
    # main (object.get(input, "git_branch", "main")), so a branch set here would run code OPA never
    # assessed as main. A task-level branch also overrides the template's own branch
    # (agent-cloud docs/MISTAKES.md 1.9). Nothing else is set either: no arguments, limit,
    # inventory_id, playbook, environment_id or tags, so the run takes only the validated values.
    body: dict[str, Any] = {
        "project_id": project,
        "template_id": template_id,
        "message": message,
    }
    if values:
        body["environment"] = json.dumps(values, sort_keys=True)
    if dry_run:
        body["params"] = {"dry_run": True, "diff": True}
    return body


# ---------- clients and request flow (I/O; no rule of its own) ----------

MAX_RESPONSE = 16 * 1024 * 1024
SEMAPHORE_TIMEOUT = 15.0
OPA_TIMEOUT = 5.0
DECISION_PATH = "/v1/data/agentcloud/decision"
UNCERTAIN = "submission outcome uncertain; a task may be running, check the template's tasks before launching again"
Log = Callable[[dict[str, Any]], None]


class Denied(Refusal):
    """OPA answered ``allowed: false``. ``reason`` is OPA's own text, returned to the caller."""

    def __init__(self, reason: str) -> None:
        super().__init__(403, "opa_denied", "OPA denied the launch")
        self.reason = reason


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect. urllib would otherwise replay the Authorization header at the new location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Failed(Exception):
    """A transport, HTTP or read failure. Its text is a label (a class name or a status), never a body."""


def _opener(context: ssl.SSLContext) -> urllib.request.OpenerDirector:
    # ProxyHandler({}): an environment proxy must never see the bearer token or the launch body.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirect(),
    )


def _fetch(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    timeout: float,
    *,
    token: str | None = None,
    payload: Any = None,
) -> bytes:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = _BEARER + token
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise _Failed(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        raise _Failed(type(exc).__name__) from None
    if len(raw) > MAX_RESPONSE:
        raise _Failed("response larger than 16 MiB")
    return raw


def _parse(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise _Failed("response was not JSON") from None


def _is_id(value: object) -> bool:
    # bool is an int subclass; a JSON true must never pass as an id.
    return isinstance(value, int) and not isinstance(value, bool)


class SemaphoreClient:
    """The Semaphore calls one launch needs, all in project ``project``. Every failure raises Refusal."""

    def __init__(
        self,
        base_url: str,
        project: int,
        token: str,
        *,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float = SEMAPHORE_TIMEOUT,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._prefix = f"/api/project/{project}"
        self._token = token
        self._timeout = timeout
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        self._opener = _opener(context)

    def _call(self, method: str, path: str, payload: Any = None, *, parse: bool = True) -> Any:
        try:
            raw = _fetch(self._opener, method, self._base + path, self._timeout, token=self._token, payload=payload)
            return _parse(raw) if parse else None
        except _Failed as exc:
            raise Refusal(502, "semaphore_unreadable", f"Semaphore {method} {path} failed ({exc})") from None

    def template(self, name: str) -> dict[str, Any]:
        """The live template whose name is exactly ``name``, read back by id. Never guesses between matches."""
        listing = self._call("GET", f"{self._prefix}/templates")
        if not isinstance(listing, list):
            raise Refusal(502, "semaphore_unreadable", "Semaphore template listing is not a list")
        matches = [t for t in listing if isinstance(t, dict) and t.get("name") == name]
        if not matches:
            raise Refusal(502, "template_not_found", f"no Semaphore template is named {name}")
        if len(matches) > 1:
            raise Refusal(502, "template_ambiguous", f"{len(matches)} Semaphore templates are named {name}")
        template_id = matches[0].get("id")
        if not _is_id(template_id):
            raise Refusal(502, "semaphore_unreadable", f"template {name} has no readable id")
        detail = self._call("GET", f"{self._prefix}/templates/{template_id}")
        if not isinstance(detail, dict) or not _is_id(detail.get("id")):
            raise Refusal(502, "semaphore_unreadable", f"template {name} detail is unreadable")
        return detail

    def branch(self, repository_id: object) -> str:
        """The Git branch of the template's repository record. Read from Semaphore, never from the caller."""
        unreadable = Refusal(502, "semaphore_unreadable", "the template's repository record is unreadable")
        if not _is_id(repository_id):
            raise unreadable
        listing = self._call("GET", f"{self._prefix}/repositories")
        if not isinstance(listing, list):
            raise unreadable
        for record in listing:
            if isinstance(record, dict) and _is_id(record.get("id")) and record["id"] == repository_id:
                git_branch = record.get("git_branch")
                if isinstance(git_branch, str) and git_branch:
                    return git_branch
                raise unreadable
        raise unreadable

    def version(self) -> str:
        info = self._call("GET", "/api/info")
        if not isinstance(info, dict):
            return "unknown"
        return str(info.get("version", "unknown"))

    def running(self, template_id: int) -> list[Any]:
        """Ids of the template's tasks that are not terminal. A task with no readable status counts as running."""
        recent = self._call("GET", f"{self._prefix}/tasks/last")
        if not isinstance(recent, list):
            raise Refusal(502, "semaphore_unreadable", "Semaphore task history is not a list")
        busy: list[Any] = []
        for task in recent:
            if not isinstance(task, dict) or task.get("template_id") != template_id:
                continue
            status = task.get("status")
            if not (isinstance(status, str) and status in TERMINAL):
                busy.append(task.get("id"))
        return busy

    def create(self, body: dict[str, Any]) -> int:
        """POST one task. Any failure is an uncertain outcome: the server may have accepted it. Never retried."""
        try:
            task = self._call("POST", f"{self._prefix}/tasks", body)
        except Refusal:
            raise Refusal(502, "outcome_uncertain", UNCERTAIN) from None
        task_id = task.get("id") if isinstance(task, dict) else None
        if not _is_id(task_id):
            raise Refusal(502, "outcome_uncertain", UNCERTAIN)
        return task_id

    def task(self, task_id: int) -> dict[str, Any]:
        detail = self._call("GET", f"{self._prefix}/tasks/{task_id}")
        if not isinstance(detail, dict):
            raise Refusal(502, "semaphore_unreadable", f"task {task_id} is unreadable")
        return detail

    def stop(self, task_id: int) -> None:
        self._call("POST", f"{self._prefix}/tasks/{task_id}/stop", {}, parse=False)


class OPAClient:
    """OPA's decision endpoint. The gate launches only on an answer of exactly ``allowed: true``."""

    def __init__(self, base_url: str, *, ca_bundle: str | None = None, timeout: float = OPA_TIMEOUT) -> None:
        context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
        self._url = base_url.rstrip("/") + DECISION_PATH
        self._timeout = timeout
        self._opener = _opener(context)

    def decide(self, decision_input: dict[str, Any]) -> str:
        """Return OPA's reason when allowed. Raises Denied on allowed false, Refusal 503 on anything else."""
        try:
            raw = _fetch(self._opener, "POST", self._url, self._timeout, payload={"input": decision_input})
            body = _parse(raw)
        except _Failed:
            raise _opa_unavailable() from None
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise _opa_unavailable()
        allowed = result.get("allowed")
        if allowed is True:
            return str(result.get("reason", ""))
        if allowed is False:
            raise Denied(str(result.get("reason", "")))
        raise _opa_unavailable()


def _opa_unavailable() -> Refusal:
    return Refusal(503, "opa_unavailable", "OPA decision unavailable")


def _authorization(headers: Mapping[str, str]) -> str | None:
    # Header names are case-insensitive. Two Authorization headers are ambiguous, so neither is trusted.
    values = [value for name, value in headers.items() if name.lower() == "authorization"]
    return values[0] if len(values) == 1 else None


def _stdout_log(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, sort_keys=True) + "\n")
    sys.stdout.flush()


def _stderr(line: str) -> None:
    # Fixed text only: an exception message can carry a survey value, a token or a path.
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass


class Gate:
    """The request flow, in design decision 4's order. Each check is a core function; this class sequences them."""

    def __init__(self, config: Config, semaphore: SemaphoreClient, opa: OPAClient, *, log: Log | None = None) -> None:
        self._config = config
        self._semaphore = semaphore
        self._opa = opa
        self._log = log if log is not None else _stdout_log
        # The dedup read and the create run under one lock. Without it, two requests for one template can both
        # read it idle and both create. The gate is the only launcher, so a process lock closes that window.
        self._launch_lock = threading.Lock()

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/healthz":
            return 200, {"status": "ok"}
        if path != "/v1/launch":
            return 404, {"error": "not_found"}
        if method != "POST":
            return 405, {"error": "method_not_allowed"}

        event: dict[str, Any] = {"event": "launch"}
        try:
            status, payload = self._launch(headers, body, event)
        except Refusal as exc:
            status, payload = exc.status, {"error": exc.code, "detail": exc.detail}
            if isinstance(exc, Denied):
                payload["reason"] = exc.reason
                event["reason"] = exc.reason
        except Exception as exc:
            # The class name only: an exception message can carry a survey value or a token.
            status, payload = 500, {"error": "internal"}
            event["exception"] = type(exc).__name__
        event["outcome"] = status
        event["error"] = None if status == 201 else payload["error"]
        try:
            self._log(event)
        except Exception:
            # A task may already exist, and its id must still reach the caller, so a failed log write
            # never replaces the response.
            _stderr("launch-gate: log write failed")
        return status, payload

    def _launch(self, headers: Mapping[str, str], body: bytes, event: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        caller = authenticate(_authorization(headers), self._config)
        event.update(caller=caller, reason=None, task_id=None)
        req = parse_request(body)
        event.update(
            agent=req.agent,
            template=req.template,
            step=req.step,
            workflow_id=req.workflow_id,
            dry_run=req.dry_run,
            survey_keys=sorted(req.survey),
        )
        authorize_local(caller, req, self._config)
        template = self._semaphore.template(req.template)
        values = merge_survey(template, req.survey)
        repo_branch = self._semaphore.branch(template.get("repository_id"))
        branch, values = pin_branch(template, req.survey, values, repo_branch)
        if req.dry_run:
            version = self._semaphore.version()
            if version.split("-")[0] not in VERIFIED_DRY_RUN_VERSIONS:
                raise Refusal(400, "dry_run_unverified", f"check mode is not verified for Semaphore {version}")
        reason = self._opa.decide(opa_input(req, branch, values))
        event["reason"] = reason
        template_id = template["id"]
        message = task_message(caller, req.workflow_id, req.step)
        body = task_body(template_id, self._config.semaphore_project, values, dry_run=req.dry_run, message=message)
        with self._launch_lock:
            busy = self._semaphore.running(template_id)
            if busy:
                ids = ", ".join(str(task) for task in busy)
                raise Refusal(409, "already_running", f"template {req.template} already has a running task: {ids}")
            task_id = self._semaphore.create(body)
        event["task_id"] = task_id
        if req.dry_run:
            self._check_mode_recorded(task_id)
        return 201, {
            "task_id": task_id,
            "message": message,
            "decision": {"allowed": True, "reason": reason},
        }

    def _check_mode_recorded(self, task_id: int) -> None:
        """Read the task back. Anything short of a positive params.dry_run is a real run: stop it and refuse."""
        try:
            task = self._semaphore.task(task_id)
        except Refusal:
            task = {}
        params = task.get("params")
        if isinstance(params, dict) and params.get("dry_run") is True:
            return
        try:
            self._semaphore.stop(task_id)
            note = "stop requested"
        except Refusal:
            note = "stop request also failed"
        raise Refusal(
            502,
            "dry_run_not_recorded",
            f"task {task_id} launched but its check mode was not recorded ({note}); "
            "treat it as a real run until inspected",
        )


# ---------- HTTPS listener and entry point (transport only; no rule of its own) ----------

DEFAULT_CONFIG = "/etc/launch-gate/config.json"
# Bounds the TLS handshake and every read or write on a connection, so a silent client ends.
CONNECTION_TIMEOUT = 10.0
# Connections served at once. Each holds a thread until its handshake and request end, so silent clients
# could otherwise exhaust threads. A connection past the cap is dropped before any thread starts.
MAX_CONNECTIONS = 16
# Wall-clock budget for one request's body, from the end of the handshake. CONNECTION_TIMEOUT bounds each
# read, not the request, so a client that trickles bytes could otherwise hold the connection open.
REQUEST_DEADLINE = 30.0
_READ_CHUNK = 64 * 1024


class TLSLoadError(Exception):
    """The certificate or key cannot be loaded. ``str()`` is the failure's class name, never a path."""


def _fingerprint(cert_path: str, key_path: str) -> tuple[tuple[str, int, int, int], ...]:
    """Identity of the two files in use. Each path is resolved on every call: renewal swaps the ``current``
    symlink to a new serial directory, and the stat has to describe the file that is in use now."""
    parts = []
    for path in (cert_path, key_path):
        resolved = os.path.realpath(path)
        status = os.stat(resolved)
        parts.append((resolved, status.st_ino, status.st_mtime_ns, status.st_size))
    return tuple(parts)


def _server_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    return context


class GateServer(ThreadingHTTPServer):
    """The HTTPS listener. Each connection gets a thread, its TLS handshake runs on that thread, and the
    certificate files are checked on every connection, so a renewal is served without a restart."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], gate: Gate, cert_path: str, key_path: str) -> None:
        # The context loads before the socket is bound, so a bad certificate never holds the port.
        self._cert_path = cert_path
        self._key_path = key_path
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        try:
            self._fingerprint = _fingerprint(cert_path, key_path)
            self._ssl_context = _server_context(cert_path, key_path)
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise TLSLoadError(type(exc).__name__) from exc
        self.gate = gate
        super().__init__(address, GateHandler)

    def get_request(self) -> tuple[ssl.SSLSocket, Any]:
        sock, addr = self.socket.accept()
        try:
            # The handshake is NOT done here. This is the accept loop; one silent client must not hold it.
            tls = self._current_context().wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        except Exception:
            sock.close()
            raise
        return tls, addr

    def process_request(self, request: Any, client_address: Any) -> None:
        # Refused before a thread exists. The client gets no reply and the gate writes no line for it.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def _current_context(self) -> ssl.SSLContext:
        """The context for the files as they stand now. A failed reload keeps the last good context serving
        and leaves the fingerprint as it was, so the next connection tries again."""
        with self._lock:
            try:
                fingerprint = _fingerprint(self._cert_path, self._key_path)
                if fingerprint != self._fingerprint:
                    self._ssl_context = _server_context(self._cert_path, self._key_path)
                    self._fingerprint = fingerprint
            except Exception as exc:
                self._reload_failed(exc)
            return self._ssl_context

    def _reload_failed(self, exc: Exception) -> None:
        # The class name only, as the gate's own events carry: a message can name a path.
        event = {"event": "tls_reload_failed", "error": type(exc).__name__}
        log = getattr(self.gate, "_log", None)
        if callable(log):
            try:
                log(event)
                return
            except Exception:
                pass
        _stderr(f"launch-gate: TLS certificate reload failed ({type(exc).__name__})")

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints a traceback with the client's address. A reset connection is ordinary traffic.
        _stderr(f"launch-gate: connection error ({type(sys.exc_info()[1]).__name__})")


def _content_length(values: list[str] | None) -> int | None:
    """The declared body length, or None when the header is missing, repeated, or not plain ASCII digits."""
    if not values or len(values) != 1:
        return None
    value = values[0]
    # int() would also take "+5", "1_0" and Unicode digits; a length must be plain digits.
    if not (value.isascii() and value.isdigit()):
        return None
    return int(value)


def _headers(message: Any) -> dict[str, str]:
    pairs = message.items()
    if sum(1 for name, _ in pairs if name.lower() == "authorization") > 1:
        # Two Authorization headers are ambiguous. The gate refuses them, so neither is passed on.
        return {name: value for name, value in pairs if name.lower() != "authorization"}
    return dict(pairs)


class GateHandler(BaseHTTPRequestHandler):
    """Frames HTTP for the gate. It reads a body only where the gate takes one, and it answers nothing to a
    connection whose TLS handshake failed."""

    server_version = "launch-gate"
    sys_version = ""
    _tls_failed = False
    _deadline = 0.0

    def setup(self) -> None:
        self.request.settimeout(CONNECTION_TIMEOUT)
        try:
            self.request.do_handshake()
        except (ssl.SSLError, OSError):
            # Plain HTTP, a client that stalls, or one that closes early: no reply and no request read.
            self._tls_failed = True
            return
        self._deadline = time.monotonic() + REQUEST_DEADLINE
        super().setup()

    def handle(self) -> None:
        if not self._tls_failed:
            super().handle()

    def finish(self) -> None:
        # setup() returned before the stream files existed, so the base finish() must not run.
        if not self._tls_failed:
            super().finish()

    def log_message(self, format: str, *args: Any) -> None:
        """The gate writes its own events. The default access log writes every client address to stderr."""

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        body = b""
        if method == "POST":
            length = _content_length(self.headers.get_all("Content-Length"))
            if length is None:
                self._reply(400, {"error": "bad_request", "detail": "Content-Length required"})
                return
            if length > MAX_BODY:
                # Refused unread: an oversize body is never buffered, and the gate never sees it.
                self._reply(413, {"error": "too_large"})
                return
            body = self._read_body(length)
            if body is None:
                self._reply(408, {"error": "timeout"})
                return
        path = self.path.split("?", 1)[0]
        status, payload = self.server.gate.handle(method, path, _headers(self.headers), body)
        self._reply(status, payload)

    def _read_body(self, length: int) -> bytes | None:
        """Read the declared body, or return None once the request's deadline has passed. Each read waits no
        longer than what is left of the deadline, and the deadline is checked between reads, so a client that
        trickles bytes cannot hold the connection past it. A short body at EOF is returned as it is, as before."""
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining > 0:
                left = self._deadline - time.monotonic()
                if left <= 0:
                    return None
                budget = min(CONNECTION_TIMEOUT, left)
                self.request.settimeout(budget)
                try:
                    chunk = self.rfile.read1(min(remaining, _READ_CHUNK))
                except TimeoutError:
                    if left > CONNECTION_TIMEOUT:
                        # A stalled client, bounded by CONNECTION_TIMEOUT as before: the connection just ends.
                        raise
                    # The read ran out the request's deadline, which was the smaller bound: answered 408.
                    return None
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            # The reply that follows writes on this socket, so the handshake-time bound is restored.
            self.request.settimeout(CONNECTION_TIMEOUT)
        return b"".join(chunks)

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True


def main(argv: list[str] | None = None) -> int:
    """Entry point: ``main [config.json]``. Returns the exit status; 2 means the gate did not start."""
    args = argv or []
    path = args[0] if args else os.environ.get("LAUNCH_GATE_CONFIG", DEFAULT_CONFIG)
    try:
        config = load_config(path, os.environ)
    except ConfigError as exc:
        _stderr(f"launch-gate: {exc}")
        return 2
    try:
        opa = OPAClient(config.opa_url, ca_bundle=config.opa_ca_bundle)
    except (OSError, ssl.SSLError) as exc:
        _stderr(f"launch-gate: OPA CA bundle unreadable ({type(exc).__name__})")
        return 2
    semaphore = SemaphoreClient(config.semaphore_url, config.semaphore_project, config.semaphore_token)
    gate = Gate(config, semaphore, opa)
    try:
        server = GateServer((config.listen_host, config.listen_port), gate, config.tls_cert, config.tls_key)
    except TLSLoadError as exc:
        _stderr(f"launch-gate: TLS certificate or key unreadable ({exc})")
        return 2
    except OSError as exc:
        _stderr(f"launch-gate: cannot listen on {config.listen_host}:{config.listen_port} ({type(exc).__name__})")
        return 2
    with server:
        _stdout_log({"event": "listening", "host": config.listen_host, "port": config.listen_port})
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
