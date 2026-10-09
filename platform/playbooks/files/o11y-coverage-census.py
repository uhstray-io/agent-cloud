#!/usr/bin/env python3
"""Build a sanitized, read-only census of repository candidates and estate receipts."""

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

TARGET_ID = re.compile(r"^(service|agent|vm|guest|runner|network|dgx):[a-z][a-z0-9_-]{1,63}$")
SERVICE_ID = re.compile(r"^[a-z][a-z0-9_-]*(/[a-z][a-z0-9_-]*)?$")
TARGET_TYPES = {"service", "agent", "vm", "guest", "runner", "network", "dgx"}
LIFECYCLE = {"deployed", "planned", "retired", "excluded"}
SIGNALS = {"logs", "metrics", "traces", "health"}


def fail(message):
    print(json.dumps({"status": "invalid_declaration", "error": message}))
    raise SystemExit(2)


def candidate_names(repo, path):
    directory = repo / path
    if not directory.is_dir():
        fail(f"candidate root {path} is missing")
    return sorted(p.name for p in directory.iterdir() if p.is_dir() and not p.name.startswith("."))


def validate(targets):
    ids = set()
    identities = {}
    required = {
        "target_id", "target_type", "lifecycle", "owner", "runtime",
        "service_identity", "environment", "signals", "collection_method",
        "budget", "receipt_reference", "template_references",
    }
    for target in targets:
        if not isinstance(target, dict) or required - target.keys():
            fail("each target must include every required coverage declaration field")
        target_id = target["target_id"]
        identity = target["service_identity"]
        if not isinstance(target_id, str) or not TARGET_ID.fullmatch(target_id):
            fail("target_id must be a stable non-address identifier")
        if target_id in ids:
            fail("duplicate target_id in estate declarations")
        ids.add(target_id)
        if target["target_type"] not in TARGET_TYPES or target["lifecycle"] not in LIFECYCLE:
            fail("target_type or lifecycle is outside the public schema")
        if target_id.split(":", 1)[0] != target["target_type"]:
            fail("target_id prefix must match target_type")
        if not isinstance(target["owner"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", target["owner"]):
            fail("owner must be a bounded team label, not a person or address")
        environment_pattern = r"[a-z][a-z0-9_-]{1,31}"
        if not isinstance(target["environment"], str) or not re.fullmatch(environment_pattern, target["environment"]):
            fail("environment must be a bounded inventory label")
        if not isinstance(identity, str) or not SERVICE_ID.fullmatch(identity):
            fail("service_identity must use the canonical service identity format")
        identities.setdefault(identity, []).append(target_id)
        if not isinstance(target["signals"], dict) or set(target["signals"]) - SIGNALS:
            fail("signals must be a mapping of supported signal names")
        if (
            not isinstance(target["runtime"], str)
            or not target["runtime"]
            or not isinstance(target["collection_method"], str)
            or not target["collection_method"]
        ):
            fail("runtime and collection_method are required labels")
        if not isinstance(target["budget"], dict) or not target["budget"]:
            fail("budget must be a non-empty mapping")
        if not isinstance(target["receipt_reference"], dict):
            fail("receipt_reference must map signals to their receipt references")
        template_refs = target.get("template_references", [])
        if not isinstance(template_refs, list) or any(not isinstance(ref, str) for ref in template_refs):
            fail("template_references must be a list of Semaphore template names")
        for signal, declaration in target["signals"].items():
            if not isinstance(declaration, dict) or not isinstance(declaration.get("applicable"), bool):
                fail(f"{signal} must declare applicability")
            if not declaration["applicable"]:
                exception = declaration.get("exception")
                if not isinstance(exception, dict) or not all(
                    exception.get(field) for field in ("reason", "reference")
                ):
                    fail(f"{signal} requires a reviewed exception reason and reference when not applicable")
                if not isinstance(exception["reference"], str) or not re.fullmatch(
                    r"(?:review/[1-9][0-9]*|semaphore/task/[1-9][0-9]*)", exception["reference"]
                ):
                    fail(f"{signal} exception reference must identify a review or Semaphore task")
                if signal == "traces" and not (exception.get("alternative") or exception.get("manual_plan")):
                    fail("an excluded trace signal requires an approved alternative or manual instrumentation plan")
            if declaration["applicable"] and any(
                not declaration.get(field)
                for field in ("selector", "collection_method", "receipt_reference", "freshness_seconds")
            ):
                fail(f"{signal} must declare its selector, method, receipt reference, and freshness")
            if declaration["applicable"] and (
                target["receipt_reference"].get(signal) != declaration["receipt_reference"]
                or not re.fullmatch(r"semaphore/task/[1-9][0-9]*", declaration["receipt_reference"])
            ):
                fail(f"{signal} receipt reference must be a Semaphore task reference")
            if declaration["applicable"] and (
                not isinstance(declaration["freshness_seconds"], int)
                or isinstance(declaration["freshness_seconds"], bool)
                or not 60 <= declaration["freshness_seconds"] <= 86400
            ):
                fail(f"{signal} freshness_seconds must be between 60 and 86400")
    return ids, {key: values for key, values in identities.items() if len(values) > 1}


def receipt_status(_target, _signal, _declaration, _inventory_revision, _repository_sha, _now):
    """Fail closed until census independently reads back the Semaphore task record."""
    return "incomplete"


def template_reference_summary(references, template_names):
    present_count = sum(1 for reference in references if reference in template_names)
    return {
        "declared_count": len(references),
        "present_count": present_count,
        "missing_count": len(references) - present_count,
    }


def build_report(repo, inventory, repository_sha, now=None):
    targets = inventory.get("targets", [])
    inventory_revision = inventory.get("inventory_revision")
    template_names = set(inventory.get("template_names", []))
    if not isinstance(targets, list):
        fail("targets must be a list")
    if inventory_revision is not None and (
        not isinstance(inventory_revision, str)
        or not re.fullmatch(r"[0-9a-f]{7,64}", inventory_revision)
    ):
        fail("inventory revision must be an exact hexadecimal revision when declared")
    if not isinstance(inventory.get("template_names", []), list):
        fail("template_names must be a list")
    ids, conflicts = validate(targets)
    now = now or datetime.now(UTC)
    declared = {}
    for target in targets:
        declared.setdefault(target["service_identity"], []).append(target)
    entries = []
    matched_target_ids = set()
    for kind, dirname in (("service", "platform/services"), ("agent", "agents")):
        for name in candidate_names(repo, dirname):
            identity = name
            if identity in conflicts:
                entries.append({
                    "target_id": f"candidate:{kind}:{name}",
                    "source_candidate": f"{dirname}/{name}",
                    "target_type": kind,
                    "lifecycle": "unclassified",
                    "service_identity": identity,
                    "coverage": "unverified",
                    "identity_conflict": True,
                    "signals": dict.fromkeys(sorted(SIGNALS), "identity_conflict"),
                })
                continue
            matches = [target for target in declared.get(identity, []) if target["target_type"] == kind]
            if len(matches) != 1:
                entries.append({
                    "target_id": f"candidate:{kind}:{name}",
                    "source_candidate": f"{dirname}/{name}",
                    "target_type": kind,
                    "lifecycle": "unclassified",
                    "service_identity": identity,
                    "coverage": "unverified",
                    "signals": {},
                })
                continue
            target = matches[0]
            matched_target_ids.add(target["target_id"])
            statuses = {
                signal: (
                    "undeclared" if signal not in target["signals"] else
                    "excepted" if not target["signals"][signal]["applicable"] else
                    receipt_status(target, signal, target["signals"][signal], inventory_revision, repository_sha, now)
                )
                for signal in sorted(SIGNALS)
            }
            deployed = target["lifecycle"] == "deployed"
            coverage = "verified" if deployed and statuses and all(
                state in {"verified", "excepted"} for state in statuses.values()
            ) else "incomplete" if deployed else "not_deployed"
            references = target.get("template_references", [])
            missing_templates = [ref for ref in references if ref not in template_names]
            if deployed and (not references or missing_templates):
                coverage = "incomplete"
            entries.append({
                "target_id": target["target_id"],
                "source_candidate": f"{dirname}/{name}",
                "target_type": target["target_type"],
                "lifecycle": target["lifecycle"],
                "service_identity": identity,
                "coverage": coverage,
                "signals": statuses,
                "template_references": template_reference_summary(references, template_names),
            })
    for target in targets:
        if target["service_identity"] not in conflicts:
            continue
        entries.append({
            "target_id": target["target_id"],
            "source_candidate": None,
            "target_type": target["target_type"],
            "lifecycle": target["lifecycle"],
            "service_identity": target["service_identity"],
            "coverage": "unverified",
            "identity_conflict": True,
            "signals": dict.fromkeys(sorted(SIGNALS), "identity_conflict"),
            "template_references": template_reference_summary(target.get("template_references", []), template_names),
        })
    for target in targets:
        if target["target_id"] in matched_target_ids or target["service_identity"] in conflicts:
            continue
        statuses = {
            signal: (
                "undeclared" if signal not in target["signals"] else
                "excepted" if not target["signals"][signal]["applicable"] else
                receipt_status(target, signal, target["signals"][signal], inventory_revision, repository_sha, now)
            )
            for signal in sorted(SIGNALS)
        }
        deployed = target["lifecycle"] == "deployed"
        references = target.get("template_references", [])
        missing_templates = [ref for ref in references if ref not in template_names]
        coverage = "verified" if deployed and statuses and all(
            state in {"verified", "excepted"} for state in statuses.values()
        ) and references and not missing_templates else "incomplete" if deployed else "not_deployed"
        entries.append({
            "target_id": target["target_id"],
            "source_candidate": None,
            "target_type": target["target_type"],
            "lifecycle": target["lifecycle"],
            "service_identity": target["service_identity"],
            "coverage": coverage,
            "signals": statuses,
            "template_references": template_reference_summary(references, template_names),
        })
    conflicting_ids = sorted(target_id for values in conflicts.values() for target_id in values)
    return {
        "status": "complete" if inventory_revision else "inventory_missing",
        "inventory_revision": inventory_revision,
        "candidate_count": sum(1 for entry in entries if entry["source_candidate"]),
        "declared_target_count": len(targets),
        "duplicate_identity_count": len(conflicts),
        "duplicate_identity_targets": conflicting_ids,
        "unverified_target_count": sum(1 for entry in entries if entry["coverage"] != "verified"),
        "targets": entries,
    }


def main():
    if len(sys.argv) != 2:
        fail("usage: o11y-coverage-census.py <repo-root>")
    try:
        inventory = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        fail("inventory input must be JSON")
    report = build_report(Path(sys.argv[1]).resolve(), inventory, inventory.get("repository_sha", ""))
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
