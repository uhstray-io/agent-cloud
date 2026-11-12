"""Whether an inventory is local-dev, from the names of its hosts (local_mode).

tasks/assert-local-mode-identity.yml checks `local_mode` against this, because an extra var outranks
the inventory. Every host but `localhost` must be named `<name>-local`, and there must be at least
one. Callers pass a host list `-e` cannot set (`groups['all']` or
`ansible_play_hosts_all`). An input that is not a list of host-name
strings raises AnsibleFilterError, so the guard fails closed. Tested by
platform/tests/test_local_dev_filter.py.
"""

import re
from collections.abc import Iterable, Mapping

from ansible.errors import AnsibleFilterError

# The regex clean-service.yml uses for the same question (its `reject('match', ...)` on line 70).
# `\Z`, not `$`: a trailing newline must not pass as a local-dev name.
LOCAL_DEV_NAME = re.compile(r"[A-Za-z0-9._-]+-local\Z")
CONTROLLER = "localhost"


def local_dev_inventory(hosts):
    """True when every host but the controller is named `<name>-local`, and there is at least one."""
    if isinstance(hosts, (str, bytes, Mapping)) or not isinstance(hosts, Iterable):
        raise AnsibleFilterError(
            "local_dev_inventory expects a list of host names, got "
            f"{type(hosts).__name__}; refusing rather than guessing the inventory's identity")
    names = list(hosts)
    if not all(isinstance(name, str) for name in names):
        raise AnsibleFilterError(
            "local_dev_inventory expects host names that are strings; refusing rather than "
            "guessing the inventory's identity")
    others = [name for name in names if name != CONTROLLER]
    return bool(others) and all(LOCAL_DEV_NAME.match(name) for name in others)


class FilterModule:
    def filters(self):
        return {"local_dev_inventory": local_dev_inventory}
