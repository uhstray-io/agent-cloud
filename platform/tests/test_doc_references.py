"""Fail when an active Markdown file links to a path that does not exist or leaves the repository."""

import re
import socket
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[2]

# Historical and tool-owned Markdown is not scanned.
EXCLUDED_PREFIXES = (
    "plan/archive/",
    ".claude/",
    ".opencode/",
    ".agents/",
    "plan/development/openspec/",
)

# Every entry needs review. Key: repo-relative Markdown file. Value: link targets that are
# knowingly left broken, each with a stated reason in the comment beside it. Keep this empty
# unless a link sits outside the edit boundary of the change that found it, and remove an
# entry as soon as its link is fixed (test_known_broken_links_still_exist enforces that).
KNOWN_BROKEN_LINKS: Mapping[str, tuple[str, ...]] = {}

LINK = re.compile(r"\[[^\]\n]*\]\(([^()\s]+)\)")
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


class BrokenLink(NamedTuple):
    source: str
    line: int
    target: str
    reason: str

    def __str__(self) -> str:
        return f"{self.source}:{self.line}: [{self.target}] {self.reason}"


def in_scope(rel: str) -> bool:
    return not rel.startswith(EXCLUDED_PREFIXES) and "node_modules" not in Path(rel).parts


def tracked_markdown() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    names = listed.decode("utf-8", errors="replace").split("\0")
    return [name for name in names if name and in_scope(name)]


def markdown_links(text: str) -> Iterable[tuple[int, str]]:
    fence = None
    for line_no, line in enumerate(text.split("\n"), 1):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        for match in LINK.finditer(line):
            yield line_no, match.group(1)


def link_problem(root: Path, source: Path, target: str) -> str | None:
    # URLs, mailto:, protocol-relative and pure #anchor links are not repository paths.
    if SCHEME.match(target) or target.startswith("//"):
        return None
    path = unquote(target.split("#", 1)[0])
    if not path:
        return None
    # resolve() follows symlinks, so a link through a symlink that leaves the repository is an escape too.
    resolved = (source.parent / path).resolve()
    if not resolved.is_relative_to(root):
        return "escapes the repository"
    if not resolved.exists():
        return "target does not exist"
    return None


def find_broken_links(root: Path, files: Iterable[str]) -> list[BrokenLink]:
    root = root.resolve()
    broken = []
    for rel in files:
        source = root / rel
        text = source.read_text(encoding="utf-8", errors="replace")
        for line_no, target in markdown_links(text):
            reason = link_problem(root, source, target)
            if reason is not None:
                broken.append(BrokenLink(rel, line_no, target, reason))
    return broken


def test_active_markdown_links_resolve_inside_repository():
    unexpected = [
        link
        for link in find_broken_links(REPO_ROOT, tracked_markdown())
        if link.target not in KNOWN_BROKEN_LINKS.get(link.source, ())
    ]
    assert not unexpected, "broken Markdown links:\n" + "\n".join(str(link) for link in unexpected)


def test_known_broken_links_still_exist():
    actual = {(link.source, link.target) for link in find_broken_links(REPO_ROOT, tracked_markdown())}
    stale = [
        f"{source}: [{target}]"
        for source, targets in KNOWN_BROKEN_LINKS.items()
        for target in targets
        if (source, target) not in actual
    ]
    assert not stale, "allowlist entries no longer name a broken link; remove them:\n" + "\n".join(stale)


def test_path_escape_is_rejected_even_when_the_target_exists(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (tmp_path / "outside.md").write_text("# outside\n", encoding="utf-8")
    (repo / "docs" / "a.md").write_text("[up](../../outside.md)\n", encoding="utf-8")

    broken = find_broken_links(repo, ["docs/a.md"])

    assert [(link.source, link.line, link.target, link.reason) for link in broken] == [
        ("docs/a.md", 1, "../../outside.md", "escapes the repository"),
    ]


def test_missing_link_is_reported_with_file_line_and_target(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "exists.md").write_text("# here\n", encoding="utf-8")
    (tmp_path / "index.md").write_text(
        "[ok](docs/exists.md)\n\n[gone](docs/removed.md#section)\n",
        encoding="utf-8",
    )

    broken = find_broken_links(tmp_path, ["index.md"])

    assert [str(link) for link in broken] == ["index.md:3: [docs/removed.md#section] target does not exist"]


def test_urls_anchors_and_fenced_examples_are_not_checked(tmp_path):
    (tmp_path / "page.md").write_text(
        "[site](https://example.com/x.md) [mail](mailto:ops@example.com)\n"
        "[top](#heading) [proto](//example.com/y.md)\n"
        "```markdown\n"
        "[example](does-not-exist.md)\n"
        "```\n"
        "~~~\n"
        "[tilde](also-missing.md)\n"
        "~~~\n",
        encoding="utf-8",
    )

    assert find_broken_links(tmp_path, ["page.md"]) == []


def test_scan_makes_no_network_calls(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted during the link scan")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    (tmp_path / "a.md").write_text("[b](b.md) [c](https://example.com/c)\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# b\n", encoding="utf-8")

    assert find_broken_links(tmp_path, ["a.md", "b.md"]) == []
