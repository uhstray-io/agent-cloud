"""In-process, controller-only task-output attribution readback."""

import importlib.util
from pathlib import Path

from ansible.plugins.action import ActionBase

_HELPER_PATH = Path(__file__).resolve().parents[1] / "files" / "diagnose-semaphore-task-attribution.py"
_SPEC = importlib.util.spec_from_file_location("semaphore_task_attribution_helper", _HELPER_PATH)
_HELPER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPER)


def _unverified(reason):
    return {
        "status": "unverified",
        "reason": reason,
        "metadata_context_matches": False,
        "template_context_matches": False,
        "repository_branch_dev": False,
        "marker_count": 0,
        "output_row_count": 0,
    }


class ActionModule(ActionBase):
    TRANSFERS_FILES = False

    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        task_vars = task_vars or {}
        if self._play_context.check_mode:
            return {**result, **_unverified("check_mode_unverified")}
        args = self._task.args
        mode = args.get("mode")
        try:
            if mode == "prepare":
                value = _HELPER.prepare({
                    "task_id": args.get("task_id"),
                    "project_id": args.get("project_id"),
                    "revision": args.get("revision"),
                })
            elif mode == "verify":
                prepared = task_vars.get("_attribution_prepared_raw")
                nonce = prepared.get("nonce_hex") if isinstance(prepared, dict) else None
                value = _HELPER.verify({
                    "api_origin": args.get("api_origin"),
                    "token": task_vars.get("_token"),
                    "task_id": args.get("task_id"),
                    "project_id": args.get("project_id"),
                    "revision": args.get("revision"),
                    "marker": args.get("marker"),
                    "nonce_hex": nonce,
                })
            else:
                value = _unverified("invalid_mode")
        except _HELPER.Refusal as error:
            value = _unverified(str(error))
        except Exception:
            value = _unverified("invalid_input")
        return {**result, "changed": False, **value}
