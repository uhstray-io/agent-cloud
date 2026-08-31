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
| 1.2 | Asserted a config gap that did not exist, without reading the file | Unverified claim | Convention |
| 1.3 | Reported a background job as successful when its exit code had been masked by a pipe | Unverified claim | Convention |
| 1.4 | Guessed a resource id instead of reading the one the create call returned | Unverified claim | Convention |
| 1.5 | Claimed per-job containerisation as an enforced control; a job that asked for nothing ran on the host | Unverified claim | Test |
| 1.6 | Called a host addressless from one ARP sweep; it was up and answering, the sweep lost the race | Unverified claim | Convention |
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
| 2.18 | A coverage test asserting "every play" over a hand-typed list of four — 40 of 52 were unguarded | Vacuous coverage | Test (derived population + ratchet) |
| 2.19 | The app healthcheck probed the path nginx serves from the FRONTEND — green across a backend that never bound | False green | Test (probe path pinned) |
| 3.1 | Wrote a probe value over a real credential in a live secret store | Live-state damage | **OPA (proposed)** |
| 3.2 | Attempted to mutate a shared orchestrator credential without asking | Live-state damage | Sandbox + **OPA (proposed)** |
| 4.1 | `while read` silently dropped an unterminated final line | Data handling | Convention |
| 4.2 | Stored `.env` values without stripping surrounding quotes | Data handling | Convention |
| 4.3 | Used a real internal IP address as a test vector | Data leak | Pre-commit (existing) |
| 4.4 | Arithmetic on a fleet API response without defaulting fields absent on offline members | Data handling | Convention |
| 4.5 | Truncated a live inventory by opening it for writing in the expression that computed its content | Live-state damage | Convention |
| 4.6 | A failure-path diagnostic printed the very values the success path was built to keep out of stdout | Secret in transcript | Convention |
| 5.1 | Security check duplicated per caller; a fix reached three copies and missed two | Duplication | Test |
| 5.2 | Committed while a test was failing, because the check did not gate the commit | Process | Pre-push hook |
| 5.3 | Merged a PR while its review was rate-limited | Process | Convention (user-stated) |
| 5.4 | A command's own planning boundary honoured over an explicit instruction to implement | Process | Convention + stop hook |
| 5.5 | Repeated 5.2 — committed with a failing test; hooks do not gate the suite | Process | Pre-push hook |
| 5.6 | Repeated 5.2 twice more — committed with a failing suite; hooks did not gate it | Process | Pre-push hook |
| 6.1 | Built an edit from an assumed file structure instead of a read one | Process | Convention |
| 6.2 | Built an interface the consumer never calls, without reading how it invokes | Process | Test |
| 6.3 | Repeated 6.2 — assumed openssl and jq exist on the orchestrator image; neither does | Process | Convention -> **Test + declared dep** |
| 8.1 | Repeated 1.3 — masked an exit code with a pipe, minutes after writing the rule against it | Unverified claim | Convention |
| 8.2 | Referenced tests by identifiers that did not exist | Unverified claim | Test |
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
| 10.9 | Local validation templates were bound to GitHub main, so every "validated locally" run executed code that was not the code being written | Wrong code under validation | Bootstrap record + declared binding |
| 9.1 | A `for` loop with an unconditional `break`, making all but one member unreachable | Minor | Convention |
| 9.2 | Typo'd duplicate key in a hand-assembled payload; call succeeded regardless | Minor | Convention |

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

### 1.3 A masked exit code reported as success

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

---

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

---

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

**Enforced by.** Bootstrap record + declared binding — bootstrap-local-dev.yml
creates/corrects the record, and every entry in templates-local.yml names
`repository: agent-cloud worktree`, which setup-templates.yml refuses to apply if
the record is missing.

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
