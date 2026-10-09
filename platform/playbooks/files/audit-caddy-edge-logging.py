"""Summarize the declared and live Caddy callback route without exposing it."""

import json
import re
import shlex
import sys
from urllib.parse import urlsplit


def _source_blocks(source, host):
    lines = source.splitlines()
    starts = [i for i, line in enumerate(lines)
              if "{" in line and not line.lstrip().startswith("#")
              and re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(host)}(?::[0-9]+)?(?![A-Za-z0-9_.-])", line)]
    blocks = []
    for start in starts:
        depth = 0
        block = []
        for line in lines[start:]:
            block.append(line)
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                break
        if depth == 0:
            blocks.append("\n".join(block))
    return blocks


def _path_matches(pattern, path):
    if not isinstance(pattern, str) or not _literal_path_supported(path):
        return None
    if pattern == "*":
        return True
    if not _literal_path_supported(pattern) or "*" in pattern:
        return None
    # Caddy v2.11.4 lower-cases ordinary path patterns and the request path.
    return pattern.lower() == path.lower()


def _literal_path_supported(path):
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    if not path.isascii() or any(char in path for char in ("%", "\\", "*", "?")):
        return False
    if "//" in path or any(part in {".", ".."} for part in path.split("/")):
        return False
    return re.fullmatch(r"/[A-Za-z0-9._~!$&'()+,;=:@/-]*", path) is not None


def _route_branches(routes, host, path, parent_handlers=None):
    parent_handlers = parent_handlers or []
    branches = []
    ambiguous = False
    if not isinstance(routes, list):
        return branches, True
    for route in routes:
        if not isinstance(route, dict):
            ambiguous = True
            continue
        alternatives = route.get("match", [{}])
        if not isinstance(alternatives, list) or not alternatives:
            ambiguous = True
            continue
        for matcher in alternatives:
            if not isinstance(matcher, dict):
                ambiguous = True
                continue
            hosts = matcher.get("host", [])
            paths = matcher.get("path", [])
            methods = matcher.get("method", [])
            if isinstance(hosts, str) or isinstance(paths, str) or isinstance(methods, str):
                ambiguous = True
                continue
            if methods and "GET" not in methods:
                continue
            host_results = []
            for matcher_host in hosts:
                if not isinstance(matcher_host, str):
                    ambiguous = True
                    host_results.append(None)
                else:
                    host_results.append(_host_match(matcher_host.lower(), host))
            if any(result is None for result in host_results):
                ambiguous = True
                continue
            if hosts and not any(host_results):
                continue
            path_results = [_path_matches(p, path) for p in paths]
            if any(result is None for result in path_results):
                ambiguous = True
                continue
            if paths and not any(path_results):
                continue
            if set(matcher) - {"host", "path", "method"}:
                ambiguous = True
                continue
            route_handlers = route.get("handle", [])
            if not isinstance(route_handlers, list) or not all(
                isinstance(handler, dict) for handler in route_handlers
            ):
                ambiguous = True
                continue
            matched_handlers = parent_handlers + route_handlers
            child_routes = [handler.get("routes") for handler in route_handlers
                            if handler.get("handler") == "subroute"]
            if child_routes:
                for children in child_routes:
                    child, child_ambiguous = _route_branches(
                        children, host, path, matched_handlers
                    )
                    branches.extend(child)
                    ambiguous = ambiguous or child_ambiguous
            else:
                branches.append((route, matched_handlers))
    return branches, ambiguous


def _port_listens(server, port):
    listeners = server.get("listen")
    if not isinstance(listeners, list):
        return False
    return any(isinstance(item, str) and item.rsplit(":", 1)[-1] == str(port)
               for item in listeners)


def _host_match(pattern, host):
    if pattern == host:
        return True
    if pattern.startswith("*."):
        if pattern.count("*") != 1 or not pattern[2:] or any(c in pattern[2:] for c in "{}?"):
            return None
        return host.endswith(pattern[1:]) and host.count(".") == pattern.count(".")
    if any(char in pattern for char in "*{}?"):
        return None
    return False


def _logger_allowed(logger, includes, excludes):
    if not includes and not excludes:
        return True
    name = logger if logger in {"", "*", "."} else logger + "."
    longest_accept = 0
    if includes:
        for namespace in includes:
            if name.startswith(namespace + "."):
                longest_accept = max(longest_accept, len(namespace))
        if longest_accept == 0:
            return False
    longest_reject = 0
    for namespace in excludes:
        if (namespace == "*" and name != ".") or (namespace == "." and name == "."):
            return False
        if name.startswith(namespace + "."):
            longest_reject = max(longest_reject, len(namespace))
    if longest_reject > longest_accept:
        return False
    return longest_accept > longest_reject or (not includes and longest_reject == 0)


def _mapped_logger(names_map, host):
    def valid_names(value):
        if isinstance(value, str):
            return True
        return (isinstance(value, list) and bool(value)
                and all(isinstance(name, str) for name in value)
                and len(set(value)) == len(value))

    if any(not isinstance(name, str) for name in names_map):
        return None, True
    exact = [value for name, value in names_map.items() if name.lower() == host]
    if exact:
        return exact[0], len(exact) != 1 or not valid_names(exact[0])
    wildcard = []
    for name, value in names_map.items():
        matched = _host_match(name.lower(), host)
        if matched is None:
            return None, True
        if matched:
            if not valid_names(value):
                return None, True
            wildcard.append(value)
    if len(wildcard) > 1:
        return None, True
    return (wildcard[0], False) if wildcard else (None, False)


def _normalize_upstream(target):
    target = re.sub(r"^https?://", "", target, flags=re.IGNORECASE).rstrip("/")
    return target.lower()


def _declared_upstreams(block):
    targets = []
    for line in block.splitlines():
        if line.lstrip().startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            return []
        if not tokens or tokens[0] != "reverse_proxy":
            continue
        args = [token for token in tokens[1:] if token not in {"{", "}"}]
        if args and args[0].startswith("@"):
            args = args[1:]
        targets.extend(_normalize_upstream(target) for target in args if target)
    return targets


def _runtime_upstreams(route):
    targets = []
    for handler in route.get("handle", []):
        if not isinstance(handler, dict):
            continue
        if handler.get("handler") == "reverse_proxy":
            upstreams = handler.get("upstreams")
            if not isinstance(upstreams, list) or not upstreams:
                return []
            for upstream in upstreams:
                if not isinstance(upstream, dict) or not isinstance(upstream.get("dial"), str):
                    return []
                targets.append(_normalize_upstream(upstream["dial"]))
        elif handler.get("handler") == "subroute":
            for child in handler.get("routes", []):
                child_targets = _runtime_upstreams(child)
                if not child_targets:
                    continue
                targets.extend(child_targets)
    return targets


def _sink_summary(config, logger_names, podman_log_config):
    logging = config.get("logging", {})
    logs = logging.get("logs", {}) if isinstance(logging, dict) else None
    if not isinstance(logs, dict):
        return "unknown", "unknown"
    if isinstance(logger_names, str):
        logger_names = [logger_names]
    if (not isinstance(logger_names, list) or not logger_names
            or not all(isinstance(name, str) for name in logger_names)
            or len(set(logger_names)) != len(logger_names)):
        return "unknown", "unknown"
    targets = {"http.log.access" if name == "" else f"http.log.access.{name}"
               for name in logger_names}
    writer_records = []
    for _, log in logs.items():
        if not isinstance(log, dict):
            return "unknown", "unknown"
        includes, excludes = log.get("include", []), log.get("exclude", [])
        if (not isinstance(includes, list) or not isinstance(excludes, list)
                or not all(isinstance(rule, str) for rule in includes + excludes)):
            return "unknown", "unknown"
        if any(_logger_allowed(target, includes, excludes) for target in targets):
            # Level and sampling controls can suppress a particular callback.
            if "level" in log or "sampling" in log:
                return "unknown", "unknown"
            writer = log.get("writer")
            output = writer.get("output") if isinstance(writer, dict) else None
            if output not in {"stdout", "stderr", "file", "net", "discard"}:
                return "unknown", "unknown"
            writer_records.append((output, writer))
    # Caddy always creates the structured default log when the config omits it.
    if "default" not in logs:
        writer_records.append(("stderr", {"output": "stderr", "implicit_default": True}))
    if not writer_records:
        return "none", "not_applicable"
    classes = {"stdout": "container_stdout", "stderr": "container_stderr",
               "file": "file", "net": "network", "discard": "discard"}
    sinks = {classes[output] for output, _ in writer_records}
    sink = next(iter(sinks)) if len(sinks) == 1 else "multiple"
    if len(sinks) > 1:
        return sink, "unknown"
    retention = "not_declared"
    if sink == "file":
        if all(any(key in writer for key in ("roll_keep", "roll_keep_for", "roll_disabled"))
               for _, writer in writer_records):
            retention = "caddy_file_policy_declared"
    elif sink in {"container_stdout", "container_stderr"}:
        if isinstance(podman_log_config, str):
            try:
                podman_log_config = json.loads(podman_log_config)
            except (TypeError, ValueError):
                return sink, "unknown"
        if not isinstance(podman_log_config, dict):
            return sink, "unknown"
        options = podman_log_config.get("Config")
        if options is None:
            options = {}
        if not isinstance(options, dict):
            return sink, "unknown"
        retention = ("container_limit_declared"
                     if any(k in options for k in ("max-size", "max-file"))
                     else "host_retention_unverified"
                     if podman_log_config.get("Type") == "journald"
                     else "not_declared")
    elif sink in {"network", "discard"}:
        retention = "sink_external_or_discarded"
    return sink, retention


def _access_logging_status(sink):
    if sink in {"none", "discard"}:
        return "disabled"
    if sink in {"unknown", "multiple"}:
        return "unknown"
    return "enabled"


def audit(data):
    callback = urlsplit(data.get("callback_uri", ""))
    if callback.scheme != "https" or not callback.hostname or callback.query or callback.fragment:
        return _unknown("invalid_callback_uri")
    host, path, port = callback.hostname.lower(), callback.path or "/", callback.port or 443
    if not _literal_path_supported(path):
        return _unknown("callback_path_unsupported")
    source = data.get("declared_sites", "")
    source_blocks = _source_blocks(source, host)
    if len(source_blocks) != 1:
        return _unknown("declared_site_ambiguous", source_blocks=len(source_blocks))
    source_log_directives = len(re.findall(r"(?m)^\s+log(?:\s|$)", source_blocks[0]))
    declared_upstreams = _declared_upstreams(source_blocks[0])
    if not declared_upstreams:
        return _unknown("declared_upstream_unavailable", source_blocks=1,
                        declared_log_directives=source_log_directives)
    config = json.loads(data.get("runtime_config", ""))
    servers = config.get("apps", {}).get("http", {}).get("servers", {})
    if not isinstance(servers, dict):
        return _unknown("runtime_servers_unavailable", source_blocks=1,
                        declared_log_directives=source_log_directives)
    candidates = []
    ambiguous = False
    for server in servers.values():
        if not isinstance(server, dict) or not _port_listens(server, port):
            continue
        branches, route_ambiguous = _route_branches(server.get("routes", []), host, path)
        ambiguous = ambiguous or route_ambiguous
        if branches:
            candidates.append((server, branches))
    if ambiguous or len(candidates) != 1 or len(candidates[0][1]) != 1:
        return _unknown("runtime_route_ambiguous", source_blocks=1,
                        declared_log_directives=source_log_directives,
                        route_candidates=sum(len(branches) for _, branches in candidates))
    server, matching_branches = candidates[0]
    matching_route, matched_handlers = matching_branches[0]
    if any(handler.get("handler") == "vars" for handler in matched_handlers):
        return _unknown("runtime_route_logging_override_ambiguous", source_blocks=1,
                        declared_log_directives=source_log_directives,
                        route_candidates=1)
    runtime_upstreams = _runtime_upstreams(matching_route)
    if not runtime_upstreams or sorted(runtime_upstreams) != sorted(declared_upstreams):
        return _unknown("runtime_upstream_mismatch", source_blocks=1,
                        declared_log_directives=source_log_directives,
                        route_candidates=1)
    access_logs = server.get("logs")
    if access_logs is None:
        access_logging, sink, retention = "disabled", "none", "not_applicable"
    elif not isinstance(access_logs, dict):
        access_logging, sink, retention = "unknown", "unknown", "unknown"
    else:
        names_map = access_logs.get("logger_names", {})
        if not isinstance(names_map, dict):
            access_logging, sink, retention = "unknown", "unknown", "unknown"
        else:
            mapped, mapping_ambiguous = _mapped_logger(names_map, host)
            if mapping_ambiguous:
                return _unknown("runtime_logger_mapping_ambiguous", source_blocks=1,
                                declared_log_directives=source_log_directives,
                                route_candidates=1)
            if mapped is None:
                skip_hosts = access_logs.get("skip_hosts", [])
                skip_unmapped = access_logs.get("skip_unmapped_hosts", False)
                if (not isinstance(skip_hosts, list)
                        or not all(isinstance(skipped, str) for skipped in skip_hosts)
                        or not isinstance(skip_unmapped, bool)):
                    access_logging, sink, retention = "unknown", "unknown", "unknown"
                else:
                    skip_results = [_host_match(skipped.lower(), host) for skipped in skip_hosts]
                    if any(result is None for result in skip_results):
                        access_logging, sink, retention = "unknown", "unknown", "unknown"
                    elif any(skip_results) or skip_unmapped:
                        access_logging, sink, retention = "disabled", "none", "not_applicable"
                    else:
                        mapped = access_logs.get("default_logger_name", "")
                        if not isinstance(mapped, str):
                            access_logging, sink, retention = "unknown", "unknown", "unknown"
                        else:
                            sink, retention = _sink_summary(
                                config, mapped, data.get("container_log_config", {})
                            )
                            access_logging = _access_logging_status(sink)
            else:
                # Caddy checks a matching logger_names host before skip_hosts;
                # an explicit mapping therefore keeps access logging active.
                sink, retention = _sink_summary(
                    config, mapped, data.get("container_log_config", {})
                )
                access_logging = _access_logging_status(sink)
    marker = data.get("marker", "")
    log_tail = data.get("container_log_tail", "")
    marker_in_logs = bool(marker and marker in log_tail)
    # The URI task verifies TLS with Ansible's normal CA validation. A returned
    # HTTP status confirms a response; server headers are implementation detail.
    probe_ok = (isinstance(data.get("probe_status"), int)
                and data.get("probe_status") in range(100, 600))
    marker_status = ("not_applicable" if sink in {"file", "network", "multiple", "discard", "none"}
                     else "yes" if marker_in_logs else
                     "no" if probe_ok and data.get("logs_read_ok") is True else "unknown")
    retention_unverified = retention in {"unknown", "host_retention_unverified"}
    report = {
        "status": "classified" if access_logging != "unknown" and not retention_unverified else "refused",
        "source_site_blocks": 1,
        "declared_log_directives": source_log_directives,
        "runtime_servers": 1,
        "runtime_route_matches": 1,
        "runtime_upstream_matches": 1,
        "access_logging": access_logging,
        "sink_class": sink,
        "retention": retention,
        "https_probe": "completed" if probe_ok else "unverified",
        "marker_in_bounded_container_logs": marker_status,
    }
    if access_logging == "disabled":
        report["finding"] = ("access_logging_discarded" if sink == "discard"
                              else "access_logging_disabled")
    if report["status"] == "refused":
        report["reason"] = ("retention_unavailable" if retention_unverified
                             else "runtime_or_sink_unavailable")
    return report


def _unknown(reason, **counts):
    return {"status": "refused", "reason": reason, **counts,
            "access_logging": "unknown", "sink_class": "unknown", "retention": "unknown",
            "https_probe": "unverified", "marker_in_bounded_container_logs": "unknown"}


def main():
    try:
        report = audit(json.load(sys.stdin))
        print(json.dumps(report, separators=(",", ":")))
        return 0 if report["status"] == "classified" else 1
    except Exception:
        print('{"status":"refused","reason":"audit_input_invalid","access_logging":"unknown","sink_class":"unknown","retention":"unknown","https_probe":"unverified","marker_in_bounded_container_logs":"unknown"}')
        return 1


if __name__ == "__main__":
    sys.exit(main())
