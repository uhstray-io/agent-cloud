#!/usr/bin/env python3
"""Semantic versions for agent-cloud, computed from pull request titles.

One script holds every rule, so the pull request check and the release job cannot
disagree. The design is plan/development/20-semantic-versioning.md.

Exit codes: 0 done (also when a channel gets no release), 1 refused or git failed,
2 invalid title or usage, 3 breaking change after 1.0.0 (manual major release only).
"""

import argparse
import json
import re
import subprocess  # nosec B404 — argv lists only, never a shell; every git call goes through _git()
import sys
from pathlib import Path
from typing import NamedTuple

TABLE = Path(__file__).resolve().parent / "semver-backfill.tsv"
LOG_FORMAT = "%H%x1f%P%x1f%s%x1f%b%x1e"
TITLE_RE = re.compile(r"(?P<type>[a-z]+)(\((?P<scope>[^()\s][^()]*)\))?(?P<bang>!)?: (?P<desc>\S.*)")
MERGE_RE = re.compile(r"^Merge pull request #(\d+) from ([^/\s]+)/(\S+)$")
SQUASH_RE = re.compile(r"^(.+) \(#(\d+)\)$")
RELEASE_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
DEV_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-dev\.([1-9]\d*)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
LEVELS = ("none", "patch", "minor", "major")
BUMP = {
    "feat": "minor",
    "fix": "patch",
    "perf": "patch",
    "security": "patch",
    "revert": "patch",
    "docs": "none",
    "chore": "none",
    "test": "none",
    "ci": "none",
    "refactor": "none",
    "build": "none",
    "style": "none",
    "plan": "none",
    "promote": "none",
}


class Version(NamedTuple):
    major: int
    minor: int
    patch: int

    def __str__(self):
        return f"{self.major}.{self.minor}.{self.patch}"


class InvalidTitle(ValueError):
    """A pull request title that is not in the Conventional Commits form."""


class NeedsMajor(RuntimeError):
    """A breaking change after 1.0.0: only the manual major release may cut it."""


def _git(repo, *args, check=True):
    return subprocess.run(  # nosec B603 B607 — fixed argv, and git is the only program run
        ["git", "-C", str(repo), *args], check=check, text=True, capture_output=True
    )


def classify_title(title):
    """The bump one pull request title asks for. Raises InvalidTitle for a title not in the form."""
    # fullmatch, not match: match would accept a valid title followed by trailing text such as a newline.
    match = TITLE_RE.fullmatch(title)
    if match is None:
        raise InvalidTitle(
            "want type(scope)!: description, a lower-case type, one space after the colon, "
            "and a non-empty description"
        )
    kind = match["type"]
    if kind not in BUMP:
        raise InvalidTitle(f"unknown type {kind!r}; use one of {', '.join(BUMP)}")
    return "major" if match["bang"] else BUMP[kind]


def _level_of_history(title):
    """A title already merged cannot fail a release, so an unreadable one counts as no bump."""
    try:
        return classify_title(title)
    except InvalidTitle:
        return "none"


def _highest(levels):
    return max(levels, key=LEVELS.index, default="none")


def apply(base, level):
    """The version one bump level gives on top of base. A breaking change on 0.x is a minor bump."""
    if level == "major":
        if base.major >= 1:
            raise NeedsMajor
        level = "minor"
    if level == "minor":
        return Version(base.major, base.minor + 1, 0)
    if level == "patch":
        return Version(base.major, base.minor, base.patch + 1)
    raise ValueError(f"no version for level {level!r}")


def _release_version(tag):
    """The Version a release tag names. The tag must already match RELEASE_RE."""
    return Version(*(int(part) for part in RELEASE_RE.match(tag).groups()))


def _release_tags(repo):
    """Every release tag in the repository as (version, tag), oldest first. Every tag counts, see last_release."""
    found = []
    for tag in _git(repo, "tag", "--list", "v*").stdout.split():
        if RELEASE_RE.match(tag) is not None:
            found.append((_release_version(tag), tag))
    return sorted(found)


def last_release(repo):
    """The highest release tag in the repository, as (version, tag); ((0, 0, 0), None) when there is none.

    Every tag counts, not only those merged into a ref: release tags are made only on main, and a merge into
    dev can land before the main-to-dev sync brings the promotion into dev. Counting only the tags reachable
    from dev would start that merge from an older release and tag it below the version already shipped.
    """
    found = _release_tags(repo)
    return found[-1] if found else (Version(0, 0, 0), None)


def _next_major(base):
    """The next major version after base: v1.0.0 from 0.x, otherwise one above base's major."""
    return "v1.0.0" if base.major == 0 else f"v{base.major + 1}.0.0"


def _records(output):
    """(sha, parents, subject, body) for each commit in LOG_FORMAT output."""
    for chunk in output.split("\x1e"):
        chunk = chunk.strip("\n")
        if chunk:
            sha, parents, subject, body = chunk.split("\x1f", 3)
            yield sha, parents.split(), subject, body


def _pull_request(parents, subject, body):
    """(number, title) when a commit is a pull request merge or a squash merge, else None."""
    # The main-to-dev sync merge ("Merge remote-tracking branch 'origin/main' into dev") matches neither form.
    merge = MERGE_RE.match(subject)
    if merge and len(parents) > 1:
        if merge[3] == "dev":  # a promotion (dev to main) is not a pull request of its own
            return None
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        return int(merge[1]), (lines[0] if lines else "")
    squash = SQUASH_RE.match(subject)
    if squash and len(parents) == 1:
        return int(squash[2]), squash[1]
    return None


def _pull_requests(repo, rev_range, first_parent):
    """(sha, number, title) for each pull request commit in rev_range, oldest first."""
    args = ["log", "--reverse", f"--format={LOG_FORMAT}"]
    if first_parent:
        args.append("--first-parent")
    found = []
    for sha, parents, subject, body in _records(_git(repo, *args, rev_range).stdout):
        pull = _pull_request(parents, subject, body)
        if pull:
            found.append((sha, *pull))
    return found


def pr_titles(repo, rev_range, first_parent):
    """(number, title) for each pull request in rev_range, oldest first."""
    return [(number, title) for _sha, number, title in _pull_requests(repo, rev_range, first_parent)]


def _notes(repo, start, end):
    """The pull requests from start to end as Markdown list lines, oldest first; empty when there are none."""
    return "".join(f"- #{number} {title}\n" for number, title in pr_titles(repo, f"{start}..{end}", first_parent=False))


def _main_version(repo, ref):
    """The version main's next release takes at ref, or None: cmd_next's rule for channel main, with no title."""
    base, tag = last_release(repo)
    rev_range = f"{tag}..{ref}" if tag else ref
    level = _highest([_level_of_history(title) for _number, title in pr_titles(repo, rev_range, first_parent=False)])
    return None if level == "none" else apply(base, level)


def _dev_max(repo, version):
    """Highest -dev.N already tagged for this version on any branch; 0 when there is none."""
    pattern = re.compile(rf"^v{re.escape(str(version))}-dev\.([1-9]\d*)$")
    numbers = [0]
    for tag in _git(repo, "tag", "--list", f"v{version}-dev.*").stdout.split():
        match = pattern.match(tag)
        if match:
            numbers.append(int(match[1]))
    return max(numbers)


def dev_number(repo, version):
    return _dev_max(repo, version) + 1


def _dev_tagged(repo, sha):
    return any(DEV_RE.match(tag) for tag in _git(repo, "tag", "--points-at", sha).stdout.split())


def dev_tags(repo, ref):
    """(tag, sha) for each untagged pull request on ref's first-parent chain, oldest first.

    Catch-up runs after the newest dev-tagged pull request. When no pull request on the chain
    carries a dev tag, only ref itself is offered, and only when it is a pull request. Each
    version is cumulative over the whole chain since the last release. Computes only.
    """
    base, tag = last_release(repo)
    rev_range = f"{tag}..{ref}" if tag else ref
    chain = [
        (sha, _level_of_history(title))
        for sha, _number, title in _pull_requests(repo, rev_range, first_parent=True)
    ]
    newest = next((i for i in reversed(range(len(chain))) if _dev_tagged(repo, chain[i][0])), None)
    if newest is not None:
        pending = list(range(newest + 1, len(chain)))
    else:
        head = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()
        pending = [len(chain) - 1] if chain and chain[-1][0] == head else []
    cumulative = []
    running = "none"
    for _sha, level in chain:
        running = _highest([running, level])
        cumulative.append(running)
    assigned = {}
    lines = []
    for index in pending:
        version = apply(base, _highest([cumulative[index], "patch"]))
        number = max(_dev_max(repo, version), assigned.get(str(version), 0)) + 1
        assigned[str(version)] = number
        lines.append((f"v{version}-dev.{number}", chain[index][0]))
    return lines


def read_table(path):
    """(version, commit, pr) rows of the backfill table; raises ValueError on a malformed row."""
    lines = path.read_text().splitlines()
    if not lines or lines[0] != "version\tcommit\tpr":
        raise ValueError(f"{path}: first line must be the header version, commit, pr (tab-separated)")
    rows = []
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        fields = line.split("\t")
        well_formed = (
            len(fields) == 3
            and RELEASE_RE.match(fields[0]) is not None
            and COMMIT_RE.match(fields[1]) is not None
            and fields[2].isdigit()
        )
        if not well_formed:
            raise ValueError(f"{path}:{number}: want vX.Y.Z, a 40-hex commit and a PR number: {line!r}")
        rows.append((fields[0], fields[1], int(fields[2])))
    return rows


def cmd_title(_repo, args):
    print(classify_title(args.title))
    return 0


def cmd_next(repo, args):
    base, tag = last_release(repo)
    rev_range = f"{tag}..{args.ref}" if tag else args.ref
    prs = pr_titles(repo, rev_range, first_parent=args.channel == "dev")
    level = _highest([_level_of_history(title) for _number, title in prs])
    if args.title is not None:
        level = _highest([level, classify_title(args.title)])
    if args.channel == "dev":
        level = _highest([level, "patch"])
    if level == "none":
        return 0
    version = apply(base, level)
    if args.channel == "main":
        print(f"v{version}")
    else:
        print(f"v{version}-dev.{dev_number(repo, version)}")
    return 0


def cmd_dev_tags(repo, args):
    for tag, sha in dev_tags(repo, args.ref):
        print(f"{tag} {sha}")
    return 0


def cmd_notes(repo, args):
    print(_notes(repo, args.start, args.end), end="")
    return 0


def _backfill_plan(repo, table):
    """(refusals, plan) for the backfill table. Plan rows are (version, commit, pr, state), state exists or missing."""
    refusals, plan = [], []
    for version, commit, pr in read_table(table):
        probe = _git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{version}^{{commit}}", check=False)
        if probe.returncode == 0:
            if probe.stdout.strip() == commit:
                plan.append((version, commit, pr, "exists"))
            else:
                refusals.append(
                    f"{version} points at {probe.stdout.strip()}, the table says {commit}; tags are never moved"
                )
        elif _git(repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode != 0:
            refusals.append(f"{version}: commit {commit} is not in this repository")
        else:
            plan.append((version, commit, pr, "missing"))
    return refusals, plan


def _backfill_message(version, pr):
    return f"agent-cloud {version} (promotion #{pr}, backfilled)"


def _commit_of(repo, ref):
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def _create_tags(repo, tags, created):
    """Create annotated (tag, commit, message) tags, all or none: every name is checked before the first is made.

    A tag that already exists is refused, never moved. Each tag made is appended to created as {tag, commit}.
    """
    for tag, _commit, _message in tags:
        if _git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode == 0:
            raise ValueError(f"{tag} already exists locally; tags are never moved")
    for tag, commit, message in tags:
        _git(repo, "-c", "tag.gpgsign=false", "tag", "-a", tag, "-m", message, commit)
        created.append({"tag": tag, "commit": commit})


def cmd_backfill(repo, args):
    refusals, plan = _backfill_plan(repo, args.table)
    if refusals:
        for line in refusals:
            print(line, file=sys.stderr)
        return 1
    for version, commit, pr, state in plan:
        if state == "exists":
            print(f"exists {version}")
        elif not args.apply:
            print(f"would create {version}")
        else:
            _git(repo, "-c", "tag.gpgsign=false", "tag", "-a", version, "-m", _backfill_message(version, pr), commit)
            print(f"created {version}")
            if args.push:
                _git(repo, "push", "origin", f"refs/tags/{version}")
                print(f"pushed {version}")
    return 0


def cmd_release_tags(repo, args):
    """Create the tags a release owes in this local clone, then print the plan as one JSON object. Never pushes.

    Scheduled (no --major): backfill the table, tag main's next version at --main-ref, then tag each dev pull
    request still owed on --dev-ref. Dev counts from the release just made, so main goes first.
    Major (--major VERSION): backfill, then tag VERSION at --main-ref when it is the next major. No dev tags.
    The caller pushes the tags it reads from stdout. This command only plans them, in a throwaway clone.
    """
    if args.major is not None and re.fullmatch(r"v[0-9]+\.0\.0", args.major) is None:
        print(f"error: --major wants vN.0.0, got {args.major!r}", file=sys.stderr)
        return 1
    refusals, plan = _backfill_plan(repo, args.table)
    if refusals:
        for line in refusals:
            print(line, file=sys.stderr)
        return 1
    if args.major is not None:
        # After the backfill, the release tags are the existing ones plus the table's: decide before creating anything.
        highest = max([last_release(repo)[0], *(_release_version(version) for version, _c, _p, _s in plan)])
        expected = _next_major(highest)
        if args.major != expected:
            print(f"computed {expected}, operator expected {args.major}; nothing tagged", file=sys.stderr)
            return 1
    created = []
    missing = [(version, commit, pr) for version, commit, pr, state in plan if state == "missing"]
    _create_tags(repo, [(version, commit, _backfill_message(version, pr)) for version, commit, pr in missing], created)
    if args.major is not None:
        _create_tags(repo, [(args.major, _commit_of(repo, args.main_ref), f"agent-cloud {args.major}")], created)
    else:
        version = _main_version(repo, args.main_ref)
        if version is not None:
            _create_tags(repo, [(f"v{version}", _commit_of(repo, args.main_ref), f"agent-cloud v{version}")], created)
        _create_tags(repo, [(tag, sha, f"agent-cloud {tag}") for tag, sha in dev_tags(repo, args.dev_ref)], created)
    releases = _release_tags(repo)
    release = None
    if releases:
        tag = releases[-1][1]
        previous = releases[-2][1] if len(releases) > 1 else None
        release = {"tag": tag, "previous": previous, "notes": _notes(repo, previous, tag) if previous else ""}
    print(json.dumps({"tags": created, "release": release}))
    return 0


def cmd_major(repo, args):
    base, _tag = last_release(repo)
    print(_next_major(base))
    return 0


HANDLERS = {
    "title": cmd_title,
    "next": cmd_next,
    "dev-tags": cmd_dev_tags,
    "notes": cmd_notes,
    "backfill": cmd_backfill,
    "release-tags": cmd_release_tags,
    "major": cmd_major,
}


def _parser():
    parser = argparse.ArgumentParser(prog="semver.py", description="Semantic versions from pull request titles.")
    parser.add_argument("--repo", default=".", help="repository to read (default: the current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    title = commands.add_parser("title", help="the bump for one pull request title")
    title.add_argument("title")
    nxt = commands.add_parser("next", help="the tag a channel gets at REF")
    nxt.add_argument("--channel", choices=("dev", "main"), required=True)
    nxt.add_argument("--ref", default="HEAD")
    nxt.add_argument("--title", help="one more pull request, not yet merged")
    dev_tags_cmd = commands.add_parser("dev-tags", help="the dev tags still owed on REF's first-parent chain")
    dev_tags_cmd.add_argument("--ref", default="HEAD")
    notes = commands.add_parser("notes", help="pull request titles in a range, as a Markdown list")
    notes.add_argument("--from", dest="start", required=True, metavar="TAG")
    notes.add_argument("--to", dest="end", required=True, metavar="REF")
    backfill = commands.add_parser("backfill", help="create the committed history tags that are missing")
    backfill.add_argument("--apply", action="store_true")
    backfill.add_argument("--push", action="store_true", help="push each created tag to origin (needs --apply)")
    backfill.add_argument("--table", type=Path, default=TABLE, help=argparse.SUPPRESS)
    release = commands.add_parser(
        "release-tags",
        help="create the release tags owed in this local clone and print the plan as JSON; never pushes",
        description=(
            "Creates the owed tags LOCALLY in the repository given by --repo (a throwaway clone) and prints one JSON "
            "object: the tags created, in order, and the release they make up. It never pushes; the caller pushes "
            "the tags it reads from the output."
        ),
    )
    release.add_argument("--main-ref", default="origin/main", help="the main ref whose next release is tagged")
    release.add_argument("--dev-ref", default="origin/dev", help="the dev ref whose owed -dev tags are created")
    release.add_argument(
        "--major", metavar="VERSION", help="tag VERSION (vN.0.0) at --main-ref as the manual major release"
    )
    release.add_argument("--table", type=Path, default=TABLE, help=argparse.SUPPRESS)
    commands.add_parser("major", help="the next major version; computes only, never tags")
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "backfill" and args.push and not args.apply:
        parser.error("--push needs --apply")
    try:
        return HANDLERS[args.command](args.repo, args)
    except InvalidTitle as err:
        print(f"invalid title: {err}", file=sys.stderr)
        return 2
    except NeedsMajor:
        print("breaking change after 1.0.0 needs the manual major release", file=sys.stderr)
        return 3
    except subprocess.CalledProcessError as err:
        print(f"git failed: {(err.stderr or '').strip() or err}", file=sys.stderr)
        return 1
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
