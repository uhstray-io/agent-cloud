# GitHub Environments (config-as-code)

GitHub environments that gate a workflow job, stored as JSON and applied with
[`apply.sh`](./apply.sh). The same pattern as [`.github/rulesets/`](../rulesets/README.md):
edit the JSON, run the script, and re-running converges instead of duplicating.

## Environments

| File | Environment | Gates |
|------|-------------|-------|
| [`release-major.json`](./release-major.json) | `release-major` | The manual major release job in [`release-version.yml`](../workflows/release-version.yml) |

### `release-major`

- **Required reviewer:** the operator, by numeric user id (`24553698`). A `major` run
  waits for this approval before its job starts.
- **Deployment branch policy:** only the branch `main` may deploy to this environment
  (`protected_branches: false`, `custom_branch_policies: true`, one policy named `main`).
  The workflow's `if:` also requires `refs/heads/main`.
- **Self-review allowed (`prevent_self_review: false`).** The operator is the only
  maintainer with admin rights, and dispatches the run and approves it. With
  `prevent_self_review: true`, the user who dispatched the run could not approve it, so a
  major release could never be cut. The approval is therefore an explicit operator
  confirmation of that one run, not a second person's review.

## Applying

`apply.sh` needs `jq`. It needs `gh` authenticated as a repository admin, but only with
`--apply`.

```bash
# Dry run (the default): prints the requests it would send, makes no API call
.github/environments/apply.sh

# Apply: PUT the environment, create the `main` branch policy only if absent, read back
.github/environments/apply.sh --apply
```

The JSON is the whole declaration this repository makes for the environment. The published
request schema for the PUT sets `additionalProperties: false`, so keep the file to the keys
the schema lists (`wait_timer`, `prevent_self_review`, `reviewers`, `deployment_branch_policy`).
Settings made outside this file are not declared here.

**Apply before the first `major` dispatch.** A job that names an environment which does not
exist creates it with no protection rules, and a job running in an unprotected environment
skips the approval. Run `--apply` once, then confirm the read-back shows the reviewer and the
`main` policy.

## Checking the live state (read-only)

```bash
gh api repos/uhstray-io/agent-cloud/environments/release-major \
  --jq '{protection_rules, deployment_branch_policy}'
gh api repos/uhstray-io/agent-cloud/environments/release-major/deployment-branch-policies \
  --jq '.branch_policies[] | {name, type}'
```

The live settings have not been read back against the JSON since it was written. Until
`--apply` has run and its read-back is recorded, the environment's live state is unverified.
