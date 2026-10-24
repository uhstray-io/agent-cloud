# PBS and observability production checkpoint

**Date:** 2026-10-24

**Status:** ACTIVE

**Context:** Sanitized source-integration and bounded production evidence for
the existing PBS change. This checkpoint supersedes earlier pending-CI and
capacity-ambiguity statements for the observations below; it does not accept
private PBS placement, role capabilities, backup, restore or retention growth.

## Source integration and composition

[PR #734](https://github.com/uhstray-io/agent-cloud/pull/734) merged reviewed head
`b7e5cacfc33c6f2ddf9fd4767c159cfe7cb8f06c` after independent reviews and fixes,
15 resolved review threads, and final-head
[CI run 40085387294](https://github.com/uhstray-io/agent-cloud/actions/runs/40085387294)
(12 successful checks, three path-inapplicable Go skips).

Actual Dev merge `b0e7beeb9fcb6fa1e076b935aac39e86c9986f75` has first parent
`07f2dc848ee5b43da616c777490ccee62772c3ca` and second parent the reviewed B7
head. PR #732 merged while B7 CI was running; the PR base snapshot `e68ca3d`
was not the actual integration tip. Always read the live Dev ref and final
merge parents/tree, rather than using a PR's creation/base snapshot as current
branch authority.

The intervening [PR #732](https://github.com/uhstray-io/agent-cloud/pull/732)
head `14748551ecf5ff214970a317e64b93811a62aa75` had exact-head CodeRabbit
approval, four resolved threads, and
[CI run 40084679138](https://github.com/uhstray-io/agent-cloud/actions/runs/40084679138)
with 12 successes and three Go skips. Its 14 Skynet/local-dev paths have zero
overlap with #734's 64 paths. All 14 file blobs in the actual Dev composition
match the peer merge; every other tracked path, including the fixed production
operation payloads and dependencies, is byte-identical to B7. The AGENTS delta
adds documentation rather than changing operating rules. Production template
publication excludes local templates unless explicitly enabled.

558 static guards passed on actual Dev. This is bounded composition and static
evidence, **not full combined-tree CI**. The CI workflow is PR-only, without
push or workflow-dispatch triggers; rerunning an old PR run is not evidence of
the actual Dev tree unless its checkout tree matches. This composition proof
qualified only the planned scoped publication/policy/capability protocol, not
arbitrary deployments or a claim that all combined behavior was exercised.

## Publication, authorization and dependency gates

| Observation | Accepted scope and remaining boundary |
|---|---|
| Task 8706, controller Dev `b0e7bee`, succeeded | Created only `Verify o11y Restore Coordination Capabilities (Dev)`, template 352, repository 5 (Dev), inventory 2, environment 2, playbook `verify-o11y-restore-coordination-capabilities.yml`, sole survey `expected_repository_sha`, no schedules. This is catalog publication only. |
| Policy template 351 | Previously published by task 8650. No policy-apply task was launched. Managed automatic approval review rejected execution because explicit user authorization was required for production `semaphore-read` grants: data `create/read/update` on `secret/data/coordination/o11y-restore-target`, metadata `read` on its fixed metadata path. User approval is pending; do not bypass through another path. Stored policy equality and effective grants remain unverified, not known wrong or missing. |
| Check-mode capability task 8710, controller Dev `b0e7bee`, terminal error | Failed during dependency setup before Ansible plays: 15 output rows, zero TASK headers. Ansible Galaxy returned HTTP 500 downloading `community.hashi_vault` 7.1.0. No policy or capability readback was established. |

The failing public artifact was
[`community-hashi_vault-7.1.0.tar.gz`](https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/artifacts/community-hashi_vault-7.1.0.tar.gz).
A separate direct HTTPS health GET, following HTTPS redirects, also returned
500 at this checkpoint. This is a dependency-service outage, not
`policy_mismatch` or evidence of a source-verifier defect. Do not retry while
upstream remains unhealthy, unpin the collection or skip the dependency gate.

The public OpenBao Compose source uses `openbao/openbao:latest`, so it does not
identify the live server version. Upstream v2.7.1 source preserves `policy.Raw`
through storage/readback and adds the legacy capability alias only for a
single-path request; the exact policy and two-path-map comparisons remain
appropriate. [Policy read/write and capabilities handlers](https://github.com/openbao/openbao/blob/v2.7.1/internal/vault/logical_system.go),
[policy storage](https://github.com/openbao/openbao/blob/v2.7.1/internal/vault/policy/policy_store.go).
This source inspection and synthetic publisher/readback tests are not live
protocol or effective-role proof.

## Gateway proof boundary

Read-only check-mode template 307 task 8705 ran with controller Dev `b0e7bee`
and expected gateway revision `b0e7bee`; it refused the revision mismatch. The
observed checkout was `a7f348067bdd3cec935f8ae64ceeb1e49492c7c3`, but no
runtime/configuration receipt was accepted. That revision is the Dev merge of
PR #714, with reviewed source head
`731fb17c5223ba454e1a925da909325facf79f83`; its completed independent bot
review, resolved finding and green final-head CI qualify under the one-review
policy. A separate final-head approval is not implied or required by that
standing policy.

Task 8712 repeated the read-only check with controller `b0e7bee` and expected
gateway checkout `a7f3480`. It errored at `Require the current files and running
container input label to match`, with 12 TASK headers in the bounded output. That comparison remains
unproven; this evidence does not identify its exact cause or establish logging
enablement, sampling, delivery, image health or telemetry absence. No gateway
deployment or runtime fix occurred.

Task-output readers must strip ANSI framing before parsing JSON, treat a
callback host prefix as framing rather than receipt identity, and validate
the fixed task and closed receipt keys. Printed partial JSON is not acceptance.

The independent source follow-up, [PR #736](https://github.com/uhstray-io/agent-cloud/pull/736),
is open at `12f04fc10cd15df073d6929b0fbdb4a79e1d796c`. It adds fixed safe
comparison reasons and a bounded command-output adapter while preserving legacy
successful-comparison acceptance. Owner verification passed 71 Python and 20
BATS cases, lint and strict OpenSpec validation. A fresh read-only GPT-6.1 Sol
Medium review completed with approval and no findings; its Ansible templar
check was blocked by temporary-directory access, distinct from the checks it
completed. See the [independent review](https://github.com/uhstray-io/agent-cloud/pull/736#issuecomment-6383999255).
[CI run 40092120662](https://github.com/uhstray-io/agent-cloud/actions/runs/40092120662)
cannot yet support merge: four Python jobs stopped before tests when the exact
`community.hashi_vault` 7.1.0 artifact returned HTTP 500, and Static Analysis hit
a collection-download 500 during Ansible lint. Other jobs, including Bash,
were still potentially running at this checkpoint; the run is not recorded
as terminal. Do not retry CI while the artifact remains unhealthy; do not skip
or unpin dependencies, work around the failure, or merge on this evidence.
This source work is independent of the
denied policy execution and private PR #110 push approval. It establishes no
cause for task 8712, production rollout, role/sampling acceptance, protected
copy, restore or capacity proof; all existing gates and checkboxes remain open.

## Receiver, capacity and backup boundaries

The exact receiver comparison from `67b9cffcdda8b3b10e840c50357c1b23f8410cbb`
to B7 adds no production dashboard, image, Compose or journal-collector delta.
The added Alloy relabeling is local-mode only; host instrumentation identity
uses its separate workflow. Controller diagnostics are supplied from Dev.
No receiver redeploy is needed for this change; do not deploy merely to
generate another capacity sample.

Capacity task 8658 supersedes the earlier `source_series_ambiguous` blocker
for its observed refresh: 2,305 evaluation points, 16 canonical source events,
and a 604,701-second event span, 99 seconds short of seven days. Its result is
`source_history_too_short`. Source ambiguity is cleared for that observation;
the separate forecast, headroom and backup gates remain open. Do not rerun or
manufacture samples merely to obtain a passing gate.

Backup task 8634 proved the uniquely selected job enabled and including o11y,
but no successful fresh backup within 26 hours. A separate task's `no_space`
hint was not attributable to that selected job. Neither fresh artifact nor
successful PBS verification was established.

## Remaining prerequisites and future work

Private site-config PR #110 remains open at remote revision `50672e3`; prepared
IP correction `e0b1ae7` awaits specific push approval. No private PBS VM,
destination, restore target, protected copy, restore or cleanup is accepted.
The [primary-unavailable plan](primary-unavailable-restore-plan.md) remains
PROPOSED and unimplemented; ordinary schema-2/v3 receipts do not prove an
outage. All parent live-acceptance checkboxes and new outage subtasks stay
unchecked. VM resize and 90d/45d/45d retention growth remain blocked.

Future foundation work may evaluate a pinned, prebuilt Semaphore execution
environment with reviewed collections/minimum-version contracts to reduce
per-job Galaxy availability dependence. Pinning the OpenBao image requires
read-only version inventory and separately planned maintenance first. Neither
is an action authorized by this checkpoint; do not restart core services for
this task or substitute an unreviewed dependency workaround.

## Cross-references

- [Capability acceptance](../../../../O11Y-RESTORE-CAPABILITY-ACCEPTANCE.md)
- [Bootstrap publication runbook](dev-bootstrap-publication-runbook.md)
- [Parent tasks](../tasks.md) and [design](../design.md)
- [Observability evidence](../../../../05-observability.md)
