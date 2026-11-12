"""Whether an inventory is a local-dev inventory, from the names of its hosts (local_mode).

The placement and linger tasks branch on `local_mode`, and an extra var outranks the inventory,
so `-e local_mode=true` on a production host would take the local branch against it. The shared
guard (tasks/assert-local-mode-identity.yml) decides the mode from the inventory instead, with
this filter over `groups['all']`:

- a local-dev inventory names every host `<name>-local` (bootstrap-local-dev.yml, the example
  inventory platform/inventory/local-dev.yml.example); no production host does;
- `localhost` is the controller and is dropped first. Local-dev's controller host is named
  `localhost` (its explicit ungrouped entry, or an implicit one), so it must not count against
  the rule. A production inventory names its controller the same way, and that is not a
  reason to call it local-dev.

`groups` is read because `-e` cannot set it: clean-service.yml:85-89 verified that on
ansible-core 2.21, and it is why the teardown reads the hosts rather than the variable.

Fail closed: an input that is not a list of host-name strings raises AnsibleFilterError, which
fails the guard. An empty inventory (nothing but `localhost`) is not local-dev.

Tested directly by platform/tests/test_local_dev_filter.py.
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
