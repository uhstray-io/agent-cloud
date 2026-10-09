"""The NAMES of the extra vars this run was launched with (refuse-internal-extra-vars.yml).

Connection identity (ansible_host, ansible_user, ansible_connection, ansible_ssh_common_args,
...) is legitimately defined by the inventory, so its presence in hostvars cannot tell an
inventory value from a forged `-e`: extra vars outrank the inventory and appear on every host.
The guard has to ask where the value came from, and no public template-visible variable or
filter says so (no `ansible_extra_vars` magic variable; checked on ansible-core 2.21).

INTERNAL API, used on purpose: `ansible.utils.vars.load_extra_vars` (utils/vars.py:182) is
the function VariableManager itself calls to load the command-line extra vars
(vars/manager.py:139), so it returns exactly the set that outranks the inventory, from every
`-e` form (key=value, JSON, @file). Behaviour is pinned by tests that run a real
ansible-playbook (platform/tests/test_connection_identity_vars.py), not by a version check.

Two rules:
  * Names only. A value may be a secret; nothing here returns, logs or raises one. The
    original exception is dropped (`from None`) because a parse error can quote the option.
  * Fail closed. Any problem raises AnsibleFilterError, which fails the guard's assert. It
    never returns an empty list, which would read as "no extra vars".
"""

from ansible.errors import AnsibleFilterError


def extra_var_names(_value=None):
    """Sorted names of the command-line extra vars. The input is ignored: a Jinja filter needs
    one, so the guard writes `'' | extra_var_names`."""
    try:
        from ansible.parsing.dataloader import DataLoader
        from ansible.utils import vars as ansible_vars

        return sorted(str(name) for name in ansible_vars.load_extra_vars(DataLoader()))
    except Exception as exc:
        raise AnsibleFilterError(
            f"cannot read the extra vars this run was launched with ({type(exc).__name__}); "
            "refusing rather than assuming there are none") from None


class FilterModule:
    def filters(self):
        return {"extra_var_names": extra_var_names}
