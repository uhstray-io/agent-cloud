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
    assert "platform/playbooks/tasks/release-notes-body.yml" in guard.RELEASE_SCRIPTS


# --- release-notes: one release's categorised notes, as JSON sections or Markdown with optional summaries ----------


def _run_stdin(repo, stdin, *args):
    """_run with stdin given: --render reads stdin, and an inherited terminal would block the test."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo.root), *args],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def _commit_as(repo, author, subject):
    """A commit on the current branch under another author name, so contributor counts can tell authors apart."""
    env = {**repo._stamped(), "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": "author@example.invalid"}
    (repo.root / f"by-{repo.clock}.txt").write_text(subject + "\n")
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", subject, env=env)


def _pr_as(repo, author, branch, number, title):
    """A pull request whose one commit is by author, merged with a merge commit that the merge title names."""
    here = repo.branch_name()
    repo.branch(branch)
    _commit_as(repo, author, title)
    repo.checkout(here)
    repo.merge_pr(branch, number, title)


def _release_pr(repo, number, title, tag):
    """A pull request merged on main, then tag on the merge: one release (or dev tag) per call."""
    repo.feature(f"pr-{number}", title)
    repo.merge_pr(f"pr-{number}", number, title)
    repo.tag(tag)


def _three_sections_repo(tmp_path):
    """v0.1.0, then v0.2.0 with pull requests in three sections and no documentation update."""
    repo = Repo(tmp_path / "repo")
    _pr_as(repo, "Ada", "pr-30", 30, "feat: add a template")
    _pr_as(repo, "Cal", "pr-31", 31, "fix: stop the crash")
    _pr_as(repo, "Ada", "pr-32", 32, "fix(api): keep the route")
    _pr_as(repo, "Dana", "pr-33", 33, "test: cover the parser")
    repo.tag("v0.2.0")
    return repo


def _describe(repo, tag, *mode):
    return _run(repo, "release-notes", "--tag", tag, *mode)


def test_sections_file_each_pull_request_under_its_category_oldest_first(tmp_path):
    repo = Repo(tmp_path / "repo")
    for number, title in [
        (10, "feat: add a template"),
        (11, "perf: cache the inventory"),
        (12, "fix: stop the crash"),
        (13, "security: pin the image"),
        (14, "docs: explain the flag"),
        (15, "test: cover the parser"),
        (16, "Update stuff"),
        (17, "revert: undo the change"),
        (18, "feat(api)!: drop the old route"),
    ]:
        repo.feature(f"pr-{number}", title)
        repo.merge_pr(f"pr-{number}", number, title)
    repo.feature("pr-19", "fix: squashed")
    repo.squash_pr("pr-19", 19, "fix: squashed")
    repo.commit("chore: a direct commit on main")  # not a pull request, so in no section
    repo.tag("v0.2.0")
    result = _describe(repo, "v0.2.0", "--sections")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert (payload["tag"], payload["previous"]) == ("v0.2.0", "v0.1.0")
    assert payload["counts"] == {"features": 3, "fixes": 3, "documentation": 1, "other": 3}
    assert {key: [item["number"] for item in items] for key, items in payload["sections"].items()} == {
        "features": [10, 11, 18],
        "fixes": [12, 13, 19],
        "documentation": [14],
        "other": [15, 16, 17],
    }
    assert payload["sections"]["features"][0] == {"number": 10, "title": "feat: add a template"}
    assert payload["sections"]["other"][1] == {"number": 16, "title": "Update stuff"}


def test_contributors_count_non_merge_commits_most_first_then_by_name_and_never_emails(tmp_path):
    repo = Repo(tmp_path / "repo")
    for subject in ("chore: one", "chore: two", "chore: three"):
        _commit_as(repo, "Dana", subject)
    _commit_as(repo, "Ada", "chore: direct")
    _pr_as(repo, "Ada", "pr-20", 20, "fix: ada pull request")
    _pr_as(repo, "Cal", "pr-21", 21, "fix: cal pull request")
    _pr_as(repo, "Bea", "pr-22", 22, "fix: bea pull request")
    repo.tag("v0.2.0")
    result = _describe(repo, "v0.2.0", "--sections")
    assert result.returncode == 0
    # Three merge commits are authored by the repository's own identity: none of them may be counted.
    assert json.loads(result.stdout)["contributors"] == [
        {"name": "Dana", "commits": 3},
        {"name": "Ada", "commits": 2},
        {"name": "Bea", "commits": 1},
        {"name": "Cal", "commits": 1},
    ]
    assert "example.invalid" not in result.stdout  # names only, never an email address


def test_render_without_summaries_is_exact_and_omits_the_empty_documentation_section(tmp_path):
    repo = _three_sections_repo(tmp_path)
    result = _run_stdin(repo, "", "release-notes", "--tag", "v0.2.0", "--render")
    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout == (
        "## Summary\n"
        "\n"
        "4 pull requests since v0.1.0: 1 feature, 2 fixes, 0 documentation updates, 1 other change.\n"
        "\n"
        "## Major Updates\n"
        "\n"
        "No breaking changes in this release.\n"
        "\n"
        "## Features\n"
        "\n"
        "<details><summary>1 pull request</summary>\n"
        "\n"
        "- #30 feat: add a template\n"
        "\n"
        "</details>\n"
        "\n"
        "## Fixes\n"
        "\n"
        "<details><summary>2 pull requests</summary>\n"
        "\n"
        "- #31 fix: stop the crash\n"
        "- #32 fix(api): keep the route\n"
        "\n"
        "</details>\n"
        "\n"
        "## Other changes\n"
        "\n"
        "<details><summary>1 pull request</summary>\n"
        "\n"
        "- #33 test: cover the parser\n"
        "\n"
        "</details>\n"
        "\n"
        "## Contributors\n"
        "\n"
        "- Ada (2 commits)\n"
        "- Cal (1 commit)\n"
        "- Dana (1 commit)\n"
    )


def test_accepted_summaries_sit_under_their_heading_with_newlines_collapsed(tmp_path):
    repo = _three_sections_repo(tmp_path)
    summaries = {"features": "Adds the template.\nIt works\n   across lines.", "fixes": "Fixes two crashes."}
    result = _run_stdin(repo, json.dumps(summaries), "release-notes", "--tag", "v0.2.0", "--render")
    assert (result.returncode, result.stderr) == (0, "")
    assert "## Features\n\nAdds the template. It works across lines.\n\n<details>" in result.stdout
    assert "## Fixes\n\nFixes two crashes.\n\n<details>" in result.stdout
    assert "## Other changes\n\n<details>" in result.stdout  # no summary was given for it


@pytest.mark.parametrize(
    ("bad", "rule"),
    [
        ("Thanks @someone for it.", "contains '@'"),
        ("See http://example.invalid/page.", "contains 'http://'"),
        ("See https://example.invalid/page.", "contains 'https://'"),
        ("A [link](example-target) here.", "contains ']('"),
        ("Uses <b>bold</b> text.", "contains '<'"),
        ("Compare x > y here.", "contains '>'"),
        ("Run `make test` now.", "contains '`'"),
        ("Intro line.\n# A heading", "a line starts with '#'"),
        ("  ## Indented heading", "a line starts with '#'"),
        ("", "not 1 to 800 characters after strip"),
        ("   \n\t ", "not 1 to 800 characters after strip"),
        ("x" * 801, "not 1 to 800 characters after strip"),
    ],
)
def test_each_rejection_rule_drops_only_its_summary_and_names_the_rule_not_the_text(tmp_path, bad, rule):
    repo = _three_sections_repo(tmp_path)
    summaries = {"features": bad, "fixes": "Fixes two crashes."}
    result = _run_stdin(repo, json.dumps(summaries), "release-notes", "--tag", "v0.2.0", "--render")
    assert (result.returncode, result.stderr.splitlines()) == (0, [f"features summary dropped: {rule}"])
    assert "## Features\n\n<details>" in result.stdout  # the section stays, its summary does not
    assert "## Fixes\n\nFixes two crashes.\n\n<details>" in result.stdout  # the other summary is kept
    stripped = bad.strip()
    assert not stripped or stripped not in result.stdout


ALLOWLIST = "contains a character outside the allowed set"


@pytest.mark.parametrize(
    ("bad", "rule"),
    [
        ("HTTPS://evil.example now", ALLOWLIST),  # an uppercase scheme passes the banned marks; the slash fails here
        ("see www.Evil.example today", "contains 'www.'"),  # GitHub links www. in any letter case
        ("[x]: //evil.example", ALLOWLIST),  # the first half of a reference-style link
        ("~~~", ALLOWLIST),  # a tilde fence would swallow the rest of the body
        ("ping &#64;octocat", ALLOWLIST),  # an encoded mention
        ("Fixes #9999 quickly", ALLOWLIST),  # a free-text reference to a pull request outside the release
        ("see other-org/repo#1 too", ALLOWLIST),  # a cross-repository reference
    ],
)
def test_model_text_outside_the_allowlist_is_dropped_under_the_rule_it_breaks(tmp_path, bad, rule):
    repo = _three_sections_repo(tmp_path)
    summaries = {"features": bad, "fixes": "Fixes two crashes."}
    result = _run_stdin(repo, json.dumps(summaries), "release-notes", "--tag", "v0.2.0", "--render")
    assert (result.returncode, result.stderr.splitlines()) == (0, [f"features summary dropped: {rule}"])
    assert "## Features\n\n<details>" in result.stdout  # the section stays, its summary does not
    assert "## Fixes\n\nFixes two crashes.\n\n<details>" in result.stdout  # the other summary is kept
    assert bad not in result.stdout


def test_curly_quotes_dashes_and_percent_signs_stay_in_model_text(tmp_path):
    repo = _three_sections_repo(tmp_path)
    text = "The gateway’s limits — “now” enforced, 100% of the time."
    result = _run_stdin(repo, json.dumps({"features": text}), "release-notes", "--tag", "v0.2.0", "--render")
    assert (result.returncode, result.stderr) == (0, "")
    assert f"## Features\n\n{text}\n\n<details>" in result.stdout


def test_a_summary_of_exactly_800_characters_is_accepted(tmp_path):
    repo = _three_sections_repo(tmp_path)
    result = _run_stdin(repo, json.dumps({"features": "x" * 800}), "release-notes", "--tag", "v0.2.0", "--render")
    assert (result.returncode, result.stderr) == (0, "")
    assert "## Features\n\n" + "x" * 800 + "\n\n<details>" in result.stdout


def test_a_non_string_summary_is_dropped_under_the_not_a_string_rule(tmp_path):
    repo = _three_sections_repo(tmp_path)
    result = _run_stdin(
        repo, json.dumps({"features": 3, "fixes": None}), "release-notes", "--tag", "v0.2.0", "--render"
    )
    assert result.returncode == 0
    assert result.stderr.splitlines() == [
        "features summary dropped: not a string",
        "fixes summary dropped: not a string",
    ]
    assert "## Features\n\n<details>" in result.stdout
    assert "## Fixes\n\n<details>" in result.stdout


@pytest.mark.parametrize(
    "raw",
    ['not json', '["features"]', "null", '{"other": "x"}', '{"features": "Fine.", "other": "x"}'],
)
def test_unreadable_or_unknown_summary_input_is_ignored_whole_with_one_stderr_line(tmp_path, raw):
    repo = _three_sections_repo(tmp_path)
    plain = _run_stdin(repo, "", "release-notes", "--tag", "v0.2.0", "--render")
    result = _run_stdin(repo, raw, "release-notes", "--tag", "v0.2.0", "--render")
    assert result.returncode == 0
    assert result.stdout == plain.stdout
    assert result.stderr.splitlines() == [
        "release-notes: summaries ignored: want a JSON object with keys "
        "features, fixes, documentation, overview, highlights"
    ]


def test_whitespace_only_stdin_means_no_summaries_and_no_warning(tmp_path):
    repo = _three_sections_repo(tmp_path)
    plain = _run_stdin(repo, "", "release-notes", "--tag", "v0.2.0", "--render")
    result = _run_stdin(repo, "  \n\t\n", "release-notes", "--tag", "v0.2.0", "--render")
    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout == plain.stdout


def test_at_signs_are_neutralised_in_rendered_titles_and_names_but_raw_in_the_json(tmp_path):
    repo = Repo(tmp_path / "repo")
    _pr_as(repo, "Cat@Work", "pr-40", 40, "fix: ping @team about the route")
    repo.tag("v0.2.0")
    payload = json.loads(_describe(repo, "v0.2.0", "--sections").stdout)
    assert payload["sections"]["fixes"] == [{"number": 40, "title": "fix: ping @team about the route"}]
    assert payload["contributors"] == [{"name": "Cat@Work", "commits": 1}]
    rendered = _run_stdin(repo, "", "release-notes", "--tag", "v0.2.0", "--render").stdout
    assert "- #40 fix: ping @​team about the route\n" in rendered
    assert "- Cat@​Work (1 commit)\n" in rendered
    assert "@" not in rendered.replace("@​", "")


def test_a_contributor_name_that_is_a_link_renders_as_plain_text(tmp_path):
    repo = Repo(tmp_path / "repo")
    _pr_as(repo, "[n](https://e.example)", "pr-70", 70, "fix: a plain title")
    repo.tag("v0.2.0")
    rendered = _run_stdin(repo, "", "release-notes", "--tag", "v0.2.0", "--render").stdout
    assert "- \\[n\\](https://e.example) (1 commit)\n" in rendered
    assert "[n](" not in rendered


def test_a_title_cannot_format_a_link_or_close_its_block(tmp_path):
    repo = Repo(tmp_path / "repo")
    _pr_as(repo, "Ada", "pr-71", 71, "fix: close </details> early")
    _pr_as(repo, "Ada", "pr-72", 72, "fix: keep crash_loop *bold* ~gone~ a|b c\\d [x](y) R&D `make`")
    repo.tag("v0.2.0")
    rendered = _run_stdin(repo, "", "release-notes", "--tag", "v0.2.0", "--render").stdout
    assert "- #71 fix: close &lt;/details&gt; early\n" in rendered
    assert "- #72 fix: keep crash\\_loop \\*bold\\* \\~gone\\~ a\\|b c\\\\d \\[x\\](y) R&amp;D \\`make\\`\n" in rendered
    assert rendered.count("<details>") == rendered.count("</details>") == 1


def test_a_breaking_title_is_escaped_like_a_section_title(tmp_path):
    repo = Repo(tmp_path / "repo")
    _pr_as(repo, "Ada", "pr-73", 73, "fix(api)!: drop <b>old_route</b>")
    repo.tag("v0.2.0")
    rendered = _run_stdin(repo, "", "release-notes", "--tag", "v0.2.0", "--render").stdout
    assert "- #73 fix(api)!: drop &lt;b&gt;old\\_route&lt;/b&gt; (breaking change)\n" in rendered


def test_previous_is_the_highest_lower_release_tag_by_version_number_and_never_a_dev_tag(tmp_path):
    repo = Repo(tmp_path / "repo")  # v0.1.0 at the first commit
    _release_pr(repo, 50, "fix: a", "v0.1.1")
    _release_pr(repo, 51, "feat: b", "v0.2.0")
    _release_pr(repo, 52, "fix: c", "v0.2.1-dev.1")
    _release_pr(repo, 53, "feat: d", "v0.3.0")
    _release_pr(repo, 54, "fix: e", "v0.9.0")
    _release_pr(repo, 55, "fix: f", "v0.10.0")

    def described(tag):
        return json.loads(_describe(repo, tag, "--sections").stdout)

    third = described("v0.3.0")
    assert third["previous"] == "v0.2.0"  # v0.2.1-dev.1 is not a release, so it is not the previous one
    assert (third["counts"]["features"], third["counts"]["fixes"]) == (1, 1)  # PRs 52 and 53 both count
    assert described("v0.2.0")["previous"] == "v0.1.1"
    assert described("v0.1.1")["previous"] == "v0.1.0"
    tenth = described("v0.10.0")
    assert tenth["previous"] == "v0.9.0"  # a text comparison would pick v0.1.1
    assert [item["number"] for item in tenth["sections"]["fixes"]] == [55]


def test_the_first_release_has_no_previous_no_sections_and_renders_one_line(tmp_path):
    repo = Repo(tmp_path / "repo")  # v0.1.0 is the only release
    assert json.loads(_describe(repo, "v0.1.0", "--sections").stdout) == {
        "tag": "v0.1.0",
        "previous": None,
        "counts": {"features": 0, "fixes": 0, "documentation": 0, "other": 0},
        "breaking": [],
        "sections": {"features": [], "fixes": [], "documentation": [], "other": []},
        "contributors": [],
    }
    rendered = _run_stdin(repo, "", "release-notes", "--tag", "v0.1.0", "--render")
    assert (rendered.returncode, rendered.stdout) == (0, "First release.\n")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--tag", "v9.9.9", "--sections"), "no release tag v9.9.9 in this repository"),
        (("--tag", "0.2.0", "--sections"), "--tag wants vX.Y.Z"),
        (("--tag", "v0.2.1-dev.1", "--sections"), "--tag wants vX.Y.Z"),  # a dev tag is not a release
        (("--tag", "v0.1.0\n", "--sections"), "--tag wants vX.Y.Z"),  # the newline is no part of a tag name
        (("--sections",), "--tag is required"),
        (("--tag", "v0.1.0"), "give exactly one of --sections or --render"),
        (("--tag", "v0.1.0", "--sections", "--render"), "give exactly one of --sections or --render"),
    ],
)
def test_a_bad_tag_or_mode_refuses_with_exit_one_and_names_the_refusal(tmp_path, args, message):
    repo = Repo(tmp_path / "repo")  # v0.1.0 exists
    repo.tag("v0.2.1-dev.1")
    result = _run_stdin(repo, "", "release-notes", *args)
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr.startswith("release-notes: ")
    assert message in result.stderr


# --- release-notes: the Summary and Major Updates sections, the overview and the highlights ---------------------------

BREAKING_LINE = "- #32 fix(api)!: drop the old route (breaking change)"


def _major_repo(tmp_path):
    """v0.1.0, then v0.2.0 with eight pull requests. 32 carries the breaking mark; the others are ordinary."""
    repo = Repo(tmp_path / "repo")
    for number, title in [
        (30, "feat: add a template"),
        (31, "fix: stop the crash"),
        (32, "fix(api)!: drop the old route"),
        (33, "test: cover the parser"),
        (34, "docs: explain the flag"),
        (35, "perf: cache the inventory"),
        (36, "chore: tidy the module"),
        (37, "feat: add a metric"),
    ]:
        _pr_as(repo, "Ada", f"pr-{number}", number, title)
    repo.tag("v0.2.0")
    return repo


def _render_with(repo, model):
    """The Markdown render of v0.2.0 with one model input given on stdin."""
    return _run_stdin(repo, json.dumps(model), "release-notes", "--tag", "v0.2.0", "--render")


def test_breaking_lists_every_bang_title_on_any_type_and_keeps_its_category(tmp_path):
    repo = Repo(tmp_path / "repo")
    _pr_as(repo, "Ada", "pr-60", 60, "feat!: drop the old endpoint")
    _pr_as(repo, "Ada", "pr-61", 61, "frobnicate!: rename the thing")
    _pr_as(repo, "Ada", "pr-62", 62, "Update stuff!")  # no title form, so not breaking
    _pr_as(repo, "Ada", "pr-63", 63, "fix: ordinary")
    repo.tag("v0.2.0")
    payload = json.loads(_describe(repo, "v0.2.0", "--sections").stdout)
    assert payload["breaking"] == [
        {"number": 60, "title": "feat!: drop the old endpoint"},
        {"number": 61, "title": "frobnicate!: rename the thing"},
    ]
    assert [item["number"] for item in payload["sections"]["features"]] == [60]
    assert [item["number"] for item in payload["sections"]["other"]] == [61, 62]
    assert [item["number"] for item in payload["sections"]["fixes"]] == [63]


def test_the_counts_line_is_plain_text_opening_the_summary_after_the_heading(tmp_path):
    repo = _major_repo(tmp_path)
    out = _render_with(repo, {}).stdout
    assert out.startswith(
        "## Summary\n\n8 pull requests since v0.1.0: 3 features, 2 fixes, 1 documentation update, 2 other changes.\n\n"
        "## Major Updates\n\n"
    )
    assert "**" not in out


def test_sections_render_summary_then_major_updates_then_the_categories_then_contributors(tmp_path):
    repo = _major_repo(tmp_path)
    result = _render_with(
        repo, {"overview": "Eight changes.", "highlights": [{"number": 30, "text": "Templates arrive."}]}
    )
    assert result.returncode == 0
    assert [line for line in result.stdout.splitlines() if line.startswith("## ")] == [
        "## Summary",
        "## Major Updates",
        "## Features",
        "## Fixes",
        "## Documentation Updates",
        "## Other changes",
        "## Contributors",
    ]
    assert result.stdout.startswith(
        "## Summary\n\nEight changes.\n\n8 pull requests since v0.1.0: 3 features, 2 fixes, 1 documentation update, "
        "2 other changes.\n\n## Major Updates\n\n"
    )


def test_an_accepted_overview_opens_the_summary_with_whitespace_collapsed(tmp_path):
    repo = _major_repo(tmp_path)
    result = _render_with(repo, {"overview": "This release adds a template.\nIt also fixes\n   the crash."})
    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout.startswith(
        "## Summary\n\nThis release adds a template. It also fixes the crash.\n\n8 pull requests since v0.1.0: "
    )


@pytest.mark.parametrize(
    ("bad", "rule"),
    [
        ("Ships <b>things</b>.", "contains '<'"),
        ("See https://example.invalid/x.", "contains 'https://'"),
        ("Intro.\n# Heading", "a line starts with '#'"),
        ("", "not 1 to 800 characters after strip"),
        ("y" * 801, "not 1 to 800 characters after strip"),
    ],
)
def test_a_rejected_overview_is_dropped_and_the_summary_still_opens_with_the_counts(tmp_path, bad, rule):
    repo = _major_repo(tmp_path)
    result = _render_with(repo, {"overview": bad})
    assert (result.returncode, result.stderr.splitlines()) == (0, [f"overview summary dropped: {rule}"])
    assert result.stdout.startswith("## Summary\n\n8 pull requests since v0.1.0: ")
    stripped = bad.strip()
    assert not stripped or stripped not in result.stdout


def test_a_highlight_for_a_number_outside_the_release_is_dropped(tmp_path):
    repo = _major_repo(tmp_path)
    highlights = [{"number": 999, "text": "Not in this release."}, {"number": 30, "text": "Templates arrive."}]
    result = _render_with(repo, {"highlights": highlights})
    assert (result.returncode, result.stderr.splitlines()) == (
        0,
        ["highlight 1 dropped: number is not a pull request in this release"],
    )
    assert f"{BREAKING_LINE}\n- #30 Templates arrive.\n\n## Features\n" in result.stdout
    assert "Not in this release." not in result.stdout


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("z" * 201, "not 1 to 200 characters after strip"),
        ("   ", "not 1 to 200 characters after strip"),
        ("Mentions @someone here.", "contains '@'"),
        ("Links [x](y) here.", "contains ']('"),
        ("Uses <b>tags</b>.", "contains '<'"),
    ],
)
def test_a_highlight_whose_text_breaks_a_rule_is_dropped_and_the_rest_kept(tmp_path, text, rule):
    repo = _major_repo(tmp_path)
    result = _render_with(repo, {"highlights": [{"number": 30, "text": text}, {"number": 31, "text": "Crash fixed."}]})
    assert (result.returncode, result.stderr.splitlines()) == (0, [f"highlight 1 dropped: {rule}"])
    assert f"{BREAKING_LINE}\n- #31 Crash fixed.\n\n## Features\n" in result.stdout
    stripped = text.strip()
    assert not stripped or stripped not in result.stdout


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("www.evil.example", "contains 'www.'"),  # no banned mark, so only the www. rule catches it
        ("See WWW.Evil.example.", "contains 'www.'"),
        ("Closes #9999.", ALLOWLIST),
    ],
)
def test_a_highlight_outside_the_allowlist_is_dropped_and_the_rest_kept(tmp_path, text, rule):
    repo = _major_repo(tmp_path)
    highlights = [{"number": 30, "text": text}, {"number": 31, "text": "Crash fixed."}]
    result = _render_with(repo, {"highlights": highlights})
    assert (result.returncode, result.stderr.splitlines()) == (0, [f"highlight 1 dropped: {rule}"])
    assert f"{BREAKING_LINE}\n- #31 Crash fixed.\n\n## Features\n" in result.stdout
    assert text not in result.stdout


def test_a_highlight_of_exactly_200_characters_is_shown_and_its_newlines_collapsed(tmp_path):
    repo = _major_repo(tmp_path)
    text = "z" * 200
    highlights = [{"number": 30, "text": text}, {"number": 31, "text": "Templates\narrive   now."}]
    result = _render_with(repo, {"highlights": highlights})
    assert (result.returncode, result.stderr) == (0, "")
    assert f"{BREAKING_LINE}\n- #30 {text}\n- #31 Templates arrive now.\n\n## Features\n" in result.stdout


@pytest.mark.parametrize(
    ("item", "rule"),
    [
        ("not an object", "not an object"),
        ({"number": "30", "text": "Quoted number."}, "number is not an integer"),
        ({"number": True, "text": "A boolean number."}, "number is not an integer"),
        ({"text": "No number."}, "number is not an integer"),
        ({"number": 30, "text": 7}, "text is not a string"),
    ],
)
def test_a_highlight_of_the_wrong_shape_is_dropped_under_the_rule_it_breaks(tmp_path, item, rule):
    repo = _major_repo(tmp_path)
    result = _render_with(repo, {"highlights": [item]})
    assert (result.returncode, result.stderr.splitlines()) == (0, [f"highlight 1 dropped: {rule}"])
    assert f"{BREAKING_LINE}\n\n## Features\n" in result.stdout


def test_at_most_five_highlights_are_shown_in_input_order_and_the_rest_are_counted(tmp_path):
    repo = _major_repo(tmp_path)  # seven valid numbers besides the breaking 32
    highlights = [{"number": n, "text": f"Note {n}."} for n in (30, 31, 33, 34, 35, 36, 37)]
    result = _render_with(repo, {"highlights": highlights})
    assert result.stderr.splitlines() == ["highlights: 2 valid beyond the first 5 not shown"]
    assert (
        f"## Major Updates\n\n{BREAKING_LINE}\n- #30 Note 30.\n- #31 Note 31.\n- #33 Note 33.\n"
        "- #34 Note 34.\n- #35 Note 35.\n\n## Features\n"
    ) in result.stdout
    assert "Note 36." not in result.stdout and "Note 37." not in result.stdout


def test_a_duplicate_highlight_number_is_dropped_and_the_first_is_kept(tmp_path):
    repo = _major_repo(tmp_path)
    highlights = [{"number": 30, "text": "First."}, {"number": 30, "text": "Second."}]
    result = _render_with(repo, {"highlights": highlights})
    assert result.stderr.splitlines() == ["highlight 2 dropped: a duplicate of an earlier highlight"]
    assert f"{BREAKING_LINE}\n- #30 First.\n\n## Features\n" in result.stdout
    assert "Second." not in result.stdout


def test_a_duplicate_is_judged_only_against_highlights_that_were_accepted(tmp_path):
    repo = _major_repo(tmp_path)
    highlights = [{"number": 30, "text": "z" * 201}, {"number": 30, "text": "Fine."}]
    result = _render_with(repo, {"highlights": highlights})
    assert result.stderr.splitlines() == ["highlight 1 dropped: not 1 to 200 characters after strip"]
    assert f"{BREAKING_LINE}\n- #30 Fine.\n\n## Features\n" in result.stdout


def test_a_breaking_change_is_not_repeated_as_a_highlight(tmp_path):
    repo = _major_repo(tmp_path)
    result = _render_with(repo, {"highlights": [{"number": 32, "text": "Old route removed."}]})
    assert result.stderr.splitlines() == ["highlight 1 dropped: a breaking change, already under Major Updates"]
    assert f"## Major Updates\n\n{BREAKING_LINE}\n\n## Features\n" in result.stdout
    assert "Old route removed." not in result.stdout


def test_major_updates_lists_breaking_changes_first_then_the_accepted_highlights(tmp_path):
    repo = _major_repo(tmp_path)
    result = _render_with(repo, {"highlights": [{"number": 30, "text": "Templates arrive."}]})
    assert result.stderr == ""
    assert f"## Major Updates\n\n{BREAKING_LINE}\n- #30 Templates arrive.\n\n## Features\n" in result.stdout


def test_major_updates_says_so_when_there_is_no_breaking_change_and_no_accepted_highlight(tmp_path):
    repo = _three_sections_repo(tmp_path)
    plain = _run_stdin(repo, "", "release-notes", "--tag", "v0.2.0", "--render")
    assert "## Major Updates\n\nNo breaking changes in this release.\n\n## Features\n" in plain.stdout
    rejected = _render_with(repo, {"highlights": [{"number": 999, "text": "Not here."}]})
    assert "## Major Updates\n\nNo breaking changes in this release.\n\n## Features\n" in rejected.stdout
