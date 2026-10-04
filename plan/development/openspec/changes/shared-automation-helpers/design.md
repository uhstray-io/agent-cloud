# Design: shared automation helpers

Author: Joseph A. Wisneski IV. Proposal awaiting operator approval.

## Principles

- **Behaviour-preserving.** Each helper replaces copies without changing outputs, verdicts,
  `set_stats` shape, `no_log` boundaries or check-mode behaviour.
- **Characterization first.** Before a copy is replaced, a test pins its current observable
  behaviour (rendered task structure via `playbook_yaml`, recorded step result, log
  redaction). The refactor must leave that test green.
- **One helper per PR**, ordered by dependency and blast radius.
- **Ratchet.** Each helper ships a guard test listing remaining hand-rolled sites; the count
  may only fall (same mechanism as `ledger_citation_ratchet.txt`).

## Phase order (dependency, then risk)

| Phase | Work | Why here |
|---|---|---|
| 0 | Test helpers (ratchet_compare, fake_http `serve()` + FakeLoki, `harness_sandbox.env_for`, `playbook_yaml.play/template`) | Every later phase's characterization tests use them |
| 1 | CI/pre-push speed | Cheap, no runtime effect; shortens every later PR |
| 2 | `bao-login.yml` | Foundation for 3, 4 and hygiene; highest copy count (47) |
| 3 | `capture-failure.yml` + `emit-step-result` folding | Depends on nothing runtime; touches 12 playbooks, so done in batches |
| 4 | `agw-read-secret.yml`; site-config deploy-key fetch | Uses `bao-login`; drift reconciled by returning the raw store plus derived flags so both callers keep their view |
| 5 | Internal-leaf split, renewal declarations, DNS from leaves | Highest logic risk; isolated behind characterization of issued cert fields |
| 6 | agentgateway readiness, compose helpers | Shell lib change affects every service; last of the runtime-bearing work |
| 7 | Guard adoption + hygiene | Small independent items |

## Key decisions

- **`bao-login.yml` owns transport assertion and login** (includes
  `assert-bao-transport.yml`), is the only `no_log` boundary, and sets one token fact. It never
  prints or registers the response outside its boundary (MISTAKES 4.6 rule).
- **`capture-failure.yml` is `no_log`-aware:** when the failed task was `no_log`, it records the
  task name only, never the result.
- **agw read drift:** the helper returns the secret map under a `no_log` fact plus non-secret
  presence flags; callers keep their current derived names. Characterization fixes both
  current views before merge.
- **Python out of YAML:** `files/internal_leaf.py` is unit-tested with pytest; the classifier
  becomes a filter plugin so templates and tasks share one definition.
- **`caddy_probe_host`** loses its literal default; absence fails with a named message.

## Risks

- Large playbooks (provision-vm) under refactor: mitigated by batching and characterization.
- Shell lib change in `common.sh`: covered by BATS before and after.
- `bats --jobs` needs GNU parallel on the runner (unverified: whether the CI image has it — the
  task checks before enabling).
