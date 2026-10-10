"""Keep controller OpenBao credentials out of module arguments and request results."""

import ipaddress
import json
import os
import signal
import threading
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ansible.plugins.action import ActionBase

MAX_RESPONSE = 64 * 1024
REQUEST_TIMEOUT = 10


class _Refusal(Exception):
    """A fixed safe failure reason."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _Refusal("redirect_refused")


@contextmanager
def _deadline(seconds):
    if threading.current_thread() is not threading.main_thread():
        raise _Refusal("request_unavailable")
    try:
        old_timer = signal.getitimer(signal.ITIMER_REAL)
        old_handler = signal.getsignal(signal.SIGALRM)
    except (AttributeError, OSError, ValueError):
        raise _Refusal("request_unavailable") from None
    if old_timer != (0.0, 0.0):
        raise _Refusal("request_unavailable")

    def expire(_signum, _frame):
        raise _Refusal("timeout")

    installed = False
    try:
        signal.signal(signal.SIGALRM, expire)
        installed = True
        signal.setitimer(signal.ITIMER_REAL, seconds)
    except (OSError, ValueError, RuntimeError):
        if installed:
            signal.signal(signal.SIGALRM, old_handler)
        raise _Refusal("request_unavailable") from None
    try:
        yield
    finally:
        if installed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)


def _origin(value, local_mode=False):
    parts = urlsplit(value)
    if (parts.scheme not in {"https", "http"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.path not in ("", "/")):
        raise _Refusal("invalid_endpoint")
    host = parts.hostname.lower()
    if parts.scheme == "http":
        try:
            address = ipaddress.ip_address(host)
            if not (address.is_loopback or address in ipaddress.ip_network("10.0.0.0/8")
                    or address in ipaddress.ip_network("192.168.0.0/16")
                    or address in ipaddress.ip_network("172.16.0.0/12")):
                raise _Refusal("insecure_endpoint")
        except ValueError:
            if host != "localhost" and not (local_mode and
                    ("." not in host or host == "host.containers.internal")):
                raise _Refusal("insecure_endpoint") from None
    return value.rstrip("/")


def _request(url, method, body=None, token=None):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    if token is not None:
        headers["X-Vault-Token"] = token
    request = Request(url, data=data, headers=headers, method=method)
    client = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with _deadline(REQUEST_TIMEOUT), client.open(request, timeout=5) as response:
            if response.status != 200:
                raise _Refusal("request_failed")
            payload = response.read(MAX_RESPONSE + 1)
    except _Refusal:
        raise
    except HTTPError as error:
        if 300 <= error.code < 400:
            raise _Refusal("redirect_refused") from None
        raise _Refusal("request_failed") from None
    except TimeoutError:
        raise _Refusal("timeout") from None
    except (URLError, OSError):
        raise _Refusal("transport_failed") from None
    if len(payload) > MAX_RESPONSE:
        raise _Refusal("response_too_large")
    try:
        result = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _Refusal("malformed_response") from None
    if not isinstance(result, dict):
        raise _Refusal("malformed_response")
    return result


def _secret(task_vars, key, env_key):
    value = task_vars[key] if key in task_vars else os.environ.get(env_key, "")
    if not isinstance(value, str) or not value:
        raise _Refusal("credentials_unavailable")
    return value


class ActionModule(ActionBase):
    TRANSFERS_FILES = False

    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        task_vars = task_vars or {}
        operation = self._task.args.get("operation")
        if operation not in {"login", "read"}:
            return {**result, "failed": True, "msg": "invalid_operation"}
        try:
            origin = task_vars.get("_runtime_bao_url") or task_vars.get("openbao_addr")
            origin = origin or os.environ.get("OPENBAO_ADDR", "")
            origin = _origin(origin, bool(task_vars.get("local_mode", False)))
            if operation == "login":
                role_id = _secret(task_vars, "bao_role_id", "BAO_ROLE_ID")
                secret_id = _secret(task_vars, "bao_secret_id", "BAO_SECRET_ID")
                data = _request(
                    origin + "/v1/auth/approle/login", "POST",
                    body={"role_id": role_id, "secret_id": secret_id},
                )
                token = data.get("auth", {}).get("client_token")
                if not isinstance(token, str) or not token:
                    raise _Refusal("login_response_invalid")
                return {**result, "changed": False, "json": {"auth": {"client_token": token}}}

            login = task_vars.get("_runtime_login")
            token = login.get("json", {}).get("auth", {}).get("client_token") if isinstance(login, dict) else None
            if not isinstance(token, str) or not token:
                raise _Refusal("runtime_token_unavailable")
            data = _request(
                origin + "/v1/secret/data/services/semaphore", "GET", token=token,
            )
            secret = data.get("data", {}).get("data", {}).get("api_token", "")
            if not isinstance(secret, str):
                raise _Refusal("secret_response_invalid")
            return {**result, "changed": False, "json": {"data": {"data": {"api_token": secret}}}}
        except _Refusal as error:
            return {**result, "failed": True, "msg": str(error)}
        except Exception:
            return {**result, "failed": True, "msg": "request_unavailable"}
