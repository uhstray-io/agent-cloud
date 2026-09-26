# MISTAKES.md — recorded failures and the rules they earned

Author: Joseph A. Wisneski IV

A running record of mistakes made while building this platform, kept so they are
not repeated and so the ones that *can* be enforced mechanically get enforced
instead of remembered.

**Every entry names where it is enforced.** A rule that lives only in this file
is the weakest kind — it depends on someone reading it at the right moment. The
goal for each entry is to move it out of "convention" and into a pre-commit
hook, a test, CI, or an OPA policy. The `Enforced by` column is the honest
status, not the aspiration.

Entries are appended, never rewritten. If a rule turns out to be wrong,
supersede it with a new entry and link both.

## How to use this file

- **Before acting on live state** (a secret store, an orchestrator, a host),
  read §3.
- **Before claiming something is verified**, read §1.
- **Before trusting a test you just wrote**, read §2.
- **When adding an OPA rule**, §7 lists the entries that are OPA-shaped and why.

## Index

| # | Mistake | Class | Enforced by |
|---|---------|-------|-------------|
| 1.1 | Claimed a value was copied verbatim when it had been retyped through a string literal | Unverified claim | Convention + test |
| 1.2 | Asserted a config gap that did not exist, without reading the file — **x4** | Unverified claim | Convention + loader test; hook proposed (count ≥ 3) |
| 1.3 | Reported a background job as successful when its exit code had been masked by a pipe — **x2** | Unverified claim | Convention |
| 1.4 | Guessed a resource id instead of reading the one the create call returned | Unverified claim | Convention |
| 1.5 | Claimed per-job containerisation as an enforced control; a job that asked for nothing ran on the host | Unverified claim | Test |
| 1.6 | Called a host addressless from one ARP sweep; it was up and answering, the sweep lost the race | Unverified claim | Convention |
| 1.7 | Recorded a memory as retained on a `completed` status whose result list was empty; no retrievable memory or fact was stored | Unverified claim | Convention |
| 1.8 | Documented an INI encoding as "verified" from a sample with no booleans; the first `true` made the value a string | Unverified claim | Test |
| 1.9 | Documented that a feature branch is invisible to Semaphore; true in the UI only, the API runs any pushed branch | Unverified claim | Convention (OPA branch rule pending) |
| 1.10 | Reported a CodeRabbit review as started from a keyword match; every request had been refused | Unverified claim | Convention |
| 1.11 | Wrote into a gate's own comment that OpenBao returns 404 only to a token allowed to read, without checking; a denied AppRole would have passed the seed access check  | Unverified claim  | Test (synthetic OpenBao, mutation-proven)  |
| 1.12 | **x2** — Reported a 30-minute deploy hang from a check-in timer, not the clock; the task was two minutes in | Unverified claim | Convention |
| 1.13 | Rejected Semaphore's matching single-environment list projection as if it were a second binding | Unverified claim | Test |
| 1.14 | **x2** — Told the user the approved OpenBao address is the inventory's `all.vars`, from the public template; production declares it under a group `localhost` is not in | Unverified claim | Convention |
| 1.15 | **x2** — Said a run-time check closed extra-var overrides; a templated extra var bypasses it, and any launcher may set extra vars | Unverified claim | Convention |
| 1.16 | Wrote "44 files log in to OpenBao" into a merged plan without running a count; the count is 43 | Unverified claim | Convention |
| 2.1 | Test compiled a pattern as raw file text, not as the runtime decodes it | False-green test | Test |
| 2.2 | Test pinned the vulnerable form of a security check in place | False-green test | Test |
| 2.3 | Negative assertion aborted under `set -e` because a no-match grep exits 1 | False-green test | Convention |
| 2.4 | Test asserted state that lived on a different branch | False-green test | CI |
| 2.5 | Safety guard specified without ever being evaluated against the declarations it judges | False-green test | Test |
| 2.6 | Test windows sized by line count, breaking when a line was added inside the construct | False-green test | Convention |
| 2.7 | A test's own quoting terminated its pattern; the subject was correct | False-green test | Convention |
| 2.8 | Repeated 2.6 twice more — assertions forbidding the comment that documents the hazard | False-green test | Convention |
| 2.9 | Fifteen negative assertions that could never fail, cited as verification | False-green test | Test (ratchet) |
| 2.10 | Repeated 2.9 — a `grep -v … \|\| true` assertion that cannot fail, written while fixing that class | False-green test | Test (mutation-verified) |
| 2.11 | Asserted a property of one random draw; ~0.5% of runs failed on unrelated PRs | Flaky test | Test (deterministic) |
| 2.12 | Refuted forbidden verbs, then forbidden modules, instead of asserting a closed set — **x2** | False green on a safety check | Test (closed allow-list) |
| 2.13 | Tested that an ordering fix was present, on a config where fact gathering ran before it | False green on a fix | Test (mutation-proven) |
| 2.14 | Added a 6th rule for resolving one address; the gate and resolver disagreed and the verdict lied | Correctness | Test + **repo-wide normalisation outstanding** |
| 2.15 | Matched a substring/token instead of the anchored construct, twice — a commented guard passed | **x2** False green | Test (anchored + active-construct) |
| 2.16 | Test population selected by the presence of the fix, so deleting the fix made it skip, not fail | Vacuous test | Test (selector on condition) |
| 2.17 | A `become:` keyword on a dynamic `include_tasks` — invalid at runtime, invisible to every static gate | Unrunnable playbook, green suite | Test (closed rule, mutation-proven) |
| 2.18 | A coverage test asserting "every play" over a hand-typed list of four — 40 of 52 were unguarded — **x2** (check-mode guard rooted in one directory) | Vacuous coverage | Test (derived population + ratchet) |
| 2.19 | The app healthcheck probed the path nginx serves from the FRONTEND — green across a backend that never bound | False green | Test (probe path pinned) |
| 2.20 | Idempotency proven on the wrong steady state: the route retire tool refused the adopted-into-managed case, and a `changed_when` parse hid its message | False-green test | Test (adopted-state case + rc-guarded parse) |
| 2.21 | A new deploy playbook shipped without the zero-hosts pre-flight; the orchestrator recorded success with nothing deployed | Wrong-reason pass | Test (this playbook); fleet-wide test proposed |
| 2.22 | The controller fixture returned `secrets: []` where live Semaphore omits an empty secret list | False-green fixture | Shared filter test + playbook fixture |
| 2.23 | Tightened the check under test and left its fixtures alone; three negative cases passed whatever the filters did | Vacuous test | Convention (this instance: mutation-checked) |
| 3.1 | Wrote a probe value over a real credential in a live secret store | Live-state damage | **OPA (proposed)** |
| 3.2 | Attempted to mutate a shared orchestrator credential without asking | Live-state damage | Sandbox + **OPA (proposed)** |
| 3.3 | Treated failed workstation login as a controller access prerequisite | Wrong executor boundary | Test + convention |
| 3.4 | A validation step's cleanup deleted a committed provider lock file | Working-tree damage | Convention |
| 3.5 | Allocated a vmid from an incomplete ledger; provisioning treated the collision as "already exists" and went on to configure the foreign VM | Live state | Test (provision-vm guard) |
| 3.6 | Allocated a static address from the inventory alone; it belonged to a live production runner that the inventory never declared, and the new VM was configured onto it | Live state | Playbook guard + test (provision-vm address probe) |
| 3.7 | A new test's scratch-repo `git init`/`git config`, run by the pre-push hook with git's exported `GIT_DIR`, wrote the shared `.git/config`: `core.bare=true` and a fake identity for every checkout | Live state | Pre-push hook clears the git environment + behavioral test (mutation-proven) |
| 3.8 | Launched a production deploy as a "dry run" through the Semaphore API with a top-level `dry_run` the server ignores; it ran for real through the secret phase | Live state | Test: committed launcher places and gates the flag before launch |
| 4.1 | `while read` silently dropped an unterminated final line | Data handling | Convention |
| 4.2 | Stored `.env` values without stripping surrounding quotes | Data handling | Convention |
| 4.3 | Used a real internal IP address as a test vector | Data leak | Pre-commit (existing) |
| 4.4 | Arithmetic on a fleet API response without defaulting fields absent on offline members | Data handling | Convention |
| 4.5 | Truncated a live inventory by opening it for writing in the expression that computed its content | Live-state damage | Convention |
| 4.6 | **x2** — A failure-path diagnostic printed the very values the success path was built to keep out of stdout | Secret in transcript | Test (static guard, `test_no_request_in_loop_items.py`) + stdout callback (`callback_plugins/redact_requests.py`) |
| 4.7 | An address edit replaced every matching line and left a production runner declared at the new VM's address | Data handling | Playbook guard + test (provision-vm address-claim check) |
| 4.8 | A credential-shaped test fixture was pushed; CI's unscoped all-detectors scan let it fail other PRs | Data handling | CI (scan scoped to the PR's commits) |
| 4.9 | Private Discord destination IDs were copied into a public test fixture | Data handling | Convention |
| 4.10 | **x3** — A heredoc script and a stdin redirect both targeted one interpreter; it parsed the operator token file as source and the syntax error printed the token | Secret in transcript | Convention — **count ≥ 3: the PreToolUse hook is now required, not proposed** |
| 5.1 | Security check duplicated per caller; a fix reached three copies and missed two | Duplication | Test |
| 5.2 | Committed while a test was failing, because the check did not gate the commit (repeat 2026-09-25: a merge after a mergeability read, joined by `;`) | Process | Pre-push hook |
| 5.3 | Merged a PR while its review was rate-limited | Process | Convention (user-stated) |
| 5.4 | A command's own planning boundary honoured over an explicit instruction to implement | Process | Convention + stop hook |
| 5.5 | Repeated 5.2 — committed with a failing test; hooks do not gate the suite | Process | Pre-push hook |
| 5.6 | Repeated 5.2 twice more — committed with a failing suite; hooks did not gate it | Process | Pre-push hook |
| 5.7 | Pushed, opened and merged a PR without the per-action authorization | Process | Convention (user-stated) |
| 5.8 | A required CI gate installed whatever upstream published last | Reproducibility | Pinned binary and SHA256 in CI |
| 5.9 | Added AI attribution trailers to six commits against the repo rule; one was pushed | Process | commit-msg hook |
| 5.10 | Switched branches inside a checkout another task was using; the rule is one worktree per work item | Process | Convention (hook proposed) |
| 5.11 | Started a second push of a branch whose first push was still running, from buffered output read as finished | Process | Convention |
| 5.12 | A bulk check-mode retrofit trusted `changed_when: false`; a dry run stopped and removed the local orb agent | Process | Test |
| 6.1 | Built an edit from an assumed file structure instead of a read one | Process | Convention |
| 6.2 | Built an interface the consumer never calls, without reading how it invokes | Process | Test |
| 6.3 | Repeated 6.2 — assumed openssl and jq exist on the orchestrator image; neither does | Process | Convention -> **Test + declared dep** |
| 6.4 | Reused an inventory variable name for a different fact; the gate read the app's public edge URL and failed, censored | Process | Convention |
| 6.5 | Deleted an Authentik blueprint file to retire its object; the object stayed and the replacement matched it by name | Assumption about files | Convention; the deploy's prod-only redirect VERIFY would have caught it |
| 6.6 | **x2** — The graph tool's auto-index rewrote the committed graph metadata under a path-derived project name while the graph file was deleted, and it sat uncommitted in a shared checkout | Assumption about files | Pre-commit gate + test |
| 6.7 | A task variable shadowed a lazily evaluated play variable and stopped seed-environment provisioning | Assumption about files | Main-variant provisioner integration test |
| 6.8 | Took the volume separator for the container separator; the production NetBox deploy would have waited on a container that does not exist | Assumed runtime semantics | Test (stub engine, mutation-checked) |
| 6.9 | Scoped a restart-policy fix to rootless podman, the runtime a review named; the repo's own test says rootful's boot unit is the same | Assumed runtime semantics | Test (rootful case) |
| 8.1 | Repeated 1.3 — masked an exit code with a pipe, minutes after writing the rule against it | Unverified claim | Convention |
| 8.2 | Referenced tests by identifiers that did not exist — **x2** (a PR number in a commit message) | Unverified claim | Test |
| 8.3 | Took two tool-invocation errors as findings before establishing a baseline | Unverified claim | Convention |
| 8.4 | Proposed a deny rule that failed OPEN on a missing field | Live-state damage | Test + evaluation |
| 11.1 | 76 assertions across the suite could never fail — `!` and `[[ ]]` are exempt from `set -e` | False-green test | **Ratchet test** |
| 11.2 | Sourced a config file instead of reading it, turning every credential into shell code | Live-state damage | Test |
| 11.3 | Committed without running the suite — third occurrence | Process | Convention |
| 10.1 | Documented a config mechanism as complete when nothing consumed it | Unverified claim | Test |
| 10.2 | Assumed a container runtime inherits the image CMD under an entrypoint override | Unverified claim | Test |
| 10.3 | Wrote a probe whose own command was interpolated away, then read the empty result as a finding | Unverified claim | Convention |
| 10.4 | Revert timer could not be re-armed; only the 2nd run fails, which is the retry-after-revert path | Safety mechanism broken when needed | Test (mutation-proven) |
| 10.5 | Added a suite to `testpaths`, which CI overrides with an explicit path — 16 tests ran nowhere | Test not covered | CI (root-level pytest) |
| 10.6 | Wrote a parser from one example file; the grammar showed four deviations it never exercised | Unverified claim | Test (6 grammar cases) |
| 10.7 | Named the rollback hazard, then gated the restore on a condition an earlier failure skips | Live-state damage | Test (block/rescue, mutation-proven) |
| 10.8 | Two Ansible constructs whose semantics only exist at runtime — a word-split `cmd:` and a `vars:` lookup re-evaluated per reference | Half-finished run, credentials left in a clone | Test (closed rule, mutation-proven) |
| 10.9 | Local validation templates were bound to GitHub main, so every "validated locally" run executed code that was not the code being written (×2: the dispatcher re-made it) | Wrong code under validation | Bootstrap record + structural bind + (Local)-first dispatch |
| 10.10 | A register on a skipped task overwrote the passing result it was guarding, misreporting a healthy credential as broken | Assumed runtime semantics | Convention |
| 10.11 | manage-secrets stored secrets with a whole-document POST, deleting every undeclared sibling key on every deploy | Destructive write to live state | Test |
| 10.12 | A numeric id crossed the Ansible→JSON boundary as a string, so an `!==` guard fired on every issue it checked | Silent type coercion | Test |
| 10.13 | `tofu validate` + `plan` passed a ruleset attribute the Cloudflare API rejects on create | Schema ≠ API acceptance | Convention |
| 10.14 | A source-address allowlist was proven only where it could not fail, then failed closed in prod | Test that cannot fail | Convention |
| 10.15 | Reboot survival was asserted for podman containers and never exercised; the boot unit starts only `restart: always`, and its rootless half was never enabled — OpenBao sat down three days | Mechanism never exercised | Test (restart policy + boot unit, mutation-proven) |
| 10.16 | The agentgateway deploy was proven only on ansible-core 2.16, which hid a list-concatenation failure on 2.19+ | Test that cannot fail | Test (real evaluation, current ansible-core) |
| 10.17 | The agentgateway upstream-key guard read a variable that never exists at play level, so it failed every production deploy; local runs disable it | Mechanism never exercised | Test in the verify PR (see entry) |
| 9.1 | A `for` loop with an unconditional `break`, making all but one member unreachable | Minor | Convention |
| 9.2 | Typo'd duplicate key in a hand-assembled payload; call succeeded regardless | Minor | Convention |
| 12.1 | `gh` reported a valid token as invalid because a sandboxed `$HOME` hid the login keychain | Environment visibility | Convention |
| 12.2 | Pi showed no models and OpenCode omitted its provider because a sandboxed `$HOME`/XDG hid the config | Environment visibility | Convention |

---

## 1. Claiming something was verified when it was not

### 1.1 "Carried over verbatim" — when it had gone through a string literal

**What happened.** A security-critical regex was moved from one playbook into a
shared task. The commit message stated it was "carried over verbatim rather than
retyped, to rule out transcription error." It had in fact passed through a Python
string literal, which silently turned the repo's established `\\.` into `\.`.

**Why it happened.** The copy *was* programmatic, so it felt verbatim. But
programmatic is not the same as byte-identical: the transport medium (a Python
string literal) applied its own escape processing. The claim described the
intent, not the result.

**Consequence.** Both forms happened to behave identically, but only by
accident — Jinja decodes `'\\.'` to `\.` deliberately, whereas `'\.'` survives
only because Python passes an unrecognised escape through unchanged, which is
deprecated. The repo briefly held two spellings of the same rule, one of them
fragile.

**The rule.** Do not describe a transformation by its intent. If the claim is
"byte-identical", diff the bytes. If it went through any interpreter — a shell,
a string literal, a template — say which, and check the output.

**Enforced by.** Convention, plus a test that pins the explicit escaping form
— `every play that resolves an OpenBao URL includes the transport guard`
in `platform/tests/test_credential_leaks.bats`.

### 1.2 Asserting a gap without reading the file

**What happened.** Reported that postiz was absent from the local Caddy route
list and that a validation step "would have failed". It was present. The
conclusion came from a grep against the wrong inventory file — a stale one left
over from an earlier month — rather than from the file actually in use.

**Why it happened.** Two files with similar names, one live and one abandoned.
The grep succeeded (returned no match), so it read as evidence rather than as a
question about which file was being searched.

**The rule.** A negative grep result is not evidence of absence until you have
confirmed you searched the right artifact. When two files could plausibly be
"the" config, establish which one the runtime loads before drawing a conclusion.

**Enforced by.** Convention.

**Occurrences: 4** — (first undated), 2026-09-05, 2026-09-25, 2026-09-25

**Repeat (2026-09-05).** Stale Postiz agent notes said the container sourced its
configuration. Without checking the actual compose command, a change added shell
quoting and a test that reproduced that assumed loader. The real loader already
used literal `export "$l"`, so the change would add quote characters to credentials
and turn empty defaults into nonempty values. Review caught it before deployment.
The quoting was removed and the stale notes corrected. The regression now executes
the actual compose loader with synthetic provider values, substituting only the
config path and final application command. All six cases failed with the incorrect
quoting and pass without it, including an unterminated final line.

**Additional enforcement.** `test_provider_config_survives_actual_loader` in
`platform/tests/test_postiz_seed_input.py`.

**Occurrence 3 — 2026-09-25.** Before handing the operator a local Semaphore launch, I
grepped `scripts/semaphore-launch.py` for `http://|https|127\.0\.0\.1|localhost|scheme|cleartext|refuse`,
got nothing, and told the operator the launcher had "no URL-scheme restriction". It refuses
anything but HTTPS; its message says "plain HTTPS origin" in capitals, and my search was
case-sensitive. The command I handed over failed on its first run.

**Occurrence 4 — 2026-09-25.** I told the operator local Semaphore "clones the main checkout
at its HEAD", and wrote it into three docs in PR #266, from `local_repo_branch` defaulting to
`HEAD`. Three lines above that variable, `bootstrap-local-dev.yml` says the record points at the
working tree and runs "in place (no clone)", so uncommitted edits run too. Codex caught it
before merge.

**Why the rule did not fire.** Neither search felt like a "gap" check. One was a quick safety
look before a command, the other a reading of a variable's name. The rule is phrased around
config gaps and wrong files, so it did not come to mind for "does this tool restrict X" or
"what does this setting do".

**Proposal (count ≥ 3, Convention alone is no longer acceptable).** A PostToolUse hook on
Grep/`grep`: when a search returns zero matches, it appends "zero matches is not absence:
check case, the path searched, and the file the runtime actually loads". That puts the rule at
the moment of the search, not in this file. A claim about what a setting does cites the lines
that implement it, not the setting's name.

### 1.3 A masked exit code reported as success

**Occurrences: 2** — (first undated), 2026-09-25

**What happened.** Ran `make local-bootstrap 2>&1 | tail -60` in the background.
The pipeline's exit status is `tail`'s, so the harness reported "exit code 0"
while `make` had exited 2. The bootstrap was reported as complete when Caddy had
failed to start.

**Why it happened.** A pipe was added for output brevity, which replaced the
exit status of the thing being measured with the exit status of the formatter.

**The rule.** Never pipe a command whose exit status matters. Redirect to a file
and read it, or capture `${PIPESTATUS[0]}`. Applies especially to background
jobs, where the exit code is the only signal that arrives unprompted.

**Enforced by.** Convention.

**Occurrence 2 — 2026-09-25.** On PR #195 I ran `bats -j 4 platform/tests/ | grep -E
'^not ok' | head`, saw no output, and reported "BATS: all 674 tests ran with no failures"
— the 674 was `bats -c` counting the files, not a run. This machine has no GNU `parallel`,
so `bats -j` executes **zero** tests (`Executed 0 instead of expected 5 tests`, bats-core
1.13.0) and prints no `not ok`; the filter's silence was read as a pass. The pre-push hook,
which runs `bats platform/tests/` serially, then refused the push on a real failure
(`test_local_netbox.bats:76` pinned a literal the change had rewritten). Why the rule did
not fire: it names exit codes, and this pipe also hid the one line that said nothing ran —
a filter for failures cannot tell "none failed" from "none ran". Corollary: a test claim
needs the run's own count of executed tests (`N passed`, the final `ok N`), never the
absence of a failure line; and run BATS the way the hook does, without `-j`.


### 1.4 Guessed a resource id rather than reading the one just returned

**What happened.** Immediately after creating a runner group, its access list was checked
at `.../runner-groups/5/repositories`. The group's real id was 6. The check silently
verified a different group, and the shape of the output gave no hint — the id had been
inferred from the count of pre-existing groups rather than read from the create response.

**The rule.** An identifier returned by the operation you just performed is the only
identifier to use. Never derive one by counting, incrementing, or inferring from
adjacent state — especially in a verification step, where a wrong id turns "verified"
into "verified something else".

**Enforced by.** Convention. The playbook itself does this correctly, threading the
created id through rather than recomputing it; the slip was in an ad-hoc check beside it,
which is an argument for verifying through the automation rather than around it.


### 1.5 A security control asserted from documentation, disproved by the first measurement

**What happened.** The self-hosted runner design claimed that setting the runner's
container-hook variable makes every job's steps execute inside a container "whether or
not the workflow asked", and argued at length that this beat a workflow-level
`container:` because the latter fails open. A smoke job declaring no container reported
`ISOLATION=host`: it ran directly against the host filesystem as the runner account. The
hook mechanism manages containers for a job that *declares* one; it does not invent one.

**Root cause.** The mechanism's existence was verified — the environment variable is
real, the hooks project is real, the version was checked — and its *behaviour* was then
inferred from what would be useful. Verifying that a feature exists is not verifying what
it does.

**The rule.** A control is not a control until it has been observed failing to permit the
thing it forbids. Test it from the attacker's position — a job that asks for nothing, a
request with the field absent — not the happy path. Where a design argues that one
mechanism is safer *because* it cannot be opted out of, that property is the one to
measure first.

**Enforced by.** Test — a smoke workflow that declares no container and reports whether
it is containerised, so the claim cannot be re-made without the measurement
contradicting it. The corrected spec states what is actually enforced: workspace
destruction between jobs, no host administration from a job, and network-level egress
denial, all three verified on the live host.

---

### 1.6 Concluded a host had no address, from one vantage, on a network with an address conflict

**What happened.** Investigating why a service was unreachable, I swept the
internal `/24` from my workstation, matched hardware addresses, and found the
target's nowhere among the eighteen that answered. I reported that the host "has no IP
address at all" and built a causal chain on it: that it boots, finds its address
already taken, and declines to configure one.

Minutes later the same address, probed from a different host, resolved to that
MAC and its neighbour entry went STALE → DELAY → **REACHABLE**. The host was up
and answering the whole time. My sweep had simply lost the ARP race and never
seen its reply.

**Root cause.** On a network where two machines claim one address, an ARP sweep
does not measure who holds it — it measures whose reply arrived first *at the
sweeping host*, and that result then persists in that host's cache. Absence of a
MAC from one table is evidence about the table, not about the network. I treated
a single vantage as the network's state, which is the very error the incident
under investigation was an instance of.

**The rule.** A negative claim about reachability needs at least two vantages
before it is stated, and on a suspected address conflict the vantages are the
finding rather than a detail. Prefer evidence that names the machine — an SSH
host-key fingerprint identifies which box answered; an ARP entry identifies only
which reply won. Probing the same address from two hosts and getting two
different host keys is a proof; one silent sweep is not.

**Enforced by.** Convention. The mechanical form would be a playbook that probes
a declared address from two or more hosts and fails when the identities differ —
worth building, since this is the second time one address answering as two
machines has cost an investigation.

### 1.7 Recorded a memory as retained on the store's own "completed", with an empty result list

**What happened.** Closing the change's task 6.3 ("retain one outcome memory"),
I called the memory store's synchronous retain twice. The first returned
`{"status":"completed","memory_ids":[]}` and I treated that as done; the second
never returned and was killed at a 300-second idle timeout. I then wrote a task
closure into `tasks.md` stating that two outcome memories had been retained, and
said the same in the summary to the operator.

Neither was retrievable. Two recall queries in domain language returned only the
memory from the previous day. The bank's `fact_count` had not moved either —
though `last_document_at` had, which is exactly the split that makes this
readable: a document was created, and no fact was extracted from it.

**Root cause.** `completed` described the call, not the outcome. The result
carried its own evidence — `memory_ids: []`, an empty list where an id belongs —
and I read the status field and stopped. The store's own documented failure mode
in `CLAUDE.md` is about the ASYNC retain returning an acceptance receipt, so I
had treated the synchronous variant as self-verifying; it is not. This is the
same shape as §1.3, where a pipe's exit code stood in for the command's, but the
rule there is about pipes and does not reach an API that reports success with an
empty payload.

**The rule.** A write is not done because the writer said `completed`. Check the
result's payload — an empty id list is a failure however the status reads — and
for anything whose whole purpose is later retrieval, read it back in the same
session and check its identity AND content. Confirm every returned `memory_id`
is present in the retrieved results with the intended content. If retrieval does
not expose ids, include a unique marker in the content before the retain and
require that marker and the intended content in the same recalled item. A
same-topic memory from an earlier write is not proof; domain terms help find
candidates, but only the identity-bound check can verify this write.

**Enforced by.** Convention. A future wrapper should reject empty `memory_ids`
and stop for reconciliation, with zero automatic retries: an empty result or a
timeout does not prove that no document was created. Any future automatic retry
must first have a verified provider idempotency guarantee using the same stable
key for the same logical write, or an equivalent duplicate-prevention mechanism,
and a finite attempt limit. Without that protection, do not retry the write.

**Follow-up, same session.** Chasing the stall produced a second lesson about
claiming causes. I checked the local model endpoint, found the model named in
`CLAUDE.md` was not among those served and that requests for it were being
answered by a different one, and framed that substitution as the root cause.
The operator corrected it immediately: the substitute is the deliberate choice,
and `CLAUDE.md` is simply stale. The measurement was real; the causal story
built on it was not, and a config file I had not verified as current was doing
the load-bearing work. Then I did it again, smaller: I published a
table of size thresholds — ~100 characters works, ~250 and up hang — and the
very next attempt hung at ~103 characters, shorter than the one that had
worked. Two data points had been enough for me to state a rule; the third
falsified it. What survives as measurement: of five attempts, one succeeded
and four hung past the 300-second idle timeout, with no size ordering between
them; recall answered instantly throughout; and a much longer memory had
stored fine the day before. That is a symptom with an unknown cause, and it is
recorded as exactly that. The wider rule this earns: when a first explanation
is falsified, the next confident-sounding pattern from the same thin evidence
deserves more suspicion, not less — reaching for a second story is the same
move as the first.

### 1.8 Documented an inventory encoding as "verified" from a sample that lacked the value class that breaks it

**What happened.** The local bootstrap emits the Caddy route table into
Semaphore's static INI inventory with `| to_json`, above a comment stating that
"ansible's ini inventory parses a JSON list value into a real list (verified
against ansible-inventory)". Every route at the time held only strings and
integers. The first route to carry a boolean (`inference_api: true`) rendered as
JSON `true`; the local Semaphore deploy of Caddy then failed with
`'str object' has no attribute 'host'` — the whole `caddy_routes` value had
arrived as one string, and the template was iterating its characters.

**Root cause.** The INI plugin does not parse JSON. It hands each value to
Python's `ast.literal_eval` and keeps the raw string when that raises. JSON is
accepted only for as long as it is also valid Python — `true`, `false` and
`null` are not — so the "verification" had confirmed a coincidence over the
sample at hand, not the mechanism. Reproduced on ansible-core 2.16.18 in the
Semaphore image and on the host: `[{"flag": true}]` → `str`,
`[{"flag": True}]` → `list`. Same shape as PR 136's parser lesson: a claim
checked against a convenient sample instead of the authority (here, the
plugin's own parsing rule) is wrong precisely where the sample is silent.

**The rule.** When a comment asserts *how* another system parses a value, the
assertion must name the mechanism (the function, the documented rule), and the
check must cover every value class the type admits — a boolean, a null, a
nested list — not only the classes present today. "Verified" over one sample
earns the words "works for the current values", nothing stronger.

**Enforced by.** Test — `platform/tests/test_local_dev_inventory.bats`
("bootstrap: the INI route table is emitted as a Python literal, not JSON")
pins the `| string` emission and refuses `| to_json` on that line. Mutation-
proven by the failure itself: the `to_json` form is what broke.


### 1.9 "A feature branch is invisible to the controller" held only in the web UI

**What happened.** `platform/semaphore/README.md` (section "What Semaphore can see",
recorded 2026-09-14) stated that the controller can run only `main` and `dev` and that a
feature branch is invisible to it. On 2026-09-22, while checking whether the `(Dev)` twins
could be replaced by choosing the branch at launch, the Semaphore source at the commit the
local controller runs (`v2.18.12^0-8a4dcf0`) showed `services/tasks/LocalJob.go:817-819`
replacing the repository's branch with the task's `git_branch` unconditionally. The
template flag `allow_override_branch_in_task` is read only by
`web/src/components/TaskForm.vue:123`; the API validates the branch name's syntax and
nothing else (`db/git_branch.go`).

**Root cause.** The claim was derived from what the UI offers and from the two repository
records, not from the API or the runner. A UI affordance was read as a server-side control.

**The rule.** A statement that a system cannot do something must name the server-side code
or live refusal that prevents it. What a UI does not offer is not a control.

**Enforced by.** Convention. For agents, OPA's branch rule in change
`service-deployment-workflow` (task 4.5) becomes the control; for human API tokens nothing
server-side limits the branch.

**Note — 2026-09-22, later the same day.** The finding is version-scoped. Semaphore
v2.19.11, to which production was pinned the same day, applies a task's branch only when the
template allows it (`services/tasks/local_executor.go:938`), so the server does enforce the
flag there. The rule stands unchanged: the original claim still named no server-side
control, and on v2.18.12 there was none.
### 1.11 A gate justified by a status-code claim nobody checked

**What happened.** On 2026-09-23 I added a read-only access check to `seed-openbao-key.yml`
for isolated seed environments (PR #205). It accepted HTTP 200 or 404 from a GET on the
target path, and its comment said: "a 404 is only returned to a token the policy allows to
read it; a denied token gets 403." I wrote that from recall. The review cited the Vault API
documentation: a 404 means the path is missing *or* the token cannot see it. For a brand-new
secret, which is exactly when the check runs, a denied AppRole would have passed and reported
"access verified". The Postiz seed's check, from PR #170, had the same hole.

**Root cause.** The pass condition rested on API behaviour asserted from memory, and the
tests used a fake server that returned what the claim predicted, so nothing could contradict it.

**The rule.** A gate's pass condition cites where the behaviour it relies on is established:
a docs URL, a read of the server code, or a run against the real service. For "may this token
do X", ask the server for the token's capabilities (`sys/capabilities-self`), never infer it
from another endpoint's status code.

**Enforced by.** Test. `tasks/assert-bao-seed-access.yml` requires `read` plus `create`,
`update` or `patch` from `sys/capabilities-self`. `platform/tests/test_postiz_access_only.py`
runs both seed playbooks against a synthetic OpenBao where every GET answers 404 and asserts
that deny, read-only and write-only tokens are refused; forcing the assertion to pass turns
six cases red. The citation rule itself is `Convention`.

### 1.12 A hang reported from a timer that was not measuring the task

**Occurrences: 2** — 2026-09-24, 2026-09-25

**What happened.** Semaphore task 1215 (Deploy agentgateway (Dev)) was running while a
background check-in fired. I told the deploy session the task had been running "30+
minutes" against a normal ~3 and was hung. The 30 minutes was the check-in's own deferral
interval, not the task's age. Checked a minute later against two real readings: the task
entered `deploy.sh` at 03:27:47Z and the clock read 03:29:33Z, about two minutes, normal. I
sent a correction before anyone stopped the task.

**Root cause.** An elapsed-time claim was taken from the nearest number in view instead of
being computed. A harness timer measures when I was scheduled to look, not how long a
remote job has run.

**The rule.** A duration is two timestamps from the thing being measured: the task's own
start (the Semaphore API's `start` or the first output line) and `date -u` now. No
timestamp pair, no duration. A "hang" claim that could lead someone to stop a live deploy
gets that check before it is sent.

**Enforced by.** Convention.

**Occurrence 2 — 2026-09-25.** Told the user a `git push` "started about 30 minutes ago"
and had not finished. The process table (`ps -o etime`) showed 2 min 34 s: the push had
begun only when an earlier batch of tool calls finished, and I dated it from when I issued
the command. Caught by checking the process before acting on it; the correction went out in
the next message. Why the rule did not fire: it was worded around remote tasks and a
harness timer; this was a local process dated from my own action log. The rule already
covers it (two timestamps from the thing measured); it was not recalled.

### 1.13 A single-environment API projection was mistaken for a second binding

**Occurrences: 1** — 2026-09-25

**What happened.** The reviewed dev publisher stopped before creating the local
OpenBao seed template. Semaphore returned both `environment_id: 1` and
`environment_ids: [1]` for every template. The isolated-environment guard rejected
the mere presence of `environment_ids`, although it named the same sole binding.

**Root cause.** The guard assumed that a list field meant multiple environments
without comparing its contents to the scalar field returned by this API version.

**The rule.** Before changing isolated template bindings, require an integer
environment ID. If the API also supplies a list, it must be exactly the
one-element list containing that scalar ID. Refuse missing or divergent metadata.

**Enforced by.** `platform/semaphore/tasks/isolated-environments.yml` and the
focused single-binding/multiple-binding regression in
`platform/semaphore/tests/test_scoped_publication.py`.

**Review follow-up.** A failed Ansible loop item can print the whole template
record, including free-form arguments. The ownership guard loops over numeric
indexes, and the regression asserts a sentinel argument is absent from output.

### 1.10 Reported that a review had started, from a keyword match on a comment I never read

**What happened.** On 2026-09-23 at 15:13Z I posted review requests to CodeRabbit on four
PRs and sorted its replies with a `jq` keyword test. My patterns for a started review
included `will review` and `Reviewing`; CodeRabbit's refusal reads "Action not completed —
Review rate limited". The test labelled two refusals (#205, site-config#16) "started", and
I told Joe that one of the two deploy prerequisites was now under review. It was not.
Twenty minutes later, a read of the full comments showed every one of the four had been
refused.

**Root cause.** A loose classifier stood between me and the evidence, and its label was
reported as the fact. The refusal phrasing had never been checked against the patterns, and
the "other" output that would have exposed the mismatch was not what got read.

**The rule.** When a reply decides what to tell the user, read the reply, or match it
against a pattern proven on that exact phrasing. Report a keyword classifier's label only
as a guess, and treat an unmatched or surprising label as a reason to read the text.

**Enforced by.** Convention.

### 1.14 A fact about the private inventory read from the public template

**Occurrences: 2** — 2026-09-25, 2026-09-25

**What happened.** Writing the run-time endpoint gap for #251, I stated that the approved
OpenBao address is "the inventory's `all.vars.openbao_addr`" and that the environment's extra
var overrides it. I had read that from this repo's `platform/inventory/production.yml:122`,
which is a template with placeholders. It went into a merged plan note and into the options I
gave the user, who chose "use the inventory value" on that basis. Checked before implementing:
the site-config inventory Semaphore runs declares `openbao_addr` under the `agent_cloud` group's
vars (line 889), not `all.vars`, and a play on implicit `localhost` does not receive another
group's vars (verified on 2.16.18, 2.20.8 and 2.21.0). So a seed run gets no inventory value at
all, and the chosen option needs an inventory change first.

**Root cause.** The public repo's inventory is a template of the private one, not a copy, and I
treated its structure as the private file's. It even reads right: same variable, same value
shape, a plausible `all.vars`.

**The rule.** A claim about what production is configured with is checked in site-config (the
file Semaphore syncs), never inferred from the public template inventory. When only the
template was read, say so in the claim.

**Enforced by.** Convention.

**Occurrence 2 — 2026-09-25.** After #256 merged, I told the user the existing isolated seed
environments "still carry the old pin" and asked to run the provisioner to remove it; the user
approved on that basis. Read from Semaphore before launching: no isolated seed environment
existed in production at all, Seed OpenBao Key (Dev) and Provision Seed Environment (Dev) were
not published, and Seed Postiz Secrets (Dev) was still bound to the shared environment. I had
inferred live orchestrator state from the code that creates it. Nothing ran on the false
premise; the user re-decided with the facts and the rollout ran as a first installation. Why
the rule did not fire: it names site-config, and this was Semaphore. Its intent covers both:
a claim about what production is configured with, or what exists there, is read from the
system of record (site-config for inventory, the Semaphore API for templates and
environments) before it is stated.

### 1.15 A security check's guarantee stated past what its tests exercised

**Occurrences: 2** — 2026-09-25, 2026-09-26

**What happened.** #256 added a run-time check that the seed run's OpenBao address is the one
the inventory declares. After Codex showed forged helper variables bypassed the first version,
I rewrote it, tested four injected plain values, and wrote in the task header, the plan and the
PR that it "closes address drift and the obvious overrides". The next grounding review sent an
extra var that is a Jinja TEMPLATE: re-rendered per task, it resolved to the declared address
inside the check and to another address in the login task. Reproduced on 2.16.18 and 2.20.8:
the plain override was refused, the templated one logged in elsewhere. Reading Semaphore
v2.17.31 then showed any launcher may supply any extra-var key; there is no survey filter.

**Root cause.** The claim covered a class ("overrides") while the tests covered instances
(plain strings). Extra vars in Ansible are not values but templates, so "the value the check
saw" and "the value the login used" are two renderings, not one.

**The rule.** A security guarantee is stated only for the inputs its tests exercise, and the
tests include the input class's strongest member (for extra vars: a template keyed on task
context). Anything wider is written as a limit, with the boundary that actually holds it.

**Enforced by.** Convention.

**Occurrence 2 — 2026-09-26.** Correcting occurrence 1 in #264, I wrote the launch-permission gap
as "redirect that template's OpenBao AppRole login" and listed building request URLs inline as a
candidate mitigation; #270 then named a shared login task as its prerequisite, and the user chose
that plan. Tested before building it: a templated extra var can call `lookup("pipe", ...)`, so a
launch runs arbitrary commands on the Semaphore runner (ansible-core 2.16.18 through 2.21.0).
The real gap is code execution, and no URL change touches it. Why the rule did not fire: I
applied it to the check I was correcting, not to the mitigation I proposed in the same note. A
proposed fix is a security claim too, and the strongest input it must withstand is the same one.

### 1.16 A count written into a committed plan without running the count

**Occurrences: 1** — 2026-09-25

**What happened.** Recording the launch-permission gap for #264, I wrote into
`plan/development/01-secrets-credentials.md` that "44 playbook and task files log in to
OpenBao", and repeated the number to the user. No command in the session produced 44. A
grounding review's agent counted 41; checking both, `git grep -l 'auth/approle/login' --
'*.yml'` returns 43 (35 playbooks, 6 shared tasks, 2 Semaphore files). The wrong figure sat in
a merged plan, sizing the change every candidate mitigation would need.

**Root cause.** The number came from a scan earlier in the session whose exact pattern and
scope were not kept, so the figure outlived the command that could reproduce it. A count reads
as measured even when it is remembered.

**The rule.** A count in a committed document carries the command that produced it and its
date, run at the time of writing; a count that cannot be re-run is not written. (The corrected
plan line now cites its command.)

**Enforced by.** Convention.

## 2. Tests that would have passed for the wrong reason

### 2.1 Compiling a pattern as file text rather than as the runtime sees it

**What happened.** A test extracted a regex from a YAML playbook and compiled it
with `re.match`. In the YAML the dots are written `\\.`; Jinja decodes that to
`\.` before the regex engine sees it. Compiling the raw text instead means the
pattern says "a literal backslash followed by any character", which matches none
of the URLs in the table.

**Consequence.** The test would have reported pass or fail for entirely the
wrong reason, and would have concealed the exact defect it was written to catch.

**The rule.** Test the artifact as the runtime receives it, not as the file
stores it. Where a value crosses a decoding boundary — YAML → Jinja → regex —
reproduce that decoding in the test, and say in a comment why.

**Enforced by.** `the transport pattern accepts internal endpoints and refuses
public ones` (`platform/tests/test_credential_leaks.bats`): it decodes with
`codecs.decode(..., 'unicode_escape')` before compiling, and a companion
structural assertion pins the escaping form so the two cannot drift.

### 2.2 A test that pinned the vulnerable form in place

**What happened.** A pre-existing test asserted that a playbook's OpenBao guard
contained the literal substring `([:/]|$)`. That tail *was* the vulnerability: it
accepted `http://127.0.0.1:80@<public-host>/`, where everything before the `@` is
URL userinfo and the request actually reaches the public host. The test therefore
actively defended the bug, and failed when the bug was fixed.

**Why it happened.** The test asserted an implementation string rather than a
behaviour. Any change to the implementation — including a correction — breaks it,
which trains the reader to "fix the test" rather than ask why it fired.

**The rule.** Assert behaviour, not implementation strings. For a security check,
that means a table of inputs and expected verdicts, including the attack the
check exists to refuse. Keep exactly one such table, and have other tests
delegate to it rather than restate fragments of the pattern.

**Enforced by.** `postiz: the seed playbook uses the shared cleartext OpenBao
guard` (`platform/tests/test_service_postiz.bats`) now asserts only that the
playbook delegates to the shared guard; the behavioural table lives in one place.

### 2.3 A no-match grep aborting its own passing case

**What happened.** A BATS assertion of the form
`[ "$(grep -l ... | wc -l)" -eq 0 ]` failed even though the count was 0. Under
BATS's error handling, the `grep` exiting 1 — which is the *passing* condition —
aborted the test before the comparison was evaluated.

**The rule.** In BATS, wrap any negative assertion whose command legitimately
exits non-zero in `run`, or append `|| true` inside the substitution. A test that
fails when the thing it checks for is absent is inverted.

**Enforced by.** Convention. (Worth noting: this one is self-revealing — it fails
loudly rather than silently, which makes it the least dangerous class here.)

### 2.4 A test asserting state from a different branch

**What happened.** Added an assertion that a template file contained
`SSH_PASSWORD`. That change lived on a different branch; on the branch where the
test was committed it could not pass.

**Why this is listed as a mistake and not a nuisance.** The failure was correct
and useful — it told me a test's scope had crossed a branch boundary. The mistake
was writing a test whose subject spanned two units of work.

**The rule.** A test must be satisfiable by the branch that introduces it. If an
assertion needs changes from elsewhere, either land them first or scope the test
to what this branch owns and note where the rest is covered.

**Enforced by.** CI (a cross-branch assertion fails the Unit Tests job).


### 2.5 A guard specified but never evaluated against what it would judge

**What happened.** A new egress-denial option for the shared firewall playbook was
specified with a safety guard: reject any denial that "covers a `firewall_ssh_cidrs`
member", so nobody could cut a host off from management. Read against the real
inventory, that guard rejects *every* legitimate declaration — the destinations worth
denying (secret store, hypervisor, orchestrator) all sit inside the very prefix the
SSH allow-list declares. The intent was right and the predicate was inverted in scope:
the hazard is a denial *broader* than the management prefix, not one contained within
it.

**Why it matters more than an ordinary bug.** It would have shipped as a working
feature with a guard that blocked its only use. The first person to declare a real
denial would have hit an abort telling them their correct input was dangerous — and the
natural response to a guard that rejects valid work is to delete the guard, which is
entry 2.2 arriving by a different road.

**The rule.** A safety assertion is not designed until it has been evaluated against
the real declarations it will judge. Write it, then run the actual values through it and
confirm both directions: the intended cases pass, the dangerous case aborts. A guard
that rejects every legitimate input is worse than no guard.

**Enforced by.** Test — `platform/tests/test_apply_firewall.bats` executes the predicate
against accepted and rejected declaration shapes, rather than only grepping for its
presence, and pins the predicate text so the two cannot drift apart. (That case arrives
with the self-hosted-runner change on `feat/github-actions-runners`; until it merges the
reference is forward-looking, which per 8.2 is worth saying rather than implying.)


### 2.6 Test windows sized by line count instead of by construct

**What happened.** Three separate times, a test asserted a property inside a task or
function using `grep -A<N> '<anchor>'`. Each broke as soon as a line was added inside
that construct — once from a comment explaining the very property being tested. In every
case the subject was correct and the test was wrong.

**Why it matters.** A test that fails for its own reasons is worse than no test: the
fastest way to make it green is to edit the test, which is entry 2.2 arriving by a
different road. It also cost three debugging detours on code that was already right.

**The rule.** Scope an assertion's window to the construct, not to a guessed line count —
`sed -n '/<start>/,/<next boundary>/p'` for a task, or extract the block, then assert
within it. If a window must be fixed-width, the assertion is probably in the wrong place.

**Enforced by.** Convention. Every window added in this change is construct-scoped, so
the pattern is present in the repository as the worked example.

### 2.7 A test whose own quoting was the failure

**What happened.** A test grepped for a playbook fragment containing `default('')`, with
the whole pattern wrapped in single quotes. The embedded `''` terminated the bash string,
so the pattern reaching `grep` was not the pattern written. The test failed against a
correct playbook, and the first reading was that the playbook was wrong.

**The rule.** When an assertion fails against code you have just read and believe to be
correct, check the assertion's own syntax before the subject — the same discipline as
8.3, applied to a test rather than a tool. For a fragment containing quotes, match a
quote-free substring instead of escaping your way through it.

**Enforced by.** Convention.


### 2.8 §2.6 repeated — a test assertion forbidding the comment that documents the hazard

**What happened.** Three times in one change, an assertion written as "this file must not
mention X" failed against correct code, because the file's *rationale* named X to explain
why it was avoided. Once for `openbao` in a script whose header explains the credential
division; once for `run_once` in a playbook whose header explains why it is absent; once
for `fuser`/`lsof` in a task whose comment explains why they are not used.

**Root cause.** A prohibition expressed as a whole-file text search cannot distinguish
code from commentary about it — and commentary naming a hazard is exactly what is worth
keeping.

**The rule.** Scope a prohibition to executable lines. A check that punishes documenting
the hazard trains people to delete the documentation.

**Enforced by.** Convention, and this is the third instance — the honest status is that
nothing mechanical catches it.


### 2.9 Fifteen negative assertions that could never fail, reported as verified

**What happened.** A branch added roughly fifteen assertions of the form
`! grep -q <forbidden> "$file"` in the MIDDLE of test bodies — including
"the key is never accepted through argv", "no real addresses", "no client secret
appears", "no speculative API socket is configured". Every one was reported as passing,
and the suite was cited as evidence in a pull request.

Merging `dev` brought in `assert_helpers.bash` and its measurement: on Bats 1.13.0 a
`!`-inverted command anywhere but the final statement of a body leaves the test
**passing**, because `set -e` is documented to ignore it. Converting those assertions to
`refute_grep` — a function call, whose status does fail the test — turned **two of them
red immediately**. They had never run.

**Root cause.** Two compounding errors. The assertions were written in the natural-looking
form without checking whether Bats would honour it, and "the suite is green" was then
treated as evidence that each assertion had been evaluated. A green suite only proves no
assertion *failed*; it does not prove any assertion *ran*.

**What the two red ones actually caught.** Both were §2.8 again — the assertion forbade
the comment that documents the hazard, not the hazard. Once scoped to executable lines,
both were mutation-tested in each direction: introducing the forbidden construct into
code fails the test, removing it passes.

**The rule.** A negative assertion must be a simple command — `refute_grep`, or `run` plus
`[ ]`. Never `! cmd`, and never `[[ ]]`, anywhere but the final line. And a new assertion
is not verified because the suite is green: mutate the thing it guards and watch it fail
once. An assertion never observed failing is indistinguishable from a comment.

**Enforced by.** Test — `platform/tests/test_assertions_are_real.bats` ratchets the count
of assertions that cannot fail; it may go down and may not go up. That ratchet is what
surfaced this, on a branch whose author believed the suite had verified these very
properties.


### 2.10 §2.9 repeated — a no-op negative assertion, written while fixing that class

**What happened.** Fixing a report that read pre-write state, I added a test asserting the
report no longer references the stale variable:

```shell
printf '%s' "$report" | grep -vqF '_existing.results' || true
```

It cannot fail, twice over. `grep -v -q` exits 0 when **any** line lacks the string, so a
report still containing `_existing.results` passes it. And `|| true` discards even that
result. A reviewer caught it; the suite could not have.

**Root cause.** `grep -v` reads as "assert absent" and means "print non-matching lines".
Under `-q` it answers a question nobody asked. The `|| true` was reflex, added so a
non-matching grep would not trip `set -e` — the very reflex that makes an assertion
inert.

**Why it is its own entry.** §2.9 recorded fifteen assertions that could never fail, and
`assert_helpers.bash` exists to prevent exactly this. I wrote a new one anyway, in the
commit that fixed the old ones, in a file that already loads the helpers. Knowing the rule
and applying it are different acts.

**The rule.** Absence is asserted with `refute_grep`, never with `grep -v`, and never with
`|| true` anywhere near it. Extract the region to a file first so the assertion has an
unambiguous subject. Then mutate the thing it guards and watch it fail once — a negative
assertion that has never been observed failing is indistinguishable from a comment.

**Enforced by.** Test — the corrected assertion is mutation-verified in both directions:
restoring the stale variable in the report fails the test, removing it passes. The
`platform/tests/test_assertions_are_real.bats` ratchet does not catch this shape, since
`grep -v` is not a bang-inverted command; that gap is worth closing.

### 2.11 A test that asserted a property of one random draw

**What happened.** `test_netbox_common.bats` asserted that a generated Django key contains
a character from `[!@#$%^]`. The generator draws 64 characters from an alphabet of 76, of
which 6 are in that class, so the assertion fails whenever the draw misses them:
`(70/76)^64` — **0.518% of runs, about one in 193**. It failed CI on an unrelated pull
request, on a file that request had not touched.

**Root cause.** The intent was "the key contains special characters", and that was
translated into a property of *one sample* rather than a property of the *generator*. A
random sample cannot establish an invariant; it can only fail to contradict it.

**Why it costs more than its failure rate.** A test that fails ~0.5% of the time on
unrelated changes trains people to re-run CI rather than read it, which is the habit that
lets a real failure through. It also cost a full diagnostic detour on a PR whose diff did
not include the file.

**The rule.** Never assert that a random value has a particular property. Assert the
deterministic ones — the length, and that nothing appears *outside* the intended alphabet
— and test the intent at its source: that the generator's alphabet contains the class, by
reading the generator. If a probabilistic assertion is genuinely unavoidable, drive the
failure probability to negligible with an explicit sample count and say so in the test.

**Enforced by.** Test — the assertion is now deterministic and was run 40 consecutive
times without failure, where the previous form had a measurable per-run failure rate.

---

### 2.12 A refute that enumerated forbidden verbs instead of asserting the invariant

**Occurrences: 2** — 2026-08-24, 2026-08-25

**What happened.** A test asserted that the network playbook's validation step
never writes to the live `/etc/netplan`. It did so by refuting two specific
forms — `mv /etc/netplan/` and `dest: /etc/netplan`. Mutation testing inserted a
third, `cp "$root/..." /etc/netplan/`, and the test passed. The check enumerated
the ways I happened to imagine the mistake being made, so it caught exactly those
and nothing else. `install`, a shell redirect, `tee` and `rm` would all have
walked past it too — verified afterwards, once the check was rewritten.

**Consequence.** A safety assertion guarding the destructive step of a playbook
that reconfigures a host's network. It read as coverage while leaving the most
likely regression — someone reaching for a different copy verb — undetected.

**The rule.** Assert the invariant, not a list of its violations. The invariant
here is "the live directory may be read, never written", which is a property of
every line, so the check strips the sandbox path and requires each remaining bare
mention to be the one permitted read. That formulation kills five write-verbs I
never enumerated.

**The exception, and why it is not the same thing.** The rewritten check
structurally cannot see a command that acts on the live system without naming a
path — `netplan apply` survived it, while being the worst thing that could appear
in a validation step: it applies config *before* the revert timer is armed, so a
bad address strands the host with nothing scheduled to undo it. That needs a
second, named assertion. Naming it is legitimate because the commands that apply
config are a **closed, enumerable set** — `netplan apply` and `netplan try`, both
of which the assertion names. Enumeration over a closed set is a specification;
enumeration over an open set (all the ways to copy a file) is a guess. An earlier
wording here said "exactly one applying subcommand", which contradicted the very
assertion it was describing: the test guards both, and `try` applies config too.

**Enforced by.** `network config: validation never touches the live /etc/netplan`
in `platform/tests/test_configure_host_network.bats`, proven against seven
mutations: five unanticipated write-verbs, plus `netplan apply` and `netplan try`.

**How it was found.** Mutation testing, not review. The assertion was written,
passed, and looked correct; only inserting the defect it claimed to prevent
revealed that it did not.

**Occurrence 2 — 2026-08-25.** The same shape, in a different guard. A test
asserting that the shared transport guard needs no privilege did so by refuting a
list of target-touching modules — `command`, `shell`, `copy`, `uri`, `slurp` and
nine others. `ansible.builtin.ping` is not on that list and walked straight past
it; measured, not supposed. Replaced with a closed allow-list: the guard is a
precondition check, so exactly one module belongs in it, and the test now requires
the set of modules present to equal `{ansible.builtin.assert}`.

Why the existing rule did not fire: 2.12's rule is written about *verbs* — "assert
the invariant, not a list of its violations" — and I read the module list as a
different kind of thing. It is not. Any enumeration over an open set is the same
mistake, whether the members are shell verbs or module names. The distinguishing
question is not what the list contains but whether the set is closed: here it is,
because the guard is allowed exactly one module, which is why the allow-list form
is available at all. This is the fourth entry in this section found that way
(§2.9, §2.10, §2.11), and it is the only method that has ever found this class.

### 2.13 A test that asserted the fix was present, on a configuration where it could not run

**What happened.** Five playbooks escalated privilege at play level without
resolving a sudo password, so they died on any host that was not already
hardened. The fix promoted the working fetch into a shared task and included it as
each play's **first task**. Tests asserted exactly that: the include exists, it is
first, it reads the right secret path. All green.

The fix was inert. **Fact gathering runs before tasks.** A play with
`become: true` and automatic gathering left on escalates during gathering, so it
died at "Gathering Facts" exactly as before — the first task never ran. The one
playbook that already worked, `harden-ssh.yml`, works because it sets
`gather_facts: false`; I had read its inline fetch and copied that, without
noticing the play-level setting that made the fetch reachable at all.

Caught by review, not by the suite. Three playbooks were changed, tested, and
committed in that state.

**Root cause.** The assertion was about the *presence and position of the fix*
rather than about *the condition that made the failure possible*. "First task" is
only meaningful if tasks are the first thing that runs, and the measured failure —
`ok=0` at Gathering Facts — was itself the evidence that they are not. I had the
disproof in hand and tested around it.

**The rule.** When fixing an ordering bug, the test must pin the precondition that
makes the ordering reachable, not merely the order. Concretely: after writing a
test for a fix, construct the *original broken configuration* and confirm the new
test fails on it. Here that is one line — `gather_facts: true` — and it would have
failed immediately. A test that cannot distinguish the fix from the bug it
replaces is not a test of that fix.

**Enforced by.** `become: automatic fact gathering is OFF wherever the resolver is
used` in `platform/tests/test_become_password_resolution.bats`, proven against
four mutations including restoring `gather_facts: true`.

**Closed 2026-08-26 — exercised against a host.** The gap named below stood for
as long as this entry did: the corrected fix was statically verified and
mutation-proven but had never run against a host, because the orchestrator runs
playbooks from the integration and production branches and the change was on
neither. It has now run. `Install Podman` against the postiz host cleared
`Gathering Facts`, reported `sudo password resolved from the secret store`, and
installed the runtime (`ok=14 changed=1 failed=0`). The ordering fix works at
runtime, not only in the suite.

**What the same run also proved — about this entry's own limits.** The first
attempt did *not* succeed. It cleared fact gathering, which is what this entry is
about, and then died one task later on a keyword that is invalid at runtime and
invisible to every static gate the repo owns (2.17). So the branch that closed
this gap was itself unrunnable while its tests were green — the identical shape,
one layer down. The lesson is not that the rule below was wrong; it is that
"statically verified and mutation-proven" was never the same claim as "runs", and
this ledger now has two entries saying so.

**Original text, kept as written.** The corrected fix is statically verified and
mutation-proven but has **not** been exercised against a host, because the
orchestrator runs playbooks from the integration and production branches and this
change is on neither yet. That is the same gap this entry is about, so it is named
here instead of being called done.

### 2.14 One value, five resolution rules, and a verdict that lied because of it

**What happened.** A review found that the access gate reported `NO-GO` while the
secrets it needed were available. The gate resolved the secret-store address as
`openbao_addr | default('')`; the shared resolver I had just added used
`openbao_addr | default(env OPENBAO_ADDR)`. With only the environment variable
set, the resolver found the sudo password and the gate's own address stayed empty
— which skipped both of the gate's lookups, so it concluded the credentials were
missing and refused to authorise hardening.

Looking wider, that same variable is defined across the playbooks in at least
**five mutually inconsistent forms**: bare, empty-default, one env fallback, two
env fallbacks, and one that falls back to a **localhost URL** — which would
silently talk to the wrong secret store rather than fail.

**Root cause.** I added a second resolution rule for a value that already had one,
without checking what the existing one was. The failure is not that either rule is
wrong; it is that two paths depending on one value disagreed about how to compute
it, so one could succeed while the other reported the opposite.

**The rule.** Before introducing a derivation for a value that other code already
derives, grep for the existing derivations and count the variants. If there is more
than one, that is the finding — reconcile or explicitly scope around it, but do not
add a sixth. A value two paths depend on gets one rule.

**Enforced by.** `access gate: it resolves the store address the same way the
resolver does` in `platform/tests/test_verify_host_access_become.bats`, proven by
reverting the gate to the no-fallback form.

**Scoped, not fixed.** Only the two rules that disagreed *within this change* were
reconciled. Normalising the address across every playbook — including retiring the
localhost-defaulting variant, which is the dangerous one — is a separate change,
recorded here so it is not mistaken for done.
### 2.15 An anchor-less allow-list, inside the fix that replaced a verb blacklist

**Occurrences: 2** — 2026-08-25, 2026-08-25

**What happened.** 2.12 records replacing a blacklist of forbidden write verbs
with an invariant: within the validation step, every mention of the live
configuration directory must be the one permitted read. The implementation
filtered the permitted read out with an unanchored `grep -v` pattern.

Unanchored, it matches a safe prefix and ignores whatever follows.
`cp -a /etc/netplan/. "SANDBOX/" && cp x /etc/netplan/` was filtered out as
permitted while appending a live write — the exact defect 2.12 exists to prevent,
reintroduced by the shape of its own fix. Found by review, not by the seven
mutations already run against that test.

**Root cause.** An allow-list entry is a claim about a whole line; written as a
substring it is only a claim about a prefix. The mutations missed it because they
shared an assumption with the code — that a violation would appear on its own
line — and mutations drawn from the same assumption as the code cannot test that
assumption.

**The rule.** Anchor an allow-list end to end, never merely match it. And when
mutating to test a filter, include at least one mutation that EXTENDS an existing
permitted line rather than adding a new one; appending to something already
allowed is the cheapest way past a substring check.

**Enforced by.** `network config: validation never touches the live /etc/netplan`
in `platform/tests/test_configure_host_network.bats`, now anchored, proven against
both an appended write on the permitted line and a separate write line.

**Occurrence 2 — 2026-08-25.** Same lesson, different surface: matching a token
*anywhere in a file* rather than binding to the active construct. Two tests on the
access gate searched the whole file for `assert-bao-transport` and for
`OPENBAO_ADDR`. A guard that had been **commented out** still satisfied the first,
because the string also appears in the prose above the task; and two *divergent*
address-resolution chains both satisfied the second, because both happened to
mention the same environment variable. Both bypasses were reproduced before
fixing. The guard check now matches an active `include_tasks:` line, and the
address check compares the whole normalised expression rather than a token within
it.

### 2.16 A test whose population was selected by the presence of the fix

**What happened.** A review pointed out that a test compared only task NAME lines
and then grepped the whole file, so a playbook whose first task was merely *named*
"Resolve the sudo password" would pass without including the resolver. Fixing that
was straightforward — bind each assertion to its own task block.

The fix did not work, and mutation testing showed why. The loop selected its
population with `grep -q 'resolve-become-password' "$f" || continue` — the same
string the assertion checks. Deleting the include therefore removed the file from
the population, and the test passed **vacuously**. The bypass the review described
survived the fix for the review's finding.

Selecting instead on the *condition* — every playbook that escalates at play level,
whether or not it currently resolves a password — killed it immediately.

**Root cause.** A filter keyed on the thing being asserted cannot fail: removing
the property removes the subject. The test was shaped like "for everything that has
X, assert X", which is a tautology dressed as coverage.

**The rule.** A test's population is selected by the CONDITION that makes the
requirement apply, never by the presence of the fix that satisfies it. If deleting
the implementation makes the test skip rather than fail, the selector is wrong.
Check it by deleting the implementation and confirming a FAILURE, not a pass.

**A second thing this surfaced.** Once the population was the honest one, the test
failed on `harden-ssh.yml` — which resolves the password with its own inline fetch
rather than the shared task. That is a real inconsistency, not a test defect, so
the assertion now accepts either shape and pins what actually matters: whichever
mechanism supplies the credential must be the FIRST task. Consolidating the two
onto one mechanism stays a follow-up, deliberately not done inside a change that
deploys through the irreversible step.

**Enforced by.** `become: the first task is what makes escalation possible` and
`become: gathering, where it happens at all, happens after escalation is possible`
in `platform/tests/test_become_password_resolution.bats`, proven against removing
the include, moving the gather out of position, and moving the inline fetch off
first position.

### 2.17 A keyword that is invalid at runtime and invisible to every static gate

**What happened.** The fix for 2.13 added `become: false` to the *include* of the
transport guard inside `platform/playbooks/tasks/resolve-become-password.yml` —
"insurance, if the guard ever grows a real task". The branch went green:
`ansible-playbook --syntax-check` passed, `ansible-lint` passed at the `production`
profile, 489 BATS and 79 pytest passed, five CodeRabbit reviews approved it, and CI
was green on the merge.

The first orchestrated run after merging died immediately:

```text
TASK [Resolve the sudo password (host may not be hardened yet)] ****
ERROR! 'become' is not a valid attribute for a TaskInclude
```

`become` is valid on `import_tasks`, where inheritance is resolved at parse time.
It is not valid on `include_tasks`, which builds a `TaskInclude` object that has no
such attribute. Reproduced locally on the same ansible-core 2.21.0, so this was not
runner drift.

**Root cause.** A *dynamic* include is not parsed until a play actually reaches it.
`--syntax-check` walks the playbook and stops at the include; ansible-lint checks
the task file's shape but not its validity as an included task under a play; the
BATS suite reads both files as text. **Every gate the repo owns operates on files
that were never assembled into a play.** So the construct was verified as text by
four independent mechanisms, none of which could observe the only thing that
mattered.

This is 2.13's shape a second time: the proof and the runtime were never in
contact. 2.13 was an ordering fix that tests confirmed was *present* on a config
where fact gathering ran before it. This was a keyword that tests confirmed was
*present* in a file that could not be loaded.

**The rule.** `become` is never a valid attribute on `include_tasks` — put it on
the included file's own tasks, where it is valid and where every caller of that
file inherits it, rather than on any one include. More generally: a construct
inside a dynamically-included file is not verified by any static gate in this repo.
Either exercise it in a real play, or pin it with a test that encodes the runtime
rule directly — and mutation-prove that test, because a text assertion about an
unloadable file passes just as happily as one about a working file.

**Where the fix landed.** `become: false` moved onto the assert task in
`platform/playbooks/tasks/assert-bao-transport.yml`. That is the mechanism-level
placement: all seven plays that include the guard now carry the declaration, not
just the one that surfaced the bug.

**Enforced by.** `become: no dynamic include anywhere carries a become keyword` in
`platform/tests/test_become_password_resolution.bats` — a **closed** rule (the
keyword is invalid on every `include_tasks`, everywhere, so enumerating the whole
set is the specification, not a blacklist). Mutation-proven: reintroducing the
keyword on the resolver's include makes it fail with the file and line; removing it
makes it pass. The companion assertion in `become: the transport guard needs no
privilege, and says so` now pins `become: false` on the guard's own task, because
its previous form required the very construct that broke the runtime.

### 2.18 "Every play that reaches OpenBao" — a hand-typed list of four

**Occurrences: 2** — 2026-08-28, 2026-09-22

**What happened.** `test_credential_leaks.bats` carried a test named *every play
that resolves an OpenBao URL includes the transport guard*. Its body looped over
four filenames written into the test. While extending that list by one for a review
finding, the population was derived from the playbooks instead — every file with a
`_bao_url:` play var — and **40 of 52** such plays had no transport guard. Among
them: every `apply-policy-*.yml`, `apply-openbao-policies.yml` (which *writes*
policy), `harden-ssh.yml`, and most `deploy-*.yml`.

The test had been green throughout, and its name had been read as a fact.

**Root cause.** The population was a list, and a list is a claim about the world
that nothing re-checks. It was correct on the day it was written — those four plays
were the ones the guard had been extracted from — and every play added afterwards
was invisible to it. A test whose population is hand-kept asserts only "these N are
fine", however its name reads.

This is the same mechanism as 2.16 (population selected by the presence of the fix)
in a different coat: there, the selector was the property under test; here, the
selector was a snapshot. Both make the test unable to fail on the case that matters,
which is the one that was not there when the test was written.

**The rule.** A test that speaks about "every X" derives X from the code. If the
honest derived result cannot be made to pass today — because fixing it is a large
change with its own risk — the test becomes a **ratchet**: a committed file names
the known exceptions, the test fails on any exception *not* in the file, **and**
fails on any file entry that is no longer an exception. The list can then only
shrink, and shrinking it is visible work rather than a comment nobody reads.

**Enforced by.** The rewritten test in `platform/tests/test_credential_leaks.bats`
with `platform/tests/known_unguarded_bao_plays.txt` as the ratchet. Guarding the
38 remaining plays is tracked as its own change — it touches live-service deploys
and was deliberately not folded into the change that found it.

**Occurrence 2 — 2026-09-22.** The check-mode contract (change
`service-deployment-workflow`, "every playbook honours a dry-run flag") derived its
population from the code, as the rule says, but from one directory:
`platform/playbooks/**`. Its allowlist was empty and the claim was reported as met.
`platform/semaphore/` holds four more Ansible files the claim covers:
`setup-templates.yml`, `bootstrap-semaphore-repositories.yml`, `sync-inventory.yml` and
the shared `tasks/runtime-access.yml`. None of them honoured `--check`. It surfaced
when the conformance collector's first local dry run (Semaphore task 1018) failed
inside the shared access task, because its OpenBao reads were skipped in check mode
and the rescue reported "Runtime Semaphore access is unavailable". The rule did not
prevent it because it names *where* the population comes from (the code) but not
*how far* it extends. A glob rooted in the directory where the fix was written is a
snapshot of where playbooks lived on that day. Widened rule: derive "every X" from
the code, and root the derivation at the scope the claim names, not at the directory
you happen to be working in. Enforced by `platform/tests/test_check_mode_contract.py`,
which now scans `platform/semaphore/**` too. Widening it went red on all four files
(mutation-checked: reverting the fix to `runtime-access.yml` fails the test), and all
four were then retrofitted by hand. `setup-templates.yml --check` against local
Semaphore reported changed=0, with templates and schedules byte-identical before and after.

### 2.19 The healthcheck watched the frontend while the backend was dead

**What happened.** postiz's container healthcheck probed `http://127.0.0.1:5000/`
and treated any status under 500 as healthy. Inside the container, nginx serves
`/` from the FRONTEND process and `/api/` from the backend. On 2026-08-30 the
backend died at startup (Temporal refused its search-attribute registration) while
pm2 kept the frontend answering — so the probe went green over a service that
could not authenticate a user, publish a post, or answer its API. Task 5.3 of the
postiz change had earlier recorded "all five containers reach health" on the
strength of that probe.

**Root cause.** The probe's path selected the one process that was NOT the thing
being asserted. "The container answers on its port" and "the service works" were
conflated; nginx made them differ per path.

**The rule.** A health probe must exercise the process whose failure the check
exists to catch. Where one port fronts several processes, probe the path routed to
the one that does the work — here `/api/`, where a 404 proves the backend bound
and a 502 proves it did not.

**Enforced by.** Test — `platform/tests/test_service_postiz.bats` pins the probe
to `/api/` and refutes the bare-`/` form (mutation-proven).

### 2.20 Idempotency proven on the wrong steady state, and the failure that revealed it was censored by its own `changed_when`

**What happened.** The first production run of the read/retire/rescue version
of the managed-sites playbook — landing the inference route — failed at the
retire step and rolled back. The inventory declared `devlog` in
`caddy_retire_sites`, as the adoption three weeks earlier required, and the
route report two tasks up had just shown `devlog [managed by inventory]`. The
tool refused: an address inside the managed region raised, by design, "refusing
to retire … Remove it from caddy_managed_sites instead." The inventory comment
beside the declaration promised the opposite: "Removing a name from here after
its block is gone is a no-op — the retirement is idempotent." The run's only
diagnostic was `Expecting value: line 1 column 1 (char 0)`.

**Root cause.** Two, stacked. The tool's idempotency test removed a hand block
twice and confirmed the second pass changed nothing — the steady state of a
route that was *never* adopted. The steady state that every real declaration
reaches — block inside the managed region, name still in the retire list — was
the case the tool treated as caller error, so the declaration converged exactly
once and refused forever after. Then `changed_when: (_retired.stdout |
from_json).changed` parsed an empty stdout (the refusal went to stderr) and
raised inside the conditional, and Ansible reported *that* exception instead of
the tool's message. The playbook's own header had cited the postiz precedent —
"A conditional cleanup gated on a later step's result is not a rollback" — and
this is the diagnostic form of the same error: a conditional that assumes the
success shape of the thing it inspects.

**The rule.** An idempotency test must exercise the state a *successful* first
run leaves behind, with the *same* declaration, and assert convergence — not the
state of a run that never happened. And a `changed_when`/`failed_when` that
parses a module's output must be guarded on the module's success (`rc == 0 and
…`), or the parse error replaces the real one.

**Enforced by.** Test — `test_caddyfile_sites.py` covers the adopted-into-managed
case (`already_managed` reported, text unchanged) and the mixed case;
`test_manage_caddy_sites_playbook.py` asserts the retire step's `changed_when`
begins with the rc guard (mutation-proven: dropping the guard fails it).

### 2.21 A new deploy playbook shipped without the zero-hosts pre-flight, and its first run was a green no-op

**Occurrences: 1** — 2026-09-17

**What happened.** `deploy-agentgateway.yml` was written by mirroring `deploy-tududi.yml`,
which has no pre-flight. Its first run through the local Semaphore (task 592) printed
`skipping: no hosts matched` for all three plays and was recorded as `success`; zero
containers existed afterwards. The group was absent because the local control plane's
inventory is a static INI inside `bootstrap-local-dev.yml`, separate from
`platform/inventory/local-dev.yml`, and only the latter had been edited. The exact
incident is described, with the fix, in the header of `preflight-target-group.yml`
(postiz, 2026-08-24) — and at the time of writing 24 of 26 `deploy-*.yml` playbooks still
do not import it.

**Root cause.** The guard exists but is opt-in per playbook, and the template new
playbooks are copied from does not carry it. A rule that lives in one file's header does
not reach a playbook written from a different file.

**The rule.** Every deploy playbook whose plays target an inventory group imports
`preflight-target-group.yml` as its first play, with the group passed twice
(`preflight_group` and `preflight_group_expected`), before any `hosts: <group>` play.
When adding a local-dev service, the group goes in BOTH inventories: the working
`local-dev.yml(.example)` and the static INI in `bootstrap-local-dev.yml`.

**Enforced by.** Test, for this playbook only: `test_service_agentgateway.bats` asserts the
import and both vars. Fleet-wide, still `Convention` — the mechanical guard this entry
proposes is one BATS test over every `platform/playbooks/deploy-*.yml` whose plays target
a `*_svc` group, asserting the import; it has to land with the 24 missing imports or as an
allow-list that only shrinks.

### 2.22 Fixture hid Semaphore's empty secret projection

**What happened.** Local Semaphore task 1709 ran the reviewed seed-environment
provisioner from `dev`, then refused its newly created isolated environment as
unreadable. Its GET response omitted `secrets`; the fixture always returned
`secrets: []` and passed. The running Semaphore v2.18.12
[loads secret metadata on single GET](https://github.com/semaphoreui/semaphore/blob/v2.18.12/api/projects/environment.go#L131-L176)
but [serializes an empty list with `omitempty`](https://github.com/semaphoreui/semaphore/blob/v2.18.12/db/Environment.go),
so both responses describe an empty environment.

**Root cause.** The shared clean-environment rule treated absence as unknown,
and the provisioner directly read `.secrets` even after the rule. The fixture
modeled a plausible API shape, not the controller's actual empty response.

**The rule.** Mirror the pinned provider's response shape in the fixture.
Accept the omitted field only where that version guarantees it means an empty
loaded list; continue to refuse explicit null or malformed secret metadata.

**Enforced by.** `test_the_clean_environment_rule_checks_the_endpoint` and
`test_provisioner_isolates_the_openbao_key_seed`, whose GET fixture now omits
empty secret lists.

### 2.23 Tightened the check under test, left the fixtures, and three negative cases went vacuous

**What happened.** PR #195 (commit 446bac9) made an ARP hit count as a VM's own only when one
of that VM's NIC MACs matches, read from a new Proxmox config fetch. The BATS helper
`judge()` in `platform/tests/test_address_steps.bats` gained a third argument for that config,
defaulting to `{"data":{}}`: no NICs. The existing cases that assert `own=False` for a
different vmid, a different name and a stopped VM kept calling it with two arguments, so they
got no MACs and read `own=False` whatever the vmid, name or status filters did. Removing any of
those three filters left the suite green. A `/simplify` review during a grounding checkpoint
found it; nothing had failed.

**Root cause.** The new condition is an AND with the old ones. A negative case proves a filter
only if every OTHER term is true for it; adding a term that is false by default for the old
fixtures satisfies every negative case at once.

**The rule.** When a change adds a conjunct to the check under test, make the fixtures' default
satisfy it, so each negative case can fail only on the term it is about, and mutate each term
once to watch its case go red.

**Enforced by.** Convention. This instance: the helper now defaults to a matching NIC, and
removing the vmid, name or running filter each turns the suite red (mutation-checked).

## 3. Acting on live state

### 3.1 Overwriting a real credential with a probe value

**What happened.** To confirm a newly-shared transport guard still allowed normal
operation, ran the real seeding playbook against the real local secret store with
`SEED_X_API_KEY=probe-only`. The playbook did exactly what it is built to do and
wrote the probe value over a live credential.

**Consequence.** One real credential was replaced. It was detected immediately
and restored from source, and read-back confirmed all nine values match the
source lengths exactly. The secret store's merge-patch behaviour meant the other
eight were untouched.

**Why it happened.** A *write* path was used to test a *read-side* guard. The
guard runs before the write, so the intended observation was complete long before
any damage — but the playbook was allowed to continue past it.

**The rule.** Never exercise a write-capable playbook with placeholder data
against live state. To verify that a guard *permits* an operation, either:
1. assert the guard task passed and stop the run (`--start-at-task`, a check
   mode, or a deliberately unreachable backend), or
2. observe the guard on the *refusing* path, which never reaches the write.

Verifying the refusing path is free; verifying the permitting path costs a write
and must be planned as one.

**Enforced by.** Convention today. **OPA-shaped — see §7, rule `no-probe-writes`.**

### 3.2 Mutating a shared orchestrator credential without asking

**What happened.** Attempted to overwrite a shared login credential in the
orchestrator's key store via its API, to make a stale password match the
documented source. The sandbox refused the call.

**Why the refusal was right.** The credential is shared by an inventory record
that other work uses. Changing it is not confined to the task at hand, and it is
not config-as-code — nothing in the repo would record that it happened. That is
the definition of the shared-mutable-state failure this platform's principles
forbid.

**The rule.** A credential or configuration object that is shared and not
declared in the repo may not be mutated as a side effect of a task. Either make
it config-as-code, or ask.

**Enforced by.** Sandbox classifier (fired correctly).
**OPA-shaped — see §7, rule `no-undeclared-shared-mutation`.**

---

### 3.3 Treated failed workstation login as a controller access prerequisite

**What happened.** During production sync testing, the workstation had no injected
Semaphore token. Its cached OpenBao CLI session returned 403, and an operator
login was presented as the prerequisite for publication. Production task 411
had already authenticated through Semaphore's controller AppRole and read the
runtime secrets successfully. The user corrected the executor distinction.

**Root cause.** Two different authentication contexts were conflated. The missing
piece was a published controller entry point for scoped configuration, not proof
that the controller needed replacement credentials.

**The rule.** Check the supported Semaphore entry point and its controller-side
evidence before requesting workstation authentication. A missing bootstrap entry
point, an operator UI session, controller AppRole authentication and target
credential validity are separate gates. Never export the controller AppRole or
read backups as a shortcut between them.

**Enforced by.** The controller publisher's executable fixture test succeeds
using AppRole authentication without a workstation Semaphore token and asserts
secret-free failure output. Exact selection, unchanged bindings and readback are
also tested. Identifying the actual executor and distinguishing deployed code
from live availability remain convention, documented in the canonical
[Semaphore operating guide](../platform/semaphore/README.md).

---

### 3.4 A validation step's cleanup deleted a committed provider lock file

**What happened.** To prove a new `ratelimit.tf` against the real provider schema
without a local `tofu`, the check ran `tofu init -backend=false && tofu validate`
in a container mounted on `platform/infra/cloudflare/`, then cleaned up with
`rm -rf .terraform .terraform.lock.hcl` so the init left nothing behind. The lock
file was already committed. `git status` showed it as deleted one step later; it was
restored with `git checkout --` before anything was staged. Validation itself was
worth having — it also caught nothing wrong — but it was one `git add -A` away from a
commit that silently unpinned the provider.

**Root cause.** The cleanup was written for a scratch directory and run inside the
working tree. `rm -rf` on a name that a tool *creates* does not distinguish the copy
the tool just created from the one the repository already tracked.

**The rule.** Tooling that writes into the tree runs on a copy: `cp -R` the
directory into the scratchpad first, or mount the scratchpad copy, and let the
cleanup delete the copy. Inside the working tree, never `rm` a path without first
running `git ls-files --error-unmatch <path>` (tracked → do not delete) — and read
`git status` before `git add`, every time, which is what caught this one.

**Enforced by.** Convention. A mechanical guard exists in principle — a pre-commit
check refusing a commit that deletes a `*.lock.hcl` / lockfile without a
`chore(deps)`-style intent — but one occurrence does not yet justify it.

### 3.5 A vmid allocated from an incomplete ledger, and a provisioner that adopted the collision

**Occurrences: 1** — 2026-09-18

**What happened.** The agentgateway VM was allocated vmid 216 as "highest in `vm-specs.yml` plus
one". Proxmox already ran `gh-runner-01` as 216 and `gh-runner-02` as 217, on alphacentauri,
with no ledger entry. `provision-vm.yml` listed cluster resources, found 216, printed "already
exists — skipping clone", and proceeded to "Configure VM resources and cloud-init" against
`nodes/apollo/qemu-server/216.conf` (Semaphore task 1060). It failed only because the runner is
on alphacentauri, not apollo. Had the nodes matched, the runner would have received
agentgateway's cores, memory and cloud-init network.

**Root cause.** Two assumptions. The ledger was treated as complete when it is one of three
records (ledger, inventory `vm_*`, Proxmox itself) and the authority is Proxmox. And the
playbook's "exists" branch meant "ours, resume" without checking that the existing VM IS the
declared one.

**The rule.** Allocate a vmid from the hypervisor's own listing (the range listing the playbook
already prints), never from the ledger alone, and record every VM the listing shows that the
ledger lacks. A provisioner that finds a VM at the declared vmid compares name AND node with the
declaration and refuses on mismatch.

**Enforced by.** Test — `platform/tests/test_provision_vm.bats` asserts the refusal guard exists
and precedes the skip. The allocation half is `Convention` until the IPAM/ledger lookup recorded
in `plan/architecture/02-service-onboarding.md` Known Gaps exists.

### 3.6 A static address chosen from an incomplete inventory, applied to a new VM, that a live production host already held

**Occurrences: 1** — 2026-09-18

**What happened.** With NetBox unavailable, the agentgateway VM's address was chosen as an
undeclared value in `site-config/inventory/production.yml` (operator instruction, provenance
noted). Provisioning configured VM 218 with it. Key distribution then failed with "connection
refused" from the Semaphore runner while a workstation got an SSH banner from the same address;
logging in with the platform management key showed the responder was `gh-runner-01`, up 25 days.
The runners were never declared in the inventory (nor the ledger, see 3.5), so "not declared"
meant nothing. The guest agent on 218 never came up, and no second MAC ever appeared for the
address, so the collision did not go live — but the VM carries the conflicting cloud-init config.

**Root cause.** The inventory records what was declared, not what exists; the authority for
addresses is NetBox (down) or the network itself. A refusal-vs-banner disagreement between two
vantages is the signature of an address conflict (see 1.6), and the runner's default-deny
firewall turned the conflict into a misleading "refused".

**The rule.** Before applying a static address to a new VM, prove it free from the network
itself when the IPAM is unavailable: an ARP/ping sweep from at least two vantages, and a
refusal to proceed if anything answers. When two vantages disagree about reachability, stop and
identify the responder before retrying. Every VM the hypervisor lists must be declared in the
inventory with its address before another allocation is made.

**Enforced by.** Playbook guard + test, since PR #189 (same day): `provision-vm.yml` pings the
declared address from the controller on the create path and refuses on any answer, fails closed
when the probe cannot run (explicit `allow_unverified_address` override only), and
`test_destroy_vm.bats` asserts the guard's presence and position. ICMP silence is evidence, not
proof; the authoritative allocation stays the IPAM lookup in `02-service-onboarding.md` Known Gaps.

### 3.7 A test's scratch repository was the real one, because the hook exported `GIT_DIR`

**What happened.** On 2026-09-23 I pushed a branch adding `test_graph_artifact_guard.bats`,
whose setup ran `git init -q repo`, `git config user.email t@example.invalid` and
`git config user.name t` inside `$BATS_TEST_TMPDIR`. It passed when run by hand. Under the
pre-push hook, git had exported `GIT_DIR`, so those commands addressed the pushing
repository: the shared `.git/config` gained `core.bare=true`, `user.email=t@example.invalid`
and `user.name=t`. Every agent-cloud checkout, including another session's, then failed with
"this operation must be run in a work tree", and any commit made in that window would have
been authored by the fake identity. About 150 BATS tests failed and the push was refused.
Repaired about ten minutes later (`core.bare=false`, local `user.*` removed); a
`git log --all --author=t@example.invalid` search found no commit under that identity.

**Root cause.** Git sets `GIT_DIR` and related variables in a hook's environment. The pre-push
hook passed them straight to the suites, so a test's git commands were never isolated, and
the only way a test could be safe was for its author to know that.

**The rule.** A hook that runs tests clears git's repository variables first, so the suites
run with the same clean git environment they get in CI. A test that creates a repository
also clears them itself, because it may be run by another hook.

**Enforced by.** `.githooks/pre-push` unsets `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` and
related variables after resolving the repository root.
`platform/tests/test_pre_push_git_env.bats` runs the hook the way git does, pointed at a
victim repository, with a fake `bats` that repeats the offending commands, and asserts the
victim's config is unchanged. Mutation: removing the unset turns it red.

### 3.8 A "dry run" that the orchestrator silently ran for real

**What happened.** On 2026-09-23 I launched `Deploy agentgateway (Dev)` (template 220) against
production through the Semaphore API, meaning to run it in check mode first. I sent
`"dry_run": true` at the top level of the task body, from a read of the API spec that did not
check where the field lives. Semaphore v2.17.31 carries Ansible's check and diff flags inside the
task's `params` object (`db/Task.go`, `AnsibleTaskParams`); the top-level key was ignored. Task
1177 ran for real: it placed the dev checkout and enabled linger on the VM, generated and stored
the gateway's database password, cookie seed and four client keys in OpenBao, and rendered
`.env` and `config.yaml`, then stopped at a failing guard before `deploy.sh`. No container
started; the seeded upstream key was reused, not overwritten ("7 secrets managed"). Everything
written is what the first real deploy writes, so nothing had to be undone, but it was not the
check-mode run that was intended.

**Root cause.** A safety flag was sent without confirming the server recorded it. The response
was not read back for the flag, and a real run and a check-mode run look the same until the
first write.

**The rule.** When a launch depends on a safety flag (check mode, diff, limit), read the created
task back and confirm the server recorded the flag before letting it run; stop the task if it did
not. For Semaphore v2.17: `"params": {"dry_run": true, "diff": true}`.

**Enforced by.** Test. `scripts/semaphore-launch.py` builds the body with the flags in `params`
and refuses check mode on an unverified server version, before the task exists; a read-back
after the POST stops the task if the server did not record check mode, as a tripwire only,
since Semaphore starts a task on creation (review of PR #220).
`platform/tests/test_semaphore_launch.py` fails if the flag is ever sent at the top level, and
covers the version gate, undeclared survey fields, a busy template and the tripwire.

## 4. Data handling

### 4.1 `while read` dropping an unterminated final line

**What happened.** Extracted credentials from a `.env` with
`while IFS='=' read -r k v; do ... done < file`. The file's last line has no
trailing newline, so `read` returned false on it and the loop body never ran for
it. One credential was silently missing, and the count looked plausible.

**The rule.** `while read` is not safe for whole-file iteration. Use
`while ... || [ -n "$k" ]`, or parse with a tool that has no such edge
(`python3 ... .splitlines()`, which is what the corrected version uses).

**Enforced by.** Convention. Detection heuristic: whenever a count is derived
from a loop, cross-check it against an independent count of the source.

### 4.2 Storing `.env` values with their quotes

**What happened.** Every value in the source `.env` is double-quoted. The first
extraction passed them through unchanged, so eight credentials were written to
the secret store as `"value"` rather than `value`.

**Why it was caught.** A read-back compared stored lengths against source
lengths; every value was exactly two characters too long.

**The rule.** `.env` is not a format with one spelling. Strip at most one
matching pair of surrounding quotes, and verify by length or hash after writing —
never by "the playbook reported success".

**Enforced by.** Convention, plus the habit of read-back verification.

### 4.3 A real internal address used as a test vector

**What happened.** A test table for an RFC1918-matching regex used the
platform's actual OpenBao address, with a comment identifying it as such. The
repo's private-IP pre-commit hook rejected the commit.

**Why the hook was right.** That address is site data. This repository is public;
real addresses belong in the private config repo. The vectors did not need to be
real — the regex tests ranges, not hosts.

**The rule.** Test vectors are generic by default. If a test needs to exercise a
range, use a documentation-safe example inside that range, never the live value.

**Enforced by.** Pre-commit hook `no-private-ips` (already in place and working).

**Residual gap, found later.** The pre-commit hook is not the only gate, and the other
one is permeable. The CI "RFC1918 IP address audit" filters its hits through an
exclusion list that drops any line containing `\.0/`, a backtick, `example`, `host:`,
`subnet`, `scope`, or `target`. A real *network* address — anything written `.0/24` — is
therefore excluded by construction, as is any address inside backticks. Treat the CI
audit as a backstop, never as the reason it is safe to write an address down.
Tightening that exclusion is open work.

**Related.** Four test vectors in the same table legitimately look like
credentials — the `scheme://userinfo@host` shape is the attack being refused, so
a URI-credential detector fires on the vector itself. Those
carry `trufflehog:ignore` with a stated reason. The annotation was verified to
work (1 finding → 0) before being relied upon. An ignore marker without a reason
in the same comment is not acceptable.


### 4.4 Arithmetic on a fleet API response without defaulting the fields

**What happened.** Two consecutive read-only capacity queries against the hypervisor
cluster crashed inside `jq`: first `null (null) and number (1073741824) cannot be
divided`, then `number (1569) and string ("G") cannot be added`. Offline nodes return
`null` for their memory and CPU counts, and one expression concatenated a number to a
unit suffix without coercing it.

**Why it happened.** The expressions were written against the shape of a *healthy*
member and then run across a fleet containing three offline ones. The API was behaving
correctly; the query assumed a uniform population.

**The rule.** When querying an inventory-style API that returns members in mixed
states, default every numeric field before arithmetic (`(.field // 0)`) and coerce
explicitly before concatenation. Assume at least one member of any real fleet is
offline, degraded, or partially populated — in a real fleet one always is.

**Enforced by.** Convention. A shared read-only capacity helper with the defaulting
done once, and a case covering an offline member, would move this out of prose; no such
helper exists yet.

---

### 4.5 Truncated a live file by opening it for writing in the same expression that computed its content

**What happened.** An edit to the private inventory was written as
`open(path, "w").write(transform(open(src).read()))`. Python evaluates the
`open(path, "w")` call before it evaluates the argument, so the destination is
truncated **first** and the content is computed second. The transform raised on a
mismatched anchor, and the file was left at zero bytes.

The file held another session's uncommitted work — 141 lines of host declarations
that existed nowhere else. It was recovered only because a copy had been taken
seconds earlier for an unrelated reason (committing a single hunk without
sweeping that work). Had that copy not existed, the loss would have been total
and silent: the very next check printed "YAML parses", because an empty file
parses fine as `None`.

**Root cause.** Two mistakes compounding. The destructive act and the fallible
act were placed in one expression, with the destruction ordered first by the
language's evaluation rules. And the verification that followed — "does it
parse?" — cannot distinguish a correct file from an empty one, so it reported
success on a destroyed file.

**The rule.** Compute the new content in full, assert it is plausible, and only
then open the destination — or write a temporary file and move it into place.
Never let a destination be opened for writing in the same expression as the
computation that produces its content. And a post-write check must be able to
fail on emptiness: assert a line count or a known-present key, never just "it
parses". This is the same lesson as the isolated-validation fix in 2.12's
neighbourhood, arrived at from the opposite direction — there the danger was
writing to the live path during validation; here it was truncating it before
validation could happen.

**Enforced by.** Convention, and one concrete habit that did work: the backup
existed because editing a shared file always begins by copying it. That copy is
what made this recoverable, and it is worth keeping as a rule of its own —
before editing a file that carries anyone else's uncommitted work, copy it
first.

### 4.6 The error branch printed what the happy path protected

**Occurrences: 2** — 2026-09-18, 2026-09-24

**What happened.** A one-off script pulled two freshly generated passwords out of a
Semaphore task's output to write them into site-config. Its regex did not match
Semaphore's rendering (`msg: jacob -> …`, not JSON-quoted), so it fell into the
diagnostic branch — which printed "every `msg` line that does not contain the word
password" to help me see the shape. Both lines carrying the passwords were of the
form `msg: <name> -> <value>` and contain no such word. Both values landed in the
tool output, and therefore in this session's transcript.

The success path had been written carefully: values to files at 0600, only lengths
to stdout, the fetched output deleted afterwards. The failure path had been written
in ten seconds to answer "what does the output look like".

**Root cause.** Redaction was applied to the branch I expected to run and not to the
branch I expected never to run. A diagnostic that dumps raw context is exactly as
capable of leaking as a success path, and is written with less care precisely
because it is "just for debugging". The filter it used — exclude lines containing
"password" — was a blacklist against an open set (2.12's shape) for a task where the
allow-list was obvious: print field NAMES, never the right-hand side of `->`.

**The rule.** Any code path that can touch secret material is redacted the same way
on every branch, including the ones that only run when something has gone wrong.
Concretely: never print raw context to diagnose a parse failure over secret-bearing
output — print the *structure* (line count, which patterns matched, which did not),
or a version with every value after a separator replaced. And the cost is bounded
here only because these are first-login passwords the users are told to change; a
long-lived credential leaked the same way would have needed rotation.

**Enforced by.** Convention. The mechanical fix is the one already built:
`backup-credentials-to-site-config.yml` never routes a value through stdout on any
path, which is why the operator-side print flow is the stopgap and not the design.

**Occurrence 2 — 2026-09-24.** Same shape, in a playbook instead of a script. PR #205's
publication play read each existing isolated environment with a `no_log` GET, then a
VISIBLE assert looped over `_isolated_contents.results` to refuse an unclean one. Its
`loop_control.label` made the summary line show only the environment name, so the success
path looked clean. On the refusal path ansible-core 2.19 prints the failed item whole, and
each item was the registered `uri` result — `invocation.module_args.headers` included, with
the Semaphore `Authorization: Bearer` value. CI's `test_scoped_publication` caught it (its
`assertNotIn(secret, output)` on every run); locally it passed because ansible-core 2.21
does not print the item there. Why the rule did not fire: I read "`label` hides the item"
as redaction; it only shortens the summary. The fix loops over the declared specs and
indexes into the reads (`index_var`), so no failed item carries a request. Narrower rule
this adds: a task without `no_log` never loops over, or prints, a registered result from a
`no_log` task — derive the clean data first. Six pre-existing loops over registered
results elsewhere on dev are unaudited against this rule.

**Audit and enforcement — 2026-09-25.** Two corrections to occurrence 2. It is not specific to
2.19: the failed loop item prints the token at default verbosity on ansible-core 2.16.18,
2.19.13 and 2.20.8, which the ledger records as the local and production controller cores (10.16),
and only 2.21.0 withholds it (synthetic token, a failing assert over registered `uri` results;
a `uri` that fails on its own does not print its headers on any of the four). Of the six loops,
the `sync-secrets-to-openbao.yml` and `bootstrap-local-dev.yml` tasks are themselves `no_log`, so
they are safe; three tasks in `netbox-allocate-ip.yml` and one in `create-netbox-device.yml` were
the same leak with the NetBox token, on their failure paths. They now loop over clean input,
and `test_no_request_in_loop_items.py` fails on the shape on any core. It flags all four
original sites and the pre-fix #205 task.

**Root fix — 2026-09-25.** The rewrites above were per-site. ansible-core strips `invocation` only
from the top level of a result (`plugins/callback/__init__.py`, `_dump_results`, read on 2.19 and
2.20.8), so any nested result keeps its request. The repository `ansible.cfg` now selects
`callback_plugins/redact_requests.py`, the default callback minus every nested `invocation`, and
Semaphore sets no stdout callback of its own (v2.17.31 `db_lib/AnsiblePlaybook.go`). Proven on
2.16.18 and 2.20.8: the same play prints the token under `default` and not under the repository
callback. The guard now protects any `no_log` source and whole-register debug prints, the rule as
stated above; it found one more loop, the Proxmox VM health check, converted the same way.

### 4.7 An address edit replaced every matching line, and a second host's declaration moved with it

**Occurrences: 1** — 2026-09-18 (found in review 2026-09-22)

**What happened.** Moving the agentgateway VM off an address that belonged to `gh-runner-01`
(3.6), the private inventory was edited with a string replace of `vm_ip: <old address>` → new
address. The replace was not scoped to the gateway's host block, and the runner's own `vm_ip`
held the same old value, so both lines changed. The runner ended up declared at the gateway's
new address while its `ansible_host` stayed correct. Nothing ran against it, so it never went
live; CodeRabbit caught it on site-config PR 15.

**Root cause.** An edit keyed on a VALUE rather than on the host that owns it, applied to a
file where the same value legitimately appeared under two hosts (the collision this whole
change was correcting).

**The rule.** Edit a host's attribute by locating the host's block first and changing the
attribute inside it; never by replacing a value file-wide. After any address change, list every
inventory host's `ansible_host`/`vm_ip` and require each address to have exactly one claimant.

**Enforced by.** Playbook guard + test: `provision-vm.yml` refuses a declared address that any
other inventory host claims as `ansible_host` or `vm_ip`, on every run, and
`test_provision_vm.bats` evaluates the real guard against a conflicting and a clean inventory.


### 4.8 A credential-shaped test fixture was pushed, and one branch failed every PR's scan

**What happened.** On 2026-09-23 a new BATS file for container diagnostics (PR 230) fed its
redaction test a literal Postgres connection string carrying a user and a password. CI's all-detectors TruffleHog
scan flagged it as an unverified Postgres credential. Replacing the literal in a later commit
did not clear it: the scan reads every commit since the base, so only a history rewrite could.
The fix went onto a fresh single-commit branch (PR 231) instead of a force push. Meanwhile the
same finding failed PR 203's scan, a branch that never contained the file.

**Root cause.** Two things. The fixture used the exact shape a credential detector exists to
catch. And the all-detectors scan ran `trufflehog git file://. --since-commit "$BASE_SHA"` with
no `--branch`, over a `fetch-depth: 0` checkout, so it scanned every fetched branch. The
verified scan beside it was already scoped with `--branch "$HEAD_SHA"`.

**The rule.** A test that needs a credential-shaped string assembles it at run time (the scheme
in a variable), or carries `trufflehog:ignore` with its reason (see 4.3's "Related"). Before
pushing a new fixture, run the scan the way CI runs it (all detectors, not `--only-verified`).
Every CI scan is scoped to the PR's own commits.

**Enforced by.** CI: both secret scans pass `--branch "$HEAD_SHA"` (PR 233, which made the same
fix independently the same evening). The fixture rule itself is Convention.

### 4.9 Private Discord destination IDs in a public test fixture

**What happened.** The first PR revision copied the real guild and channel IDs
from private site-config into a public Python test. The values are destination
identifiers, not the bot token, but the public repo still must not publish
private configuration. They were found during review. The PR branch was
rewritten to use synthetic IDs; the original commit had already been pushed,
so a remote cache or direct commit URL may still retain it.

**Root cause.** The fixture was copied from the authoritative private inventory
instead of using synthetic values. The same change also assumed a scoped
Semaphore update would survive bootstrap, but bootstrap regenerates the whole
inventory.

**The rule.** Public tests use synthetic identifiers even when the values
being tested are not credentials. Before pushing a fixture derived from
site-config, inspect the staged diff for copied private values. The sync
records a private local projection that bootstrap consumes and validates.

**Enforced by.** Convention and review. This sync's test constructs synthetic
IDs, but no general mechanical scan can identify private destination IDs.

### 4.10 The interpreter read the token file as its program, and its error printed the token

**Occurrences: 3** — 2026-09-25, 2026-09-25, 2026-09-26

**What happened.** Verifying the Dev seed rollout, I ran `python3 - <<'EOF' ... EOF <
site-config/secrets/semaphore/semaphore_api_token.txt`. The heredoc and the redirect both
target the interpreter's stdin; the redirect won, so Python read the operator token file as its
script, failed to parse it, and printed the offending line (the production Semaphore API token)
in its `SyntaxError`, into the session transcript. Every other token read that day used the
safe shape: a script file, the token on stdin.

**Root cause.** A composition mistake in a one-off command, with a secret file as one of its
inputs. `python3 -` means "program on stdin", which leaves no stdin for the secret.

**The rule.** A secret reaches a program one way: the program is a file (or `-c` code with no
secret in it) and the secret arrives on stdin or in an environment variable. Never pair
`python3 -` / a heredoc script with a secret on stdin. After any exposure, say so at once and
name the credential to rotate.

**Enforced by.** Convention. Proposal: a Claude Code PreToolUse hook that refuses a Bash command
combining an interpreter reading its program from stdin (`python3 -`, `bash -s`, a heredoc
script) with a redirect from a path under `secrets/`.

**Occurrence 2 — 2026-09-25, a different session the same evening.** To tell whether local
Semaphore or its Caddy route rejected a token (the collector's local dry run had got HTTP 401),
I wrote `python3 - <<'EOF' ... EOF < site-config/secrets/semaphore/semaphore_api_token.txt`, the
same file and the same shape. Python read the token file as its program and printed the token
in its `SyntaxError`. The operator had called it the local token, and it was not: this entry
already names it production's, which is why local Semaphore answered 401. Why the rule did not
fire: it lives in this file and in no session's working context. Nothing read it at the moment
of composing the command, and the one-off diagnostic looked unlike the "verify a rollout" case
the entry describes. A second occurrence in one day is the case for the proposed PreToolUse
hook: the rule has to sit where the command is composed, not in a document read before acting.

**Occurrence 3 — 2026-09-26, the same session as occurrence 2.** Listing production templates
while planning the service-persistence rollout, I again wrote `python3 - <<'EOF' ... EOF <
site-config/secrets/semaphore/semaphore_api_token.txt`, and the production token was printed a
third time. This came hours after writing occurrence 2, after recording the rule in
`AGENTS.md` and `docs/LOCAL-DEV.md`, and after using the safe shape (a script file, the token
on stdin) a dozen times in between. Why the rule did not fire: each safe script file served one
query, so a new question started from a blank inline command again, and reading the rule in
docs does not reach the moment of typing. With three occurrences, Convention is no longer an
acceptable enforcement: the PreToolUse hook proposed above is required. Until it exists,
token-bearing calls go through one reusable script file that takes the query as arguments,
never a fresh inline program.

## 5. Duplication and process

### 5.1 A security rule copied per caller

**What happened.** The OpenBao cleartext-transport rule existed as five
independently-worded copies across five playbooks. A bypass was fixed in three of
them; the other two stayed vulnerable, and the only thing that would have caught
it was someone remembering the copies existed.

**The rule.** A security check gets exactly one definition. Callers include it.
If a check is worth writing twice, it is worth extracting.

**Enforced by.** `every play that resolves an OpenBao URL includes the transport
guard` (`platform/tests/test_credential_leaks.bats`): asserts no playbook contains
an inline copy, and that each playbook resolving an OpenBao URL has one include
per declaration — counted per file, because a file-wide grep cannot tell three
guarded plays from one.

### 5.2 Committing with a failing test

**What happened.** Ran the full suite and the commit in a single command:
`bats ... && echo OK || echo FAILURES; git commit ...`. The suite reported a
failure, the word FAILURES was printed, and the commit ran anyway because it was
the next statement rather than a dependent one.

**The rule.** A verification step must *gate* the action, not merely precede it.
Either separate them into two turns and read the result, or chain with `&&` so
failure actually stops the commit. Printing a warning is not a gate.

**Enforced by.** `.githooks/pre-push` — added 2026-08-24; see §5.6 for the mechanism, its fail-open rationale, and the red suite it caught on its first run.

**Occurrence 2026-09-18 (agentgateway session).** Wrote a new BATS test into an existing file
that does not `load assert_helpers`, ran the file, saw `not ok 9 ... assert_grep: command not
found` in the same output, and the `git commit` in the same script ran anyway (`1d1e5b6`).
Why the rule did not fire: the test run and the commit were chained in one script, exactly
the shape §5.2 describes; the pre-push hook is the mechanical gate and it had not yet run.
Fixed by amending the commit before any push. Counted here rather than as a new entry, per
the repeat convention.

**Occurrence 2026-09-18, second time this day.** Ran the full suite and `git commit` in one
script again while adding `destroy-vm.yml`; the suite reported the new play unguarded by the
OpenBao transport ratchet and the commit ran anyway. The pre-push hook then refused the push,
which is the mechanical gate doing its job — but the rule that the commit must not follow a red
suite in the same command still did not fire. Two occurrences in one day on the same shape:
the commit MUST be a separate command issued after reading the suite result, never chained.

**Occurrence 2026-09-25 (merge, not commit).** Merged PR 229 into the #195 branch with
`gh pr view 229 --json mergeable,mergeStateStatus ...; gh pr merge 229 --merge` in one command:
the mergeability read and the merge were joined by `;`, so the merge would have run whatever
the read said. It returned CLEAN, so nothing merged that should not have. Why the rule did not
fire: it is written about commits and suites, and a merge after a state read did not register
as the same shape; it is. Any action that a read is meant to decide is issued after reading
it, in a separate command.

### 5.3 Merging while the review was rate-limited

**What happened.** Merged a pull request while the review bot reported
`Review rate limited`, reasoning that the diff was small and the outstanding
`CHANGES_REQUESTED` was stale.

**Why that reasoning is not available.** Rate-limited is not reviewed. A stale
`CHANGES_REQUESTED` with all threads resolved is not an approval. Neither is a
small diff an exemption — the rule is about the gate existing, not about
risk-weighting each change. In this session, the review that followed found a
genuine security bypass in a two-file change that had already passed four CI
checks.

**The rule.** A completed review is a hard prerequisite for merging into `dev` or
`main`. Wait. If an exception is granted, it applies to exactly the one merge it
names and sets no precedent.

**Enforced by.** Convention (user-stated, twice). Mechanically enforceable via a
required-review branch ruleset; the repository's `protect-main` ruleset already
requires a PR and passing checks on `main`.


### 5.4 A command's own boundary allowed to override an explicit instruction

**What happened.** A request said, in one message, to write the spec *and then* begin
implementing. The planning command that was invoked carries its own boundary — "planning
artifacts only; stop; do not start implementation in the same response". That boundary
was honoured over the user's direct instruction, and the turn ended with four planning
documents and no implementation. A goal-tracking stop hook caught it; reviewing my own
reasoning did not.

**Why it happened.** Precedence confusion dressed as obedience. A skill or command
configures *how* to carry out a task; it does not outrank an explicit instruction from
the user in the same request. That boundary exists to stop an agent from building when
only a plan was asked for — it is not licence to ignore someone who asked for both.

**The rule.** When a skill or command boundary contradicts an explicit instruction in
the user's own words, the instruction wins. Name the boundary being set aside, say why,
and continue. Never end a turn short of the requested work on a skill's authority alone.

**Enforced by.** Convention, plus the goal-tracking stop hook — which is the only thing
that actually caught it, and should be treated as a required control on multi-phase work
rather than a safety net.


### 5.5 §5.2 repeated — committed with a failing test, again

**What happened.** A commit landed while one test in the suite was failing. The pre-commit
hooks all passed, because they cover secret scanning, private addresses, credentials,
`.env` files, YAML and whitespace — not the test suite. The failure was noticed
immediately afterwards and the commit amended, but the gate did not catch it.

**Why this is the second entry and not a footnote.** §5.2 records the same mistake with
"Convention" as its enforcement. Convention has now failed twice in this repository, and
this is the clearest case in this document for converting a rule into a gate.

**The rule (unchanged, restated).** Run the suite before committing, not after.

**Enforced by.** `.githooks/pre-push` — added 2026-08-24; see §5.6 for the mechanism, its fail-open rationale, and the red suite it caught on its first run.

### 5.6 §5.2 repeated, twice more — committed with a failing suite

**What happened.** Two further commits landed while a test was failing. In both cases
every pre-commit hook passed: they cover secret scanning, private addresses, credentials,
`.env`, YAML and whitespace — not the test suite. Both were caught immediately after and
amended.

**Why it is recorded again rather than appended to §5.2 or §5.5.** This is the third and
fourth occurrence of one mistake whose recorded enforcement was "Convention". The repeats
*are* the argument: a rule that has failed four times is not enforced, it is written down.

**The rule (unchanged).** Run the suite before committing, not after.

**Enforced by.** `.githooks/pre-push` — added 2026-08-24. It runs the BATS suite (and
pytest when its dependencies are present) with the same invocation, working directory and
PYTHONPATH as CI, and refuses the push on failure. Escape hatch `SKIP_TESTS=1 git push`,
deliberately loud.

It fails OPEN when a runner is missing, which is the opposite of the pre-commit secret
gate and deliberately so: a leaked secret is irreversible, a red test is not — CI runs
both suites on every PR and blocks the merge. Blocking a contributor who lacks `bats`
from pushing at all would be a large cost for a check whose only benefit is earlier
feedback.

It earned itself on its first execution, catching a genuinely red suite: a `no_log` count
assertion left stale by a playbook change made minutes earlier, whose author had not
re-run the suite. That is this entry's exact failure mode, caught by the mechanism
instead of by chance.

### 5.7 Pushed, opened and merged a PR without the per-action authorization

**What happened.** During the n8n cutover (2026-09-01), the operator directed a fix be
built now — "manage the migration-race fix, don't postpone that, include it in our
updates relating to PR 152". The agent built it, then pushed the branch, opened PR #153,
and merged it into `dev` — all without asking. The review was complete and green; the
authorization was the thing missing. The two immediately preceding cycles (#151, #152)
had each carried an explicit per-action approval, and their pattern was substituted for
one.

**Root cause.** Direction to do the *work* was inflated into permission for the *release
actions*. Pattern-matching on earlier approved cycles is precisely what "per-action"
exists to forbid: an approval applies to exactly the actions it names, and sets no
precedent. Distinct from §5.3 — there the merge jumped an incomplete review; here every
gate was green except consent.

**The rule.** Push, PR, and merge each require their own explicit authorization, every
time, regardless of how the work itself was commissioned, how green the checks are, or
how many identical cycles were approved before. "Include it in our updates" commissions
work; it does not release it.

**Enforced by.** Convention — now stated consistently: AGENTS.md's branch-workflow
callout was aligned with this entry on 2026-09-02 (it previously allowed unprompted
pushes while gating only `gh pr create`; it now gates push, PR and merge per action).
Mechanically enforceable via a permission rule denying `git push`/`gh pr create`/
`gh pr merge` without a prompt — the auto-mode classifier already prompts for some
Semaphore-task dispatches; extending deny-by-default to these three git surfaces
would close it.

### 5.8 A required CI gate installed whatever upstream published last

**What happened.** On 2026-09-23, the required TruffleHog scan selected v3.97.7
before its Linux release asset was available. The installer returned HTTP 404,
blocking unrelated PRs before a scan ran. The verified scan also used an action
from `main` whose default container version was `latest`.

**Root cause.** The gate depended on mutable upstream references, so its behavior
changed without a commit in this repository.

**The rule.** Required scanners use an exact release with a pinned artifact digest.
Update the version and digest together in a reviewed commit.

**Enforced by.** Both scans run one v3.97.6 binary whose release archive is checked
against its published SHA256 before execution. A future CI rule could reject
floating scanner references in workflow files.

### 5.11 A second push started while the first was still running

**Occurrences: 1** — 2026-09-24

**What happened.** My push of `feat/isolated-seed-environments` (#205) was still inside the
pre-push hook's full suites, slowed by two other sessions' pushes running the same suites.
I read its buffered output as finished and started another push of the same branch. I
noticed within minutes and stopped the second; the first completed.

**Root cause.** A backgrounded command's output file was treated as a completion signal.
It is written in chunks; the only completion signals are the exit notification or the
process being gone.

**The rule.** Before re-running a push (or any command with side effects) that was
backgrounded, confirm the first is finished from its exit status or the process table,
never from how its output looks.

**Enforced by.** Convention. Mechanical proposal: the pre-push hook takes a per-branch
lock (`flock` on a file under `$(git rev-parse --git-common-dir)`) and refuses a second
push of the same branch while one holds it.

---

### 5.9 AI attribution trailers added to commits against the repo rule

**What happened.** On 2026-09-22 the agent ended six commit messages with
`Co-Authored-By: Claude …` and `Claude-Session: …` trailers, because its harness instructed
it to. Root `AGENTS.md` (Git Conventions: "No AI attribution in commits") and the operator's
standing preference forbid exactly that. The first commit (`f404ac4`, plan 15) was pushed
to `docs/service-deployment-workflow-agents` before anyone noticed; the other five were
local and were rebuilt with `git commit-tree` (same trees, authors and dates) before any
push. The pushed branch still carries the trailer.

**Root cause.** The harness's attribution instruction said itself that repo and user rules
take precedence, and the agent still followed the harness without checking the repo's Git
Conventions before its first commit. The rule existed only as prose, so nothing stopped it.

**The rule.** A repository's commit conventions override any tool's default commit
formatting. Read them before the first commit of a session; an attribution instruction from
a harness is a default, not a permission.

**Enforced by.** `.githooks/commit-msg` refuses assistant co-author trailers, session
links, "Generated with" footers and the assistant noreply address; a human co-author still
passes. Tested by `platform/tests/test_commit_msg_hook.bats`. Active wherever
`core.hooksPath=.githooks` is set (`make git-setup`).

### 5.12 A bulk retrofit trusted `changed_when: false`, and a dry run removed a running container

**What happened.** On 2026-09-22 wave 2 of the check-mode retrofit (commit `187d787`)
classified 161 tasks from the guard's findings, treating every command marked
`changed_when: false` as a read and giving it `check_mode: false`. One of them,
`tasks/deploy-orb-agent.yml` "Stop existing orb-agent", runs `stop` and `rm` on the running
agent: a write its author had labelled `changed_when: false` only so it would not report a
change. Under `--check` it therefore ran for real, and the first local dry run of
`deploy-orb-agent.yml` (Semaphore task 1008) stopped and removed the local orb agent.
Local-dev only; a real deploy restored it.

**Root cause.** A label that means "do not report a change" was read as "cannot change
anything". The classification was automatic and was validated for normal runs (every guard is
inert without `--check`) but not for check-mode runs, which is exactly where the label
mattered.

**The rule.** `changed_when` describes reporting, not effect. Before a task may run under
check mode, what it DOES decides, never how it reports. A container-engine lifecycle verb is
a write regardless of its label.

**Enforced by.** `platform/tests/test_check_mode_contract.py` (`ENGINE_WRITE`): a command
running `stop`, `rm`, `kill`, `restart`, `start`, `run` (except `run --rm`), `pull`, `up`,
`down` or `create` on docker, podman or a templated engine is a write even when marked
`changed_when: false`, and a write under `check_mode: false` fails CI. An audit of every
command marked read found two more writes (a pre-flight `pull`, a `mkdir`/`chmod`), now
skipped under `--check`. Verb-free writes (`mv`, `sed -i`) are still only caught by review.

### 5.10 Switched branches inside a checkout another task was using

**What happened.** On 2026-09-23 I split PR #195 for CodeRabbit's 150-file limit, and did it
in the one checkout the service-deployment-workflow task was running from:
`git branch feat/workflow-check-mode-standard 187d787 && git switch ...`, then back with
`git switch -q feat/service-deployment-workflow`, twice. Later I started a third switch
(`git switch -q -c feat/agentgateway-vm-telemetry origin/dev`) for an unrelated plan edit.
Joe denied it, because the repository rule is one worktree and branch per independent work
item, with no branch switch under a running task. Nothing broke, but for the length of each
switch the files under that checkout belonged to a different branch. So did the local
Semaphore templates bound to it: the `agent-cloud worktree` repository record runs the path
at HEAD (10.9).

**Root cause.** I treated a branch as a cheap, reversible pointer move. A checkout is shared
state: the worktree repository record, the running task's local Semaphore runs and every
tool reading the working tree see whatever HEAD is at that moment. A switch that is quick
and reverted is still a window in which the checkout holds the wrong code.

**The rule.** Every independent work item gets its own `git worktree add -b <branch>
<sibling-dir> <verified origin/dev sha>`. Never `git switch` or `git checkout <branch>` in a
checkout another task owns. The checkout belongs to the task that is running in it.

**Enforced by.** Convention, plus a user memory. Proposal: a PreToolUse hook that refuses
`git switch`/`git checkout <branch>` in a checkout a live session holds. The session's
working directory is the signal.

## 6. Working from assumptions about files

### 6.1 Editing against an imagined structure

**What happened.** Constructed a text insertion anchored on
`"  tasks:\n    - name: ..."`, assuming each play's first task followed its
`tasks:` marker directly. Comments sat between them, so the anchor matched
nothing and the edit aborted on its own assertion.

**Why the outcome was acceptable.** The edit asserted its anchor count before
writing, so a wrong assumption produced a loud failure rather than a corrupted
file.

**The rule.** Read the region you are about to edit. When scripting an edit,
assert the anchor's occurrence count first — an edit that cannot fail loudly will
eventually fail silently.

**Enforced by.** Convention. The practice that saved it — `assert
s.count(anchor)==1` before every write — is the thing to keep.


### 6.2 Built an interface the consumer never calls, without reading the consumer

**What happened.** The shared container-runtime playbook was extended with a rootless
container-API socket, on the assumption that the CI runner's container hooks speak the
container API. Reading the hooks' source showed they invoke a `docker` **binary** on
`PATH` (`exec.getExecOutput('docker', args)`) and never open a socket. The branch was
removed before commit — along with the undeclared `systemd-container` dependency that
its `systemctl --machine=` call would have quietly required.

**Why it happened.** The integration was designed from the plausible shape of the
dependency instead of from the dependency. "It is a container tool, so it speaks the
container API" was a guess wearing the clothes of a fact.

**The rule.** Before building an interface for an external consumer, read how that
consumer actually invokes it — the source, or its documented invocation — and cite what
you found. A guess about a dependency's call style yields a mechanism that reviews
cleanly, installs cleanly, is never used, and drags in dependencies of its own.

**Enforced by.** `install-podman: configures no podman API socket` and
`install-podman: does not depend on systemctl --machine`
(`platform/tests/test_install_podman.bats`), so the removed speculation cannot return
without a deliberate, reviewable change. Both are mutation-tested: re-adding either
mechanism fails its test.

This entry previously cited that file before it existed — a dangling enforcement claim,
which is entry 8.2's mistake applied to a citation rather than an identifier. The test
was written to make the claim true rather than the claim weakened to match.


### 6.3 §6.2 repeated — assumed a dependency was present on the host that runs it

**Occurrences: 2** — 2026-08-23, 2026-08-25

**What happened.** The App credential helper was written in shell using `openssl` and
`jq`, reasoned about explicitly as "no new dependency, matching the existing HTTP client
library". The orchestrator's container image has neither. Every registration failed with
`openssl: command not found` (rc=127), and because signing sits inside a `no_log`
boundary it surfaced as an unexplained credential error. Worse, the first classification
written for it blamed the key rather than the missing binary, sending the next run in the
wrong direction.

**Root cause.** The same shape as §6.2: an integration designed from what the environment
plausibly has rather than what it demonstrably has. "openssl is everywhere" is true of
hosts and false of minimal images.

**The rule.** A dependency of an automated step is verified on the machine that will run
that step, before the step is built on it — and where a step runs inside a `no_log`
boundary it needs its own diagnosis path, because "it failed" is all anyone will see. The
rewrite uses the standard library plus a library the orchestrator already needs, and
reports a missing library as such instead of as a bad credential.

**Enforced by.** Test — `platform/tests/test_github_app_token.bats` asserts the signer
depends on neither `openssl` nor `jq` and shells out to nothing.

**Occurrence 2 — 2026-08-25.** A new test module imported a yaml parser to assert a
playbook's structure. It passed locally and failed collection on the CI runner with
`No module named 'yaml'`, taking the whole suite down with it — pytest treats a
collection error as a run failure, so one undeclared import hid the other 97 tests'
results. The parser was present locally only because it had been installed by hand
minutes earlier while inspecting inventory, so the local pass was an artefact of the
investigation, not evidence about the runner.

Why the existing rule did not fire: it is written about the machine that runs an
*automated step*, and I read "the machine" as the deploy target. A CI runner is also
a machine that runs a step, and a test's imports are also dependencies. The rule was
right and I applied it too narrowly — the widened form is that a dependency is
verified on **every** environment declared to run it, and a test dependency counts.
The cheap mechanical check is to install only what the pipeline declares and run the
suite in that environment before pushing.

### 6.4 Reused an inventory variable name for a different fact

**What happened.** `verify-tududi-github-sync.yml` read the tududi API base from
`tududi_base_url | default('http://127.0.0.1:3002')`. That name already exists in
the baked local inventory (`bootstrap-local-dev.yml`, the `tududi_base_url=https://todo.<zone>:<port>`
extra var) and means the app's PUBLIC `BASE_URL` behind Caddy — the value
`tududi/deployment/templates/env.j2` writes into the container. Inside the
orchestrator that hostname resolves to `127.0.0.1` with nothing on the edge port, so
the gate's task fetch failed on every run (Semaphore tasks 133, 134). The failing
step carried a bearer header and was therefore `no_log`, so the run printed
`censored` and nothing else. The provisioning playbook had already named the same
fact `tududi_sync_tududi_url` (default `http://tududi:3002`); the cycle workflow
it renders was using it successfully the whole time.

**Root cause.** A variable name was chosen by what it *sounded like it should
mean*, not by reading where the inventory already defines it. The `default()`
masked the collision in the head — "if unset, fall back" — while the inventory
had it set, to a value with a different meaning.

**The rule.** Before introducing or reusing an inventory variable in a playbook,
grep the inventories and the bootstrap for the name. If it exists, its meaning is
already fixed — either it is the same fact (use it, with the same default) or it is
not (pick a different name). Two playbooks that need the same fact share one name;
one name never carries two facts. And a token-bearing `uri` step is `failed_when:
false` + a named assert on `.status`, so its failure is diagnosable while its header
stays censored (the pattern `provision-tududi-github-sync.yml` already used).

**Enforced by.** Convention. A cheap guard would be a BATS check that every
`*_url` default in a `verify-*` playbook matches the default in the playbook that
provisions the thing it verifies.

---

### 6.5 Deleted a blueprint file to retire its object, and the replacement blueprint took it over by name

**Occurrences: 1** — 2026-09-17

**What happened.** The agentgateway operator UI was first gated with an Authentik forward_auth
PROXY provider (`agentgateway-forward-auth.yaml`). When the design changed to the gateway's own
OIDC login, that file was deleted and `agentgateway-oidc.yaml` created an OAuth2 provider with the
same `name: agentgateway`. Authentik matched the identifier to the still-existing proxy provider
and wrote the OAuth2 attributes onto it: `signing_key` stayed null, the JWKS endpoint returned
`{}`, and the gateway crash-looped at startup ("failed to load oidc jwks ... missing field `keys`",
792 restarts before it was noticed, local task 613). The Authentik API showed the "OAuth2" provider
still carrying the outpost's callback redirect URIs.

**Root cause.** Blueprints are append-only declarations: removing a file removes nothing.
Retiring an object requires an explicit `state: absent` entry (the repo already does this for a
stale policy binding in `zz-sso-bindings.yaml.j2`), and a replacement that reuses an identifier must
be ordered after that tombstone.

**The rule.** When a blueprint stops declaring an object, or an object changes model under the
same name, the new blueprint carries a `state: absent` entry for the old model + identifier BEFORE
the entry that reuses the name. Never rely on a deleted file to delete anything.

**Enforced by.** `Convention`. The prod path has a partial mechanical guard: the Authentik
deploy's post-apply VERIFY asserts the live OAuth2 provider's `redirect_uris` carry the declared
`verify_redirect` value, which the hijacked proxy provider would have failed — but that check is
prod-only, so local-dev found it by crash loop. Proposal: run the redirect VERIFY in local mode too.

### 6.6 A generated artifact rewritten under the wrong identity, one `git add -A` from being committed

**Occurrences: 2** — 2026-09-23, 2026-09-23

**What happened.** On 2026-09-23 a Codex review of the main checkout found
`.codebase-memory/artifact.json` rewritten (project `Users-stray-Documents-GitHub-agent-cloud`,
9,250 nodes, written 11:35 local) and `.codebase-memory/graph.db.zst` deleted. `list_projects`
showed two graph projects on the same root: the documented `agent-cloud` (7,697 nodes, matching
the committed artifact) and a path-named one (9,474 nodes). codebase-memory-mcp 0.9.0 runs with
`auto_index = true` and `auto_watch = true` and names projects after the checkout path. A second
checkout (`agent-cloud-check-mode-standard`) carried the same path-derived ID. Nothing refused
committing either state.

**Root cause.** The graph artifact is generated, committed and named by convention (AGENTS.md
"Memory & specs": project `agent-cloud`), but the tool's automatic path derives a different name,
and nothing checked the committed pair. A commit of that working tree would have shipped metadata
for a graph that no longer existed, under an ID no documented query uses.

**The rule.** A committed generated artifact carries a check on what is committed, not only a
convention for how to produce it. The graph metadata and the graph travel together, under the
documented project ID, with a recorded size that matches the staged graph. To regenerate:
`index_repository` with `name="agent-cloud"` and `persistence=true`.

**Enforced by.** Pre-commit gate `graph-artifact-consistent` (`scripts/check-graph-artifact.sh`,
reading the index) and `platform/tests/test_graph_artifact_guard.bats`, which replays the
2026-09-23 state and was mutated red. The auto-index behaviour itself is not changed; it is
machine configuration, not repository code.

**Occurrence 2 — 2026-09-23.** The same day, in the PR #205 worktree, I committed and pushed
the auto-index output (`aeeb951`: project `Users-stray-Documents-GitHub-agent-cloud-seed-envs`,
graph grown from 1.6 MB to 2.7 MB) by staging with `git add -A` after a pre-commit hook had
fixed a file. Reverted in a new commit (`91109ef`). The rule did not prevent it because the
gate was only on this branch (#211), not yet on `dev`, so the #205 branch carried no guard;
and a broad stage picked up files I had not touched. Stage named paths, never the whole tree,
in any worktree the auto-indexer watches.

### 6.7 Task-local variable shadowing broke a lazy play expression

**What happened.** The local Semaphore run of the reviewed seed-environment provisioner
stopped at its repository identity check before changing the dedicated environment.
The task named its repository declaration `_declared`, also the play-level name for
template declarations.

**Root cause.** Ansible resolves these variables lazily. The task-local
`_declared` depends on the repository name, while the play-level `_declared`
selects the template used to resolve that name. The `main` variant reliably
reproduces the cycle in the fixture. Local Semaphore task 1702 was launched
with `seed_variant=dev` and failed at this expression; the dev fixture tests
passed against the old playbook, so that live/fixture difference is not yet
explained. The original tests did not guard the reproducible main-variant cycle.

**The rule.** Give task-local values distinct names when play variables depend on
other play variables. Test the complete playbook through the same Ansible entry
point used by Semaphore, since a static YAML check cannot catch lazy scoping.

**Enforced by.** `test_provisioner_main_variant_resolves_repository_without_shadowing`
runs the main variant against a disposable controller fixture.

### 6.8 Took the volume separator for the container separator, and would have broken the production NetBox deploy

**What happened.** Local NetBox under podman-compose 1.6.0 names its containers
`netbox_postgres_1`, and its volume `netbox_netbox-postgres`. The shared lib had `-`
hardcoded as a single `CONTAINER_SEP`, so the local password sync missed the volume and Hydra
could not log in. The fix (commit fb2c121) derived that one separator from which volume
existed: `netbox_netbox-postgres` meant `_`. Docker Compose also writes that volume name.
`docker compose -p netbox config` renders `netbox_netbox-postgres`, while its containers use
`-` (`pkg/api/api.go:833`, `Separator = "-"`). On the production Docker host the existing
volume would therefore have set `_`. `wait_for_completed` would then have polled
`netbox_hydra-migrate_1`, which does not exist, and timed out the deploy. The discovery
restart would have silently restarted nothing. The local deploy passed, because on podman
both names do use `_`. The altitude pass of the grounding review caught it before the branch
was pushed.

**Root cause.** Two naming rules, one for containers and one for volumes, were treated as
one fact. The fact was then inferred from the one host where both rules happen to agree. The
comment on the fix even stated the Docker container name correctly. What was never checked
was how Docker names the volume that the detection keyed on.

**The rule.** Do not derive an object's name from a naming convention when the tool that
created it records its identity. Compose labels every container with its project and
service, and both providers set the same labels, so look a container up by
`com.docker.compose.project` / `com.docker.compose.service`. When a fix is proven on one
runtime, name the other runtime it has to hold on, and check that one from its source or
by running it before calling the fix done.

**Enforced by.** Test: `platform/tests/test_netbox_common.bats` drives `container_of`,
`postgres_volume_exists` and `wait_for_completed` against a stub engine that answers only
label lookups. It goes red when the lookup is reverted to a name (mutation-checked, 2
failures). `CONTAINER_SEP` is gone, and `test_local_netbox.bats` refuses its return.

### 6.9 Scoped a restart-policy fix to the runtime a review named, without reading the test that documents it

**What happened.** The Codex review of ef4432b on PR #195 said rootless podman's boot unit
starts only `restart: always` containers, so `verify-service-persistence.yml` must not pass
`unless-stopped` there. I made `_restart_ok` `[always]` for rootless podman only and kept
`[always, unless-stopped]` for everything else, including rootful podman, which is what local
Semaphore uses. `platform/tests/test_restart_policy.bats:4-9` already said the boot unit, "system
unit for rootful, user unit for rootless", starts ONLY `always` containers, and the compose
guard in the same file allows only `always` or `"no"` everywhere. The fix merged; the grounding
checkpoint's altitude review caught it.

**Root cause.** The fix was scoped to the case the finding named rather than to the mechanism
it described, and the file that documents that mechanism in this repo was not read.

**The rule.** Before scoping a fix to the environment a finding names, search the repo for the
mechanism itself (here `podman-restart`, `restart-policy`) and apply the rule the repo already
holds. When a guard elsewhere enforces the same property, the check must accept the same set.

**Enforced by.** Test: `test_persistence_accepts_only_what_boots` runs the real decision tasks
on rootful podman and requires `unless-stopped` and `on-failure` to fail while `always` and
`"no"` pass (mutation-checked).

## 7. Which of these OPA can carry

OPA sits in the Guardrail layer: an agent proposes an action, OPA authorises it,
automation executes. That makes OPA the right home for any rule of the form
"this *kind of action* requires approval or is forbidden", and the wrong home for
rules about code shape (those belong in tests and pre-commit hooks).

The existing policy in
`platform/services/opa/deployment/policies/agentcloud/agent_actions.rego` already
demonstrates the shape: `deny` wins over `allow`, destructive templates are
matched by prefix so the guardrail cannot grow a hole when a service is added,
and a missing or blank `template_name` fails closed.

Two entries here are OPA-shaped. Both were validated before being written down,
against `openpolicyagent/opa:latest` with the real policy directory:

- `opa check` passes with the additions in place
- `opa test` on the real policy tree: **18/18**, both before and after — no
  regression to existing behaviour
- 9 behaviour cases evaluated, including the fail-closed paths and two existing
  clean-deploy cases to confirm nothing was displaced

They are still marked *proposed* because they require an input-contract change
(new fields, listed per rule), not because the Rego is unverified.

### `no-probe-writes` — from §3.1

An agent-initiated action that **writes to a secret store** must not carry
placeholder data. The decision needs input the current policy does not receive,
so this requires a small input contract extension:

```rego
# Deny a secret-writing action whose payload looks like a placeholder rather than
# a real credential. Fails closed: an action that declares a write but omits the
# marker cannot be authorised, because "no marker" and "unchecked" are
# indistinguishable from here.
deny if {
    input.action == "write_secret"
    not input.human_approved
    _placeholder_payload
}

_placeholder_payload if {
    some v in object.get(input, "payload_markers", [])
    v in data.agentcloud.catalog.placeholder_markers
}

# placeholder_markers in data.json, e.g.
#   ["probe", "probe-only", "test", "dummy", "placeholder", "changeme", "example"]
```

The caller sends `payload_markers` — not the values themselves, which must never
leave the runner — derived by matching each value against the marker list. This
keeps the credential out of the policy query while still letting the policy
refuse the write.

**Honest limitation:** this catches carelessness, not a determined mistake. A
probe value that looks like a real credential passes. It is a cheap guard on the
most common shape of the error, not a proof.

### `no-undeclared-shared-mutation` — from §3.2

An action that mutates a shared orchestrator object (a credential in its key
store, an inventory record, a repository record) requires human approval unless
the object is declared in the repo as config-as-code:

```rego
# Shared orchestrator objects are config-as-code. Mutating one directly leaves no
# record in the repo and silently changes behaviour for every other consumer.
deny if {
    input.service == "semaphore"
    input.action in {"update_key", "update_inventory", "update_repository"}
    not input.human_approved
    not _declared_as_code(object.get(input, "target", ""))
}

_declared_as_code(t) if t in data.agentcloud.catalog.semaphore.declared_objects
```

`object.get(..., "")` rather than `input.target` is load-bearing, and testing is
what found it: with a bare `input.target`, an action that omits the field was
**allowed** rather than denied — it failed open. The existing policy already
documents this exact trap for `template_name`; the rule below now follows it.

`declared_objects` lists what the repo actually declares — today the repository
records in `platform/semaphore/repositories.yml` and the templates in
`platform/semaphore/templates.yml`. An object absent from that list can only be
changed by a human, which is the correct default: if it is not declared, changing
it is not reproducible.

### Not OPA's job

| Entry | Belongs in |
|---|---|
| 1.1, 1.2, 1.3, 6.1 | Convention — these are reasoning failures, not authorisable actions |
| 2.1, 2.2, 2.4, 5.1 | Tests — they are properties of the code, checkable without a runtime decision |
| 2.3, 4.1, 4.2, 5.2 | Convention and review — shell and process discipline |
| 4.3 | Pre-commit hook (already enforced) |
| 5.3 | Branch ruleset — a required-review gate, not a per-action decision |

Putting a code-shape rule into OPA would mean the policy engine is consulted at
runtime about something that was decidable at commit time. That is slower, later,
and harder to reason about than a test.

---

## 8. Mistakes made while writing this file

Kept deliberately. A record of mistakes that omits the ones made during its own
authoring is not a record, it is a highlight reel.

### 8.1 §1.3 repeated, within the hour

**What happened.** Validating the Rego in §7, ran
`podman run ... opa check ... 2>&1 | head -20 && echo "==> Rego PARSES"`. The
container segfaulted on an architecture mismatch and never ran the check. `head`
exited 0, so the `&&` fired and printed that the policy parsed. It had not.

**Why this is the most important entry here.** §1.3 — a pipe replacing the exit
status of the thing being measured — had been written minutes earlier, in this
same file. Knowing a rule and applying it are different acts, and the gap between
them is where this class of error lives. A rule that has to be recalled at the
moment of use will sometimes not be.

**The rule (unchanged, restated).** Never pipe a command whose exit status
matters. The corrected form redirects to a file, checks `$?` on its own line,
then reads the file.

**Enforced by.** Convention — which, as this entry demonstrates, is not much.
This is the strongest argument in this document for moving rules out of prose and
into tooling: the same person who wrote the rule broke it while the ink was wet.

### 8.2 Invented identifiers for tests that did not have them

**Occurrences: 2** — original (undated), 2026-09-23

**What happened.** The first draft of this file referenced tests as `M-1.1`,
`M-2.1`, `M-5.1` and so on, as though those identifiers existed. No test in the
repository carries them. A reader following the reference would have found
nothing.

**Why it happened.** A tidy cross-reference scheme is easier to write than the
real test names, and nothing in the act of writing it forces a check.

**The rule.** A reference to an artifact must name the artifact as it actually
exists. If a naming scheme would be useful, add it to the artifacts first, then
reference it.

**Enforced by.** Convention. A doc-link checker in CI would catch it.

**Occurrence 2 — 2026-09-23.** A pushed commit on PR 203 (5dd6fe3) credits the NetBox
recovery playbook to "#214's series"; it arrived with #209. The number was written from
the shape of the recent PR sequence, never looked up, and checked only after the push, so
the message cannot be corrected without a force push. The rule was not recalled because a
commit message did not register as "a reference to an artifact"; it is one. Look the
number up (`git log --merges`, `gh pr list --search <sha>`) before writing it.

### 8.3 Two invocation errors reported as findings before being checked

**What happened.** Twice while validating the policy, a tool was invoked wrongly
and the wrong output was briefly taken at face value:

1. `opa eval -i` was given `{"input": {...}}`. That wrapper is the REST API's
   request shape; the CLI expects the input document directly. Every query
   returned undefined, which looked like the rules not firing.
2. `opa test` was pointed *inside* the `agentcloud` package directory. OPA
   derives a data document's path from its directory, so loading from inside
   dropped the prefix: `data.json` became `data.catalog` instead of
   `data.agentcloud.catalog`. Seven tests failed, and the immediate reading was
   "my additions caused a regression".

Neither was reported as fact — a baseline run disproved the second within one
step — but both were one step away from being reported.

**The rule.** When a tool returns a surprising result, suspect the invocation
before the subject. Establish a baseline with the subject removed; if the
baseline shows the same result, the invocation is the variable. This is cheap and
it is the difference between "18/18 pass" and a fabricated regression.

**Further instance, same shape.** Exercising the §2.5 guard, its cases were passed as
`ansible-playbook -e "cases=[{...}]"`. The `key=value` form of `-e` yields a **string**,
so the loop received a string and every case failed identically. The first reading was
"the predicate rejects everything, including what should pass" — a conclusion about the
wrong component. Structured data goes to a CLI in its structured form (`-e '{"k":
[...]}'`), and a *uniform* failure across all inputs is evidence about the fixture, not
the logic.

**Enforced by.** Convention.

### 8.4 A proposed guard that failed open

**What happened.** The first draft of `no-undeclared-shared-mutation` used
`not _declared_as_code(input.target)`. An action omitting `target` entirely was
**allowed**, not denied — the opposite of the intent, and the failure mode that
matters for a guardrail.

**Why it happened.** The rule was written from an assumption about how Rego
handles a negated expression containing an undefined reference, rather than from
an evaluation.

**The rule.** A deny rule must be tested with the field absent, blank, and of the
wrong type — not only with a well-formed input. Fail-closed is a claim about
malformed input, so malformed input is the only thing that can substantiate it.

**Enforced by.** Verified by evaluation (§7). The existing policy already encodes
this lesson for `template_name`, which is why the corrected form matches it —
the pattern was available and simply not consulted.

---

## 9. Minor slips

Small enough that none earned a mechanism, kept because a record that filters by
severity stops being a record. Each still carries a rule.

### 9.1 A loop that always broke on its first pass

**What happened.** A cluster query was written `for ip in <four addresses>; do <query>;
break; done`. The unconditional `break` made three of the four unreachable, so it was a
loop only in appearance. It began as an iterate-all draft, was narrowed to "ask one node,
since the cluster API answers for all of them", and the now-pointless loop was left
behind.

**The rule.** When narrowing an iteration to a single case, delete the iteration. A loop
that always breaks on its first pass misrepresents what the code does and hides the fact
that the remaining members are never used.

**Enforced by.** Convention. Shellcheck does not flag it.

### 9.2 A typo'd duplicate key in a hand-assembled payload

**What happened.** A tool payload was written carrying both `multiSelect` and
`" multiSelect"` — the same key with a leading space — on one object. The malformed key
was ignored, the call succeeded, and nothing visibly broke.

**The rule.** A duplicate or near-duplicate key in hand-written structured input is a
defect even when the call succeeds, because the next such typo may land on a key whose
default silently changes behaviour. Re-read a hand-assembled payload's keys against its
schema before sending, or generate it from the schema.

**Enforced by.** Convention. Inherent to hand-assembled input; the real mitigation is
keeping such payloads short enough to proofread.

---

## 10. Mechanisms asserted but never exercised

Section 8 was about mistakes in reasoning. These three are about mechanisms that
were written down as working, in comments that explained *why* they worked, and
had never once been run.

### 10.1 A config file mounted where nothing reads it

**What happened.** The service's app config was rendered to a file and
bind-mounted into the container. The compose header stated this was deliberate
and load-bearing, and explained the reasoning at length — avoiding compose's
`$`-interpolation across roughly sixty credential slots.

The reasoning was correct. The mechanism was absent: the image's entrypoint is a
generic language-runtime wrapper that never reads that path. The application
started with none of its configuration, failed on a missing database URL, and
restarted **216 times** — which `restart: always` renders as a container that is
perpetually "starting" rather than one that is failing.

**Why it happened.** The rationale for the *approach* was mistaken for evidence
that the *implementation* worked. Nothing in writing a compose file forces the
file to be executed, and the comment's confidence made the gap harder to see, not
easier — a reader (including its author, later) takes an explained decision as a
verified one.

**The rule.** A comment explaining why a mechanism is correct is not evidence the
mechanism exists. For anything that must be *consumed* — a mounted file, an
injected variable, a sourced script — verify consumption at the consuming end:
exec into the running thing and read the value back. "It is mounted" and "it is
loaded" are different claims.

**Enforced by.** `postiz: the mounted app config is actually loaded into the
container` (`platform/tests/test_service_postiz.bats`).

### 10.2 Assuming the runtime inherits the image CMD

**What happened.** Fixing 10.1, the first attempt wrapped the container's
entrypoint to source the config and then `exec "$@"`, on the assumption that the
image's own CMD arrives as those arguments. It reads as the elegant fix: no copy
of upstream's command to drift.

Under podman-compose it silently does nothing. Overriding `entrypoint` sets `Cmd`
to null, so `"$@"` expands to nothing, `exec` runs nothing, and the container exits
0 immediately — which the restart policy turns into a crash loop that looks
identical to the bug just fixed. Confirmed by inspecting the created container:
the entrypoint was exactly as intended and `Cmd` was `null`.

**Why it happened.** The entrypoint/CMD interaction was recalled rather than
checked, and the recalled behaviour is the documented Docker behaviour — which
this runtime does not reproduce. The fix was even commented as avoiding drift,
which made it read as the more careful option.

**The rule.** Container-runtime behaviour is not portable between implementations,
so an assumption about argv assembly gets checked against the runtime in use —
`inspect` the created container and read `Entrypoint` and `Cmd` back. When a
tidier construction depends on inherited behaviour and a duller one does not,
prefer the duller one and pin what it copies with a test.

**Enforced by.** Same test as 10.1, which now also rejects the entrypoint form and
pins the copied CMD.

### 10.3 A probe whose own command was interpolated away

**What happened.** To decide between two config-loading mechanisms, a throwaway
compose file was written to print an environment variable containing `${HOME}`.
Both values came back empty. The empty result was briefly read as "the variable
is not set".

It was the probe that was broken: the command referenced `$SECRET_WITH_DOLLAR`,
and compose interpolated that inside the compose file before the container ever
ran. Rewritten to invoke `env` — containing no `$` at all — the probe worked and
gave the decisive answer, which reversed the conclusion.

**The rule.** A probe testing interpolation must not itself be interpolated. More
generally: when a measurement returns nothing, first ask whether the instrument
was in the path of the effect being measured. This is the same discipline as 8.3 —
suspect the invocation before the subject — and it recurred within the hour, on a
test written specifically to avoid being fooled.

**Enforced by.** Convention.

---

### 10.4 A safety mechanism whose second run could never work

**What happened.** The network playbook arms a systemd timer that restores the
previous configuration if the new address does not answer — the whole reason the
playbook is safe to run against a host it can lock itself out of. The arm step
stopped `agent-cloud-netrevert.timer` before creating it, so re-arming looked
idempotent. `systemd-run --on-active` creates a `.timer` **and** a `.service`.
Stopping only the timer leaves the service loaded, and the next arm fails:
`Unit agent-cloud-netrevert.service was already loaded or has a fragment file.`

**Consequence.** The failing run is the SECOND one — which is the retry after a
revert, i.e. the exact path the mechanism exists to make survivable. First run
works, so nothing looks wrong until the moment it is needed. It fails closed (the
arm precedes the apply, so the abort leaves the network untouched), which is the
only reason this is a defect and not an outage.

**Why it happened.** Every existing test for the arm/disarm mechanism was a grep
over the playbook text. Greps confirm a command was *written*; they cannot
observe that systemd rejects it. The mechanism had never been executed anywhere.

**The rule.** Where a mechanism's correctness depends on how an external system
responds, run it against that system once — a throwaway container is enough.
Verified here by booting systemd in a container: second arm `rc=1`, and with
`stop` + `reset-failed` naming **both** units, `rc=0` three runs running.

**Enforced by.** `network config: arming the revert is re-runnable after a
previous arm`, proven against three mutations including a straight revert to the
original one-unit form.

### 10.5 Added a test suite to a config key nothing reads

**What happened.** A new pytest suite was added under
`platform/services/caddy/deployment/tests/`, and its directory was added to
`testpaths` in `pyproject.toml`. That was reported as wiring the tests into CI.
CI runs `pytest tests/ -v` with `working-directory:
platform/services/netbox/deployment` — an explicit path argument, which
overrides `testpaths` entirely. The pre-push hook did the same. So all sixteen
tests ran on my machine, in the one command I typed by hand, and nowhere else.

**Consequence.** The worst shape a test can take: a suite that exists, passes
when run deliberately, and is absent from every gate. A regression in the parser
would have reached `dev` with CI green, and the green would have been honest —
CI never saw the file.

**Root cause.** `testpaths` is a *default* for when pytest is invoked with no
path. Adding to it looks like registration but is inert wherever a path is
passed. I checked that the tests passed; I did not check that the thing which
runs tests in CI would select them.

**The rule.** Adding a test suite is not done when the tests pass. It is done
when the suite has been observed running **through the gate that will run it** —
the CI command, invoked the way CI invokes it. `pytest <path>` proves the tests
work; only reproducing CI's own invocation proves they are covered.

**Enforced by.** CI and `.githooks/pre-push` now run `pytest` from the repository
root with no path argument, so `testpaths` is authoritative and adding a suite is
one line in `pyproject.toml`. Verified by reproducing CI's invocation: 95
collected, up from 79.

### 10.6 Wrote a parser from one example file instead of from the grammar

**What happened.** The Caddyfile parser was written by reading the live
Caddyfile and matching its shape, then verified by checking that its output
matched a hand-written `awk` probe over that same file. Both agreed, and it was
called correct. Reading the published Caddyfile specification afterwards found
four deviations, every one of which the live file happened not to exercise:

- addresses may be separated by whitespace as well as commas — `a.io b.io {`
  parsed as the single address `"a.io b.io"`, which no lookup can match, so the
  multi-address safety refusal in `retire()` would not have fired either
- `#` starts a comment only at line start or after whitespace, so
  `reverse_proxy http://host/#frag` had its upstream truncated to
  `http://host/` — reporting an upstream the server does not use
- `(name) {` snippets and `&(name) {` named routes are not sites; both were
  reported as sites, and `retire` would have deleted a snippet every site imports
- heredoc contents are literal, so one unbalanced brace inside one desynced
  brace depth for the rest of the file and `parse_sites` returned **nothing** —
  the file read as having no routes rather than as unparseable

**Root cause.** Agreement between two readings of the *same* example is not
evidence about the language. The `awk` probe and the parser shared the
assumption they were both meant to test.

**The rule.** When parsing a format that has a specification, the specification
is the test oracle — not a sample, however real. A sample tells you the parser
handles that sample. Read the grammar and write one case per stated rule; the
rules the sample does not exercise are exactly where the parser will be wrong.

**Enforced by.** Six grammar-conformance cases in
`test_caddyfile_sites.py`, each quoting the documented rule it pins, each of
which failed before it was written. The single-line-block case is additionally
confirmed against the live `caddy` binary, which rejects it.

### 10.7 Named the rollback hazard, then closed only half of it

**What happened.** Adding a retire step to the Caddyfile playbook created a new
failure mode, and I identified it correctly in the commit message: blockinfile's
own backup is written *after* the retire, so restoring it would roll back the
managed block while leaving a hand-maintained route deleted. The fix was a
pre-edit backup taken before any change.

The restore that consumed it stayed an ordinary task, gated on
`when: _val.rc != 0` — the validation result. If `retire --write` succeeded and
`blockinfile` then failed, the play aborted at that task, and every task after
it, including the restore, was never reached. `_val` was never even registered.
So the exact sequence I had described was still unhandled: a route deleted, and
nothing to put it back.

**Root cause.** A conditional restore only runs if control reaches it. Gating on
"validation failed" silently assumes validation *ran*, which is false for every
failure earlier in the sequence — and the earlier steps are the ones doing the
destructive work.

**The rule.** Rollback belongs in a construct that cannot be skipped by the
failure it exists to handle — `block`/`rescue` in Ansible, `defer`/`finally`
elsewhere. A cleanup task guarded by `when:` on a later step's result is not a
rollback; it is a rollback for one of the several ways the thing can fail.

**Enforced by.** `test_manage_caddy_sites_playbook.py`, which parses the
playbook and asserts every mutating step sits inside the guarded block and that
the rescue restores the pre-edit copy. Proven against four mutations, including
moving the retire task back outside the block — the original bug.

**How it was found.** A security review pass, which flagged it as a non-security
correctness note while reporting no vulnerabilities. The finding that mattered
was the one outside the thing being looked for.

### 10.8 Two constructs that lint cleanly and mean something else when they run

**What happened.** A new playbook copies credentials from OpenBao into the private
repo and pushes a branch. It passed `ansible-lint` at the `production` profile and
read correctly. Run end-to-end against a mock store and a local bare remote, it
failed twice, for two unrelated reasons:

1. `ansible.builtin.command` with `cmd:` **word-splits**. `git config user.name
   Joseph A. Wisneski IV` arrived as four arguments and git answered `error: no
   action specified`. The quotes that look like they protect the value are consumed
   by YAML — `command` never invokes a shell, so nothing else honours them.

2. A `vars:` entry holding `lookup('pipe', 'date -u +%Y%m%dT%H%M%SZ')` is
   **re-evaluated at every reference**. `git switch -c {{ _branch }}` and
   `git push origin {{ _branch }}` named two different branches one second apart.
   The push failed with `src refspec ... does not match any` — *after* the
   credentials had been written and committed into the clone on the runner.

**Root cause.** Both are runtime semantics with no static representation.
`ansible-lint` checks shape, not argument arity, and cannot know whether a value
contains a space. Nothing at all expresses "this Jinja expression is evaluated
lazily, so two references may disagree" — the file reads as if `_branch` is a
value when it is a recipe.

The second is the dangerous one. It does not fail cleanly: it fails *after* the
credential material is on disk, in a clone, in a state the run then abandons. A
mechanism that only half-completes is worse than one that refuses.

**The rule.** A playbook that has only been linted has never run. Exercise it end
to end before it touches anything real — a mock store and a local bare repository
are enough, and both bugs surfaced on the first two attempts. Concretely:
`command` uses `argv:` whenever any argument can contain a space; and a value that
must be stable across references is computed with `set_fact`, never held in
`vars:`, because `vars:` is a template re-run on each use.

This is [2.17](#217-a-keyword-that-is-invalid-at-runtime-and-invisible-to-every-static-gate)
one layer out. That entry is about a construct inside a dynamically-included file
being invisible to static gates; this is about constructs in a plain playbook whose
*meaning* is invisible to them. Same lesson, wider: the gates this repo owns read
files, and a playbook is not a file, it is a program.

**Enforced by.** `cred backup: no non-deterministic lookup sits in a play's vars
block` and `cred backup: every git call carrying a space uses argv, not cmd` in
`platform/tests/test_backup_credentials.bats`. The first is a **closed** rule
across every playbook in the repo — `lookup('pipe'/'url'/'random_choice')`,
`now()` and the `random` filter are refused inside any play's `vars:` block, while
`lookup('file')` is deliberately allowed because re-reading a file gives the same
answer. Both mutation-proven by reintroducing the exact original bug.

### 10.9 Local validation ran GitHub's code, not the working tree

**Occurrences: 2** — 2026-08-30, 2026-08-31

**What happened.** Every `(Local)` Semaphore template was bound to the repository
record named `agent-cloud`, which points at GitHub `main`. The deploy DIR carried
the working tree (rsync'd by place-monorepo), so service files were current — but
the PLAYBOOKS executed were main's. Found 2026-08-30 when a newly added deploy
gate (`COMPOSE_OVERLAYS`) silently never fired across two deploy attempts: the
running playbook predated it. Every prior "validated locally" that depended on
playbook logic had actually exercised whatever main held at the time.

**Root cause.** Two declared owners of one record name. bootstrap-local-dev.yml
pointed `agent-cloud` at the read-only working-tree mount, then its own later
stage ran bootstrap-semaphore-repositories.yml, which converges that same name to
the GitHub URL repositories.yml declares. The later write always won, and nothing
compared what local templates ran against what local-dev exists to test.

**The rule.** A record two declarations both claim will be owned by whichever
applies last — give each purpose its own name. The working-tree record is
`agent-cloud worktree` (local path, HEAD; Semaphore runs a path-type repository
in place, uncommitted changes included), created by bootstrap-local-dev.yml and
claimed by no other declaration; templates-local.yml binds every local template
to it explicitly.

**Enforced by.** Bootstrap record + structural bind — bootstrap-local-dev.yml
creates/corrects the record, and setup-templates.yml binds the ENTIRE local
template list to it with one `map('combine')` at the load site, so a new entry
cannot omit the binding. It refuses to apply if the record is missing.

**Occurrence 2 — 2026-08-31.** The rule fixed the BINDING but not the
DISPATCHER: `scripts/local-dev.sh _run_template` selected a template by playbook
path alone and took the first match, and the shared GitHub-main-bound template
sorts before the `(Local)` one — so `local-dev.sh deploy n8n` ran main's
playbooks while the deploy DIR carried the branch (same split as occurrence 1,
one layer up). Caught because the branch's new env template visibly didn't
render. The dispatcher now prefers the `(Local)`-named template for a playbook
(scripts/local-dev.sh), which is the structural fix at the selection site.

### 10.10 A register on a skipped task overwrote the result it was guarding

**What happened.** provision-n8n-postiz-credential.yml tested a credential
(`register: _test`, status OK), then a conditional heal block whose re-test also
carried `register: _test`. The heal was correctly skipped — but in Ansible a
SKIPPED task still overwrites its `register` variable with the skip result
(`{'skipped': true}`), so `_test.json` vanished, the "Record the heal" task's
`when` (reading `_test`) flipped true, the report printed `is-connected:
unknown`, and the final assert failed two Semaphore runs (tasks 64/65) while
manual curl of the same endpoint answered `{"status":"OK"}` every time. The
`no_log` on the real task made the censored output unreadable, so the diagnosis
had to be reproduced outside the play.

**Root cause.** Reusing one register name across a primary task and its
conditional retry sibling. Register-on-skip is documented Ansible behavior, not
a bug — the play's logic assumed "skipped = untouched".

**The rule.** Never reuse a `register` name on a task that can be skipped when a
prior task's result is still live under that name. Register the conditional
task under its own name and fold both into ONE `set_fact` the downstream tasks
read (`_test_status` here) — facts survive skips; registers do not.

**Enforced by.** Convention — plus the play's own end state: the final assert
now reads the fact, so a recurrence fails loudly instead of misreporting.

### 10.11 The shared secret store-back deleted every key it did not declare

**What happened.** `tasks/manage-secrets.yml` stored its resolved secrets with a
KV-v2 POST of the whole `_resolved` dict — a full-document write. `_resolved` is
built only from `_secret_definitions`, so any key living beside the
deploy-managed ones was silently deleted on every deploy. Found when a routine
local `Deploy n8n` re-run erased `secret/services/n8n:n8n_api_key` (captured by
`Store n8n API Key` minutes earlier) and the credential-provisioning play failed
its named required-key assert. Latent for every service whose path accumulates
post-deploy keys; postiz's operator credentials survive only because
deploy-postiz happens to declare them all as `type: existing`.

**Root cause.** The store-back predates `tasks/bao-merge-keys.yml` (the shared
merge-patch extraction) and was never migrated onto it — the exact
read-modify-clobber shape that extraction exists to kill, sitting in the one
file every composable deploy includes.

**The rule.** A writer owns the KEYS it manages, never the whole path. Every
OpenBao write goes through `tasks/bao-merge-keys.yml` (server-side merge-patch,
CAS-guarded create); a whole-document POST to `secret/data/...` is a defect even
when it round-trips today's keys correctly.

**Enforced by.** Test — `platform/tests/test_manage_secrets.bats` refuses any
direct write method to `secret/data` in manage-secrets and requires the shared
merge include.

### 10.12 A numeric id crossed the Ansible→JSON boundary as a string, so an equality guard fired on everything

**What happened.** The sync engine gained a guard: if an issue carries a
`Priority` field whose numeric `field_id` differs from the one declared in the
mapping, refuse the cycle by name rather than write into an unrelated field.
Two callers hand the engine that id. The n8n workflow passes it through a Jinja
`dict(...)` expression, which preserves the integer. The verification playbook
assembles its payload as `"{{ ... | int }}"` — a quoted scalar — so Ansible
handed over the STRING `"22329653"`. GitHub returns `issue_field_id` as a
number, `22329653 !== "22329653"`, and the guard fired on every prioritised
issue. The gate failed a converged pair with `task '' / issue #15` while the
cycle reported zero ops on the same data.

It was caught in the same session, by the gate's own invariant — and only
because the gate had just been fixed to pass the id at all. Before that fix the
gate silently verified a DIFFERENT field set than the cycle, which is the
quieter half of the same defect.

**Root cause.** `| int` inside a quoted Ansible scalar does not survive
serialization: the filter runs, then the result is re-rendered as the string
body of that scalar. Type only survives when the value is produced inside a
Jinja expression that is consumed as a native object — which is why the sibling
call site, written as `dict(github_priority_field_id=(x | int))`, was correct
and looked identical at a glance. An `!==` comparison against externally-typed
data then silently means "always different".

**The rule.** A value that will be compared with `===`/`!==` is coerced at the
boundary that receives it, not at the boundaries that send it. The engine now
does `Number(input.priorityFieldId) || null` once, so every caller is safe
regardless of how its payload was assembled. More generally: when a guard's
failure mode is "fires on everything", assert the NEGATIVE case in a test —
that the guard stays silent on matching input — because a guard that always
fires passes any test that only checks it can fire.

**Enforced by.** Test — `core-scenarios.js` scenario 19(e2) passes the id as a
string and asserts zero recovery errors, and 19(f) still asserts a genuinely
wrong id refuses. Mutation-checked: removing the coercion turns 19(e2) red.

### 10.13 `tofu validate` and `plan` passed an attribute the API rejects on create

**What happened.** The first `http_ratelimit` ruleset copied `logging = { enabled = true }`
from the adopted `http_request_firewall_custom` skip rules. `tofu fmt`, `tofu validate`
against provider 5.x and a Semaphore `plan` (task 881: "1 to add") all passed. The `apply`
(task 882) failed inside `Creating...` with HTTP 400, code 20018, "it can only be used with
the skip action", pointer `/rules/0/logging/enabled`. Nothing was created; the phase
entrypoint was still absent afterwards, so state stayed clean.

**Root cause.** The provider schema declares `logging` on every rule because SOME actions
accept it; which actions do is an API-side rule the schema does not encode. `validate`
proves schema conformance and `plan` never calls the create endpoint for a new resource,
so neither could see it. The attribute was copied from rules of a different action without
checking its per-action validity.

**The rule.** For a resource type's FIRST use in a new phase or with a new action, the
apply IS the validation: expect and read the API error, and do not present a green `plan`
as proof the create will succeed. When copying attributes between rules of different
actions, check the API reference for that attribute's action restriction first.

**Enforced by.** Convention. The apply path already fails loudly and leaves no partial
state, so the cost of this class is one failed task, not damage.

### 10.14 A source-address allowlist was proven only where it could not fail

**What happened.** The inference route's `remote_ip` allowlist (Cloudflare's published
ranges) was rendered, `caddy validate`d, BATS-tested and exercised locally — where the
variable is `private_ranges` and the only peer is the podman gateway, so the matcher could
not fail. The risk that the production container sees a rewritten peer address was named
in the PR and the task list as a pre-check, but no sanctioned way to perform the check
existed (no Semaphore task reads the container's network mode; workstation SSH is not a
platform path), so the deploy went ahead as the test. `Manage Caddy Sites` (task 888)
succeeded and the route answered 404 to everyone through Cloudflare until the revert
(site-config #14) landed.

**Root cause.** A control whose correctness depends on a runtime observable (the peer
address the process sees) was validated only against configuration and against an
environment where the observable had a different value. "Fails closed" was accepted as a
safe property; it is safe for the origin and an outage for the users.

**The rule.** Before deploying a control keyed on a runtime observable, make that
observable readable through the sanctioned executor and read it FOR THE TRAFFIC THE
CONTROL WILL JUDGE. The network mode and an arbitrary access-log line do not show which
peer Caddy sees for a Cloudflare-proxied request to the route in question; a read-only
Semaphore task must send a known request through Cloudflare to that hostname, record its
host and timestamp, and print the matching access-log entry's peer address — that value,
not a proxy for it, is the precondition. If the read cannot be built in time, the deploy
is a scheduled test with an announced outage window, not a landing.

**Enforced by.** Convention. The mechanical form is the read-only task itself, added as a
precondition to the deploy playbook when the route carries a source-address matcher.

### 10.15 Reboot survival was asserted for podman containers and never exercised

**What happened.** The production OpenBao host rebooted on 2026-09-19. Its container came
back `Created`, not running, and stayed that way until an operator-authorized SSH session
started it on 2026-09-22. Every Semaphore deploy that reads OpenBao was blocked for those
three days. Two defects combined:

1. The boot unit on the host runs `podman start --all --filter restart-policy=always`
   (read from `systemctl cat podman-restart.service`, podman 4.9.3). OpenBao's compose file
   declared `restart: unless-stopped`, so the unit skipped it. Twelve compose files and the
   orb-agent `run` flag used that policy, and four NetBox services (the app, Postgres and
   both Redis instances) declared no policy at all, which Compose treats as never restart.
2. For rootless services, the linger task's header said lingering "in turn restarts the
   (rootless) containers". It does not. Linger starts the user's systemd instance, and the
   user copy of `podman-restart.service` ships `disabled` on the Ubuntu 24.04 image (read
   with `systemctl --user is-enabled` on the OpenBao host). Nothing in that instance
   starts a container. Only five of the fourteen composable deploys included the task at all.

After starting, OpenBao was also sealed, because production has no auto-unseal. That gap
was already recorded (plan/development/01, problem 2 and phase B2); this outage is its first
measured cost.

**Root cause.** A reboot-survival property was written down from how Docker behaves and
never tested with a reboot. Podman's current upstream documentation says `unless-stopped`
restarts at boot, which is true of newer releases and false of the unit installed here, so
reading the docs instead of the host would not have caught it either.

**The rule.** A claim that a service survives a reboot is verified on the target host's
own boot unit, not on documentation: read the unit's `ExecStart` filter, and read whether
the unit is enabled for the account that owns the containers. Declare `restart: always`
(or `"no"` for one-shot containers) in every compose file, and enable podman's user unit
wherever rootless containers run.

**Enforced by.** Test. `platform/tests/test_restart_policy.bats` parses every compose file
and refuses any service whose effective policy (base file plus overlays, missing key
included) is not `always` or `"no"`, refuses any `--restart unless-stopped`, requires the shared deploy preamble to
include the linger task, and requires that task to link the user unit into
`default.target.wants`. Both guards were mutated once and went red. An actual reboot
test of a service host is still not exercised by any automation.

### 10.16 The agentgateway deploy was proven only on an executor where its bug could not show

**What happened.** `deploy-agentgateway.yml` built `_client_defs` as TEXT, a
`[{% for c in agw_clients %}{"name": ...}{% endfor %}]` template, and then declared
`_secret_definitions: [...] + _client_defs`. The deploy was proven repeatedly on the local
controller (`semaphoreui/semaphore:v2.18.12-ansible2.16.5`, ansible-core 2.16.18). That version
turns template text that looks like a list into a list, so the proof passed. On 2026-09-23, just
before the first production run, a test harness on the workstation (ansible-core 2.21.0) failed
with `can only concatenate list (not "_AnsibleTaggedStr") to list`. The production controller
image `semaphoreui/semaphore:v2.19.11` ships ansible-core 2.20.8 and fails the same way. So
`Deploy agentgateway (Dev)` would have failed at secret resolution on its first production run.

**Root cause.** Two things combined. The expression depended on a coercion that ansible-core
2.19's data tagging removed. And the only executor it was ever run on predated the removal. As in
10.14, the proof ran where the defect could not appear. The varying factor here was the
executor's version, not a runtime observable, so 10.14's rule did not cover it.

**The rule.** Widens 10.14: when a proof runs on an executor that differs from production's
(controller image, ansible-core, engine), either run it on production's version too, or name the
difference as an unproven precondition. For Ansible, build lists and dicts as native values:
filters, or a statement block whose only output is the value itself. Never rely on template
text being coerced, and never on JSON text assembled with escapes.

**Enforced by.** Test: `platform/tests/test_agentgateway_secret_defs.py` evaluates the play's real
`_client_defs` and `_secret_definitions` with `ansible-playbook`. CI installs the current
ansible-core, so the old expression fails there (2 failures; the fix passes). Convention for the
general case.

### 10.17 A guard that could never pass in production, tested only where it is switched off

**What happened.** `deploy-agentgateway.yml` refuses to deploy when the upstream key is empty,
unless `agw_upstream_requires_key: false`. It read `secrets.vllm_api_key`. `manage-secrets.yml`
defines `secrets` only as a task-level variable on its template task; at play level the name
does not exist, so the expression always resolved to empty and the guard failed every production
deploy (task 1177, 2026-09-23), with the key present in OpenBao. Local-dev sets
`agw_upstream_requires_key: false` for LM Studio, so every local proof skipped the guard.

**Root cause.** The guard referenced a variable by the name the templates use, without checking
that the name existed in the play's scope, and the only environments it ran in had it disabled.

**The rule.** A guard is proven in a configuration where it is ENABLED and its input is present
(it must pass) and absent (it must fail). The play-level fact manage-secrets sets is `_resolved`.

**Enforced by.** The fix and its regression test land with the deploy session's keyed-verify
change to the same playbook; until that PR merges, `Convention`.

## 11. The largest one

### 11.1 Seventy-six assertions that could not fail

**What happened.** An audit of this test suite found **76 assertions that can never
fail.** Bats runs a test body under `set -e`, but bash's `set -e` is documented to
ignore the status of two constructs, and both are the natural way to write an
assertion:

```bash
! some_command        # a bang-inverted pipeline
[[ "$a" == "$b" ]]    # the [[ ]] keyword
```

Measured on Bats 1.13.0: either one, **anywhere except the final statement of a
test**, leaves the test passing when it should fail. Only as the last line does
the body's exit status carry it.

**Why this is the largest entry in this document.** The negative form is the
common shape of a *security* assertion — "must NOT contain the loopback address",
"must NOT source the config file", "no playbook keeps its own copy of the guard".
Every one of those was decoration. It also undercuts the mutation testing this
document repeatedly cites: a mutation that "was killed" may have been killed by a
neighbouring positive assertion rather than the one under test.

**How it was found.** Not by reading. A test was failing against code believed
correct, so a deliberately false assertion was inserted to check the test was
running at all — and the test still passed. That single probe is what exposed the
class.

**Two things it was hiding**, both surfaced the moment the assertions became real:
an "app config does not go through compose interpolation" check that matched a
*comment*, and the same for a dev-only setting. Neither was a defect in the
subject; both were assertions written against a token rather than a construct.

**The rule.** An assertion must be a simple command whose non-zero status fails
the test — `[ ]`, a plain `grep`, or a function call. Never a bare `!` pipeline or
`[[ ]]` except as the final line. When a test fails against code you believe is
correct, insert a deliberately false assertion and confirm the test *can* fail
before debugging the subject.

**Enforced by.** `platform/tests/test_assertions_are_real.bats` — a ratchet. The
count may go down freely and may not go up. `platform/tests/assert_helpers.bash`
provides `refute_grep` / `assert_contains` / `refute_contains`, which work because
a function call is a simple command. 20 assertions were converted in the file
where the class was found; the ratchet holds the rest at a visible number rather
than pretending the debt is gone.

### 11.2 Sourcing a config file instead of reading it

**What happened.** Loading the app's config was implemented as `. /config/app.env`
inside the container. The template renders every credential slot unquoted, so
`.` subjected each value to full shell parsing. Demonstrated: `x$(id -u)y` **executed**,
`ab$HOME/cd` expanded, and `my user` word-split to empty.

Two consequences. It is command execution in the container for anyone able to write
the secret path — which is a *credential-seeding* privilege, not an execution one.
And it silently reintroduced the exact `${...}` corruption the whole configuration
design existed to prevent, arriving at the same failure from the opposite
direction.

**Why it was not obvious.** The change was made to fix a real bug (nothing was
loading the file at all), it worked, and the service came up healthy. A fix that
resolves the visible symptom is the easiest place to stop looking.

**The rule.** Never `source` a file whose contents are data. Read it line by line
and `export "$line"`, which assigns without re-expanding. Reserve `.` for files
that are code and are yours.

**Enforced by.** A test asserting the read form and rejecting the sourcing form —
and it had to be scoped to the command value, because a first version was
satisfied by the explanatory comment beside it.

### 11.3 Committed without running the suite, a third time

**What happened.** A documentation commit added a line quoting a forbidden address
in order to prohibit it. A test scanned the whole directory for that address and
so failed — in CI, after the push. The suite was not run between the edit and the
commit.

§5.2 records this. §5.5 records it recurring. This is the third, and the failure
was introduced by a commit whose entire purpose was to document earlier failures.

**The rule (unchanged).** Run the suite before committing. The gap is that nothing
enforces it: the pre-commit hooks cover secrets, addresses, credentials and
whitespace, not the tests.

**Enforced by.** Nothing yet. A pre-push hook remains the proposal — the suite
takes ~40 seconds, too slow per commit and well matched to the moment code leaves
the machine. Still the repository owner's call, and still the clearest
convention-to-gate conversion available here.


---

## 12. Host state invisible in an agent run, blamed on credentials

Two entries, one root cause: Paperclip runs agents under a sandboxed `$HOME`, so
host config and host credentials can be invisible inside a run. Each tool reports
that invisibility in its own idiom — an invalid token, no available models, a
provider that simply is not in the list — and none of those wordings says
*missing visibility*, which is why the class gets chased as a credentials or
config problem instead.
The dangerous part of the class is that the suggested fix ("re-authenticate",
"reconfigure the provider") acts on the wrong object and can overwrite the thing
that was already correct.

### 12.1 `gh` reported a valid token as invalid

**What happened.** Inside an agent run, `gh` found the account entry in
`hosts.yml`, found no token, and reported *"The token in default is invalid."*
The token is fine — it was never the token. The run simply cannot see it, and
the suggested remedy (`gh auth login`) would have overwritten a valid
credential.

**Root cause.** macOS resolves the *login* keychain from `$HOME`. Under a
sandboxed `$HOME`, only `/Library/Keychains/System.keychain` remains in the
search list, and `gh`'s token lives in the login keychain under service
`gh:github.com`. Measured 2026-09-24: the same `security find-generic-password
-s 'gh:github.com' -w` lookup succeeds with the host `$HOME` and fails (exit
44) with a sandboxed one; with the host `hosts.yml` visible but the login
keychain unreachable, `gh auth status` prints "The token in default is invalid"
verbatim.

**The fix.** Source the company's `gh-host-auth.sh` before any `gh`/`git` work
in a run. It reads the secret from the login keychain by explicit path, unwraps
go-keyring's `go-keyring-base64:` envelope, and exports `GH_TOKEN`. The
non-obvious step is clearing the inherited `osxkeychain` credential helper (it
comes from the system gitconfig, `/opt/homebrew/etc/gitconfig`): left in place,
`git clone` authenticates successfully and *then* tries to cache the credential
into a keychain it cannot reach. The script's header records that failure as
`fatal: failed to store: -60008`. One honest caveat from re-verification the
same day: with current git (2.55.0) the store attempt did not abort the clone at
all, so the exact error text is version-dependent — the helper is cleared
because it cannot reach its target, regardless of whether that currently aborts.
The script derives the keychain path from `$HOME` unless
`PAPERCLIP_GITHUB_HOST_HOME` is set, so under a fully clean sandboxed
environment that variable must carry the host home.

**The rule.** When a credential tool inside a sandboxed run reports a credential
*invalid*, first ask whether the credential is *visible* — check what
`$HOME`/keychain search list/config dir the tool is actually resolving against —
before touching the credential itself.

**Enforced by.** Convention — the helper script exists and must be sourced; no
gate enforces it.

### 12.2 Pi showed no models; OpenCode omitted its provider

**What happened.** The same sandboxed `$HOME`, one layer up. Pi printed *"No
models available. Use /login…"* even though the host's `~/.pi/agent/models.json`
is correct. OpenCode silently dropped the entire custom provider — its model
list carried no entry for a provider its host config declares — no error, just
absence, because its config resolves from `$XDG_CONFIG_HOME` and its credentials
from `$XDG_DATA_HOME`, both of which the sandbox can redirect.

**Why OpenCode's shape is the worse one.** A loud "no models" prompts an
investigation; a silently missing provider reads as "never configured" and
invites reconfiguring something that was already right — which is how correct
config gets overwritten.

**The fix.** Pi: point `PI_CODING_AGENT_DIR` back at the host's
`<host-home>/.pi/agent`. OpenCode: point `XDG_CONFIG_HOME` and `XDG_DATA_HOME`
back at the host's `<host-home>/.config` and `<host-home>/.local/share`.
(`<host-home>` is written as a placeholder deliberately — this repo is public
and its own pre-push audit treats machine paths as username leaks: `AGENTS.md`,
Mandatory Pre-Push Audit.) Both verified
2026-09-24: `pi --list-models` under a sandboxed `$HOME` prints exactly "No
models available" and works once the env var is set; an empty redirected
`XDG_CONFIG_HOME` makes `opencode models` list no entry for the host-declared
provider, while pointing it at the host `.config` lists it again.

**Caveat, recorded as found.** Inside a Paperclip `opencode_local` run the
symptom no longer reproduces with the run's own defaults, because the harness
now seeds the host OpenCode config into the run-scoped `XDG_CONFIG_HOME`. The
trap is still live for any redirect that does not carry the host config, so the
entry documents the mechanism, not just the historic symptom.

**The rule.** "No models available" and "provider not found" are visibility
failures wearing authentication's clothes. Before re-logging-in or
re-configuring, resolve where the tool actually looks — `$HOME`,
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, tool-specific dirs — and confirm those paths
contain the host config.

**Enforced by.** Convention — the env corrections are known and applied by
convention inside runs; nothing mechanical catches a run that forgets them.
