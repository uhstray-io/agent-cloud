"""Guards for the pytest shard hook (plan 21 phase 2, D3) and the pre-push fast lane (plan 21 phase 4, D6):
the assignment, the durations builder, the wiring and the lane's selection."""

import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import harness_sandbox
import pytest
import shards

REPO = Path(__file__).resolve().parents[2]
TESTPATHS = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"][
    "testpaths"
]
CADDY_TESTS = "platform/services/caddy/deployment/tests"
BUILDER = REPO / "scripts" / "test-durations.py"


class _Config:
    """The two attributes the hook reads from pytest's config."""

    def __init__(self, rootpath):
        self.rootpath = rootpath

    def getini(self, name):
        assert name == "testpaths"
        return TESTPATHS


@pytest.fixture
def root_conftest():
    spec = importlib.util.spec_from_file_location("root_conftest_under_test", REPO / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- assignment ---------------------------------------------------------------------------------------


def test_enumerate_files_finds_test_modules_once_and_skips_pycache(tmp_path):
    (tmp_path / "a" / "tests" / "__pycache__").mkdir(parents=True)
    (tmp_path / "a" / "tests" / "test_one.py").write_text("")
    (tmp_path / "a" / "tests" / "__pycache__" / "test_cached.py").write_text("")
    (tmp_path / "a" / "tests" / "helper.py").write_text("")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "test_two.py").write_text("")
    assert shards.enumerate_files(tmp_path, ["a/tests", "b", "a/tests"]) == ["a/tests/test_one.py", "b/test_two.py"]


@pytest.mark.parametrize("n", range(1, 13))
def test_every_real_test_file_is_in_exactly_one_shard(n):
    files = shards.enumerate_files(REPO, TESTPATHS)
    assert "platform/tests/test_shards.py" in files
    assert not [f for f in files if "__pycache__" in f]
    assignment = shards.assign(files, shards.file_weights(files, {}, REPO), n)
    members = [sorted(f for f, shard in assignment.items() if shard == k) for k in range(1, n + 1)]
    assert sorted(sum(members, [])) == files, "shards must be disjoint and cover every file"
    assert set(assignment.values()) <= set(range(1, n + 1))


def test_assignment_is_deterministic_and_breaks_ties_by_path():
    files = ["d.py", "b.py", "c.py", "a.py"]
    weights = dict.fromkeys(files, 1.0)
    expected = {"a.py": 1, "b.py": 2, "c.py": 1, "d.py": 2}
    assert shards.assign(files, weights, 2) == expected
    assert shards.assign(list(reversed(files)), weights, 2) == expected
    assert shards.assign(files, weights, 2) == expected


def test_heaviest_file_goes_first_and_later_files_fill_the_lightest_shard():
    weights = {"big.py": 10.0, "s1.py": 1.0, "s2.py": 1.0, "s3.py": 1.0}
    assert shards.assign(list(weights), weights, 2) == {"big.py": 1, "s1.py": 2, "s2.py": 2, "s3.py": 2}


def test_fallback_weight_is_def_test_count_times_the_median(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "test_known.py").write_text("def test_one():\n    pass\n")
    (tmp_path / "x" / "test_new.py").write_text(
        "import pytest\n\n"
        "def helper():\n    pass\n\n"
        "def test_a():\n    pass\n\n"
        "async def test_b():\n    pass\n\n"
        "class TestC:\n    def test_c(self):\n        pass\n"
    )
    entry = {"max": 1.0, "skipped": False, "tests": []}
    durations = {
        "x/test_known.py": {**entry, "time": 12.0, "nodes": 3},  # mean 4.0 per test
        "x/test_other.py": {**entry, "time": 2.0, "nodes": 1},  # mean 2.0
        "x/test_third.py": {**entry, "time": 18.0, "nodes": 2},  # mean 9.0
    }  # median of the means 4.0; the mean over every test is 32/6: the two must not be confused
    files = ["x/test_known.py", "x/test_new.py"]
    weights = shards.file_weights(files, durations, tmp_path)
    assert weights["x/test_known.py"] == 12.0  # the recorded time
    assert weights["x/test_new.py"] == 3 * 4.0  # three def test_ lines (one nested) times the median 4.0
    assert shards.file_weights(files, {}, tmp_path)["x/test_new.py"] == 3 * 1.0  # nothing recorded: median 1.0


@pytest.mark.parametrize("value, expected", [("1/1", (1, 1)), ("2/3", (2, 3)), ("8/8", (8, 8))])
def test_parse_shard_accepts_k_of_n(value, expected):
    assert shards.parse_shard(value) == expected


@pytest.mark.parametrize("value", ["0/2", "3/2", "x", "", "2", "1/0", "-1/2", "2/3/4", "+2/3", " 2/3"])
def test_parse_shard_rejects_everything_else(value):
    with pytest.raises(ValueError):
        shards.parse_shard(value)


# ---- durations builder --------------------------------------------------------------------------------

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" errors="0" failures="0" skipped="1" tests="4" time="1.35">
<testcase classname="platform.tests.test_demo" name="test_function"
 file="platform/tests/test_demo.py" line="3" time="0.5"/>
<testcase classname="platform.tests.test_demo" name="test_function"
 file="platform/tests/test_demo.py" line="3" time="0.25"/>
<testcase classname="platform.tests.test_demo.TestThing" name="test_method[one]"
 file="platform/tests/test_demo.py" line="9" time="0.5"/>
<testcase classname="platform.tests.test_demo" name="test_skipped"
 file="platform/tests/test_demo.py" line="15" time="0.0">
<skipped type="pytest.skip" message="needs a sandbox">needs a sandbox</skipped>
</testcase>
<testcase classname="platform.tests.test_other" name="test_mixed"
 file="platform/tests/test_other.py" line="20" time="0.125"/>
<testcase classname="platform.tests.test_other" name="test_mixed"
 file="platform/tests/test_other.py" line="20" time="0.5">
<skipped type="pytest.skip" message="needs a sandbox">needs a sandbox</skipped>
</testcase>
<testcase classname="platform.tests.test_other" name="test_ipv6[fe80::1]"
 file="platform/tests/test_other.py" line="30" time="0.25"/>
</testsuite>
"""

EXPECTED = {
    # test_function's two runs summed (0.75) is the slowest node; one node skipped marks the file.
    "platform/tests/test_demo.py": {"time": 1.25, "max": 0.75, "nodes": 3, "skipped": True,
                                    "tests": ["test_function", "test_method", "test_skipped"]},
    # Skipped in one run, not the other: the node, and so the file, is not skipped.
    # A parameter holding "::" (an IPv6 address) does not split the test name.
    "platform/tests/test_other.py": {"time": 0.875, "max": 0.625, "nodes": 2, "skipped": False,
                                     "tests": ["test_ipv6", "test_mixed"]},
}


def _run_builder(tmp_path, xml_text):
    report = tmp_path / "report.xml"
    report.write_text(xml_text, encoding="utf-8")
    out = tmp_path / "durations.json"
    proc = subprocess.run(
        [sys.executable, str(BUILDER), str(report), "--out", str(out)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=harness_sandbox.DEFAULT_TIMEOUT_SECONDS,
    )
    return proc, out


def test_builder_sums_xunit1_testcases_per_node_and_records_each_file(tmp_path):
    proc, out = _run_builder(tmp_path, JUNIT)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == EXPECTED
    assert len(out.read_text(encoding="utf-8").splitlines()) == len(EXPECTED) + 2  # one entry per line, plus braces


@pytest.mark.parametrize(
    "xml_text",
    [
        '<testsuite><testcase classname="platform.tests.test_demo" name="test_function" time="0.1"/></testsuite>',
        '<testsuite tests="0"></testsuite>',
    ],
    ids=["no-file-attribute", "empty-report"],
)
def test_builder_refuses_a_report_it_cannot_map_and_writes_nothing(tmp_path, xml_text):
    proc, out = _run_builder(tmp_path, xml_text)
    assert proc.returncode == 1
    assert not out.exists()
    assert "test-durations:" in proc.stderr


# ---- the hook, end to end -----------------------------------------------------------------------------


# The netbox tests import two worker modules from these directories; CI and the pre-push hook set the same PYTHONPATH.
NETBOX_WORKER_PATH = os.pathsep.join(
    str(REPO / path)
    for path in (
        "platform/services/netbox/deployment/workers/proxmox_discovery",
        "platform/services/netbox/deployment/workers/pfsense_sync",
    )
)


def _collect(shard, testpath):
    """Node IDs pytest collects from ``testpath`` in a fresh process, with PYTEST_SHARD set to ``shard`` or unset."""
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_SHARD"}
    env["PYTHONPATH"] = NETBOX_WORKER_PATH
    if shard is not None:
        env["PYTEST_SHARD"] = shard
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", testpath],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=harness_sandbox.DEFAULT_TIMEOUT_SECONDS,
    )
    assert proc.returncode in (0, pytest.ExitCode.NO_TESTS_COLLECTED), proc.stdout[-3000:] + proc.stderr[-3000:]
    return {line.strip() for line in proc.stdout.splitlines() if "::" in line}


def _split_testpath(assignment, files):
    """The smallest testpath whose files the real assignment puts in both shard 1 and shard 2. Chosen, not hardcoded,
    so a change in the repo's file set cannot leave the wiring test with an empty shard."""
    candidates = []
    for testpath in TESTPATHS:
        members = [rel for rel in files if rel.startswith(testpath + "/")]
        if {assignment[rel] for rel in members} == {1, 2}:
            candidates.append((len(members), testpath))
    assert candidates, "no testpath is split between shards 1 and 2"
    return min(candidates)[1]


def test_the_hook_splits_a_directory_collection_between_two_shards():
    files = shards.enumerate_files(REPO, TESTPATHS)
    durations = shards.load_durations(REPO / "platform" / "tests" / "test_durations.json")
    assignment = shards.assign(files, shards.file_weights(files, durations, REPO), 2)
    testpath = _split_testpath(assignment, files)
    whole = _collect(None, testpath)
    one, two = _collect("1/2", testpath), _collect("2/2", testpath)
    assert one and two, "each shard must hold at least one test"
    assert not one & two
    assert one | two == whole
    for shard, collected in ((1, one), (2, two)):
        expected = {node for node in whole if assignment[node.split("::")[0]] == shard}
        assert collected == expected, f"shard {shard} collected the wrong files"


def test_an_invalid_shard_spec_is_a_usage_error():
    env = dict(os.environ, PYTEST_SHARD="0/2")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", CADDY_TESTS],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=harness_sandbox.DEFAULT_TIMEOUT_SECONDS,
    )
    assert proc.returncode == pytest.ExitCode.USAGE_ERROR
    assert "PYTEST_SHARD" in proc.stdout + proc.stderr


def test_the_hook_is_inert_without_pytest_shard(root_conftest, monkeypatch):
    monkeypatch.delenv("PYTEST_SHARD", raising=False)
    result = root_conftest.pytest_ignore_collect(
        collection_path=REPO / "platform/tests/test_shards.py", config=_Config(REPO)
    )
    assert result is None


def test_the_hook_leaves_directories_and_non_test_files_to_pytest(root_conftest, monkeypatch):
    monkeypatch.setenv("PYTEST_SHARD", "2/2")
    for path in (REPO / "platform/tests", REPO / "platform/tests/shards.py", REPO / "conftest.py"):
        assert root_conftest.pytest_ignore_collect(collection_path=path, config=_Config(REPO)) is None, path


def test_a_directory_named_like_a_test_module_is_never_skipped_whole(root_conftest, monkeypatch, tmp_path):
    (tmp_path / "test_dir.py").mkdir()

    class _TmpConfig:
        rootpath = tmp_path

        def getini(self, name):
            return ["."]

    monkeypatch.setenv("PYTEST_SHARD", "2/2")
    assert root_conftest.pytest_ignore_collect(collection_path=tmp_path / "test_dir.py", config=_TmpConfig()) is None


def test_the_hook_gives_each_test_file_to_exactly_one_shard_and_never_returns_false(root_conftest, monkeypatch):
    results = {}
    for spec in ("1/2", "2/2"):
        monkeypatch.setenv("PYTEST_SHARD", spec)
        results[spec] = root_conftest.pytest_ignore_collect(
            collection_path=REPO / "platform/tests/test_shards.py", config=_Config(REPO)
        )
    assert sorted(results.values(), key=repr) == [None, True]
    assert all(value is not False for value in results.values())


def test_a_test_file_outside_the_enumeration_runs_in_shard_one_only(root_conftest, monkeypatch):
    phantom = REPO / "platform/tests/test_not_a_real_module_xyz.py"
    monkeypatch.setenv("PYTEST_SHARD", "1/2")
    assert root_conftest.pytest_ignore_collect(collection_path=phantom, config=_Config(REPO)) is None
    monkeypatch.setenv("PYTEST_SHARD", "2/2")
    assert root_conftest.pytest_ignore_collect(collection_path=phantom, config=_Config(REPO)) is True


def test_the_hook_refuses_an_invalid_spec(root_conftest, monkeypatch):
    monkeypatch.setenv("PYTEST_SHARD", "0/2")
    with pytest.raises(pytest.UsageError, match="PYTEST_SHARD"):
        root_conftest.pytest_ignore_collect(
            collection_path=REPO / "platform/tests/test_shards.py", config=_Config(REPO)
        )


# ---- the pre-push fast lane (plan 21 phase 4, D6) -----------------------------------------------------

LANE_ENV_NAMES = ("PYTEST_FAST", "PYTEST_FAST_CHANGED", "PYTEST_SHARD")


@pytest.fixture(autouse=True)
def _no_lane_or_shard_in_the_environment(monkeypatch):
    """A pre-push run exports PYTEST_FAST and a CI shard exports PYTEST_SHARD. Neither may reach these tests,
    in process or in the subprocesses they start, whose environments are copied from os.environ."""
    for name in LANE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


ONE_TEST = "def test_a():\n    pass\n"
TWO_TESTS = "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n"


def _entry(seconds, *, skipped=False, tests=("test_a",)):
    """One durations entry as scripts/test-durations.py writes it."""
    return {"time": seconds, "max": seconds, "nodes": len(tests), "skipped": skipped, "tests": list(tests)}


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel


@pytest.mark.parametrize("has_entry", [True, False], ids=["recorded-slow", "no-entry"])
def test_fast_lane_keeps_a_changed_file_whatever_its_timing(tmp_path, has_entry):
    rel = _write(tmp_path, "a.py", ONE_TEST)
    durations = {rel: _entry(9.0)} if has_entry else {}
    assert shards.fast_lane([rel], durations, tmp_path, {rel}) == {rel}


def test_fast_lane_drops_a_slow_recorded_file_that_did_not_change(tmp_path):
    rel = _write(tmp_path, "a.py", ONE_TEST)
    assert shards.fast_lane([rel], {rel: _entry(9.0)}, tmp_path, set()) == set()


def test_fast_lane_keeps_a_file_with_no_durations_entry(tmp_path):
    rel = _write(tmp_path, "new.py", ONE_TEST)
    assert shards.fast_lane([rel], {}, tmp_path, set()) == {rel}


@pytest.mark.parametrize("seconds, kept", [(0.0, True), (0.1, True), (0.2499, True), (0.25, False), (2.0, False)])
def test_fast_lane_cuts_at_a_quarter_second(tmp_path, seconds, kept):
    rel = _write(tmp_path, "a.py", ONE_TEST)
    assert (shards.fast_lane([rel], {rel: _entry(seconds)}, tmp_path, set()) == {rel}) is kept


def test_fast_lane_excludes_a_file_ci_skipped_unless_it_changed(tmp_path):
    rel = _write(tmp_path, "a.py", ONE_TEST)
    durations = {rel: _entry(0.1, skipped=True)}
    assert shards.fast_lane([rel], durations, tmp_path, set()) == set()
    assert shards.fast_lane([rel], durations, tmp_path, {rel}) == {rel}


def test_fast_lane_keeps_a_file_with_a_test_the_durations_file_lacks(tmp_path):
    rel = _write(tmp_path, "a.py", TWO_TESTS)
    assert shards.fast_lane([rel], {rel: _entry(2.0)}, tmp_path, set()) == {rel}  # slow, test_b unrecorded
    assert shards.fast_lane([rel], {rel: _entry(0.1)}, tmp_path, set()) == {rel}  # fast, test_b unrecorded
    assert shards.fast_lane([rel], {rel: _entry(2.0, tests=("test_a", "test_b"))}, tmp_path, set()) == set()


def test_fast_lane_keeps_a_file_that_does_not_parse(tmp_path):
    rel = _write(tmp_path, "a.py", "def test_a(:\n")
    assert shards.fast_lane([rel], {rel: _entry(2.0)}, tmp_path, set()) == {rel}


NAME_SOURCE = (
    "import unittest\n\nimport pytest\n\n\n"
    "def helper():\n    def test_in_a_helper():\n        pass\n\n\n"
    "def test_module_level():\n    def test_in_a_test():\n        pass\n\n\n"
    "def testprefixed():\n    pass\n\n\n"
    "if True:\n    def test_in_a_branch():\n        pass\n\n\n"
    "class TestCollected:\n    def test_method(self):\n        pass\n\n"
    "    def helper_method(self):\n        pass\n\n"
    "    @pytest.mark.parametrize('value', [1, 2])\n    def test_parametrized(self, value):\n        pass\n\n"
    "    class TestNested:\n        def test_deep(self):\n            pass\n\n\n"
    "class Plain:\n    def test_not_collected(self):\n        pass\n\n\n"
    "class CaseBase(unittest.TestCase):\n    def test_unittest_method(self):\n        pass\n"
)


def test_name_extraction_matches_what_pytest_collects(tmp_path):
    _write(tmp_path, "pytest.ini", "[pytest]\n")
    _write(tmp_path, "test_collected.py", NAME_SOURCE)
    proc = _pytest_collect(tmp_path, {}, "test_collected.py")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    collected = {node.rsplit("::", 1)[-1].split("[", 1)[0] for node in _node_ids(proc)}
    assert collected == shards.collected_test_names(NAME_SOURCE)
    assert collected == {"test_module_level", "testprefixed", "test_in_a_branch", "test_method",
                         "test_parametrized", "test_deep", "test_unittest_method"}


def test_name_extraction_ignores_a_string_and_an_async_name_is_collected():
    assert shards.collected_test_names('TEXT = """\ndef test_in_a_string():\n    pass\n"""\n') == set()
    assert shards.collected_test_names("async def test_async():\n    pass\n") == {"test_async"}


def test_name_extraction_reads_a_class_with_a_base_as_a_candidate():
    assert shards.collected_test_names("class Mixin(object):\n    def test_mixin(self):\n        pass\n") == {
        "test_mixin"
    }


# A scratch repository: each file's durations entry, or none, decides what the lane keeps.
LANE_TREE = {
    "suite/test_quick.py": (ONE_TEST, _entry(0.1)),  # fast: kept
    "suite/test_just_under.py": (ONE_TEST, _entry(0.2499)),  # kept
    "suite/test_edge.py": (ONE_TEST, _entry(0.25)),  # at the cutoff: dropped
    "suite/test_skipped.py": (ONE_TEST, _entry(0.1, skipped=True)),  # dropped
    "suite/test_slow.py": (ONE_TEST, _entry(2.0)),  # dropped
    "suite/test_changed.py": (ONE_TEST, _entry(2.0)),  # kept when named in the changed file
    "suite/test_missing.py": (TWO_TESTS, _entry(2.0)),  # kept: test_b is not recorded
    "suite/test_unknown.py": (ONE_TEST, None),  # kept: no entry
}
LANE_ALL = {f"{rel}::test_a" for rel in LANE_TREE} | {"suite/test_missing.py::test_b"}
LANE_KEPT = {
    "suite/test_quick.py::test_a",
    "suite/test_just_under.py::test_a",
    "suite/test_missing.py::test_a",
    "suite/test_missing.py::test_b",
    "suite/test_unknown.py::test_a",
}


def _lane_tree(tmp_path):
    """A scratch repository with the root conftest.py and shards.py as in REPO, testpaths = suite, the LANE_TREE
    files, an unlisted test file under outside/, and a durations file holding the entries."""
    _write(tmp_path, "conftest.py", (REPO / "conftest.py").read_text(encoding="utf-8"))
    _write(tmp_path, "platform/tests/shards.py", (REPO / "platform/tests/shards.py").read_text(encoding="utf-8"))
    _write(tmp_path, "pytest.ini", "[pytest]\ntestpaths = suite\n")
    _write(tmp_path, "outside/test_outside.py", ONE_TEST)
    durations = {}
    for rel, (text, entry) in LANE_TREE.items():
        _write(tmp_path, rel, text)
        if entry is not None:
            durations[rel] = entry
    _write(tmp_path, "platform/tests/test_durations.json", json.dumps(durations))
    return tmp_path


def _node_ids(proc):
    return {line.strip() for line in proc.stdout.splitlines() if "::" in line}


def _pytest_collect(cwd, env_extra, *args):
    """``pytest --collect-only`` in ``cwd``, in a fresh process, with the lane's and the shard's variables unset
    unless ``env_extra`` sets them."""
    env = {key: value for key, value in os.environ.items() if key not in LANE_ENV_NAMES}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=harness_sandbox.DEFAULT_TIMEOUT_SECONDS,
    )


def test_the_lane_collects_exactly_the_files_its_selection_keeps(tmp_path):
    tree = _lane_tree(tmp_path)
    changed = _write(tree, "changed.txt", "suite/test_changed.py\n\n")
    proc = _pytest_collect(tree, {"PYTEST_FAST": "1", "PYTEST_FAST_CHANGED": str(tree / changed)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _node_ids(proc) == LANE_KEPT | {"suite/test_changed.py::test_a"}


@pytest.mark.parametrize(
    "env_extra",
    [
        {"PYTEST_FAST": "1"},
        {"PYTEST_FAST": "1", "PYTEST_FAST_CHANGED": "missing.txt"},
        {"PYTEST_FAST": "1", "PYTEST_FAST_CHANGED": ""},
    ],
    ids=["unset", "missing-file", "empty"],
)
def test_without_a_changed_file_the_lane_selects_by_timing_alone(tmp_path, env_extra):
    proc = _pytest_collect(_lane_tree(tmp_path), env_extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _node_ids(proc) == LANE_KEPT


@pytest.mark.parametrize("env_extra", [{}, {"PYTEST_FAST": ""}], ids=["unset", "empty"])
def test_without_the_lane_every_file_in_the_testpaths_is_collected(tmp_path, env_extra):
    proc = _pytest_collect(_lane_tree(tmp_path), env_extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _node_ids(proc) == LANE_ALL


def test_a_file_outside_the_testpaths_is_never_ignored_by_the_lane(tmp_path):
    proc = _pytest_collect(_lane_tree(tmp_path), {"PYTEST_FAST": "1"}, "outside")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _node_ids(proc) == {"outside/test_outside.py::test_a"}


def test_pytest_does_not_consult_the_hook_for_a_file_named_on_the_command_line(tmp_path):
    proc = _pytest_collect(_lane_tree(tmp_path), {"PYTEST_FAST": "1"}, "suite/test_slow.py")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _node_ids(proc) == {"suite/test_slow.py::test_a"}


@pytest.mark.parametrize(
    "env_extra, message",
    [
        ({"PYTEST_FAST": "0"}, "PYTEST_FAST='0'"),
        ({"PYTEST_FAST": "true"}, "PYTEST_FAST='true'"),
        ({"PYTEST_FAST": "1", "PYTEST_SHARD": "1/2"}, "one or the other"),
    ],
    ids=["zero", "word", "with-a-shard"],
)
def test_the_lane_refuses_a_bad_value_and_a_shard_spec(tmp_path, env_extra, message):
    proc = _pytest_collect(_lane_tree(tmp_path), env_extra)
    assert proc.returncode == pytest.ExitCode.USAGE_ERROR, proc.stdout + proc.stderr
    assert message in proc.stdout + proc.stderr
