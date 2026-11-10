"""scripts/semver.py: bump rules, pull request parsing, tag numbering, catch-up and backfill.

Every git scenario runs on a throwaway repository under tmp_path. Commit dates advance one
second per commit, so history order never depends on a tie.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "semver.py"
TABLE = REPO / "scripts" / "semver-backfill.tsv"
EPOCH = 1_767_225_600  # 2026-01-01T00:00:00Z


def _load_script():
    # A unique module name: a bare `import semver` could pick up an installed PyPI package.
    spec = importlib.util.spec_from_file_location("semver_script_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


semver = _load_script()


@pytest.fixture(autouse=True)
def _clean_git_env(monkeypatch):
    # Git hooks export GIT_DIR and friends; inherited, they would aim these commands at another repository.
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


class Repo:
    """A throwaway repository with a commit clock, plus helpers that build pull request shapes."""

    def __init__(self, root, release="v0.1.0"):
        self.root = root
        self.clock = 0
        root.mkdir()
        self.git("init", "-q", "-b", "main")
        for key, value in (
            ("user.name", "t"),
            ("user.email", "t@example.invalid"),
            ("commit.gpgsign", "false"),
            ("tag.gpgsign", "false"),
        ):
            self.git("config", key, value)
        self.commit("chore: init")
        if release:
            self.tag(release)

    def git(self, *args, env=None):
        return subprocess.run(
            ["git", "-C", str(self.root), *args], check=True, text=True, capture_output=True, env=env
        ).stdout

    def _stamped(self):
        self.clock += 1
        stamp = f"{EPOCH + self.clock} +0000"
        return {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}

    def commit(self, subject):
        (self.root / f"file-{self.clock + 1}.txt").write_text(subject + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject, env=self._stamped())

    def head(self, ref="HEAD"):
        return self.git("rev-parse", ref).strip()

    def branch_name(self):
        return self.git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def checkout(self, name):
        self.git("checkout", "-q", name)

    def branch(self, name):
        self.git("checkout", "-q", "-b", name)

    def feature(self, name, subject):
        """Branch from here, commit once, and come back: a pull request branch before its merge."""
        here = self.branch_name()
        self.branch(name)
        self.commit(subject)
        self.checkout(here)

    def merge_raw(self, subject, body, branch):
        """A merge commit with this message; GitHub's format is subject, blank line, title."""
        self.git("merge", "-q", "--no-ff", "-m", subject, "-m", body, branch, env=self._stamped())

    def merge_pr(self, branch, number, title):
        self.merge_raw(f"Merge pull request #{number} from uhstray-io/{branch}", title, branch)

    def squash_pr(self, branch, number, title):
        self.git("merge", "-q", "--squash", branch)
        self.git("commit", "-q", "-m", f"{title} (#{number})", env=self._stamped())

    def tag(self, name, ref="HEAD"):
        self.git("tag", "-a", name, "-m", name, ref)


def _cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True, check=False)


def _run(repo, *args):
    return _cli("--repo", str(repo.root), *args)


def _dev_repo(tmp_path, release="v0.1.0"):
    """main at the release, with dev branched from it and checked out."""
    repo = Repo(tmp_path / "repo", release=release)
    repo.branch("dev")
    return repo


def _table(path, rows):
    path.write_text("version\tcommit\tpr\n" + "".join(f"{v}\t{c}\t{pr}\n" for v, c, pr in rows))


@pytest.mark.parametrize(
    ("title", "level"),
    [
        ("feat: add a thing", "minor"),
        ("feat(semaphore): add a template", "minor"),
        ("fix: stop the crash", "patch"),
        ("perf: cache the inventory", "patch"),
        ("security: pin the image", "patch"),
        ("revert: undo the change", "patch"),
        ("docs: explain the flag", "none"),
        ("chore(deps): bump a pin", "none"),
        ("test: cover the parser", "none"),
        ("ci: pin ruff", "none"),
        ("refactor: split the module", "none"),
        ("build: tidy the image", "none"),
        ("style: reformat", "none"),
        ("plan: add plan 20", "none"),
        ("promote: dev to main", "none"),
    ],
)
def test_each_type_maps_to_one_bump(title, level):
    assert semver.classify_title(title) == level


@pytest.mark.parametrize(
    "title",
    ["feat!: drop a route", "feat(api)!: drop a route", "docs!: rename the doc", "fix(x)!: y"],
)
def test_bang_marks_a_breaking_change_on_any_known_type(title):
    assert semver.classify_title(title) == "major"


@pytest.mark.parametrize(
    "title",
    [
        "feat:add",
        "feat : add",
        "feat:  add",
        "Feat: add",
        "feat add",
        "frobnicate: add",
        "feat(): add",
        "feat: ",
        "feat!add: x",
        "feat: add\n",
        "feat: add\nmore",
        "",
    ],
)
def test_titles_outside_the_form_are_refused(title):
    with pytest.raises(semver.InvalidTitle):
        semver.classify_title(title)


def test_title_command_prints_the_bump_and_refuses_a_bad_title():
    assert _cli("title", "feat(api)!: drop the old route").stdout == "major\n"
    assert _cli("title", "docs: explain").stdout == "none\n"
    bad = _cli("title", "Feat: x")
    assert (bad.returncode, bad.stdout) == (2, "")
    assert "invalid title" in bad.stderr


def test_zero_x_breaking_change_is_a_minor_bump(tmp_path):
    repo = Repo(tmp_path / "repo", release="v0.13.0")
    repo.branch("api")
    repo.commit("feat!: drop the old endpoint")
    repo.checkout("main")
    repo.merge_pr("api", 900, "feat!: drop the old endpoint")
    result = _run(repo, "next", "--channel", "main", "--ref", "main")
    assert (result.returncode, result.stdout) == (0, "v0.14.0\n")


def test_breaking_change_after_one_point_oh_stops_for_the_manual_major(tmp_path):
    repo = Repo(tmp_path / "repo", release="v1.2.3")
    repo.feature("api", "feat!: drop the old endpoint")
    repo.merge_pr("api", 901, "feat!: drop the old endpoint")
    result = _run(repo, "next", "--channel", "main", "--ref", "main")
    assert result.returncode == 3
    assert result.stdout == ""
    assert "manual major release" in result.stderr


def test_main_with_only_documentation_gets_no_tag(tmp_path):
    repo = Repo(tmp_path / "repo")
    repo.feature("docs", "docs: readme")
    repo.merge_pr("docs", 902, "docs: readme")
    assert _run(repo, "next", "--channel", "main", "--ref", "main").stdout == ""
    assert _run(repo, "next", "--channel", "main", "--ref", "main", "--title", "ci: pin").stdout == ""


def test_next_counts_one_more_title_before_it_is_merged(tmp_path):
    repo = Repo(tmp_path / "repo")
    result = _run(repo, "next", "--channel", "main", "--ref", "main", "--title", "feat: add")
    assert (result.returncode, result.stdout) == (0, "v0.2.0\n")
    bad = _run(repo, "next", "--channel", "main", "--ref", "main", "--title", "Feat: add")
    assert (bad.returncode, bad.stdout) == (2, "")


def test_unreadable_merged_title_counts_as_no_bump(tmp_path):
    repo = Repo(tmp_path / "repo")
    repo.feature("misc", "wip")
    repo.merge_pr("misc", 30, "Update stuff")
    assert _run(repo, "next", "--channel", "main", "--ref", "main").stdout == ""


def test_dev_gets_a_patch_candidate_even_when_nothing_bumps(tmp_path):
    repo = _dev_repo(tmp_path)
    repo.feature("docs", "docs: readme")
    repo.merge_pr("docs", 40, "docs: readme")
    result = _run(repo, "next", "--channel", "dev", "--ref", "dev")
    assert (result.returncode, result.stdout) == (0, "v0.1.1-dev.1\n")


def test_dev_numbering_counts_up_and_restarts_when_the_version_lifts(tmp_path):
    repo = _dev_repo(tmp_path)
    expected = ["v0.1.1-dev.1", "v0.1.1-dev.2", "v0.2.0-dev.1", "v0.2.0-dev.2"]
    for number, (branch, subject) in enumerate(
        [("fix-a", "fix: a"), ("fix-b", "fix: b"), ("feat-c", "feat: c"), ("fix-d", "fix: d")], start=10
    ):
        repo.feature(branch, subject)
        repo.merge_pr(branch, number, subject)
        result = _run(repo, "next", "--channel", "dev", "--ref", "dev")
        assert result.stdout == expected[number - 10] + "\n"
        repo.tag(expected[number - 10])


def test_dev_ignores_the_main_to_dev_sync_merge(tmp_path):
    repo = _dev_repo(tmp_path)
    repo.checkout("main")
    repo.commit("docs: main only")
    repo.checkout("dev")
    repo.merge_raw("Merge remote-tracking branch 'origin/main' into dev", "feat: sync title", "main")
    result = _run(repo, "next", "--channel", "dev", "--ref", "dev")
    assert result.stdout == "v0.1.1-dev.1\n"


def test_merge_and_squash_formats_are_both_read_and_promotions_are_skipped(tmp_path):
    repo = _dev_repo(tmp_path)
    repo.checkout("main")
    repo.commit("docs: main only")
    repo.checkout("dev")
    repo.feature("feat-a", "feat: a")
    repo.merge_pr("feat-a", 12, "feat: a")
    repo.merge_raw("Merge remote-tracking branch 'origin/main' into dev", "feat: sync title", "main")
    repo.feature("fix-b", "fix: b")
    repo.squash_pr("fix-b", 13, "fix: b")
    repo.checkout("main")
    repo.merge_raw("Merge pull request #50 from uhstray-io/dev", "feat: promoted", "dev")
    result = _run(repo, "notes", "--from", "v0.1.0", "--to", "main")
    assert (result.returncode, result.stdout) == (0, "- #12 feat: a\n- #13 fix: b\n")


def test_promotion_alone_does_not_count_on_main(tmp_path):
    repo = Repo(tmp_path / "repo")
    repo.branch("dev")
    repo.commit("fix: direct on dev")
    repo.checkout("main")
    repo.merge_raw("Merge pull request #51 from uhstray-io/dev", "feat: promoted", "dev")
    assert _run(repo, "next", "--channel", "main", "--ref", "main").stdout == ""


def test_dev_tags_catches_up_untagged_merges_in_order_with_n_counting(tmp_path):
    repo = _dev_repo(tmp_path)
    repo.feature("fix-a", "fix: a")
    repo.merge_pr("fix-a", 10, "fix: a")
    repo.tag("v0.1.1-dev.1")
    repo.feature("fix-b", "fix: b")
    repo.merge_pr("fix-b", 11, "fix: b")
    b = repo.head()
    repo.feature("feat-c", "feat: c")
    repo.merge_pr("feat-c", 12, "feat: c")
    c = repo.head()
    repo.feature("fix-d", "fix: d")
    repo.merge_pr("fix-d", 13, "fix: d")
    d = repo.head()
    result = _run(repo, "dev-tags", "--ref", "dev")
    assert result.returncode == 0
    assert result.stdout.splitlines() == [f"v0.1.1-dev.2 {b}", f"v0.2.0-dev.1 {c}", f"v0.2.0-dev.2 {d}"]
    assert repo.git("tag", "-l", "v0.*-dev.*") == "v0.1.1-dev.1\n"  # computes only, tags nothing


def test_dev_tags_prints_nothing_once_every_merge_is_tagged(tmp_path):
    repo = _dev_repo(tmp_path)
    repo.feature("fix-a", "fix: a")
    repo.merge_pr("fix-a", 10, "fix: a")
    repo.feature("fix-b", "fix: b")
    repo.merge_pr("fix-b", 11, "fix: b")
    first = _run(repo, "dev-tags", "--ref", "dev").stdout.splitlines()
    assert len(first) == 1
    for line in first:
        name, sha = line.split()
        repo.tag(name, sha)
    rerun = _run(repo, "dev-tags", "--ref", "dev")
    assert (rerun.returncode, rerun.stdout) == (0, "")


def test_dev_tags_restarts_numbering_when_a_feature_lifts_the_version(tmp_path):
    repo = _dev_repo(tmp_path)
    repo.feature("fix-0", "fix: zero")
    repo.merge_pr("fix-0", 20, "fix: zero")
    repo.tag("v0.1.1-dev.1")
    repo.feature("fix-a", "fix: a")
    repo.merge_pr("fix-a", 21, "fix: a")
    a = repo.head()
    repo.feature("fix-b", "fix: b")
    repo.merge_pr("fix-b", 22, "fix: b")
    b = repo.head()
    repo.feature("feat-c", "feat: c")
    repo.merge_pr("feat-c", 23, "feat: c")
    c = repo.head()
    result = _run(repo, "dev-tags", "--ref", "dev")
    assert result.stdout.splitlines() == [f"v0.1.1-dev.2 {a}", f"v0.1.1-dev.3 {b}", f"v0.2.0-dev.1 {c}"]


def test_dev_tags_skips_the_main_to_dev_sync_merge(tmp_path):
    repo = _dev_repo(tmp_path)
    repo.feature("fix-a", "fix: a")
    repo.merge_pr("fix-a", 30, "fix: a")
    repo.tag("v0.1.1-dev.1")
    repo.checkout("main")
    repo.commit("docs: main only")
    repo.checkout("dev")
    repo.merge_raw("Merge remote-tracking branch 'origin/main' into dev", "feat: sync title", "main")
    repo.feature("fix-b", "fix: b")
    repo.merge_pr("fix-b", 31, "fix: b")
    b = repo.head()
    result = _run(repo, "dev-tags", "--ref", "dev")
    assert result.stdout.splitlines() == [f"v0.1.1-dev.2 {b}"]


def test_dev_tags_without_any_dev_tag_offers_only_the_ref_commit(tmp_path):
    repo = _dev_repo(tmp_path)
    repo.feature("fix-a", "fix: a")
    repo.merge_pr("fix-a", 40, "fix: a")
    repo.feature("fix-b", "fix: b")
    repo.squash_pr("fix-b", 41, "fix: b")
    repo.feature("feat-c", "feat: c")
    repo.merge_pr("feat-c", 42, "feat: c")
    head = repo.head()
    result = _run(repo, "dev-tags", "--ref", "dev")
    assert result.stdout.splitlines() == [f"v0.2.0-dev.1 {head}"]
    repo.commit("fix: direct commit on dev")
    assert _run(repo, "dev-tags", "--ref", "dev").stdout == ""


def test_dev_tags_refuses_a_breaking_change_after_one_point_oh(tmp_path):
    repo = _dev_repo(tmp_path, release="v1.2.3")
    repo.feature("fix-a", "fix: a")
    repo.merge_pr("fix-a", 50, "fix: a")
    repo.tag("v1.2.4-dev.1")
    repo.feature("api", "feat!: drop the old route")
    repo.merge_pr("api", 51, "feat!: drop the old route")
    result = _run(repo, "dev-tags", "--ref", "dev")
    assert (result.returncode, result.stdout) == (3, "")
    assert "manual major release" in result.stderr


def test_dev_counts_from_the_shipped_release_before_the_sync_brings_it_in(tmp_path):
    # The promotion is tagged v0.14.0 on main, and a merge lands on dev before the main-to-dev sync brings
    # the promotion into dev. The patch must count from v0.14.0, not from the release dev can reach (v0.13.0).
    repo = _dev_repo(tmp_path, release="v0.13.0")
    repo.feature("feat-a", "feat: a")
    repo.merge_pr("feat-a", 70, "feat: a")
    repo.checkout("main")
    repo.merge_raw("Merge pull request #71 from uhstray-io/dev", "feat: promoted", "dev")
    repo.tag("v0.14.0")
    repo.checkout("dev")
    repo.feature("fix-b", "fix: b")
    repo.merge_pr("fix-b", 72, "fix: b")
    b = repo.head()
    assert _run(repo, "dev-tags", "--ref", "dev").stdout.splitlines() == [f"v0.14.1-dev.1 {b}"]
    assert _run(repo, "next", "--channel", "dev", "--ref", "dev").stdout == "v0.14.1-dev.1\n"


def test_notes_lists_pull_requests_oldest_first(tmp_path):
    repo = Repo(tmp_path / "repo")
    repo.feature("one", "fix: one")
    repo.merge_pr("one", 60, "fix: one")
    repo.feature("two", "feat: two")
    repo.squash_pr("two", 61, "feat: two")
    repo.tag("v0.2.0")
    result = _run(repo, "notes", "--from", "v0.1.0", "--to", "v0.2.0")
    assert result.stdout == "- #60 fix: one\n- #61 feat: two\n"


def test_backfill_lists_creates_and_is_idempotent(tmp_path):
    repo = Repo(tmp_path / "repo", release=None)
    first = repo.head()
    repo.commit("feat: two")
    second = repo.head()
    table = tmp_path / "table.tsv"
    _table(table, [("v0.1.0", first, 60), ("v0.2.0", second, 74)])
    dry = _run(repo, "backfill", "--table", str(table))
    assert (dry.returncode, dry.stdout) == (0, "would create v0.1.0\nwould create v0.2.0\n")
    assert repo.git("tag", "-l") == ""
    applied = _run(repo, "backfill", "--apply", "--table", str(table))
    assert (applied.returncode, applied.stdout) == (0, "created v0.1.0\ncreated v0.2.0\n")
    assert repo.git("cat-file", "-t", "refs/tags/v0.1.0").strip() == "tag"  # annotated
    assert repo.head("v0.1.0^{commit}") == first
    rerun = _run(repo, "backfill", "--apply", "--table", str(table))
    assert (rerun.returncode, rerun.stdout) == (0, "exists v0.1.0\nexists v0.2.0\n")


def test_backfill_refuses_a_moved_tag_and_creates_nothing(tmp_path):
    repo = Repo(tmp_path / "repo", release=None)
    first = repo.head()
    repo.commit("feat: two")
    second = repo.head()
    repo.tag("v0.1.0", ref=second)  # the wrong place
    table = tmp_path / "table.tsv"
    _table(table, [("v0.1.0", first, 60), ("v0.2.0", second, 74)])
    result = _run(repo, "backfill", "--apply", "--table", str(table))
    assert result.returncode == 1
    assert "v0.1.0" in result.stderr
    assert repo.head("v0.1.0^{commit}") == second  # never moved
    assert repo.git("tag", "-l", "v0.2.0") == ""  # nothing created on a refusal


def test_push_needs_apply(tmp_path):
    repo = Repo(tmp_path / "repo")
    assert _run(repo, "backfill", "--push").returncode == 2


def test_backfill_table_lists_the_sixteen_promotions():
    assert TABLE.read_text().splitlines()[0] == "version\tcommit\tpr"
    rows = semver.read_table(TABLE)
    assert [(version, pr) for version, _commit, pr in rows] == [
        ("v0.1.0", 60), ("v0.2.0", 74), ("v0.3.0", 76), ("v0.4.0", 79),
        ("v0.4.1", 82), ("v0.5.0", 88), ("v0.5.1", 93), ("v0.5.2", 97),
        ("v0.6.0", 100), ("v0.7.0", 104), ("v0.8.0", 112), ("v0.9.0", 132),
        ("v0.10.0", 447), ("v0.11.0", 515), ("v0.12.0", 607), ("v0.13.0", 656),
    ]
    assert all(re.fullmatch(r"[0-9a-f]{40}", commit) for _version, commit, _pr in rows)


def test_major_is_v1_from_zero_and_the_next_major_after_it(tmp_path):
    repo = Repo(tmp_path / "repo", release="v0.13.0")
    assert _run(repo, "major").stdout == "v1.0.0\n"
    assert repo.git("tag", "-l", "v1.0.0") == ""  # computes only
    later = Repo(tmp_path / "later", release="v1.2.3")
    assert _run(later, "major").stdout == "v2.0.0\n"


def _release_run(repo, *args):
    """release-tags on a test repo with an empty backfill table, so only the scheduled or major tags are in play."""
    table = repo.root.parent / "empty-table.tsv"
    _table(table, [])
    return _run(repo, "release-tags", "--main-ref", "main", "--dev-ref", "dev", "--table", str(table), *args)


def _promotion_with_dev_merge(tmp_path):
    """main promoted to v0.2.0 by PR 80, and PR 81 merged on dev before the main-to-dev sync brings PR 80 in."""
    repo = _dev_repo(tmp_path)
    repo.checkout("main")
    repo.feature("feat-a", "feat: a")
    repo.merge_pr("feat-a", 80, "feat: a")
    repo.checkout("dev")
    repo.feature("fix-b", "fix: b")
    repo.merge_pr("fix-b", 81, "fix: b")
    return repo


def test_release_tags_tags_main_then_dev_and_dev_counts_from_the_new_release(tmp_path):
    repo = _promotion_with_dev_merge(tmp_path)
    main_head, dev_head = repo.head("main"), repo.head("dev")
    result = _release_run(repo)
    assert (result.returncode, result.stderr) == (0, "")
    payload = json.loads(result.stdout)  # one JSON object and nothing else on stdout
    assert payload["tags"] == [{"tag": "v0.2.0", "commit": main_head}, {"tag": "v0.2.1-dev.1", "commit": dev_head}]
    assert repo.head("v0.2.0^{commit}") == main_head
    assert repo.head("v0.2.1-dev.1^{commit}") == dev_head
    assert payload["release"] == {"tag": "v0.2.0", "previous": "v0.1.0", "notes": "- #80 feat: a\n"}


def test_release_tags_after_a_promotion_tags_every_dev_merge_since_even_when_the_head_is_the_sync(tmp_path):
    # PR #996 review: the run that cuts a main release moves the base past every existing dev tag. Two
    # pull requests merged on dev in one schedule window, then the main-to-dev sync made dev's head a
    # non-pull-request merge. Offering only the head (the first-run rule) would tag neither, for good.
    repo = _dev_repo(tmp_path)
    repo.feature("feat-a", "feat: a")
    repo.merge_pr("feat-a", 70, "feat: a")
    first = _release_run(repo)
    assert [t["tag"] for t in json.loads(first.stdout)["tags"]] == ["v0.2.0-dev.1"]
    repo.checkout("main")
    repo.merge_raw("Merge pull request #71 from uhstray-io/dev", "feat: promoted", "dev")
    repo.checkout("dev")
    repo.feature("fix-b", "fix: b")
    repo.merge_pr("fix-b", 72, "fix: b")
    b = repo.head()
    repo.feature("fix-c", "fix: c")
    repo.merge_pr("fix-c", 73, "fix: c")
    c = repo.head()
    repo.merge_raw("Merge remote-tracking branch 'origin/main' into dev", "feat: sync title", "main")
    result = _release_run(repo)
    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout)["tags"] == [
        {"tag": "v0.2.0", "commit": repo.head("main")},
        {"tag": "v0.2.1-dev.1", "commit": b},
        {"tag": "v0.2.1-dev.2", "commit": c},
    ]


def test_release_tags_rerun_creates_nothing_and_still_reports_the_newest_release(tmp_path):
    repo = _promotion_with_dev_merge(tmp_path)
    assert _release_run(repo).returncode == 0
    rerun = _release_run(repo)
    assert (rerun.returncode, rerun.stderr) == (0, "")
    assert json.loads(rerun.stdout) == {
        "tags": [],
        "release": {"tag": "v0.2.0", "previous": "v0.1.0", "notes": "- #80 feat: a\n"},
    }


def test_release_tags_names_the_previous_release_and_lists_its_pull_requests_oldest_first(tmp_path):
    repo = _dev_repo(tmp_path)
    repo.checkout("main")
    repo.commit("chore: a plain commit on main")
    repo.tag("v0.1.1")  # the previous release is this one, not v0.1.0
    repo.feature("one", "fix: one")
    repo.merge_pr("one", 60, "fix: one")
    repo.feature("two", "feat: two")
    repo.squash_pr("two", 61, "feat: two")
    payload = json.loads(_release_run(repo).stdout)
    assert payload["tags"][0] == {"tag": "v0.2.0", "commit": repo.head("main")}
    assert payload["release"] == {
        "tag": "v0.2.0",
        "previous": "v0.1.1",
        "notes": "- #60 fix: one\n- #61 feat: two\n",
    }


def test_release_tags_first_release_has_no_previous_and_no_notes(tmp_path):
    repo = Repo(tmp_path / "repo", release=None)
    repo.branch("dev")  # dev stays at the first commit, so it owes no dev tag
    repo.checkout("main")
    repo.feature("one", "feat: one")
    repo.merge_pr("one", 5, "feat: one")
    result = _release_run(repo)
    assert result.returncode == 0
    assert json.loads(result.stdout)["release"] == {"tag": "v0.1.0", "previous": None, "notes": ""}


def test_release_tags_backfill_comes_first_and_counts_toward_the_next_version(tmp_path):
    repo = Repo(tmp_path / "repo", release=None)
    first = repo.head()
    repo.branch("dev")  # dev stays at the first commit, so it owes no dev tag
    repo.checkout("main")
    repo.feature("one", "feat: one")
    repo.merge_pr("one", 5, "feat: one")
    table = tmp_path / "table.tsv"
    _table(table, [("v0.1.0", first, 60)])
    result = _run(repo, "release-tags", "--main-ref", "main", "--dev-ref", "dev", "--table", str(table))
    assert result.returncode == 0
    assert [entry["tag"] for entry in json.loads(result.stdout)["tags"]] == ["v0.1.0", "v0.2.0"]
    assert repo.git("cat-file", "-t", "refs/tags/v0.1.0").strip() == "tag"  # annotated, as backfill makes them


def test_release_tags_stops_for_the_manual_major_and_tags_nothing(tmp_path):
    repo = _dev_repo(tmp_path, release="v1.2.3")
    repo.checkout("main")
    repo.feature("api", "feat!: drop the old endpoint")
    repo.merge_pr("api", 92, "feat!: drop the old endpoint")
    result = _release_run(repo)
    assert (result.returncode, result.stdout) == (3, "")
    assert "manual major release" in result.stderr
    assert repo.git("tag", "-l", "v1.*") == "v1.2.3\n"


def test_release_tags_major_tags_the_next_major_at_main_and_no_dev_tags(tmp_path):
    repo = _dev_repo(tmp_path, release="v0.13.0")
    repo.checkout("main")
    repo.feature("api", "feat!: drop the old route")
    repo.merge_pr("api", 90, "feat!: drop the old route")
    repo.checkout("dev")
    repo.feature("fix-b", "fix: b")
    repo.merge_pr("fix-b", 91, "fix: b")
    main_head = repo.head("main")
    result = _release_run(repo, "--major", "v1.0.0")
    assert (result.returncode, result.stderr) == (0, "")
    payload = json.loads(result.stdout)
    assert payload["tags"] == [{"tag": "v1.0.0", "commit": main_head}]  # the dev merge owes a dev tag, not made
    assert payload["release"] == {"tag": "v1.0.0", "previous": "v0.13.0", "notes": "- #90 feat!: drop the old route\n"}


def test_release_tags_major_refuses_a_version_other_than_the_computed_one(tmp_path):
    repo = _dev_repo(tmp_path, release="v0.13.0")
    before = repo.git("tag", "-l", "v*")
    mismatch = _release_run(repo, "--major", "v2.0.0")
    assert (mismatch.returncode, mismatch.stdout) == (1, "")
    assert "computed v1.0.0, operator expected v2.0.0; nothing tagged" in mismatch.stderr
    malformed = _release_run(repo, "--major", "1.0.0")
    assert (malformed.returncode, malformed.stdout) == (1, "")
    assert "wants vN.0.0" in malformed.stderr
    assert repo.git("tag", "-l", "v*") == before  # nothing created on either refusal


def test_release_tags_refuses_a_moved_backfill_tag_and_creates_nothing(tmp_path):
    repo = Repo(tmp_path / "repo", release=None)
    first = repo.head()
    repo.commit("feat: two")
    second = repo.head()
    repo.tag("v0.1.0", ref=second)  # the wrong place
    repo.branch("dev")
    repo.checkout("main")
    repo.feature("feat-x", "feat: x")
    repo.merge_pr("feat-x", 99, "feat: x")  # a release the run would otherwise create
    table = tmp_path / "table.tsv"
    _table(table, [("v0.1.0", first, 60), ("v0.2.0", second, 74)])
    result = _run(repo, "release-tags", "--main-ref", "main", "--dev-ref", "dev", "--table", str(table))
    assert (result.returncode, result.stdout) == (1, "")
    assert "v0.1.0" in result.stderr and "tags are never moved" in result.stderr
    assert repo.head("v0.1.0^{commit}") == second  # never moved
    assert repo.git("tag", "-l", "v0.2.0") == ""  # nothing created on a refusal


def test_release_tags_refuses_to_move_a_tag_that_already_exists(tmp_path, monkeypatch, capsys):
    # Defence in depth: last_release normally sees every release tag, so a collision needs a wrong count.
    repo = _dev_repo(tmp_path)
    init = repo.head()
    repo.checkout("main")
    repo.feature("feat-a", "feat: a")
    repo.merge_pr("feat-a", 80, "feat: a")
    repo.checkout("dev")
    repo.tag("v0.2.0", ref=init)  # a release tag the count below does not see
    monkeypatch.setattr(semver, "last_release", lambda _repo: (semver.Version(0, 1, 0), "v0.1.0"))
    table = tmp_path / "empty.tsv"
    _table(table, [])
    code = semver.main(
        ["--repo", str(repo.root), "release-tags", "--main-ref", "main", "--dev-ref", "dev", "--table", str(table)]
    )
    captured = capsys.readouterr()
    assert (code, captured.out) == (1, "")
    assert "v0.2.0 already exists locally; tags are never moved" in captured.err
    assert repo.head("v0.2.0^{commit}") == init  # never moved


def test_merge_guard_operator_merges_every_file_the_release_tagging_runs_from():
    # The release playbook runs semver.py with a token that can push tags, so the merge guard must refuse those
    # paths. test_merge_when_green iterates RELEASE_SCRIPTS, so only this pins what the tuple names.
    spec = importlib.util.spec_from_file_location("merge_guard_pin", REPO / "scripts" / "merge-when-green.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    assert "scripts/semver.py" in guard.RELEASE_SCRIPTS
    assert "scripts/semver-backfill.tsv" in guard.RELEASE_SCRIPTS
    assert "platform/playbooks/release-version.yml" in guard.RELEASE_SCRIPTS
