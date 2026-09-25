"""The capability rule for seeding one OpenBao KV-v2 path (tasks/assert-bao-seed-access.yml).

A seed reads the path first, then POSTs a new path or PATCHes an existing one, so it needs
`read` plus `create` (new) or `patch` (existing); `root` covers everything. A GET alone cannot
tell a missing path from one the token cannot see, which is why capabilities decide (reviews
of PR #205). A filter so the rule is one function, tested directly.
"""


def seed_access_missing(capabilities, exists):
    """The capabilities this seed still lacks on its path, joined for a message; '' when none."""
    held = set(capabilities or [])
    if "root" in held:
        return ""
    return " and ".join(c for c in ("read", "patch" if exists else "create") if c not in held)


class FilterModule:
    def filters(self):
        return {"seed_access_missing": seed_access_missing}
