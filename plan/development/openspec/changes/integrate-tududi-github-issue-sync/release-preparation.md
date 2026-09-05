# Tududi sync: preservation release preparation

Status: **local safety implementation; production gates remain open**. This
feature targets `dev`. It is not a production release or permission to promote
all of `dev`. No live ownership, credential, workflow or inventory mutation was
performed during this safety preparation.

## Reviewed source boundary (2026-09-05 snapshot)

| Source | Exact revision |
| --- | --- |
| Public `main` baseline | `11792d2604acb7745d9e2ad7c4eb209e5bf4ce4c` |
| Public `dev` base for this feature | `2077363e93d45c1b8e9984b7d12cfcc7eb32d981` |
| Private configuration `main` | `2857a98de1430ad70bbf940a19cfb5bd41110458` |
| Pending private transport declaration | `2e6739945836fa832b20dd15b73e8ab8e88bf417` |

These are preparation snapshots, not release-time assertions. The exact final
public release hash does not exist yet. Record and review it after this change
passes through `dev`, together with the final private configuration hash.
The base comparison contains 141 commits / 120 changed files, including unrelated
identity, proxy, Postiz, backup and migration changes. Bulk promotion is excluded.

The selective production patch must include the sync implementation from PR159
(merge `108c090`), this preservation change, and the multi-upstream firewall
implementation from PR161 (merge `2077363`, implementation `6395ff1`). Include:

1. The Tududi sync core, templates, mapping, verifier, contract and tests; the
   provision, refresh, verify and store-token playbooks; their sync task includes
   and the token helper. Preserve the current nine-pair declaration with only
   dev-test enabled. Add the shared deactivation include from this change.
2. The shared `bao-merge-keys.yml` helper (source `f1ee378`, absent on main) for
   initial token provisioning, and `resolve-become-password.yml` (source
   `444eca2`, absent on main) for the firewall playbook. Review their dependency
   on `assert-bao-transport.yml` against the exact main version.
3. Semaphore schedule upsert support (source `af2c8e1`) and only the sync template
   declarations, including the proof-only survey default. Existing unchanged
   GitHub signing, target preflight and repository declarations need no copy.
4. Relevant firewall and safety tests, including the CI `ansible-core` dependency.
   Select files/hunks rather than whole unrelated commits. Test the assembled
   patch on its own main base before approval; passing on dev is insufficient.

## Safety behavior and evidence

Provisioning never deletes workflow or credential objects. Disabled mappings and
boolean/string `false` use the same provider-independent deactivation/read-back
path. Stale workflows are deactivated; current objects are updated by identity.
Only the exact `tududi-github-sync:` namespace is owned. Duplicate names and
truncated lists refuse changes. **Above 250 workflows, pagination must be
implemented and verified before this release can offer a complete rollback**;
a truncated list deliberately fails instead of claiming partial success.

Token validation proves the stored value for the configured service account.
Failure cannot rotate, replace or revoke access. Initial mint requires both an
absent stored token and no active labelled rows. An interrupted mint stops with
a names-only diagnostic; it may leave a row requiring reconciliation. No deletion
or revocation is authorized to clear that state. Semaphore defaults the token
survey to proof-only; explicitly selecting false is an initial-provisioning action.

The fixture suite executes the actual Ansible control flow against disposable
loopback endpoints, including read-back failure, disabled re-runs, initial
creation, duplicate/truncated-list refusal and steady-state credential proof.
The container-helper test executes the actual helper with app-model doubles.
These prove control flow, not deployed API compatibility or production readiness.

## Production gates and sequence

1. Resolve task 6.0: preserve the chosen service account and every existing owner.
   The owner-of-record choice remains undecided. Prove that the operator and
   service account both see GitHub-created work without inventing an ownership
   transition. Check dev-test for competing local cycles and cross-instance
   markers; preserve existing records and linkage.
2. Review the exact selective public/private diff and obtain direct approval for
   the concrete operations. No production operation proceeds from this
   document alone.
3. Publish the private transport declaration through the existing
   `platform/semaphore/sync-inventory.yml` mechanism and verify the static
   inventory read-back. A private Git merge alone does not publish it. Run the
   reviewed firewall revision scoped to tududi only after comparing live rules,
   defaults and existing access; the playbook also enables UFW and sets defaults.
4. Verify deployed versions, complete engine inventory, transport and existing
   credentials. Task 371's HTTP -1 proved a transport failure, not an invalid
   token. Task 390's missing refresher playbook exposed the main binding gap.
   Place reviewed templates/schedule through setup tooling; verify their exact
   repository, inventory and variable-group bindings. Provision dev-test only.
5. Prove both directions, shared visibility, quiet cycles, hierarchy, descriptions,
   priority and duplicate resistance; run the per-pair gate and observe successful
   scheduled token refresh plus subsequent sync. Enable approved private pairs
   individually, starting with huhhb. Public agent-cloud stays disabled.

The verifier preserves provider business data but places helper scripts and mints
an ephemeral GitHub token. Include those effects in the approved operation scope.
No whole-service redeploy, n8n migration, seed, clean or restore is implied.

## Stop without destruction

Use the reviewed provisioning template with `sync_enabled=false`. Require a
complete workflow listing, read back every owned workflow inactive, and wait for
in-flight executions to finish. Preserve objects, credentials, records and
markers. Deactivation stops future work; it does not undo propagated edits.
Reverting firewall inventory does not remove previously added rules; leave the
narrow declaration in place until a separately reviewed access change.

Tasks 6.0, 6.1, 6.3 and 6.4 remain open. This preparation does not prove live
visibility, production convergence or successful memory retention, and the
OpenSpec change must not be archived on the strength of local tests.
