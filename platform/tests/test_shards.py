"""Guards for the pytest shard hook (plan 21 phase 2, D3): the assignment, the durations builder and the wiring."""

import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

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
    durations = {
        "x/test_known.py::test_one": {"time": 1.0, "skipped": False},
        "x/test_known.py::test_two": {"time": 2.0, "skipped": False},
        "x/test_known.py::test_three": {"time": 9.0, "skipped": False},
    }  # median 2.0, mean 4.0: the two must not be confused
    files = ["x/test_known.py", "x/test_new.py"]
    weights = shards.file_weights(files, durations, tmp_path)
    assert weights["x/test_known.py"] == 12.0  # the recorded times summed
    assert weights["x/test_new.py"] == 3 * 2.0  # three def test_ lines (one nested) times the median 2.0
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
<testcase classname="platform.tests.test_demo" name="test_mixed"
 file="platform/tests/test_demo.py" line="20" time="0.125"/>
<testcase classname="platform.tests.test_demo" name="test_mixed"
 file="platform/tests/test_demo.py" line="20" time="0.5">
<skipped type="pytest.skip" message="needs a sandbox">needs a sandbox</skipped>
</testcase>
</testsuite>
"""

EXPECTED = {
    "platform/tests/test_demo.py::TestThing::test_method[one]": {"time": 0.5, "skipped": False},
    "platform/tests/test_demo.py::test_function": {"time": 0.75, "skipped": False},
    "platform/tests/test_demo.py::test_skipped": {"time": 0.0, "skipped": True},
    "platform/tests/test_demo.py::test_mixed": {"time": 0.625, "skipped": False},  # skipped in one run, not the other
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
    )
    return proc, out


def test_builder_maps_xunit1_testcases_to_node_ids_and_sums_duplicates(tmp_path):
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


def test_a_test_file_the_durations_do_not_know_runs_in_shard_one_only(root_conftest, monkeypatch):
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
