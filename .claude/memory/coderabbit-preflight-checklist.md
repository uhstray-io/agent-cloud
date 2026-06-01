---
name: coderabbit-preflight-checklist
description: Recurring CodeRabbit/CI findings to fix UPFRONT (before pushing) so PRs pass the first review, plus how to triage a CodeRabbit review. Distilled from PRs #19, #27, #28, #30, #32, #35, #36, #40. STANDING PRACTICE — every CodeRabbit finding we confirm as real gets distilled into this file so the next PR prevents it upfront.
metadata:
  node_type: memory
  type: reference
---

Apply these BEFORE pushing a PR. Each is a finding CodeRabbit or CI has flagged repeatedly on this repo — fixing them upfront avoids review round-trips (and conserves the CodeRabbit hourly rate limit; see [[coderabbit-rate-limits]]).

## Markdown (markdownlint)

- **Every fenced code block needs a language** (MD040). Use `text` for trees/diagrams/plain output, `bash`, `json`, `hcl`, `yaml`, etc. Closing ``` fences are exempt.
- **Relative links must resolve.** Count the directory depth from the file to the repo root and verify (`[ -e ../../x ] && echo OK`). A file N dirs deep needs N `../` to reach root. **Both directions break:** too few `../` (Phase 3) *and* too many (PR #40 had `../../../platform/...` from a `plan/architecture/` doc — only 2 levels deep — which escapes the repo root). `plan/architecture/*` and `plan/development/*` are both 2 deep → `../../` to root, `../development/` or `../architecture/` to the sibling dir. After writing links, grep the diff for `\.\./\.\./\.\./` in any 2-deep doc.
- **No ASCII-art box-drawing in `plan/**.md`** — CI hard-fails on Unicode `[\x{2500}-\x{257F}]`. All diagrams must be fenced ```mermaid```. (Root files like `kickstart.md` with tree `├──` art are exempt — the gate only scans `plan/`.)

## Markdown / docs internal consistency (PR #40)

CodeRabbit reviews `plan/**/*.md` for internal consistency — a new section that contradicts the same doc's existing body, or an index whose status disagrees with the source's frontmatter, gets flagged. Check these before pushing a docs PR:

- **A new section must not contradict the doc's own body.** PR #40's biggest catch was self-inflicted: an appendix table cell claimed `depends_on: condition: service_healthy` makes the app "wait for postgres/redis/minio to report healthy" — directly contradicting the **same file's §4**, which says podman-compose 1.0.6 parses but does NOT enforce it. When you add an appendix/example to an existing doc, re-read the sections it touches and align claims.
- **Index status must match the source doc's frontmatter.** `architecture-reference.md` listed `WEBSMITH-INTEGRATION-PLAN.md` as `IMPLEMENTED` while the plan's own `**Status:**` was `ACTIVE` and it was 9/11. Don't mark a multi-phase plan "IMPLEMENTED" until it's done — mirror the source's status + note the phase count.
- **Use the canonical section names, not one-off variants.** The deviation headings are exactly `## Alignment with agent-cloud conventions` and `## Tracking future deviations` — not `## Deviations from Spec`. Grep the repo for the existing heading before inventing a near-synonym.

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
- **Don't gate an idempotent install on a presence check that misses partial state.** `apt: { name: [a, b], state: present }` is already idempotent — gating it on `when: a_is_missing` skips remediating a host that has `a` but not `b`. Drop the gate; let `state: present` install only what's missing.

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
- **`depends_on: condition: service_healthy` is NOT a working readiness gate on podman-compose 1.0.6** — it's parsed but not enforced (containers start in order without waiting for health). Enforced only on podman-compose ≥ 1.3.0 (and Docker). On Podman VMs, readiness is gated by explicit health-wait helpers in deploy/post-deploy scripts (`lib/common.sh`), with `depends_on` kept for documentation + Docker compat. See `plan/architecture/PODMAN-VS-DOCKER-COMPOSE.md` §4. Never describe `service_healthy` as the effective wait mechanism on this platform.

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

## Secrets / IPs in committed files

- **Even commented "placeholder" IPs fail the CI gate if they're RFC1918.** `10.0.0.x`, `192.168.x.x`, `172.16–31.x.x` trip the credential-leak / RFC1918 audit regardless of a `# placeholder` comment. In examples/inventory use `<vm-ip>`-style tokens or `{{ host }}` Jinja vars, never a real-looking private IP. (Public IPs like `1.1.1.1` Cloudflare DNS are fine — not RFC1918.)
- **The local audit grep must cover ALL RFC1918 ranges** — `10\.`, `192\.168\.`, AND `172\.(1[6-9]|2[0-9]|3[01])\.`. A grep that only checks `192.168.` misses `10.x` and passes something CI then rejects.

## Process

- Run the relevant linters locally before pushing: `ansible-lint`, `templ generate && go build ./... && go vet ./...`, `ruff check`, `python -m py_compile`, `yaml.safe_load`. Then the secret/IP audit covering all RFC1918 ranges (see above) + no literal creds.

## Working with CodeRabbit reviews (triage discipline)

- **Verify cited "coding guidelines" against the actual repo.** CodeRabbit has fabricated non-existent rules (e.g. claimed secret templates must use `{{ vault_* }}`/`{{ _secret_* }}` — that guideline exists nowhere here; the real mechanism is `manage-secrets.yml`'s `secrets.*` dict, confirmed by every NetBox template). Don't blindly comply — confirm, and **push back with evidence** (precedent files, the actual mechanism). Blindly "fixing" the secrets namespace would have rendered every credential blank.
- **A `fail` check from rate-limiting is NOT a finding** — see [[coderabbit-rate-limits]]. Distinguish it from `fail` + real findings before reacting.
- **Active vs resolved findings:** an inline comment with `line: null` is outdated/resolved (re-anchored against an old commit) — ignore it; only `line != null` comments are live on the current commit.
- **Skipping a finding is fine when it's wrong** — reply with the concrete reason (precedent, contradicting config, our `.yamllint.yml` 200-char limit, etc.), then `@coderabbitai review`. CodeRabbit consistently concedes well-evidenced pushback.
- **Loop:** fix real ones + skip-with-evidence the false positives → re-run local linters → push → `@coderabbitai review` → confirm `line:null` on the resolved ones. Batch fixes per round.
- **STANDING PRACTICE — close the loop into this file.** Every CodeRabbit finding we *confirm as real* (especially self-inflicted ones like the PR #40 Podman contradiction) gets distilled into the relevant section above before the PR merges, so the next PR prevents it upfront instead of re-discovering it. Preventing the mistake beats triaging it. False positives we rebut don't go here — but a recurring *category* of false positive (e.g. CodeRabbit's repeated "pin actions to SHA" / "doc lives elsewhere" claims) is worth a one-line note so we rebut fast.
