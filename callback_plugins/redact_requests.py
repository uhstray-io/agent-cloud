"""The default stdout callback, minus the request of every NESTED result.

ansible-core drops a result's `invocation` (the module arguments it was called with, request
headers included) only at the TOP level, and only below -vvv (plugins/callback/__init__.py,
`_dump_results`). A result nested inside another - a registered `uri` read that is the `item`
of a failed loop step, or one of a loop's `results` - keeps its `invocation`, so a NetBox
token or a Semaphore Bearer value in its headers reaches the task output. Reproduced at default
verbosity on ansible-core 2.16.18, 2.19.13 and 2.20.8 (docs/MISTAKES.md 4.6).

This strips `invocation` from every nested result, at every verbosity, and leaves the top level
to ansible-core's own rule. Output is otherwise the default callback's, byte for byte.
Enabled for every run from the repository root by ansible.cfg (Semaphore runs from there).
"""

from collections.abc import Mapping

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


class CallbackModule(DefaultCallback):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "redact_requests"

    def _dump_results(self, result, *args, **kwargs):
        return super()._dump_results(strip_nested_invocations(result), *args, **kwargs)
