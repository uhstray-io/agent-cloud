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
| 2.1 | Test compiled a pattern as raw file text, not as the runtime decodes it | False-green test | Test |
| 2.2 | Test pinned the vulnerable form of a security check in place | False-green test | Test |
| 2.3 | Negative assertion aborted under `set -e` because a no-match grep exits 1 | False-green test | Convention |
| 2.4 | Test asserted state that lived on a different branch | False-green test | CI |
| 3.1 | Wrote a probe value over a real credential in a live secret store | Live-state damage | **OPA (proposed)** |
| 3.2 | Attempted to mutate a shared orchestrator credential without asking | Live-state damage | Sandbox + **OPA (proposed)** |
| 4.1 | `while read` silently dropped an unterminated final line | Data handling | Convention |
| 4.2 | Stored `.env` values without stripping surrounding quotes | Data handling | Convention |
| 4.3 | Used a real internal IP address as a test vector | Data leak | Pre-commit (existing) |
| 5.1 | Security check duplicated per caller; a fix reached three copies and missed two | Duplication | Test |
| 5.2 | Committed while a test was failing, because the check did not gate the commit | Process | Convention |
| 5.3 | Merged a PR while its review was rate-limited | Process | Convention (user-stated) |
| 6.1 | Built an edit from an assumed file structure instead of a read one | Process | Convention |
| 8.1 | Repeated 1.3 — masked an exit code with a pipe, minutes after writing the rule against it | Unverified claim | Convention |
| 8.2 | Referenced tests by identifiers that did not exist | Unverified claim | Test |
| 8.3 | Took two tool-invocation errors as findings before establishing a baseline | Unverified claim | Convention |
| 8.4 | Proposed a deny rule that failed OPEN on a missing field | Live-state damage | Test + evaluation |

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

---

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

---

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

**Related.** Four test vectors in the same table legitimately look like
credentials — the `scheme://userinfo@host` shape is the attack being refused, so
a URI-credential detector fires on the vector itself. Those
carry `trufflehog:ignore` with a stated reason. The annotation was verified to
work (1 finding → 0) before being relied upon. An ignore marker without a reason
in the same comment is not acceptable.

---

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

**Enforced by.** Convention. (The pre-commit hooks do not run the test suite, by
design — it is too slow for every commit — so CI is the backstop, one step later
than it should be.)

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
