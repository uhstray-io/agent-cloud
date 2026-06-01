---
name: coderabbit-preflight-checklist
description: Recurring CodeRabbit/CI findings to fix UPFRONT (before pushing) so PRs pass the first review. Distilled from PRs #19, #27, #28.
metadata:
  node_type: memory
  type: reference
---

Apply these BEFORE pushing a PR. Each is a finding CodeRabbit or CI has flagged repeatedly on this repo — fixing them upfront avoids review round-trips (and conserves the CodeRabbit hourly rate limit; see [[coderabbit-rate-limits]]).

## Markdown (markdownlint)

- **Every fenced code block needs a language** (MD040). Use `text` for trees/diagrams/plain output, `bash`, `json`, `hcl`, `yaml`, etc. Closing ``` fences are exempt.
- **Relative links must resolve.** Count the directory depth from the file to the repo root and verify (`[ -e ../../x ] && echo OK`). A file N dirs deep needs N `../` to reach root. Deep service READMEs are the usual offender.

## Ansible (ansible-lint — CI hard-fails on these)

- **`name[casing]`: every play/task `name:` starts with an uppercase letter.** (Module params like `name: git` don't count.) Run `ansible-lint` locally before pushing — it's the fastest way to catch this.
- **`retries`/`delay` do NOTHING without `until`.** Any `uri`/`command` poll loop needs `register: x` + `until: x.status == 200` (or similar) or it runs exactly once.
- **`failed_when`/`changed_when` list form is AND.** `failed_when: [a, b, c]` fails only when ALL are true. For "fail on any error except X" use a single bool expr: `failed_when: result is failed and 'X' not in (result.msg | default('') | lower)`.
- **Mark detection/check tasks `changed_when: false`** (a `command`/`shell` that only reads state).
- **`ansible.builtin.template` resolves `src` on the CONTROLLER, not the remote.** Point it at a `{{ playbook_dir }}/../...` path, never a remote clone path like `{{ _deploy_dir }}/...`.
- **`apt_key` is removed on Ubuntu 24.04** (and deprecated). Use the signed-by keyring pattern: `get_url` the (`.asc`) key into `/etc/apt/keyrings/`, then `apt_repository` with `repo: "deb [signed-by=/etc/apt/keyrings/<x>.asc] ..."`.
- **`set -o pipefail` + `head -1`** false-fails when the upstream writes more than one line (SIGPIPE). Drop the `head`/pipe and slice in Ansible, or avoid pipefail for that probe.
- **No self-referencing vars** (`jinja[invalid]` recursion). A `vars:` entry `x: "{{ x | default(d) }}"` references itself → infinite templating. Default *inside* the consuming template, or source from a differently-named var.
- **`no-handler`** fires when a task's `when:` is solely `<registered>.changed`. For an included composable task where the step must run in-sequence (not deferred to a play-end handler flush), `# noqa: no-handler` with a one-line justification is the accepted fix.
- **Delegated tasks resolve vars from the DELEGATED host**, not the current one. For `delegate_to: caddy_host`, take `container_engine` / `ansible_user` from `hostvars[target].*` (default engine **podman**), not the play host's values.

## Ansible — generated config fragments + cross-service wiring

- **Validate before you persist; a reload-only guard isn't enough.** When a playbook writes a rendered config fragment (Caddy `sites/*.caddy`, nginx, etc.), `<engine> exec ... validate` the FULL config and **roll back** (restore prior / remove) on failure. A bad fragment that only fails `reload` still sits on disk and breaks the *next container restart*.
- **Distinguish engine/container faults from config-parse errors.** A non-zero `<engine> exec ... validate` rc can mean "container down", not "bad config". Do a reachability pre-check (`caddy version`); on an engine fault **fail without rolling back** (the fragment is probably fine — a false rollback silently reverts a good change). Only roll back when validate fails *after* confirmed reachability.
- **Coordinate cross-service endpoints in INVENTORY, not via silent fact fallbacks.** Don't compute another service's upstream as `hostvars[...].ansible_default_ipv4 | default('127.0.0.1')` — a missing fact renders a valid-but-wrong fragment routing to loopback. Require an explicit inventory var (`<svc>_minio_upstream`) and `assert` it. A play only gathers its own hosts' facts anyway.
- **Guard path-traversal on any name concatenated into a delegated dest path.** `assert` it's a safe basename, e.g. `name is match('^[a-z0-9][a-z0-9-]*\.caddy$')`.
- **HSTS baseline:** `Strict-Transport-Security max-age=15552000;` — do NOT add `includeSubDomains`/`preload` prematurely (preload is hard to reverse; per UhhCraft SPEC it's deferred to post-launch). Keep fragments consistent with the central Caddyfile.

## Ansible secrets ↔ env templates (platform convention)

- **`tasks/manage-secrets.yml` injects credentials as a `secrets` dict.** Service `env.j2` templates MUST read `{{ secrets.<name> }}` for every credential (like all NetBox templates) — NOT flat `<service>_<name>` vars (those are never set → blank creds).
- **Split: secrets vs config.** Credentials → `_secret_definitions` (become `secrets.*`). Non-secret deployment config (URLs, ports, buckets, tuning) → inventory/group_vars `<service>_*` with `| default(...)`.
- **`_secret_definitions` names == the `secrets.*` keys the template reads == the app's required env keys.** Keep all three in lockstep.

## Compose / Docker

- **No `:latest`.** Pin third-party images to a tag/digest; make the service's own image overridable via env (`${SVC_IMAGE:-...}`).
- **No `build:` in a prod compose** — deploy pulls a CI-built image; the stack never builds at deploy time.
- **Percent-encode credentials embedded in connection URLs** (`DATABASE_URL`, `REDIS_URL`): `{{ secrets.pw | urlencode }}` — a `@ : / # ? %` in a password corrupts parsing otherwise.
- **Dockerfile:** match the pip interpreter to the runtime python (`python3.11 -m ensurepip` + `python -m pip`, not the distro `python3-pip`); add a `HEALTHCHECK`; set `--start-period` for slow first-boot (model/weight load).
- **Pin security-sensitive deps** (e.g. `python-multipart>=0.0.27`).

## Go / app config

- **Env-var names in `env.j2`/`.env.example` must match `config.go` exactly** — the app boots via `requireEnv()` and panics on a missing/misnamed key. Keep `env.j2`, `.env.example`, and `config.go` in lockstep.
- **Propagate `ctx`** into outbound HTTP calls (use the `*WithContext` SDK method / `http.NewRequestWithContext`).
- **Don't leak internals in error responses** — never return `str(e)`/raw errors to clients (CodeQL flags information exposure); log full detail server-side, return a generic message.
- **Money as integer cents**, never float. Atomic DB mutations (`UPDATE ... RETURNING`, `INSERT ... ON CONFLICT`) for idempotency.

## SQL / migrations

- **No redundant index** on a column that already has a `UNIQUE` constraint (the constraint creates one).
- **Status-regression guards:** terminal states immutable; forward-only transitions (`WHERE status = 'pending'`, `WHERE status IN (...)`).
- **Honor `active`/hidden state** in every read path; **never purge in-flight rows** (e.g. `status NOT IN ('pending','processing')`).
- **Normalize email** (CITEXT) and add source/ownership CHECK constraints.

## Process

- Run the relevant linters locally before pushing: `ansible-lint`, `templ generate && go build ./... && go vet ./...`, `ruff check`, `python -m py_compile`, `yaml.safe_load`. Then the secret/IP audit (no RFC1918 IPs, no literal creds).
