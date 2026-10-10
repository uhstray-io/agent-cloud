"""Source contract for the controller-only Semaphore attribution diagnostic."""

import importlib.util
import json
import signal
import time
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/diagnose-semaphore-task-attribution.py"
PLAYBOOK = ROOT / "platform/playbooks/diagnose-o11y-semaphore-attribution.yml"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"
SPEC = importlib.util.spec_from_file_location("semaphore_attribution", HELPER)
ATTRIBUTION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ATTRIBUTION)

REVISION = "a" * 40
TOKEN = "secret-token-never-returned"
PROJECT_ID = "7"
TASK_ID = "41"
NONCE_HEX = (b"n" * 32).hex()


def prepared(monkeypatch, task_id=TASK_ID, project_id=PROJECT_ID):
    monkeypatch.setattr(ATTRIBUTION.secrets, "token_bytes", lambda size: b"n" * size)
    return ATTRIBUTION.prepare({
        "task_id": task_id,
        "project_id": project_id,
        "revision": REVISION,
    })


def api_rows(task_id=41, project_id=7, status="running", template_id=99,
             template_name=ATTRIBUTION.TEMPLATE_ID,
             playbook=ATTRIBUTION.PLAYBOOK_ID, branch="dev"):
    task = {
        "id": task_id,
        "project_id": project_id,
        "template_id": template_id,
        "status": status,
        "playbook": playbook,
        "git_branch": branch,
        "repository_id": 5,
        "inventory_id": 17,
    }
    template = {
        "id": template_id,
        "project_id": project_id,
        "name": template_name,
        "playbook": ATTRIBUTION.PLAYBOOK_ID,
        "repository_id": 5,
        "inventory_id": 17,
    }
    repository = {"id": 5, "project_id": project_id, "git_branch": "dev"}
    return task, template, repository


def fake_reader(marker, outputs=None, *, metadata=None, template=None, repository=None):
    task, expected_template, expected_repository = api_rows()
    metadata = [task] if metadata is None else metadata
    template = expected_template if template is None else template
    repository = expected_repository if repository is None else repository
    outputs = ([{"task_id": 41, "time": "t", "output": marker}] if outputs is None else outputs)
    calls = []

    def read(path):
        calls.append(path)
        if path.endswith("/tasks/41"):
            return metadata.pop(0) if isinstance(metadata, list) and len(metadata) > 1 else (
                metadata[0] if isinstance(metadata, list) else metadata
            )
        if path.endswith("/templates/99"):
            return template
        if path.endswith("/repositories/5"):
            return repository
        if path.endswith("/tasks/41/output"):
            return outputs.pop(0) if isinstance(outputs, list) and outputs and isinstance(outputs[0], list) else outputs
        raise AssertionError(path)

    return read, calls


def verify_input(marker, nonce_hex=NONCE_HEX, **updates):
    data = {
        "api_origin": "http://127.0.0.1:3000",
        "token": TOKEN,
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "revision": REVISION,
        "marker": marker,
        "nonce_hex": nonce_hex,
    }
    data.update(updates)
    return data


def test_prepare_emits_only_versioned_digest_and_uses_256_bit_nonce(monkeypatch):
    observed = []

    def nonce(size):
        observed.append(size)
        return b"z" * size

    monkeypatch.setattr(ATTRIBUTION.secrets, "token_bytes", nonce)
    result = ATTRIBUTION.prepare({"task_id": TASK_ID, "project_id": PROJECT_ID, "revision": REVISION})
    assert observed == [32]
    assert result["status"] == "prepared"
    assert result["marker"].startswith(ATTRIBUTION.MARKER_PREFIX)
    assert len(result["marker"].split(":", 1)[1]) == 64
    assert result["nonce_hex"] == (b"z" * 32).hex()


@pytest.mark.parametrize(
    ("task_id", "project_id", "reason"),
    [("41/42", "7", "invalid_task_id"), ("41", "0", "invalid_project_id")],
)
def test_prepare_rejects_forged_or_malformed_candidate_ids(task_id, project_id, reason):
    try:
        ATTRIBUTION.prepare({"task_id": task_id, "project_id": project_id, "revision": REVISION})
    except ATTRIBUTION.Refusal as error:
        assert str(error) == reason
    else:
        pytest.fail("invalid candidate was accepted")


def test_task_and_output_metadata_must_match_candidate_id(monkeypatch):
    marker = prepared(monkeypatch)["marker"]
    task, _, _ = api_rows(task_id=42)
    read, _ = fake_reader(marker, metadata=task)
    result = ATTRIBUTION.verify(verify_input(marker), read=read, sleep=lambda _seconds: None)
    assert result["status"] == "unverified"
    assert result["reason"] == "task_metadata_mismatch"
    assert result["marker_count"] == 0

    read, _ = fake_reader(
        marker,
        outputs=[{"task_id": 42, "time": "t", "output": marker}],
    )
    result = ATTRIBUTION.verify(verify_input(marker), read=read, sleep=lambda _seconds: None)
    assert result["status"] == "unverified"
    assert result["reason"] == "output_task_mismatch"


def test_forged_digest_is_not_observed(monkeypatch):
    marker = prepared(monkeypatch)["marker"]
    forged = marker[:-1] + ("0" if marker[-1] != "0" else "1")
    read, calls = fake_reader(marker)
    result = ATTRIBUTION.verify(verify_input(forged), read=read, sleep=lambda _seconds: None)
    assert result["reason"] == "marker_invalid"
    assert result["marker_count"] == 0
    assert calls == []


def test_verify_aggregate_deadline_interrupts_slow_read_and_restores_signal_state(monkeypatch):
    if ATTRIBUTION.signal.getitimer(ATTRIBUTION.signal.ITIMER_REAL) != (0.0, 0.0):
        pytest.skip("process already has an active real-time timer")
    marker = prepared(monkeypatch)["marker"]
    previous_handler = signal.getsignal(signal.SIGALRM)

    def slow_read(_path):
        time.sleep(1)

    started = time.monotonic()
    result = ATTRIBUTION.verify(
        verify_input(marker), read=slow_read, sleep=lambda _seconds: None,
        deadline_seconds=0.03,
    )

    assert result["status"] == "unverified"
    assert result["reason"] == "verification_deadline_exceeded"
    assert time.monotonic() - started < 0.5
    assert signal.getsignal(signal.SIGALRM) is previous_handler
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_stolen_marker_with_different_valid_nonce_fails_before_api(monkeypatch):
    prepared_result = prepared(monkeypatch)
    stolen_nonce = bytes.fromhex(prepared_result["nonce_hex"])
    different_nonce = bytes((value ^ 0xff) for value in stolen_nonce).hex()
    read, calls = fake_reader(prepared_result["marker"])

    result = ATTRIBUTION.verify(
        verify_input(prepared_result["marker"], nonce_hex=different_nonce),
        read=read,
        sleep=lambda _seconds: None,
    )

    assert result["reason"] == "marker_invalid"
    assert calls == []


@pytest.mark.parametrize(
    ("marker_kind", "nonce_hex"),
    [("absent", (b"n" * 32).hex()), ("malformed", (b"n" * 32).hex()),
     ("valid", "z" * 64)],
)
def test_absent_or_malformed_marker_and_nonce_refuse_before_api(
    monkeypatch, marker_kind, nonce_hex
):
    prepared_result = prepared(monkeypatch)
    marker = {
        "absent": "",
        "malformed": ATTRIBUTION.MARKER_PREFIX + "bad",
        "valid": prepared_result["marker"],
    }[marker_kind]
    read, calls = fake_reader(marker)

    result = ATTRIBUTION.verify(
        verify_input(marker, nonce_hex=nonce_hex),
        read=read,
        sleep=lambda _seconds: None,
    )

    assert result["reason"] == "marker_invalid"
    assert calls == []


@pytest.mark.parametrize(
    "context",
    [
        {"task_id": "42"},
        {"project_id": "8"},
        {"revision": "b" * 40},
    ],
)
def test_marker_cannot_be_substituted_across_framed_context(monkeypatch, context):
    prepared_result = prepared(monkeypatch)
    read, calls = fake_reader(prepared_result["marker"])

    result = ATTRIBUTION.verify(
        verify_input(prepared_result["marker"], **context),
        read=read,
        sleep=lambda _seconds: None,
    )

    assert result["reason"] == "marker_invalid"
    assert calls == []


@pytest.mark.parametrize(
    ("task_id", "project_id", "reason"),
    [("", PROJECT_ID, "invalid_task_id"), (TASK_ID, "", "invalid_project_id")],
)
def test_missing_task_candidate_or_inventory_project_refuses_before_api(
    task_id, project_id, reason
):
    read, calls = fake_reader("")
    result = ATTRIBUTION.verify(
        verify_input("", task_id=task_id, project_id=project_id),
        read=read,
        sleep=lambda _seconds: None,
    )

    assert result["reason"] == reason
    assert calls == []


def test_duplicate_marker_is_rejected(monkeypatch):
    marker = prepared(monkeypatch)["marker"]
    read, _ = fake_reader(marker, outputs=[
        {"task_id": 41, "time": "t", "output": marker},
        {"task_id": 41, "time": "t", "output": marker},
    ])
    result = ATTRIBUTION.verify(verify_input(marker), read=read, sleep=lambda _seconds: None)
    assert result["reason"] == "duplicate_marker"
    assert result["marker_count"] == 2
    assert result["status"] == "unverified"


def test_delayed_marker_is_bounded_and_reports_partial_only_after_match(monkeypatch):
    marker = prepared(monkeypatch)["marker"]
    delayed = [[], [{"task_id": 41, "time": "t", "output": marker}]]
    read, calls = fake_reader(marker, outputs=delayed)
    sleeps = []
    result = ATTRIBUTION.verify(
        verify_input(marker), read=read, sleep=lambda seconds: sleeps.append(seconds)
    )
    assert result["status"] == "partial"
    assert result["reason"] == "marker_observed"
    assert result["marker_count"] == 1
    assert result["output_row_count"] == 1
    assert len([path for path in calls if path.endswith("/output")]) == 2
    assert sleeps == [ATTRIBUTION.POLL_SECONDS]
    assert "authenticated" not in json.dumps(result)


@pytest.mark.parametrize(
    ("outputs", "reason"),
    [
        (["malformed-row"], "output_rows_invalid"),
        ([{"task_id": 41, "time": "t", "output": None}], "output_rows_invalid"),
        ([{"task_id": 41, "time": "t", "output": "x"}] * (ATTRIBUTION.MAX_ROWS + 1), "output_rows_invalid"),
    ],
)
def test_malformed_or_excessive_output_rows_refuse(monkeypatch, outputs, reason):
    marker = prepared(monkeypatch)["marker"]
    read, _ = fake_reader(marker, outputs=outputs)
    result = ATTRIBUTION.verify(verify_input(marker), read=read, sleep=lambda _seconds: None)
    assert result["status"] == "unverified"
    assert result["reason"] == reason


@pytest.mark.parametrize(
    ("status", "reason"),
    [("success", "terminal_task"), ("failed", "terminal_task"), ("unexpected", "task_context_unverifiable")],
)
def test_terminal_or_unrecognized_task_state_refuses(monkeypatch, status, reason):
    marker = prepared(monkeypatch)["marker"]
    task, _, _ = api_rows(status=status)
    read, _ = fake_reader(marker, metadata=task)
    result = ATTRIBUTION.verify(verify_input(marker), read=read, sleep=lambda _seconds: None)
    assert result["reason"] == reason


def test_template_and_repository_context_are_required(monkeypatch):
    marker = prepared(monkeypatch)["marker"]
    _, template, _ = api_rows(template_name="unexpected")
    read, _ = fake_reader(marker, template=template)
    result = ATTRIBUTION.verify(verify_input(marker), read=read, sleep=lambda _seconds: None)
    assert result["reason"] == "template_context_mismatch"

    _, template, repository = api_rows()
    repository["git_branch"] = "main"
    read, _ = fake_reader(marker, repository=repository)
    result = ATTRIBUTION.verify(verify_input(marker), read=read, sleep=lambda _seconds: None)
    assert result["reason"] == "repository_context_mismatch"


def test_nullable_task_overrides_fall_back_but_context_overrides_refuse(monkeypatch):
    marker = prepared(monkeypatch)["marker"]
    task, _, _ = api_rows()
    task.update(playbook=None, git_branch=None, repository_id=None, inventory_id=None)
    read, _ = fake_reader(marker, metadata=task)
    result = ATTRIBUTION.verify(verify_input(marker), read=read, sleep=lambda _seconds: None)
    assert result["status"] == "partial"

    for key, value in (("playbook", "other.yml"), ("git_branch", "main"),
                       ("repository_id", 6), ("inventory_id", 18)):
        task, _, _ = api_rows()
        task[key] = value
        read, calls = fake_reader(marker, metadata=task)
        result = ATTRIBUTION.verify(
            verify_input(marker), read=read, sleep=lambda _seconds: None
        )
        assert result["reason"] == "task_context_mismatch"
        assert calls


def test_context_change_after_marker_observation_refuses(monkeypatch):
    marker = prepared(monkeypatch)["marker"]
    task, _, _ = api_rows()
    changed = dict(task, git_branch="main")
    read, _ = fake_reader(marker, metadata=[task, changed])

    result = ATTRIBUTION.verify(verify_input(marker), read=read, sleep=lambda _seconds: None)

    assert result["status"] == "unverified"
    assert result["reason"] == "task_context_mismatch"
    assert result["marker_count"] == 1


def test_oversized_and_redirected_responses_are_sanitized(monkeypatch):
    class Response:
        status = 200

        def read(self, _limit):
            return b"x" * (ATTRIBUTION.MAX_RESPONSE + 1)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Client:
        def open(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(ATTRIBUTION, "build_opener", lambda *_args: Client())
    with pytest.raises(ATTRIBUTION.Refusal, match="response_too_large"):
        ATTRIBUTION._client_read("http://127.0.0.1:3000", TOKEN, "/api/project/7/tasks/41")

    class RedirectClient:
        def open(self, *_args, **_kwargs):
            raise HTTPError("http://127.0.0.1:3000", 302, "secret location", {}, None)

    monkeypatch.setattr(ATTRIBUTION, "build_opener", lambda *_args: RedirectClient())
    prepared_result = prepared(monkeypatch)
    result = ATTRIBUTION.verify(
        verify_input(prepared_result["marker"]),
        read=lambda _path: ATTRIBUTION._client_read(
            "http://127.0.0.1:3000", TOKEN, "/api/project/7/tasks/41"
        ),
        sleep=lambda _seconds: None,
    )
    assert result["reason"] == "redirect_refused"
    assert "secret location" not in json.dumps(result)


def test_public_result_never_contains_credentials_or_raw_output(monkeypatch):
    marker = prepared(monkeypatch)["marker"]
    raw = "private log line with /private/path and " + TOKEN
    read, _ = fake_reader(marker, outputs=[{"task_id": 41, "time": "t", "output": raw}])
    result = ATTRIBUTION.verify(verify_input(marker), read=read, sleep=lambda _seconds: None)
    serialized = json.dumps(result)
    assert TOKEN not in serialized
    assert "/private/path" not in serialized
    assert "private log line" not in serialized
    assert "nonce" not in serialized
    assert "authenticated" not in serialized


def test_playbook_guard_check_mode_and_marker_output_contract():
    playbook = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    assert playbook[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert playbook[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    play = playbook[2]
    assert play["hosts"] == "localhost"
    tasks = play["tasks"]
    check_mode = next(task for task in tasks if "Report check mode" in task["name"])
    assert check_mode["when"] == "ansible_check_mode"
    assert "check_mode_unverified" in str(check_mode["ansible.builtin.debug"])
    prepare = next(task for task in tasks if "Prepare one framed" in task["name"])
    assert prepare["when"] == "not ansible_check_mode"
    assert prepare["semaphore_task_attribution"]["mode"] == "prepare"
    assert prepare["no_log"] is True
    marker = next(task for task in tasks if "Emit the digest marker" in task["name"])
    assert marker["ansible.builtin.debug"]["msg"] == "{{ _attribution_prepared_raw.marker }}"
    verify = next(task for task in tasks if "Read back only" in task["name"])
    assert verify["no_log"] is True
    assert verify["when"] == [
        "not ansible_check_mode", "_attribution_prepared_raw.status == 'prepared'"
    ]
    assert verify["semaphore_task_attribution"]["mode"] == "verify"
    assert "semaphore_vars.task_details.id" in str(prepare["semaphore_task_attribution"])
    assert "o11y_attribution_project_id" in str(prepare["semaphore_task_attribution"])
    assert "o11y_attribution_project_id" in str(verify["semaphore_task_attribution"])
    assert "semaphore_project_id" not in PLAYBOOK.read_text(encoding="utf-8")
    assert "nonce_hex" not in str(verify["semaphore_task_attribution"])
    assert "SEMAPHORE_TASK_ID" not in PLAYBOOK.read_text(encoding="utf-8")
    assert "SEMAPHORE_PROJECT_ID" not in PLAYBOOK.read_text(encoding="utf-8")
    runtime_access = next(task for task in tasks if "Resolve the existing controller" in task["name"])
    assert runtime_access["ansible.builtin.include_tasks"] == "../semaphore/tasks/runtime-access.yml"
    assert any("ansible_check_mode" in condition for condition in runtime_access["when"])
    assert "podman" not in PLAYBOOK.read_text(encoding="utf-8")


def test_template_is_dev_bound_exact_sha_survey_without_schedule():
    catalog = yaml.safe_load(TEMPLATES.read_text(encoding="utf-8"))
    entries = [item for item in catalog["templates"]
               if item.get("name") == ATTRIBUTION.TEMPLATE_ID]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["repository"] == "agent-cloud dev"
    assert entry["playbook"] == ATTRIBUTION.PLAYBOOK_ID
    assert [item["name"] for item in entry["survey_vars"]] == ["expected_repository_sha"]
    assert "schedule" not in entry and "cron" not in entry
