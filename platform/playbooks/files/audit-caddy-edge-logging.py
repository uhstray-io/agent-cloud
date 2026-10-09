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
    if pattern == "*":
        return True
    if not isinstance(pattern, str):
        return None
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return pattern == path


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
            if hosts and not any(_host_match(h.lower(), host) for h in hosts):
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
        return host.endswith(pattern[1:]) and host.count(".") == pattern.count(".")
    return False


def _logger_matches(logger, pattern):
    return logger == pattern or logger.startswith(pattern + ".")


def _mapped_logger(names_map, host):
    if any(not isinstance(name, str) for name in names_map):
        return None, True
    exact = [value for name, value in names_map.items() if name.lower() == host]
    if exact:
        return exact[0], len(exact) != 1
    wildcard = [value for name, value in names_map.items() if _host_match(name.lower(), host)]
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
    targets = {"http.log.access" if name == "" else f"http.log.access.{name}"
               for name in logger_names}
    writers = []
    unknown_filter = False
    for log in logs.values():
        if not isinstance(log, dict):
            unknown_filter = True
            continue
        includes, excludes = log.get("include", []), log.get("exclude", [])
        if not isinstance(includes, list) or not isinstance(excludes, list):
            unknown_filter = True
            continue
        admitted = not includes or any(_logger_matches(target, rule)
                                       for target in targets for rule in includes)
        rejected = any(_logger_matches(target, rule)
                       for target in targets for rule in excludes)
        if admitted and not rejected:
            writer = log.get("writer")
            output = writer.get("output") if isinstance(writer, dict) else None
            if output not in {"stdout", "stderr", "file", "net", "discard"}:
                unknown_filter = True
            else:
                writers.append(output)
    if not writers and not logs:
        # Caddy always has a default logger; its default writer is stderr.
        writers = ["stderr"]
    if unknown_filter:
        return "unknown", "unknown"
    if not writers:
        return ("unknown", "unknown") if unknown_filter else ("none", "not_applicable")
    classes = {"stdout": "container_stdout", "stderr": "container_stderr",
               "file": "file", "net": "network", "discard": "discard"}
    sinks = {classes[w] for w in writers}
    sink = next(iter(sinks)) if len(sinks) == 1 else "multiple"
    if len(sinks) > 1:
        return sink, "unknown"
    retention = "not_declared"
    if "file" in writers:
        if any(isinstance(log, dict)
               and isinstance(log.get("writer"), dict)
               and log["writer"].get("output") == "file"
               and any(key in log["writer"] for key in ("roll_keep", "roll_keep_for", "roll_disabled"))
               for log in logs.values()):
            retention = "caddy_file_policy_declared"
    elif sink in {"container_stdout", "container_stderr"}:
        if isinstance(podman_log_config, str):
            try:
                podman_log_config = json.loads(podman_log_config)
            except (TypeError, ValueError):
                return sink, "unknown"
        if not isinstance(podman_log_config, dict):
            return sink, "unknown"
        options = podman_log_config.get("Config", {}) if isinstance(podman_log_config, dict) else {}
        if not isinstance(options, dict):
            return sink, "unknown"
        retention = ("container_limit_declared"
                     if any(k in options for k in ("max-size", "max-file"))
                     else "not_declared")
    elif sink in {"network", "discard"}:
        retention = "sink_external_or_discarded"
    return sink, retention


def audit(data):
    callback = urlsplit(data.get("callback_uri", ""))
    if callback.scheme != "https" or not callback.hostname or callback.query or callback.fragment:
        return _unknown("invalid_callback_uri")
    host, path, port = callback.hostname.lower(), callback.path or "/", callback.port or 443
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
            if mapped is None and access_logs.get("skip_unmapped_hosts") is True:
                access_logging, sink, retention = "disabled", "none", "not_applicable"
            else:
                if mapped is None:
                    mapped = access_logs.get("default_logger_name", "")
                names = [mapped] if isinstance(mapped, str) else mapped
                if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                    access_logging, sink, retention = "unknown", "unknown", "unknown"
                else:
                    skip_hosts = access_logs.get("skip_hosts", [])
                    if not isinstance(skip_hosts, list):
                        access_logging, sink, retention = "unknown", "unknown", "unknown"
                    elif any(_host_match(skipped.lower(), host) for skipped in skip_hosts):
                        access_logging, sink, retention = "disabled", "none", "not_applicable"
                    else:
                        sink, retention = _sink_summary(config, names, data.get("container_log_config", {}))
                        access_logging = ("enabled" if sink not in {"unknown", "none"}
                                          else "disabled" if sink == "none" else "unknown")
    marker = data.get("marker", "")
    log_tail = data.get("container_log_tail", "")
    marker_in_logs = bool(marker and marker in log_tail)
    # The URI task verifies TLS with Ansible's normal CA validation. A returned
    # HTTP status confirms a response; server headers are implementation detail.
    probe_ok = (isinstance(data.get("probe_status"), int)
                and data.get("probe_status") in range(100, 600))
    marker_status = ("yes" if marker_in_logs else
                     "no" if probe_ok and data.get("logs_read_ok") is True else "unknown")
    report = {
        "status": "classified" if access_logging != "unknown" and retention != "unknown" else "refused",
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
        report["finding"] = "access_logging_disabled"
    if report["status"] == "refused":
        report["reason"] = ("retention_unavailable" if retention == "unknown"
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
