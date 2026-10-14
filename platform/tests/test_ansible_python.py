"""The shared ansible interpreter helper: plain shebang, pip/uv trampoline, and what happens
when ansible-playbook is missing or its interpreter cannot import ansible."""

import stat

import ansible_python as helper
import pytest

TRAMPOLINE = "#!/bin/sh\n'''exec' '{python}' \"$0\" \"$@\"\n' '''\n# -*- coding: utf-8 -*-\nimport sys\n"
DISTLIB_TRAMPOLINE = "#!/bin/sh\n'''exec' {python} \"$0\" \"$@\"\n' '''\nimport sys\n"


def _executable(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def stub_python(tmp_path):
    """An interpreter that accepts `-c 'import ansible'`."""
    return _executable(tmp_path / "stub python", "#!/bin/sh\nexit 0\n")


@pytest.fixture
def on_path(tmp_path, monkeypatch):
    """Install `text` as the only ansible-playbook on PATH and drop the helper's cache."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    helper._verified.cache_clear()

    def install(text):
        _executable(bin_dir / "ansible-playbook", text)

    yield install
    helper._verified.cache_clear()


def test_plain_shebang_names_its_interpreter():
    assert helper.interpreter_from_script("#!/venv/bin/python\nimport sys\n") == "/venv/bin/python"
    assert helper.interpreter_from_script("#!/venv/bin/python3 -E\n") == "/venv/bin/python3"


def test_uv_trampoline_names_the_quoted_interpreter_on_line_two():
    text = TRAMPOLINE.format(python="/a/very/long/venv/bin/python")
    assert helper.interpreter_from_script(text) == "/a/very/long/venv/bin/python"


def test_trampoline_interpreter_with_a_space_in_its_path():
    text = TRAMPOLINE.format(python="/with space/bin/python")
    assert helper.interpreter_from_script(text) == "/with space/bin/python"


def test_pip_trampoline_names_the_unquoted_interpreter_on_line_two():
    assert helper.interpreter_from_script(DISTLIB_TRAMPOLINE.format(python="/venv/bin/python")) == "/venv/bin/python"


def test_sh_script_that_is_not_a_trampoline_names_no_interpreter():
    assert helper.interpreter_from_script("#!/bin/sh\necho hello\n") is None
    assert helper.interpreter_from_script("#!/bin/sh\n") is None
    assert helper.interpreter_from_script("print('no shebang')\n") is None
    assert helper.interpreter_from_script("") is None


def test_env_shebang_resolves_through_path(monkeypatch, stub_python):
    monkeypatch.setenv("PATH", str(stub_python.parent))
    assert helper.interpreter_from_script("#!/usr/bin/env 'stub python'\n") == str(stub_python)


def test_ansible_python_reads_a_plain_shebang(on_path, tmp_path):
    plain = _executable(tmp_path / "stub-python", "#!/bin/sh\nexit 0\n")
    on_path(f"#!{plain}\n")
    assert helper.ansible_python() == str(plain)


def test_ansible_python_reads_the_trampoline_not_bin_sh(on_path, stub_python):
    on_path(TRAMPOLINE.format(python=stub_python))
    assert helper.ansible_python() == str(stub_python)


def test_interpreter_that_cannot_import_ansible_fails_naming_it(on_path, tmp_path):
    broken = _executable(tmp_path / "broken-python", "#!/bin/sh\necho 'No module named ansible' >&2\nexit 1\n")
    on_path(TRAMPOLINE.format(python=broken))
    with pytest.raises(RuntimeError, match="broken-python.*cannot import ansible.*No module named ansible"):
        helper.ansible_python()


def test_unrecognised_script_fails_instead_of_guessing(on_path):
    on_path("#!/bin/sh\necho not ansible\n")
    with pytest.raises(RuntimeError, match="cannot find the interpreter"):
        helper.ansible_python()


def test_malformed_shebang_names_no_interpreter():
    assert helper.interpreter_from_script("#!/venv/bin/python 'unbalanced\n") is None
    assert helper.interpreter_from_script("#!/bin/sh\n'''exec' '/venv/bin/python \"$0\"\n") is None


@pytest.mark.parametrize("text", ["#!/venv/bin/python 'unbalanced\n", "#!/bin/sh\n'''exec' '/venv/bin/python \"$0\"\n"])
def test_malformed_script_fails_naming_the_script(on_path, tmp_path, text):
    on_path(text)
    with pytest.raises(RuntimeError, match=r"cannot find the interpreter .*ansible-playbook"):
        helper.ansible_python()


def test_missing_ansible_playbook_skips(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(pytest.skip.Exception, match="ansible-playbook not installed"):
        helper.ansible_python()


def test_needs_ansible_marker_skips_only_without_ansible_playbook(tmp_path, monkeypatch, on_path):
    on_path("#!/bin/sh\n")
    assert helper._skip_marker().args[0] is False
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    absent = helper._skip_marker()
    assert (absent.name, absent.args[0]) == ("skipif", True)
