## 1. Publisher boundary and fixtures

- [x] 1.1 Reject explicit `jwt_params.ttl` declarations before writes and delete the custom duration parser.
- [x] 1.2 Remove TTL from valid publisher fixtures and test early-refusal for `5m` and `25h`, including a valid earlier environment-bearing declaration.
- [x] 1.3 Preserve native enabled/audience publication, scoped live JWT TTL, outer-object readback, and mutation refusal; document the supported subset and future max-binding prerequisite.
- [x] 1.4 Validation gate: run the focused publisher fixture and catalog tests, Ruff, Ansible syntax, strict worktree-local `openspec validate semaphore-jwt-native-default-ttl`, and `git diff --check`; verify the explicit-TTL refusal scenario and scoped-live-TTL scenario. **Receipt:** 53 tests and 40 subtests passed; Ruff, both publisher playbook syntax checks, worktree-local OpenSpec validation, and diff check passed.
