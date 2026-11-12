# 24 — Local-dev mode read from the inventory, never from `local_mode`

**Date:** 2026-11-12
**Status:** PROPOSED
**Author:** Joseph A. Wisneski IV
**Context:** Plane AC-726, under AC-696 in the `skynet-launch-containment` module. skynet and the launch gate already read the mode this way (AC-784, #1127). `local_mode` lifts production refusals and selects local-only branches, and an extra var outranks the inventory, so `-e local_mode=true` on a production launch changes what runs. AC-695 (#1078), AC-696 (#1105) and AC-702 (#1106) closed it for skynet, the launch gate and the shared placement, linger and OpenBao-transport tasks by checking `local_mode` against the inventory's identity (`tasks/assert-local-mode-identity.yml`). The 2026-11-12 grounding checkpoint found that a value check cannot close a templated extra var: an extra var renders afresh at each read (`docs/MISTAKES.md` 1.15), so a template can render an honest value for the check and `true` for a later read. On 2026-11-12 the operator chose option (b): keep AC-698's decision (no run-start refusal of `-e local_mode` by name) and make nothing decide on `local_mode` at all. This plan is that option.

## Decision

Every reader takes local-dev mode from the inventory's identity: `(groups['all'] | local_dev_inventory)` (filter `platform/playbooks/filter_plugins/local_dev.py`: every host but `localhost` named `<name>-local`, at least one). `groups` is a magic variable an extra var cannot set (`tasks/clean-service.yml:85-89`), and the repository proves it: `platform/tests/test_bao_transport_identity.py::test_forged_groups_do_not_make_a_production_inventory_local_dev` passes `-e` of `groups` and is still refused.

Every real local-dev inventory reads as local-dev under the filter (checked 2026-11-12): the one inventory the local Semaphore uses (`bootstrap-local-dev.yml`, `_inv_ini`) names `localhost` and sixteen `<name>-local` hosts; genesis runs on `platform/inventory/local-dev.yml`, copied from `local-dev.yml.example`, whose hosts are all `<name>-local`; and since AC-696 the identity check already refuses any placement run whose inventory disagrees.

- **Inline, with no intermediate variable.** A play or task variable holding the result can itself be set with `-e`. Underscore names are refused by the run-start guard (`refuse-internal-extra-vars.yml`), but `--limit` excluding localhost and `--start-at-task` skip that play (its own header). The inline filter call has no such hole.
- **`local_mode` stays an inventory declaration.** The local-dev inventory keeps setting it, and `tasks/assert-local-mode-identity.yml` keeps refusing a value that disagrees with the identity, so a misdeclared inventory is still caught. After this plan it is the only reader.
- **Templates too.** `.j2` files rendered by the template module read the same expression; `groups` and the repository's filters are available there (`ansible.cfg:19`).
- **`deploy.sh`'s `LOCAL_MODE`** is set by the playbook from the same expression.

Rejected: refusing `-e local_mode` by name at run start (closes it in one place, but reverses AC-698, and is skipped by `--limit` excluding localhost); pinning the mode once per run in a `set_fact` (an extra var outranks a fact, so a templated `-e` of the fact's name re-opens it when the run-start guard is skipped).

## Scope (measured on `dev` at `b8f4b28e`, before AC-784)

`local_mode` appears 467 times in 128 files under `platform/playbooks/` and `platform/services/` (`git grep -c`), 298 of them as `local_mode | default(false ...`. By area:

| Area | Files | Occurrences |
|---|---|---|
| o11y | 58 | 197 |
| skynet, launch gate, OPA | 13 | 69 |
| shared tasks | 6 | 53 |
| other | 19 | 44 |
| CA and Caddy | 9 | 26 |
| agentgateway | 8 | 25 |
| console | 6 | 24 |
| service workflow | 3 | 22 |
| local-dev only | 6 | 21 |

Counts include comments; each PR re-counts its own files. Tests: 22 files set `local_mode` true 69 times to drive local branches (`git grep` under `platform/tests/`).

Out of scope: `tasks/clean-service.yml`, `clean-deploy-o11y.yml` and `stop-local-service.yml` (their own `-local` rule, AC-730); `rollback-inference-route.yml`'s keep-id check (AC-729).

## Enforcement — a ratchet test

`platform/tests/test_local_mode_reads.py` lists every file under `platform/playbooks/` and `platform/services/` and fails when a file reads `local_mode` outside its allowlist. It parses rather than greps, so message prose that names the variable (`fail_msg: "local_mode is ..."`) is not a read:

- YAML files: the values of `when`, `that`, `failed_when`, `changed_when` and `until` are bare Jinja; every other string contributes only its `{{ }}` and `{% %}` blocks. Keys are not reads, so inventory text that assigns `local_mode` (as `bootstrap-local-dev.yml` writes) is not one either.
- `.j2` files: only the Jinja blocks.
- Python under `platform/playbooks/` (filter plugins, `files/`) and the services' shell scripts: a `local_mode` / `LOCAL_MODE` read is listed with the reason it is allowed (a shell script reads the `LOCAL_MODE` the playbook exports from the inventory identity), or refused.

- Permanent exemptions: `tasks/assert-local-mode-identity.yml` (it compares the declaration), and inventory writers named in the test with the reason.
- A migration allowlist, created in the first PR with every file that reads it today. The test also fails when an allowlisted file no longer reads `local_mode`, so each PR must shrink the list; the last PR empties it and deletes the list.
- Mutation check per PR: re-adding one raw read to a migrated file turns the test red.

## Test fixtures

A test that drives a local branch with `-e local_mode=true` on a host not named `<name>-local` stops reaching that branch. Each PR moves its playbooks' tests to `-local` host names (the AC-695 and AC-696 pattern: `skynet-local`, `demo-local`, `local_mode_inventories.py`), keeping `local_mode: true` where the identity check runs. A test whose play targets only `localhost` needs a `-local` host added to its inventory, not a rename: the filter ignores `localhost`, and an inventory with no other host is not local-dev.

Twenty-two test files import `jinja2` and mention `local_mode` (`git grep`); a template rendered with a plain `jinja2.Environment` has neither `groups` nor the repository's filters. The PR that migrates a template updates its tests' render to provide both (one shared helper, added by the first PR that needs it). A forged-value test (`forgeries.templated_forgeries`) per migrated playbook family proves a production inventory takes the production branch with `-e local_mode=true`, plain and templated.

The first PR adds a fourth forgery form to `platform/tests/forgeries.py`: honest for exactly the renders the identity check makes, then forged. The test counts the check's renders in a first run, then reruns with that count. `stateful` is honest only on the first render, and the identity check reads `local_mode` more than once, so the check itself refuses `stateful`. That makes `stateful` unable to show a later raw read. The new form passes the check and then shows any reader that still reads `local_mode`: the AC-784 review found the gap when its mutations (a derivation put back on `local_mode`) passed every behavioural test and failed only the text pins.

## Known risk — `add_host` changes `groups['all']` during a run

`configure-host-network.yml:311` adds `<host>-newaddr`, which does not end in `-local`; on a local-dev run every read after it would see a non-local inventory. `renew-internal-certs.yml:313`, `distribute-ssh-keys.yml:83` and `provision-vm.yml:1093` add declared or inventory names. The PR that migrates each of these four reads its `add_host` order first and either keeps the reads before it or names the added host so the identity holds.

## Sequence (one PR each, highest risk first, per AC-726)

1. **1a — ratchet, forgery form and the shared tasks.** This plan; `test_local_mode_reads.py` with the full migration allowlist; the new forgery form in `forgeries.py`; and the shared tasks every deploy reaches: `tasks/place-monorepo.yml`, `tasks/enable-linger.yml` (its test asserts each gated linger task is skipped by name, the #1120 review's open finding), `tasks/install-podman-compose.yml`, `tasks/assert-bao-transport.yml` and `tasks/deploy-orb-agent.yml`.
2. **1b — the high-risk gates:** `rotate-console-credentials.yml`, `collect-service-conformance.yml`, `verify-service-persistence.yml`, `ensure-service-persistence.yml` and `instrument-host-o11y.yml`.
3. **Gates that run before a playbook's placement check:** `deploy-o11y.yml`, `deploy-agentgateway.yml`, `deploy-orb-agent.yml`, `deploy-step-ca.yml`; and `tasks/agw-validate-config.yml`, `tasks/assert-o11y-scrape-limits.yml`, `tasks/o11y-alert-probe.yml`.
4. **The o11y family not yet covered**, including the readers the grounding checkpoint found outside AC-726's list: `tasks/o11y-private-artifact-operation.yml`, `tasks/o11y-backup-artifact-census.yml`, `tasks/o11y-pbs-physical-storage-survey.yml`, `tasks/prepare-o11y-availability.yml`, `tasks/o11y-prepare-expected-target-transition.yml`; and the o11y templates. Split in two if the diff passes about 40 files.
5. **CA, Caddy, agentgateway rest, console, authentik and the remaining playbooks**, with the four `add_host` playbooks audited as above.
6. **Close:** empty the allowlist and delete it; `AGENTS.md` states the rule beside the credential rules; `docs/MISTAKES.md` names the test as the enforcement for the forged-mode class.

## Release budget

Each `dev` → `main` promotion after v0.15.0 is capped at 100 pull requests and 120 changed files (`23-release-train.md`). v0.15.0 is cut: the dev merge guard reported "6/100 pull requests and 19/120 files since v0.15.0" on 2026-11-12. This plan changes about 150 files including tests, more than one release holds, so it spans at least two releases. Each PR is sized to fit beside the release's other work, and the guard's budget line is read before each merge. The guard also requires a `simplify` and a `security-review` commit status on each PR's exact final head (`scripts/merge-when-green.py`), so a smaller PR also makes a re-run after review cheaper.

## Acceptance

- `test_local_mode_reads.py` passes with no migration allowlist.
- The full pytest and BATS suites pass, locally and in CI.
- For each family, a production inventory run with `-e local_mode=true` (plain and templated) takes the production branch.
- Local-dev deploys still run: one `make local-deploy-<svc>` per family on the local Semaphore, recorded in its PR.
