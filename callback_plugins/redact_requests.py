"""The default stdout callback, minus the request of every NESTED result.

ansible-core drops a result's `invocation` (the module arguments it was called with, request
headers included) only at the TOP level, and only below -vvv (plugins/callback/__init__.py,
`_dump_results`). A result nested inside another - a registered `uri` read that is the `item`
of a failed loop step, or one of a loop's `results` - keeps its `invocation`, so a NetBox
token or a Semaphore Bearer value in its headers reaches the task output. Reproduced at default
verbosity on ansible-core 2.16.18, 2.19.13 and 2.20.8 (docs/MISTAKES.md 4.6).

This strips `invocation` from every nested result, at every verbosity, and leaves the top level
to ansible-core's own rule; an unlabeled loop item, which is displayed as its label, is stripped
the same way. Output is otherwise the default callback's, byte for byte.

It also hides the error detail of a failed `no_log` task. ansible-core 2.19 moved the error
out of the result dict into an object beside it, and the censoring that empties the result
keeps that object: the default callback then prints "[ERROR]: Task failed", the exception's
message, its whole "caused by" chain and the source context, all of which can carry a
run-time value (a filter's input quoted in its own error, an assert's rendered `fail_msg`,
a rendered argument quoted by the "Finalization of task args" failure). 2.18 printed nothing
of the sort for a censored result. The censoring keeps a task's warnings and deprecations as
well, and those are hidden the same way. Reproduced on 2.20.8 with the fake secret of
platform/tests/test_no_log_error_redaction.py; the same play on 2.18.15 prints none.
Enabled for every run from the repository root by ansible.cfg (Semaphore runs from there).
"""

from collections.abc import Mapping

from ansible import constants as C
from ansible.plugins.callback.default import CallbackModule as DefaultCallback

DOCUMENTATION = """
    name: redact_requests
    type: stdout
    short_description: default output without the request of any nested result
    description:
        - The default callback, with C(invocation) removed from every nested result.
    extends_documentation_fragment:
      - default_callback
      - result_format_callback
    requirements:
      - set as stdout_callback in configuration
"""


def strip_nested_invocations(value, top=True):
    """A copy of `value` with `invocation` removed from every mapping below the top one."""
    # ponytail: matches the key at any depth, so an API response field that happens to be
    # named `invocation` is hidden from the display too; the registered value is untouched.
    if isinstance(value, Mapping):
        return {k: strip_nested_invocations(v, top=False) for k, v in value.items()
                if top or k != "invocation"}
    if isinstance(value, (list, tuple)):
        return [strip_nested_invocations(v, top=False) for v in value]
    return value


def is_no_log_result(result, task_result):
    """True when `result` (a result dict) or its task is no_log. Any one signal is enough.

    `censored` is what ansible-core's own censoring leaves in a no_log result, including one that
    failed before the module ran and so never carried `_ansible_no_log`.
    """
    return (
        "censored" in result
        or bool(result.get("_ansible_no_log"))
        or getattr(task_result.task, "no_log", None) is True
    )


class CallbackModule(DefaultCallback):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "redact_requests"

    def _dump_results(self, result, *args, **kwargs):
        return super()._dump_results(strip_nested_invocations(result), *args, **kwargs)

    def _get_item_label(self, result):
        # A loop with no loop_control.label shows the item itself as its label, and that item
        # can be a registered result carrying its request (Codex review of PR #247).
        return strip_nested_invocations(super()._get_item_label(result), top=False)

    def _handle_exception(self, result, use_stderr=False):
        # ansible-core 2.19+ keeps the exception on the callback's current task result, beside
        # the result dict; 2.18 has no such attribute and never displayed it for a censored
        # result, so it is left to the parent there.
        current = getattr(self, "_current_task_result", None)
        if current is not None and current.exception and is_no_log_result(result, current):
            result.pop("exception", None)
            self._display.display(
                f"[ERROR]: Task failed: '{current.task.get_name()}' "
                "(details hidden: the task is no_log)",
                color=C.COLOR_ERROR,
                stderr=use_stderr,
            )
            return
        super()._handle_exception(result, use_stderr=use_stderr)

    def _handle_warnings(self, res):
        # The same preservation as the exception above: censoring keeps `warnings` and
        # `deprecations`, and a module's own message can quote a value it was handed. 2.18
        # censors both away, so only 2.19+ has anything to hide.
        current = getattr(self, "_current_task_result", None)
        if current is not None and is_no_log_result(res, current):
            hidden = len(current.warnings) + len(current.deprecations)
            res.pop("warnings", None)
            res.pop("deprecations", None)
            if hidden:
                self._display.display(
                    f"[WARNING]: '{current.task.get_name()}' emitted {hidden} warning(s)/deprecation(s); "
                    "hidden: the task is no_log",
                    color=C.COLOR_WARN,
                )
            return
        super()._handle_warnings(res)
