# Tasks: shared automation helpers

Proposal awaiting operator approval — do not start. One helper per PR. Every migration batch
is preceded by a characterization task covering exactly the sites that batch replaces;
characterization is never sampled. Acceptance test in brackets.

## 0. Test helpers
- [ ] 0.1 `ratchet_compare` helper generalising `ledger_citation_ratchet.txt` [pytest: count rise fails, fall passes]
- [ ] 0.2 `fake_http.serve()` + `FakeLoki` [test_fake_http.py covers both]
- [ ] 0.3 `harness_sandbox.env_for` [test_harness_sandbox.py]
- [ ] 0.4 `playbook_yaml.play()` / `template()` [pytest on a fixture playbook]
- [ ] 0.5 Validation gate: scenario "Refactor PR gate"

## 1. CI and pre-push speed
- [ ] 1.1 Verify GNU parallel on the runner; switch `bats platform/tests/` to `--jobs` [CI green, wall time recorded]
- [ ] 1.2 Cache pip + collections across the three jobs [cache hit on second run]
- [ ] 1.3 Merge duplicate expensive tests [assertion inventory unchanged]
- [ ] 1.4 Pre-push uses the same parallel flag [hook run green]
- [ ] 1.5 Validation gate: scenario "CI run"

## 2. OpenBao login task
- [ ] 2.1 Add `tasks/bao-login.yml` and a ratchet seeded at 47 sites [BATS: transport refusal, login no_log]
- [ ] 2.2 For each batch of ≤5 files (10 batches over 46 files): characterize every login site in the batch — transport assertion present or absent, no_log scope, token fact name [pytest per file]
- [ ] 2.3 Migrate that batch; its characterization stays green except the intended addition of a transport assertion to the 17 files without one, recorded in the test [ratchet falls]
- [ ] 2.4 Validation gate: scenarios "A new hand-rolled login is refused", "Cleartext endpoint refused before login"

## 3. Step-result fold and failure capture
- [ ] 3.1 `tasks/capture-failure.yml` (no_log-aware) and per-host fold in `emit-step-result` [tests: one-host failure, no_log leak]
- [ ] 3.2 For each batch of ≤4 of the 12 cited playbooks: characterize its recorded step result and rescue message [pytest per playbook]
- [ ] 3.3 Migrate that batch [characterization green]
- [ ] 3.4 Validation gate: scenarios "One host fails", "A no_log task fails"

## 4. Secret reads and deploy key
- [ ] 4.1 Characterize rollback (:232, :596) and conformance (:166) reads incl. current drift
- [ ] 4.2 `tasks/agw-read-secret.yml`; migrate the three sites [characterization green]
- [ ] 4.3 Characterize both deploy-key reads (backup-credentials :157, backup-step-ca :95)
- [ ] 4.4 Move the fetch into `site-config-clone.yml`; migrate both [characterization green]
- [ ] 4.5 Validation gate: scenario "Callers keep their view"

## 5. Internal certificates
- [ ] 5.1 Characterize issued leaf fields, validation refusals and classifier outputs
- [ ] 5.2 `files/internal_leaf.py` + pytest; task keeps validation only
- [ ] 5.3 Classifier filter plugin [pytest]
- [ ] 5.4 Characterize renewal reload/proof per leaf and current DNS record list
- [ ] 5.5 Per-leaf declarations; DNS derived from server-profile leaves [BATS/pytest]
- [ ] 5.6 Validation gate: scenarios "Leaf fields unchanged", "Invalid declaration refused before issuance", "New server-profile leaf"

## 6. agentgateway readiness and compose helpers
- [ ] 6.1 Characterize every agentgateway readiness/probe consumer (inventory them first) [BATS]
- [ ] 6.2 `gateway-ready.sh` + `vars/agw-probe-target.yml`; migrate consumers [characterization green]
- [ ] 6.3 Characterize compose invocation in each deploy.sh that will adopt the helpers [BATS]
- [ ] 6.4 `compose_files()` / `compose_up_if_changed`; migrate [BATS]
- [ ] 6.5 Validation gate: scenarios "Unchanged inputs", "Gateway not ready"

## 7. Guards and hygiene
- [ ] 7.1 Characterize each playbook that will adopt `require-reviewed-checkout`, then adopt [pytest]
- [ ] 7.2 Characterize current `preflight-target-group` callers; add `target_service` mode [BATS]
- [ ] 7.3 Characterize `check-o11y-liveness` exit paths; revoke token in `always:` [pytest]
- [ ] 7.4 Drop literal `caddy_probe_host` default (`wire-caddy-cloudflare-token.yml:37`) [refute_grep + fail test]
- [ ] 7.5 OPA `launch_branches` sync test vs Semaphore repository records [pytest]
- [ ] 7.6 Characterize the two webhook shape checks; shared assert task replaces both [BATS]
- [ ] 7.7 Characterize the PVE waits in provision-vm, resize-vm, provision-template, destroy-vm; shared wait task [characterization green]
- [ ] 7.8 Validation gate: scenarios "Unreviewed checkout", "Unknown target service", "Liveness check fails", "Probe host unset", "Branch lists diverge", "Malformed webhook URL"
