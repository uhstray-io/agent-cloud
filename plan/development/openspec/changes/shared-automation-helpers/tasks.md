# Tasks: shared automation helpers

Proposal awaiting operator approval — do not start. One helper per PR, characterization test
first, ratchet test with each helper. Acceptance test in brackets.

## 0. Test helpers
- [ ] 0.1 `ratchet_compare` helper generalising `ledger_citation_ratchet.txt` [pytest: count rise fails, fall passes]
- [ ] 0.2 `fake_http.serve()` + `FakeLoki` [test_fake_http.py covers both]
- [ ] 0.3 `harness_sandbox.env_for` [test_harness_sandbox.py]
- [ ] 0.4 `playbook_yaml.play()` / `template()` [pytest on a fixture playbook]
- [ ] 0.5 Validation gate: scenario "Refactor PR gate" — helpers usable by characterization tests

## 1. CI and pre-push speed
- [ ] 1.1 Verify GNU parallel on the runner; switch `bats platform/tests/` to `--jobs` [CI green, wall time recorded]
- [ ] 1.2 Cache pip + collections across the three jobs [cache hit on second run]
- [ ] 1.3 Merge duplicate expensive tests [suite count of assertions unchanged]
- [ ] 1.4 Pre-push uses the same parallel flag [hook run green]
- [ ] 1.5 Validation gate: scenario "CI run"

## 2. OpenBao login task
- [ ] 2.1 Characterize three representative login sites [pytest]
- [ ] 2.2 Add `tasks/bao-login.yml` [BATS: transport refusal, no_log on login]
- [ ] 2.3 Ratchet test seeded at 47 sites; migrate in batches, lowering the count [ratchet]
- [ ] 2.4 Validation gate: scenarios "A new hand-rolled login is refused", "Cleartext endpoint refused before login"

## 3. Step-result fold and failure capture
- [ ] 3.1 Characterize step results of the 13 listed playbooks [pytest]
- [ ] 3.2 `tasks/capture-failure.yml` (no_log-aware) [test: no result content leaks]
- [ ] 3.3 `emit-step-result` per-host errors/evidence fold [test: one-host failure]
- [ ] 3.4 Migrate playbooks in batches of ≤4 [characterization green]
- [ ] 3.5 Validation gate: scenarios "One host fails", "A no_log task fails"

## 4. Secret reads and deploy key
- [ ] 4.1 Characterize rollback (:232, :596) and conformance (:166) reads incl. current drift
- [ ] 4.2 `tasks/agw-read-secret.yml`; migrate three sites [characterization green]
- [ ] 4.3 Deploy-key fetch into `site-config-clone.yml`; migrate both backup playbooks [BATS]
- [ ] 4.4 Validation gate: scenario "Callers keep their view"

## 5. Internal certificates
- [ ] 5.1 Characterize issued leaf fields and classifier outputs
- [ ] 5.2 `files/internal_leaf.py` + pytest; task keeps validation only
- [ ] 5.3 Classifier filter plugin [pytest]
- [ ] 5.4 Per-leaf reload/proof declarations in renewal; DNS records derived from server-profile leaves [BATS/pytest]
- [ ] 5.5 Validation gate: scenario "Refactor PR gate"

## 6. agentgateway readiness and compose helpers
- [ ] 6.1 `gateway-ready.sh` + `vars/agw-probe-target.yml`; migrate consumers [BATS]
- [ ] 6.2 `compose_files()` / `compose_up_if_changed` in `platform/lib/common.sh` [BATS]
- [ ] 6.3 Validation gate: scenario "Refactor PR gate"

## 7. Guards and hygiene
- [ ] 7.1 Adopt `require-reviewed-checkout` where an exact commit is required [pytest list]
- [ ] 7.2 `preflight-target-group` `target_service` mode [BATS]
- [ ] 7.3 `check-o11y-liveness` revokes its token in `always:` [pytest structure]
- [ ] 7.4 Drop literal `caddy_probe_host` default; inventory-sourced [refute_grep]
- [ ] 7.5 OPA `launch_branches` sync test vs Semaphore repository records [pytest]
- [ ] 7.6 Discord webhook assert task; migrate three callers [BATS]
- [ ] 7.7 PVE wait task; migrate four callers [characterization]
- [ ] 7.8 Validation gate: scenario "Refactor PR gate"
