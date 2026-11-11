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
from collections import Counter
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
    carries a dev tag, the answer depends on whether the repository has any dev tag at all.
    With none (until the first dev tag exists), only ref itself is offered, and only when it is a pull
    request, so the first run does not tag every merge since the last release. With some, the
    chain simply starts after a release that has no dev tag beyond it yet, so every pull request
    on it is offered: a run that cuts a main release must not strand the dev merges that landed
    since. Each version is cumulative over the whole chain since the last release. Computes only.
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
    elif any(DEV_RE.match(name) for name in _git(repo, "tag", "--list", "v*-dev.*").stdout.split()):
        pending = list(range(len(chain)))
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


CATEGORY_OF = {"feat": "features", "perf": "features", "fix": "fixes", "security": "fixes", "docs": "documentation"}
HEADINGS = (
    ("features", "Features"),
    ("fixes", "Fixes"),
    ("documentation", "Documentation Updates"),
    ("other", "Other changes"),
)
SUMMARY_KEYS = ("features", "fixes", "documentation")
SUMMARY_MAX = 800
SUMMARY_BANNED = ("@", "http://", "https://", "](", "<", ">", "`")
# Model text (summaries, the overview, highlight text) may hold only these characters. Every other mark is Markdown
# or HTML syntax, a URL, or a reference (#N, owner/repo#N), so the text is dropped rather than rendered. A GH-N
# reference is letters and digits, so it passes this set and is refused separately, in _unsafe_text_problem.
SAFE_TEXT = re.compile(r"[\w\s.,;:'\"()!?%+=–—‘’“”-]+")
HIGHLIGHT_MAX = 200
HIGHLIGHTS_SHOWN = 5
INPUT_KEYS = (*SUMMARY_KEYS, "overview", "highlights")


def _previous_release(releases, tag):
    """The release tag just below tag, from _release_tags' list; None for the first release. Dev tags never count."""
    target = _release_version(tag)
    lower = [name for version, name in releases if version < target]
    return lower[-1] if lower else None


def _category(title):
    """The section a pull request title files under, by its type. A type not listed, or no title form, is other."""
    match = TITLE_RE.fullmatch(title)
    return CATEGORY_OF.get(match["type"], "other") if match else "other"


def _is_breaking(title):
    """True when the title carries the breaking mark (!) on any type. A title outside the form is never breaking."""
    match = TITLE_RE.fullmatch(title)
    return match is not None and match["bang"] is not None


def _sections(prs):
    """The pull requests in each section, oldest first. Every pull request lands in exactly one section."""
    sections = {"features": [], "fixes": [], "documentation": [], "other": []}
    for number, title in prs:
        sections[_category(title)].append((number, title))
    return sections


def _contributors(repo, start, end):
    """(name, commits) per author of a non-merge commit in start..end: most commits first, then by name. Names only."""
    names = _git(repo, "log", "--no-merges", "--format=%an", f"{start}..{end}").stdout.splitlines()
    counts = Counter(name for name in names if name)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _banned_problem(text):
    """The first banned mark in text as the rule it breaks, or None when there is none."""
    for mark in SUMMARY_BANNED:
        if mark in text:
            return f"contains {mark!r}"
    return None


def _unsafe_text_problem(text):
    """The rule model text breaks: www. in any case, a GH- reference, or a character outside SAFE_TEXT. None if none."""
    if "www." in text.casefold():
        return "contains 'www.'"
    # An ASCII lookbehind, not \b: Python counts _, é and Α as word characters, so \b misses _GH-1_ and éGH-1, which
    # GitHub still links. High-5 is not a reference; x_GH-13 is refused too, which only drops a summary. No (?i): with
    # it, [a-z] also matches the long s, the Kelvin sign and the dotless and dotted i, so the case is spelled out.
    if re.search(r"(?<![A-Za-z0-9])[gG][hH]-[0-9]", text):
        return "contains a GH- reference"
    if SAFE_TEXT.fullmatch(text) is None:
        return "contains a character outside the allowed set"
    return None


def _summary_problem(text):
    """The rule a model summary breaks, or None if it may be rendered. A rejected summary is dropped, never repaired."""
    stripped = text.strip()
    if not 1 <= len(stripped) <= SUMMARY_MAX:
        return f"not 1 to {SUMMARY_MAX} characters after strip"
    problem = _banned_problem(stripped)
    if problem is not None:
        return problem
    if any(line.lstrip().startswith("#") for line in stripped.splitlines()):
        return "a line starts with '#'"
    return _unsafe_text_problem(stripped)


def _highlight_text_problem(text):
    """The rule a highlight's text breaks, or None. Newlines are collapsed by the caller, not rejected."""
    stripped = text.strip()
    if not 1 <= len(stripped) <= HIGHLIGHT_MAX:
        return f"not 1 to {HIGHLIGHT_MAX} characters after strip"
    problem = _banned_problem(stripped)
    if problem is not None:
        return problem
    return _unsafe_text_problem(stripped)


def _highlight_rule(item, in_release, breaking_numbers, seen):
    """The rule one highlight breaks, or None if it may be shown. A rejected highlight is dropped, never repaired."""
    if not isinstance(item, dict):
        return "not an object"
    number, text = item.get("number"), item.get("text")
    if not isinstance(number, int) or isinstance(number, bool):
        return "number is not an integer"
    if number not in in_release:
        return "number is not a pull request in this release"
    if number in breaking_numbers:
        return "a breaking change, already under Major Updates"
    if not isinstance(text, str):
        return "text is not a string"
    problem = _highlight_text_problem(text)
    if problem is not None:
        return problem
    if number in seen:
        return "a duplicate of an earlier highlight"
    return None


def _read_highlights(value, sections, breaking):
    """The highlights to show, in input order, at most HIGHLIGHTS_SHOWN. Each rejection prints one stderr line."""
    if not isinstance(value, list):
        print("highlights dropped: not a list", file=sys.stderr)
        return []
    in_release = {number for items in sections.values() for number, _title in items}
    breaking_numbers = {number for number, _title in breaking}
    accepted = []
    for index, item in enumerate(value, start=1):
        seen = {number for number, _text in accepted}
        rule = _highlight_rule(item, in_release, breaking_numbers, seen)
        if rule is not None:
            print(f"highlight {index} dropped: {rule}", file=sys.stderr)
            continue
        accepted.append((item["number"], " ".join(item["text"].split())))
    if len(accepted) > HIGHLIGHTS_SHOWN:
        extra = len(accepted) - HIGHLIGHTS_SHOWN
        print(f"highlights: {extra} valid beyond the first {HIGHLIGHTS_SHOWN} not shown", file=sys.stderr)
    return accepted[:HIGHLIGHTS_SHOWN]


def _read_input(raw, sections, breaking):
    """The validated model input: summaries, an overview and highlights. Each rejection prints one stderr line."""
    model = {"summaries": {}, "overview": None, "highlights": []}
    if not raw.strip():
        return model
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict) or not set(data) <= set(INPUT_KEYS):
        print(
            "release-notes: summaries ignored: want a JSON object with keys "
            "features, fixes, documentation, overview, highlights",
            file=sys.stderr,
        )
        return model
    for key in (*SUMMARY_KEYS, "overview"):
        if key not in data:
            continue
        value = data[key]
        problem = "not a string" if not isinstance(value, str) else _summary_problem(value)
        if problem is not None:
            print(f"{key} summary dropped: {problem}", file=sys.stderr)
        elif key == "overview":
            model["overview"] = " ".join(value.split())
        else:
            model["summaries"][key] = " ".join(value.split())
    if "highlights" in data:
        model["highlights"] = _read_highlights(data["highlights"], sections, breaking)
    return model


def _count(number, singular, plural=None):
    """The number and its noun, singular at one. The plural defaults to the singular plus s."""
    if number == 1:
        return f"1 {singular}"
    return f"{number} {plural or singular + 's'}"


def _neutral(text):
    """Keep a title or a name from notifying anyone: a zero-width space after each @, so GitHub does not link it."""
    return text.replace("@", "@​")


# HTML first (the & of each entity must not be escaped again), then Markdown marks. The backslash comes first in the
# Markdown tuple so the backslashes added for the other marks are not doubled.
ESCAPED_HTML = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))
ESCAPED_MARKDOWN = ("\\", "`", "*", "_", "[", "]", "~", "|")


def _escape(text):
    """Keep a title or a name from formatting, linking, rendering math, closing its block or notifying anyone."""
    for mark, entity in ESCAPED_HTML:
        text = text.replace(mark, entity)
    for mark in ESCAPED_MARKDOWN:
        text = text.replace(mark, "\\" + mark)
    # A zero-width space breaks each URL scheme separator and each www. that GitHub would autolink. The $ becomes an
    # inline HTML span, which GitHub's math renderer does not read as a delimiter. Both come after the HTML escape,
    # so the span is not escaped itself.
    text = text.replace("://", ":\u200b//")
    text = re.sub(r"(?i)(www)\.", "\\1\u200b.", text)
    text = text.replace("$", "<span>$</span>")
    return _neutral(text)


def _escape_name(name):
    """A contributor name at the start of a list item: _escape, then no heading, list or #N link at its start."""
    text = _escape(name).replace("#", "#\u200b")  # a #N in a name would link to a pull request
    text = re.sub(r"^(\d+)([.)])", r"\1\\\2", text)  # 1. and 1) would open an ordered list
    if text[:1] in ("-", "+", "="):  # a bullet, a setext underline or a rule would open a block
        text = "\\" + text
    return text


def _render(previous, sections, breaking, contributors, model):
    """The Markdown body: Summary, Major Updates, the sections, Contributors. The first release gets one line."""
    if previous is None:
        return "First release.\n"
    counts = {key: len(items) for key, items in sections.items()}
    parts = ["## Summary"]
    if model["overview"] is not None:
        parts.append(model["overview"])
    parts.append(
        f"{_count(sum(counts.values()), 'pull request')} since {previous}: "
        f"{_count(counts['features'], 'feature', 'features')}, "
        f"{_count(counts['fixes'], 'fix', 'fixes')}, "
        f"{_count(counts['documentation'], 'documentation update')}, "
        f"{_count(counts['other'], 'other change')}."
    )
    lines = [f"- #{number} {_escape(title)} (breaking change)" for number, title in breaking]
    lines += [f"- #{number} {text}" for number, text in model["highlights"]]
    parts.append("## Major Updates")
    parts.append("\n".join(lines) if lines else "No breaking changes in this release.")
    for key, heading in HEADINGS:
        items = sections[key]
        if not items:
            continue
        parts.append(f"## {heading}")
        if key in model["summaries"]:
            parts.append(model["summaries"][key])
        lines = "\n".join(f"- #{number} {_escape(title)}" for number, title in items)
        parts.append(f"<details><summary>{_count(len(items), 'pull request')}</summary>\n\n{lines}\n\n</details>")
    if contributors:
        lines = "\n".join(f"- {_escape_name(name)} ({_count(commits, 'commit')})" for name, commits in contributors)
        parts.append(f"## Contributors\n\n{lines}")
    return "\n\n".join(parts) + "\n"


def cmd_release_notes(repo, args):
    """One release described against the release before it: --sections prints JSON, --render prints Markdown."""
    if args.sections == args.render:  # both, or neither
        print("release-notes: give exactly one of --sections or --render", file=sys.stderr)
        return 1
    if args.tag is None:
        print("release-notes: --tag is required", file=sys.stderr)
        return 1
    if RELEASE_RE.fullmatch(args.tag) is None:
        print(f"release-notes: --tag wants vX.Y.Z, got {args.tag!r}", file=sys.stderr)
        return 1
    releases = _release_tags(repo)
    if args.tag not in [name for _version, name in releases]:
        print(f"release-notes: no release tag {args.tag} in this repository", file=sys.stderr)
        return 1
    previous = _previous_release(releases, args.tag)
    prs = [] if previous is None else pr_titles(repo, f"{previous}..{args.tag}", first_parent=False)
    contributors = [] if previous is None else _contributors(repo, previous, args.tag)
    sections = _sections(prs)
    breaking = [(number, title) for number, title in prs if _is_breaking(title)]
    if not args.sections:
        model = _read_input(sys.stdin.read(), sections, breaking)
        print(_render(previous, sections, breaking, contributors, model), end="")
        return 0
    payload = {
        "tag": args.tag,
        "previous": previous,
        "counts": {key: len(items) for key, items in sections.items()},
        "breaking": [{"number": number, "title": title} for number, title in breaking],
        "sections": {
            key: [{"number": number, "title": title} for number, title in items] for key, items in sections.items()
        },
        "contributors": [{"name": name, "commits": commits} for name, commits in contributors],
    }
    print(json.dumps(payload))
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
    "release-notes": cmd_release_notes,
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
    release_notes = commands.add_parser(
        "release-notes",
        help="categorised notes for one release: JSON sections, or Markdown with summaries on stdin",
        description=(
            "Describes one release tag against the release before it. --sections prints one JSON object. --render "
            "prints Markdown and reads optional model input from stdin: a JSON object with keys features, fixes and "
            "documentation (summaries), overview, and highlights (a list of objects with number and text)."
        ),
    )
    release_notes.add_argument("--tag", help="the release tag to describe, vX.Y.Z (it must exist in the repository)")
    release_notes.add_argument("--sections", action="store_true", help="print the sections as one JSON object")
    release_notes.add_argument("--render", action="store_true", help="print Markdown; summaries are read from stdin")
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
