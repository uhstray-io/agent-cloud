"""Every image a compose file defaults to is a fully qualified reference.

Podman refuses a short name (``openpolicyagent/opa:1.0.0-static``) when the host defines no
unqualified-search registry, which is the production VMs' configuration: the first production
Deploy OPA (Dev), Semaphore task 10339, created no container ("Error: short-name ...", exit code
125). The local-dev podman resolved the same name, so only production found it.

A reference is fully qualified when its first path component names a registry: it contains a
``.`` or a ``:``, or it is ``localhost``. The default inside ``${VAR:-default}`` is what a deploy
uses when the inventory sets nothing, so that is what is checked. A required variable
(``${VAR:?message}``) has no default and is skipped: the inventory supplies the reference. An image
with ``pull_policy: never`` is a local build's tag, never pulled, and is skipped too.
"""

import re
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SUBST = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(:?-)(?P<default>[^}]*)\}$")


def compose_files():
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--", "platform/**/*compose*.yml",
         "platform/**/*compose*.yaml", "agents/**/*compose*.yml", "agents/**/*compose*.yaml"],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    return [REPO / f for f in out]


class _Loader(yaml.SafeLoader):
    """Compose overlays use !override / !reset; read the value under the tag."""


def _untagged(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_Loader.add_multi_constructor("!", _untagged)


def default_images(path):
    data = yaml.load(path.read_text(), Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    if not isinstance(data, dict):  # a matching name that is not a compose file
        return
    for name, service in (data.get("services") or {}).items():
        image = (service or {}).get("image")
        # A locally built image that is never pulled (NetBox's) names the build, not a registry.
        if not isinstance(image, str) or (service or {}).get("pull_policy") == "never":
            continue
        match = SUBST.match(image.strip())
        if match:
            yield name, match.group("default")
        elif "${" not in image:
            yield name, image.strip()


def qualified(ref):
    first = ref.split("/", 1)[0]
    return "/" in ref and ("." in first or ":" in first or first == "localhost")


def test_there_are_compose_images_to_check():
    assert sum(1 for f in compose_files() for _ in default_images(f)) > 10


def test_every_default_image_is_fully_qualified():
    offenders = [
        f"{f.relative_to(REPO)}: {svc} -> {ref}"
        for f in compose_files()
        for svc, ref in default_images(f)
        if not qualified(ref)
    ]
    assert not offenders, "short-name images (prefix the registry, e.g. docker.io/):\n" + "\n".join(offenders)


def test_the_check_tells_a_short_name_from_a_qualified_one():
    assert not qualified("openpolicyagent/opa:1.0.0-static")
    assert not qualified("postgres:16")
    assert qualified("docker.io/openpolicyagent/opa:1.0.0-static")
    assert qualified("cr.agentgateway.dev/agentgateway:v1.5.0")
    assert qualified("localhost/agent-cloud-console:local")
    assert qualified("registry.local:5000/x:1")
