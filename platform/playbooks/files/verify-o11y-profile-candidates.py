#!/usr/bin/env python3
"""Validate rendered o11y profile candidates in bounded, isolated Alloy containers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import uuid

IMAGE = "docker.io/grafana/alloy:v1.5.1"
SCRAPE_ENV_LIMITS = {
    "O11Y_SCRAPE_SAMPLE_LIMIT": 100_000,
    "O11Y_SCRAPE_BODY_SIZE_LIMIT_BYTES": 167_772_160,
    "O11Y_SCRAPE_LABEL_LIMIT": 640,
    "O11Y_SCRAPE_LABEL_NAME_LENGTH_LIMIT": 1_280,
    "O11Y_SCRAPE_LABEL_VALUE_LENGTH_LIMIT": 20_480,
}
BASELINE_COMPONENT = "loki.write.default"
PROFILE_COMPONENT = "pyroscope.scrape.alloy_self_profile"
API_READ_LOOP = 'while IFS= read -r line || [[ -n $line ]]; do printf \'%s\\n\' "$line"; done'
IMAGE_TIMEOUT = 8
STARTUP_SECONDS = 5
READY_SECONDS = 30
READY_RETRY_SECONDS = 0.5
API_SECONDS = 5
MAX_API_BYTES = 262_144
MAX_CONFIG_BYTES = 1_048_576
REFUSALS = {
    "candidate_invalid",
    "image_unavailable",
    "image_unpinned",
    "container_start_failed",
    "component_status_unavailable",
    "component_status_invalid",
    "component_unhealthy",
    "image_mismatch",
    "cleanup_failed",
}
PROFILE_TYPES = {
    "process_cpu": True,
    "memory": False,
    "block": False,
    "goroutine": False,
    "mutex": False,
    "fgprof": False,
    "godeltaprof_memory": False,
    "godeltaprof_mutex": False,
    "godeltaprof_block": False,
}


class Refusal(Exception):
    """A fixed category that never contains candidate or runtime data."""

    def __init__(self, category: str) -> None:
        if category not in REFUSALS:
            category = "candidate_invalid"
        super().__init__(category)
        self.category = category


def _profile_blocks(config: str) -> dict[str, str]:
    """Extract profile blocks with brace matching that respects comments and strings."""
    declarations = list(re.finditer(r'(?m)^\s*(pyroscope\.(?:scrape|write))\s+"([A-Za-z0-9_]+)"\s*\{', config))
    blocks: dict[str, str] = {}
    for match in declarations:
        key = f"{match.group(1)}.{match.group(2)}"
        if key in blocks:
            raise Refusal("candidate_invalid")
        depth = 1
        index = match.end()
        quoted = False
        escaped = False
        line_comment = False
        block_comment = False
        while index < len(config) and depth:
            char = config[index]
            next_char = config[index + 1] if index + 1 < len(config) else ""
            if line_comment:
                if char == "\n":
                    line_comment = False
            elif block_comment:
                if char == "*" and next_char == "/":
                    block_comment = False
                    index += 1
            elif quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == "/" and next_char == "/":
                line_comment = True
                index += 1
            elif char == "/" and next_char == "*":
                block_comment = True
                index += 1
            elif char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        if depth or quoted or block_comment:
            raise Refusal("candidate_invalid")
        blocks[key] = config[match.start():index]
    return blocks


def validate_candidates(disabled: str, enabled: str, profile_address: str = "alloy:12345") -> dict[str, object]:
    """Check the two rendered configurations contain exactly the approved pilot."""
    if (not isinstance(disabled, str) or not isinstance(enabled, str)
            or profile_address not in {"alloy:12345", "127.0.0.1:12345"}):
        raise Refusal("candidate_invalid")
    if max(len(disabled.encode("utf-8")), len(enabled.encode("utf-8"))) > MAX_CONFIG_BYTES:
        raise Refusal("candidate_invalid")
    if not re.search(r'(?m)^\s*loki\.write\s+"default"\s*\{', disabled):
        raise Refusal("candidate_invalid")
    if _profile_blocks(disabled):
        raise Refusal("candidate_invalid")
    blocks = _profile_blocks(enabled)
    if set(blocks) != {"pyroscope.write.private", "pyroscope.scrape.alloy_self_profile"}:
        raise Refusal("candidate_invalid")
    remainder = enabled
    for block in blocks.values():
        remainder = remainder.replace(block, "", 1)
    if remainder.rstrip() != disabled.rstrip():
        raise Refusal("candidate_invalid")
    scrape = blocks["pyroscope.scrape.alloy_self_profile"]
    writer = blocks["pyroscope.write.private"]
    required_scrape = (
        'job_name = "o11y/alloy"',
        f'"__address__" = "{profile_address}"',
        '"service_name" = "o11y/alloy"',
        '"target_id" = "service:o11y"',
        'scrape_interval = "60s"',
        'scrape_timeout = "10s"',
        'delta_profiling_duration = "5s"',
        "follow_redirects = false",
        "proxy_from_environment = false",
        "forward_to = [pyroscope.write.private.receiver]",
    )
    if any(scrape.count(value) != 1 for value in required_scrape):
        raise Refusal("candidate_invalid")
    if scrape.count('"__address__"') != 1 or scrape.count("profile.") != len(PROFILE_TYPES):
        raise Refusal("candidate_invalid")
    if 'url = "http://pyroscope:4040"' not in writer or writer.count("url =") != 1:
        raise Refusal("candidate_invalid")
    for profile_type, enabled_value in PROFILE_TYPES.items():
        pattern = rf"profile\.{profile_type}\s*\{{\s*enabled\s*=\s*{str(enabled_value).lower()}\s*\}}"
        if len(re.findall(pattern, scrape)) != 1:
            raise Refusal("candidate_invalid")
    return {
        "target_count": 1,
        "profile_type_count": len(PROFILE_TYPES),
        "profile_types": list(PROFILE_TYPES),
        "profile_address": profile_address,
    }


def _bounded_command(argv: list[str], *, timeout: float, max_bytes: int) -> tuple[int, bytes]:
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        output = bytearray()
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Refusal("component_status_unavailable")
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, min(65_536, max_bytes + 1 - len(output)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > max_bytes:
                    raise Refusal("component_status_invalid")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal("component_status_unavailable")
        try:
            return process.wait(timeout=remaining), bytes(output)
        except subprocess.TimeoutExpired as exc:
            raise Refusal("component_status_unavailable") from exc
    except OSError as exc:
        raise Refusal("component_status_unavailable") from exc
    finally:
        selector.close()
        if process is not None:
            try:
                _stop_process(process)
            finally:
                if process.stdout is not None:
                    process.stdout.close()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    """Stop a child process group and refuse if it cannot be reaped promptly."""
    try:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    process.kill()
                process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("cleanup_failed") from exc
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError as exc:
                raise Refusal("cleanup_failed") from exc


def _transfer_config(process: subprocess.Popen[bytes], config: str) -> float:
    """Write all config bytes without blocking, bounded by the startup deadline."""
    assert process.stdin is not None
    deadline = time.monotonic() + STARTUP_SECONDS
    payload = memoryview(config.encode("utf-8"))
    descriptor = process.stdin.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdin, selectors.EVENT_WRITE)
        written = 0
        while written < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or process.poll() is not None:
                raise Refusal("container_start_failed")
            if not selector.select(remaining):
                raise Refusal("container_start_failed")
            try:
                count = os.write(descriptor, payload[written:])
            except BlockingIOError:
                continue
            if count <= 0:
                raise Refusal("container_start_failed")
            written += count
        if process.poll() is not None or time.monotonic() >= deadline:
            raise Refusal("container_start_failed")
        process.stdin.close()
        return deadline
    except OSError as exc:
        raise Refusal("container_start_failed") from exc
    finally:
        selector.close()


def _wait_for_startup(process: subprocess.Popen[bytes], deadline: float) -> None:
    while True:
        if process.poll() is not None:
            raise Refusal("container_start_failed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def _image_identity() -> tuple[str, str]:
    try:
        return_code, output = _bounded_command(
            ["podman", "image", "inspect", IMAGE, "--format", "{{.Id}} {{json .RepoDigests}}"],
            timeout=IMAGE_TIMEOUT,
            max_bytes=4096,
        )
    except Refusal as exc:
        raise Refusal("image_unavailable") from exc
    if return_code != 0:
        raise Refusal("image_unavailable")
    try:
        inspected = output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refusal("image_unpinned") from exc
    match = re.fullmatch(r"((?:sha256:)?[0-9a-f]{64})\s+(\[.*\])\s*", inspected)
    if not match:
        raise Refusal("image_unpinned")
    try:
        digests = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        raise Refusal("image_unpinned") from exc
    repo_digest = next((
        value for value in digests if isinstance(value, str)
        and re.fullmatch(r"docker\.io/grafana/alloy@sha256:[0-9a-f]{64}", value)
    ), None) if isinstance(digests, list) else None
    if repo_digest is None:
        raise Refusal("image_unpinned")
    return f"sha256:{match.group(1).removeprefix('sha256:')}", repo_digest


def _live_receiver_image_identity() -> str:
    try:
        return_code, output = _bounded_command(
            ["podman", "container", "inspect", "o11y-alloy", "--format", "{{.Image}} {{.State.Status}}"],
            timeout=IMAGE_TIMEOUT,
            max_bytes=512,
        )
    except Refusal as exc:
        raise Refusal("image_unavailable") from exc
    if return_code != 0:
        raise Refusal("image_unavailable")
    try:
        image_id, state = output.decode("ascii").strip().split()
    except UnicodeDecodeError as exc:
        raise Refusal("image_unavailable") from exc
    except ValueError as exc:
        raise Refusal("image_unavailable") from exc
    if state != "running" or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", image_id):
        raise Refusal("image_unavailable")
    return f"sha256:{image_id.removeprefix('sha256:')}"


def _receiver_scrape_environment() -> dict[str, str]:
    try:
        return_code, output = _bounded_command(
            ["podman", "container", "inspect", "o11y-alloy", "--format", "{{json .Config.Env}}"],
            timeout=IMAGE_TIMEOUT,
            max_bytes=16_384,
        )
    except Refusal as exc:
        raise Refusal("image_unavailable") from exc
    if return_code != 0:
        raise Refusal("image_unavailable")
    try:
        entries = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal("candidate_invalid") from exc
    if not isinstance(entries, list) or len(entries) > 512 or any(not isinstance(entry, str) for entry in entries):
        raise Refusal("candidate_invalid")
    values = {}
    for entry in entries:
        key, separator, value = entry.partition("=")
        if key in SCRAPE_ENV_LIMITS:
            if not separator or key in values or not re.fullmatch(r"[1-9][0-9]*", value):
                raise Refusal("candidate_invalid")
            number = int(value)
            if number > SCRAPE_ENV_LIMITS[key]:
                raise Refusal("candidate_invalid")
            values[key] = value
    if set(values) != set(SCRAPE_ENV_LIMITS):
        raise Refusal("candidate_invalid")
    return values


def _cleanup_container(container: str) -> None:
    """Remove only the named pilot container and prove that exact name is absent."""
    try:
        subprocess.run(
            ["podman", "rm", "--force", container], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=API_SECONDS, check=False,
        )
        exists = subprocess.run(
            ["podman", "container", "exists", container], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=API_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("cleanup_failed") from exc
    # Podman documents 1 as absent; 0 means present and any other code is uncertain.
    if exists.returncode != 1:
        raise Refusal("cleanup_failed")


def _candidate_command(container: str, image_ref: str, scrape_env: dict[str, str]) -> list[str]:
    if set(scrape_env) != set(SCRAPE_ENV_LIMITS):
        raise Refusal("candidate_invalid")
    env_args = [arg for key, value in scrape_env.items() for arg in ("--env", f"{key}={value}")]
    return [
        "podman", "run", "--rm", "--interactive", "--pull=never", "--log-driver=none", "--name", container,
        "--network=none", "--add-host=alloy:127.0.0.1", "--read-only", "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=32m,mode=0700",
        "--pids-limit=64", "--memory=256m", "--cpus=1", "--cap-drop=all", "--security-opt=no-new-privileges",
        *env_args,
        "--user=0:0", image_ref, "run", "--disable-reporting", "--server.http.disable-support-bundle",
        "--storage.path=/tmp/alloy", "--server.http.listen-addr=127.0.0.1:12345", "/dev/stdin",
    ]


def _component_states(config: str, image_ref: str, scrape_env: dict[str, str], *, disabled: bool) -> str:
    container = f"o11y-profile-candidate-{uuid.uuid4().hex[:12]}"
    command = _candidate_command(container, image_ref, scrape_env)
    process: subprocess.Popen[bytes] | None = None
    try:
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
            deadline = _transfer_config(process, config)
            _wait_for_startup(process, deadline)
        except OSError as exc:
            raise Refusal("container_start_failed") from exc

        # The API response includes component arguments/debug data. It is held only in
        # memory and projected to one fixed health category before leaving this helper.
        api_script = (
            "exec 3<>/dev/tcp/127.0.0.1/12345 || exit 10; "
            "printf 'GET /api/v0/web/components HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n' >&3; "
            f"{API_READ_LOOP} <&3"
        )
        ready_deadline = time.monotonic() + READY_SECONDS
        components = None
        while components is None:
            if process.poll() is not None:
                raise Refusal("container_start_failed")
            remaining = ready_deadline - time.monotonic()
            if remaining <= 0:
                raise Refusal("component_status_unavailable")
            try:
                api_rc, api_bytes = _bounded_command(
                    ["podman", "exec", container, "bash", "-c", api_script],
                    timeout=min(API_SECONDS, remaining), max_bytes=MAX_API_BYTES,
                )
            except Refusal:
                api_rc, api_bytes = -1, b""
            if api_rc == 0:
                headers, separator, body = api_bytes.partition(b"\r\n\r\n")
                if not separator:
                    headers, separator, body = api_bytes.partition(b"\n\n")
                if separator:
                    try:
                        components = _parse_component_response(headers, body)
                    except Refusal:
                        pass
            if components is None:
                remaining = ready_deadline - time.monotonic()
                if remaining <= 0:
                    raise Refusal("component_status_unavailable")
                time.sleep(min(READY_RETRY_SECONDS, remaining))
        if disabled:
            return _project_disabled_components(components)
        states = _component_health_map(components)
        if BASELINE_COMPONENT not in states:
            raise Refusal("component_status_invalid")
        return _project_component_health(components, PROFILE_COMPONENT)
    finally:
        try:
            if process is not None:
                _stop_process(process)
        finally:
            _cleanup_container(container)


def _project_component_health(components: object, name: str) -> str:
    """Project one component's API record to a fixed health state."""
    if not isinstance(components, list) or len(components) > 4096:
        raise Refusal("component_status_invalid")
    matches = []
    for component in components:
        if not isinstance(component, dict):
            raise Refusal("component_status_invalid")
        component_id = component.get("component_id", component.get("ComponentID", component.get("localID")))
        if component_id == name:
            matches.append(component)
    if len(matches) != 1:
        raise Refusal("component_status_invalid")
    health = matches[0].get("health", matches[0].get("Health"))
    if isinstance(health, str) and health.lower() in {"healthy", "unhealthy"}:
        return health.lower()
    if isinstance(health, dict):
        state = health.get("state", health.get("State"))
        if isinstance(state, str) and state.lower() in {"healthy", "unhealthy"}:
            return state.lower()
        value = health.get("healthy", health.get("Healthy"))
        if value is True:
            return "healthy"
        if value is False:
            return "unhealthy"
    raise Refusal("component_status_invalid")


def _parse_component_response(headers: bytes, body: bytes) -> list[object]:
    try:
        status_line = headers.splitlines()[0].decode("ascii")
    except (IndexError, UnicodeDecodeError) as exc:
        raise Refusal("component_status_invalid") from exc
    if not re.fullmatch(r"HTTP/1\.[01] 200(?: .*)?", status_line):
        raise Refusal("component_status_invalid")
    try:
        components = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal("component_status_invalid") from exc
    _component_health_map(components)
    return components


def _component_health_map(components: object) -> dict[str, object]:
    if not isinstance(components, list) or len(components) > 4096:
        raise Refusal("component_status_invalid")
    result = {}
    for component in components:
        if not isinstance(component, dict):
            raise Refusal("component_status_invalid")
        component_id = component.get("component_id", component.get("ComponentID", component.get("localID")))
        if not isinstance(component_id, str) or not component_id or len(component_id) > 256 or component_id in result:
            raise Refusal("component_status_invalid")
        result[component_id] = component
    return result


def _project_disabled_components(components: object) -> str:
    states = _component_health_map(components)
    if BASELINE_COMPONENT not in states or PROFILE_COMPONENT in states:
        raise Refusal("component_status_invalid")
    return "loaded_without_profile"


def validate_runtime(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) not in (
        {"disabled", "enabled"}, {"disabled", "enabled", "profile_address"}
    ):
        raise Refusal("candidate_invalid")
    structure = validate_candidates(
        payload["disabled"], payload["enabled"], payload.get("profile_address", "alloy:12345")
    )
    image_id, image_ref = _image_identity()
    live_image_id = _live_receiver_image_identity()
    if live_image_id != image_id:
        raise Refusal("image_mismatch")
    scrape_env = _receiver_scrape_environment()
    checks = {}
    for candidate, config in (("disabled", payload["disabled"]), ("enabled", payload["enabled"])):
        health = _component_states(
            config, image_ref, scrape_env, disabled=(candidate == "disabled")
        )
        checks[candidate] = health
    if checks["enabled"] != "healthy":
        raise Refusal("component_unhealthy")
    if checks["disabled"] != "loaded_without_profile":
        raise Refusal("container_start_failed")
    return {
        "status": "validated",
        "image": IMAGE,
        "image_id": image_id,
        "live_receiver_image_id": live_image_id,
        "image_parity": "verified",
        "disabled_config_sha256": hashlib.sha256(payload["disabled"].encode("utf-8")).hexdigest(),
        "enabled_config_sha256": hashlib.sha256(payload["enabled"].encode("utf-8")).hexdigest(),
        "disabled_config": "loaded_profile_absent",
        "enabled_config": "loaded",
        "profile_component": "healthy",
        **structure,
        "network": "none",
        "pull": "never",
        "sampling_or_delivery": "not_proven",
        "persistence": "not_proven",
    }


def _run_disabled(config: str, image_ref: str, scrape_env: dict[str, str]) -> str:
    return _component_states(config, image_ref, scrape_env, disabled=True)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        report = validate_runtime(payload)
        code = 0
    except Refusal as exc:
        report = {"status": "unavailable", "reason": exc.category}
        code = 2
    except Exception:
        report = {"status": "unavailable", "reason": "candidate_invalid"}
        code = 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
