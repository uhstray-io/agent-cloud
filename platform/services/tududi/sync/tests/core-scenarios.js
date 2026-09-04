const path = require('path');
const CORE = path.join(__dirname, '..', 'lib', 'sync-core.js');
const core = require(CORE);
const assert = require('assert');
const pair = { tududi_project: 'huhhb', github_repo: 'uhstray-io/huhhb' };
const cfg = { pair, syncTag: 'gh-sync', syncLogin: 'uhstray-sync', writeCap: 10 };

// 1) tagged task, no issue -> create with marker
let r = core.computeOps({ ...cfg, tasks: [{ uid: 'u1', title: 'Fix login', note: 'body', status: 0, tags: ['gh-sync','auth'], updated_at: '2026-09-01T10:00:00Z', tagged: true }], issues: [] });
assert.strictEqual(r.ops.length, 1);
assert.strictEqual(r.ops[0].type, 'create_issue');
assert.ok(r.ops[0].body.includes('uid: u1'));
assert.deepStrictEqual(r.ops[0].labels, ['auth']); // sync tag never propagates

// 2) same-title unlinked open issue is ADOPTED, never re-created (interrupted
//    creation from either side; a human filing it twice). Empty baselines
//    make the differing body a LWW conflict with the loser preserved; the
//    marker rides the issue update; the link is announced with a keyed comment.
r = core.computeOps({ ...cfg, tasks: [{ uid: 'u2', title: 'Fix login', note: '', status: 0, tags: ['gh-sync'], updated_at: '2026-09-01T10:00:00Z', tagged: true }], issues: [{ number: 7, title: 'Fix login', body: 'no marker here', state: 'open', labels: [], updated_at: '2026-09-01T09:00:00Z', user_login: 'uhstray-sync', comments: [] }] });
assert.ok(!r.ops.some(o => o.type === 'create_issue' || o.type === 'create_task'), 'adoption creates nothing');
assert.strictEqual(r.recoveryErrors.length, 0);
assert.strictEqual(r.stats.adopted, 1);
{
  const link = r.ops.find(o => o.type === 'update_issue' && o.number === 7);
  assert.ok(link && core.parseMarker(link.patch.body).uid === 'u2', 'marker with the task uid lands on the adopted issue');
  assert.ok(r.ops.some(o => o.type === 'comment_issue' && o.body.includes('tududi-sync-event: u2/linked/')), 'keyed linked comment');
  // body differs: the task (newer) wins the conflict; the issue's text is preserved in a comment
  assert.ok(r.ops.some(o => o.type === 'comment_issue' && o.body.includes('conflict_description') && o.body.includes('no marker here')));
  assert.strictEqual(core.stripMarker(link.patch.body).trim(), '');
}
// adoption when every field already agrees still writes the marker (else it re-adopts forever)
r = core.computeOps({ ...cfg, tasks: [{ uid: 'u2', title: 'Fix login', note: 'same', status: 0, tags: ['gh-sync'], updated_at: '1', tagged: true }], issues: [{ number: 7, title: 'Fix login', body: 'same', state: 'open', labels: [], updated_at: '1', user_login: 'human', comments: [] }] });
assert.ok(r.ops.some(o => o.type === 'update_issue' && core.parseMarker(o.patch.body).uid === 'u2'));
assert.ok(!r.ops.some(o => o.type === 'comment_issue' && o.body.includes('conflict_')), 'agreeing fields raise no conflict');

// 3) quiet cycle -> no ops
const proj = core.projections({ title: 'T', description: 'D', statusMapped: 'open', labelNames: ['x'] }, 'gh-sync');
const marker = { uid: 'u3', baselines: core.baselinesOf(proj), tududi_updated_at: '1', github_updated_at: '1' };
const body = core.withMarker('D', marker);
r = core.computeOps({ ...cfg, tasks: [{ uid: 'u3', title: 'T', note: 'D', status: 0, tags: ['gh-sync','x'], updated_at: '1', tagged: true }], issues: [{ number: 1, title: 'T', body, state: 'open', labels: ['x'], updated_at: '1', user_login: 'a', comments: [] }] });
assert.strictEqual(r.ops.length, 0);
assert.strictEqual(r.stats.skipped_quiet, 1);

// 4) different fields both sides -> clean merge, both patched, no conflict comment
r = core.computeOps({ ...cfg, tasks: [{ uid: 'u3', title: 'T new', note: 'D', status: 0, tags: ['gh-sync','x'], updated_at: '2', tagged: true }], issues: [{ number: 1, title: 'T', body: core.withMarker('D changed on gh', marker), state: 'open', labels: ['x'], updated_at: '2', user_login: 'a', comments: [] }] });
const types = r.ops.map(o => o.type).sort();
assert.deepStrictEqual(types, ['update_issue', 'update_task']);
assert.ok(!r.ops.some(o => o.type === 'comment_issue'));
const up = r.ops.find(o => o.type === 'update_issue');
assert.strictEqual(up.patch.title, 'T new');
const tp = r.ops.find(o => o.type === 'update_task');
assert.strictEqual(tp.patch.note, 'D changed on gh');

// 5) same-field conflict -> newer wins, losing value in keyed comment; retry suppressed
const conflictIssue = { number: 1, title: 'T gh', body, state: 'open', labels: ['x'], updated_at: '2026-09-01T12:00:00Z', user_login: 'a', comments: [] };
r = core.computeOps({ ...cfg, tasks: [{ uid: 'u3', title: 'T tududi', note: 'D', status: 0, tags: ['gh-sync','x'], updated_at: '2026-09-01T13:00:00Z', tagged: true }], issues: [conflictIssue] });
const cmt = r.ops.find(o => o.type === 'comment_issue');
assert.ok(cmt && cmt.body.includes('T gh') && cmt.body.includes('tududi-sync-event: u3/conflict_title/'));
assert.strictEqual(r.ops.find(o => o.type === 'update_issue').patch.title, 'T tududi');
// retry with the comment already present -> no duplicate comment
const withCmt = { ...conflictIssue, comments: [{ body: cmt.body }] };
r = core.computeOps({ ...cfg, tasks: [{ uid: 'u3', title: 'T tududi', note: 'D', status: 0, tags: ['gh-sync','x'], updated_at: '2026-09-01T13:00:00Z', tagged: true }], issues: [withCmt] });
assert.ok(!r.ops.some(o => o.type === 'comment_issue'));

// 6) untag -> keyed audit comment + close not_planned; no task op
r = core.computeOps({ ...cfg, tasks: [{ uid: 'u3', title: 'T', note: 'D', status: 0, tags: ['x'], updated_at: '9', tagged: false }], issues: [{ number: 1, title: 'T', body, state: 'open', labels: ['x'], updated_at: '1', user_login: 'a', comments: [] }] });
assert.deepStrictEqual(r.ops.map(o => o.type), ['comment_issue', 'update_issue']);
assert.strictEqual(r.ops[1].patch.state_reason, 'not_planned');
// the close moves the status baseline with it — the sync owns this closed state
assert.strictEqual(core.parseMarker(r.ops[1].patch.body).baselines.status, core.hash('closed:not_planned'));
assert.strictEqual(core.stripMarker(r.ops[1].patch.body).trim(), 'D', 'the body text itself is untouched');

// 7) write cap exceeded -> throws, whole cycle refused
let threw = false;
try {
  core.computeOps({ ...cfg, writeCap: 0, tasks: [{ uid: 'u9', title: 'X', note: '', status: 0, tags: ['gh-sync'], updated_at: '1', tagged: true }], issues: [] });
} catch (e) { threw = /write cap exceeded/.test(e.message); }
assert.ok(threw);

// 8) no delete op type exists anywhere in the module source
const src = require('fs').readFileSync(CORE, 'utf8');
assert.ok(!/delete_issue|delete_task/.test(src));

console.log('ALL CORE SCENARIOS PASS');

// 9) label added on GitHub -> update_task carries tududi-shaped tag OBJECTS,
//    with the sync control tag re-attached (never stripped off the task).
{
  const proj = core.projections({ title: 'T', description: 'D', statusMapped: 'open', labelNames: ['backend'] }, 'gh-sync');
  const marker = { uid: 'uL', baselines: core.baselinesOf(proj), tududi_updated_at: '1', github_updated_at: '1' };
  const body = core.withMarker('D', marker);
  const r = core.computeOps({ pair, syncTag: 'gh-sync', syncLogin: 'a', writeCap: 10,
    tasks: [{ uid: 'uL', title: 'T', note: 'D', status: 0, tags: ['backend','gh-sync'], updated_at: '1', tagged: true }],
    issues: [{ number: 1, title: 'T', body, state: 'open', labels: ['backend','urgent'], updated_at: '2', user_login: 'human', comments: [] }] });
  const tp = r.ops.find(o => o.type === 'update_task');
  assert.ok(tp, 'expected an update_task op');
  assert.ok(Array.isArray(tp.patch.tags) && tp.patch.tags.every(t => typeof t === 'object' && 'name' in t), 'tags must be {name} objects');
  const names = tp.patch.tags.map(t => t.name).sort();
  assert.deepStrictEqual(names, ['backend','gh-sync','urgent'], 'urgent added, sync tag preserved');
  console.log('SCENARIO 9 (tududi-shaped tag back-prop) PASS');
}

// 10) Who closed it decides. The same observable state — task open+tagged,
//     issue closed:not_planned — has two histories the baseline tells apart:
//   a) the SYNC closed it on un-tag (baseline moved to closed:not_planned) and
//      the task was re-tagged -> a tududi-side change: reopen, task untouched
//      (the live bounce 0 -> 5 -> 1 on dev-test #2 came from a baseline that
//      had NOT moved);
//   b) a HUMAN closed it on GitHub (baseline still open) -> a GitHub-side
//      change: the task is CANCELLED and the close is never reverted (live
//      cycle 118 on dev-test #2 reopened a human's close — this pins the fix).
{
  const proj = core.projections({ title: 'T', description: 'D', statusMapped: 'open', labelNames: ['backend'] }, 'gh-sync');
  const run = (statusBase) => core.computeOps({ pair, syncTag: 'gh-sync', syncLogin: 'a', writeCap: 10,
    tasks: [{ uid: 'uR', title: 'T', note: 'D', status: 0, tags: ['backend','gh-sync'], updated_at: '3', tagged: true }],
    issues: [{ number: 1, title: 'T',
      body: core.withMarker('D', { uid: 'uR', baselines: { ...core.baselinesOf(proj), status: statusBase }, tududi_updated_at: '1', github_updated_at: '1' }),
      state: 'closed', state_reason: 'not_planned', labels: ['backend'], updated_at: '2', user_login: 'a', comments: [] }] });

  let r = run(core.hash('closed:not_planned'));
  assert.ok(r.ops.find(o => o.type === 'update_issue' && o.patch.state === 'open'), 'expected the reopen op');
  assert.strictEqual(r.ops.find(o => o.type === 'update_task' && 'status' in o.patch), undefined, 'reopen must not rewrite the tududi status');

  r = run(core.hash('open'));
  assert.ok(!r.ops.some(o => o.type === 'update_issue' && o.patch.state === 'open'), 'a human close is never reverted');
  const tp = r.ops.find(o => o.type === 'update_task');
  assert.ok(tp && tp.patch.status === core.TUDUDI_STATUS.CANCELLED, 'the task follows the human close');
  console.log('SCENARIO 10 (baseline tells a retag reopen from a human close) PASS');
}

// 11) GitHub-origin: an open, human-authored, unlinked issue becomes a tagged
//     task in the paired project; link_body carries the uid placeholder the
//     executor fills in. Closed unlinked issues are history — never backfilled.
//     A sync-authored orphan is reported, never turned into a task.
{
  const r = core.computeOps({ ...cfg, tasks: [],
    issues: [
      { number: 11, title: ' Filed on GitHub ', body: 'from gh', state: 'open', labels: ['Bug'], updated_at: '5', user_login: 'human', comments: [] },
      { number: 12, title: 'old closed', body: '', state: 'closed', state_reason: 'completed', labels: [], updated_at: '1', user_login: 'human', comments: [] },
      { number: 13, title: 'orphan', body: 'bot made me', state: 'open', labels: [], updated_at: '1', user_login: 'uhstray-sync', comments: [] },
    ] });
  const ct = r.ops.filter(o => o.type === 'create_task');
  assert.strictEqual(ct.length, 1, 'exactly one task, for the open human issue');
  assert.strictEqual(ct[0].number, 11);
  assert.strictEqual(ct[0].project, 'huhhb');
  assert.strictEqual(ct[0].task.name, 'Filed on GitHub');
  assert.strictEqual(ct[0].task.note, 'from gh');
  assert.strictEqual(ct[0].task.status, core.TUDUDI_STATUS.NOT_STARTED);
  assert.deepStrictEqual(ct[0].task.tags.map(t => t.name).sort(), ['bug', 'gh-sync'], 'labels become tags, sync tag attached');
  assert.ok(ct[0].link_body.includes(`uid: ${core.LINK_UID_PLACEHOLDER}`));
  assert.ok(!ct[0].link_body.includes('__UID__: '), 'placeholder only in the uid line');
  assert.strictEqual(core.stripMarker(ct[0].link_body).trim(), 'from gh', 'issue text preserved above the marker');
  assert.ok(!r.ops.some(o => o.number === 12 || o.number === 13));
  assert.strictEqual(r.recoveryErrors.length, 1);
  assert.strictEqual(r.recoveryErrors[0].issue_number, 13);
  // the filled-in marker parses back to the created task's identity
  const filled = ct[0].link_body.split(core.LINK_UID_PLACEHOLDER).join('newuid');
  assert.strictEqual(core.parseMarker(filled).uid, 'newuid');
  console.log('SCENARIO 11 (GitHub-origin creation) PASS');
}

// 12) Interrupted GitHub-origin creation (task made, marker PATCH failed):
//     next cycle the tagged task and the open issue share a title -> adopted,
//     and NEITHER a second task nor a second issue is created.
{
  const r = core.computeOps({ ...cfg,
    tasks: [{ uid: 'u12', title: 'Filed on GitHub', note: 'from gh', status: 0, tags: ['gh-sync', 'bug'], updated_at: '6', tagged: true }],
    issues: [{ number: 11, title: 'Filed on GitHub', body: 'from gh', state: 'open', labels: ['bug'], updated_at: '5', user_login: 'human', comments: [] }] });
  assert.ok(!r.ops.some(o => o.type === 'create_task' || o.type === 'create_issue'), 'no duplicate in either direction');
  assert.ok(r.ops.some(o => o.type === 'update_issue' && core.parseMarker(o.patch.body).uid === 'u12'));
  assert.strictEqual(r.recoveryErrors.length, 0);
  console.log('SCENARIO 12 (interrupted GitHub-origin creation recovers by adoption) PASS');
}

// 13) An UNTAGGED task carrying the issue's title blocks GitHub-origin
//     creation with a recovery error: the owner kept it private, and a second
//     task would duplicate it. Nothing is written.
{
  const r = core.computeOps({ ...cfg,
    tasks: [{ uid: 'u13', title: 'Private twin', note: '', status: 0, tags: [], updated_at: '1', tagged: false }],
    issues: [{ number: 14, title: 'Private twin', body: '', state: 'open', labels: [], updated_at: '1', user_login: 'human', comments: [] }] });
  assert.strictEqual(r.ops.length, 0);
  assert.strictEqual(r.recoveryErrors.length, 1);
  assert.strictEqual(r.recoveryErrors[0].task_uid, 'u13');
  console.log('SCENARIO 13 (untagged twin blocks GitHub-origin creation) PASS');
}

// 14) Two unlinked open issues with the tagged task's title: adoption would be
//     a guess -> recovery error, no creation, no link. And neither issue
//     becomes a task while the ambiguity stands.
{
  const r = core.computeOps({ ...cfg,
    tasks: [{ uid: 'u14', title: 'Twins', note: '', status: 0, tags: ['gh-sync'], updated_at: '1', tagged: true }],
    issues: [
      { number: 15, title: 'Twins', body: '', state: 'open', labels: [], updated_at: '1', user_login: 'human', comments: [] },
      { number: 16, title: 'Twins', body: '', state: 'open', labels: [], updated_at: '1', user_login: 'human', comments: [] },
    ] });
  assert.ok(!r.ops.some(o => o.type === 'create_issue' || o.type === 'update_issue'));
  assert.ok(!r.ops.some(o => o.type === 'create_task'), 'the twin issues must not become extra tasks (was: 1 task + 2 issues -> 3 tasks)');
  assert.strictEqual(r.recoveryErrors.length, 1, 'one ambiguity, reported once');
  assert.ok(r.recoveryErrors.some(e => e.task_uid === 'u14'));
  console.log('SCENARIO 14 (ambiguous adoption refuses to guess) PASS');
}

// 15) DANGLING link: an open issue's marker names a task this project does not
//     hold (deleted, moved, or written by another tududi instance). It is
//     neither an orphan (no duplicate task is created) nor quiet: a recovery
//     error names it. A CLOSED dangling issue is finished business — silent.
{
  const mk = (uid) => core.withMarker('text', { uid, baselines: {}, tududi_updated_at: '1', github_updated_at: '1' });
  const r = core.computeOps({ ...cfg, tasks: [],
    issues: [
      { number: 17, title: 'Dangling', body: mk('gone'), state: 'open', labels: [], updated_at: '1', user_login: 'human', comments: [] },
      { number: 18, title: 'Closed dangling', body: mk('gone-too'), state: 'closed', labels: [], updated_at: '1', user_login: 'human', comments: [] },
    ] });
  assert.strictEqual(r.ops.length, 0, 'a dangling link creates nothing');
  assert.strictEqual(r.recoveryErrors.length, 1);
  assert.strictEqual(r.recoveryErrors[0].issue_number, 17);
  assert.strictEqual(r.recoveryErrors[0].task_uid, 'gone');
  assert.ok(r.recoveryErrors[0].reason.startsWith('dangling link'));
  console.log('SCENARIO 15 (dangling link is reported, never silently skipped) PASS');
}

// 16) The SAME marker uid on two issues (a copy-pasted body carries the hidden
//     marker). Found by scenario 15's first draft: a Map keyed by uid kept only
//     the last issue, the first fell out of the linked set, looked unlinked and
//     was re-created as a task. Now: both stay linked (no orphan, no create),
//     the uid is ambiguous (its tagged task gets no ops, not even a new
//     issue), and one recovery error names both issue numbers.
{
  const body = core.withMarker('text', { uid: 'u16', baselines: {}, tududi_updated_at: '1', github_updated_at: '1' });
  const r = core.computeOps({ ...cfg,
    tasks: [{ uid: 'u16', title: 'Copied', note: 'text', status: 0, tags: ['gh-sync'], updated_at: '2', tagged: true }],
    issues: [
      { number: 19, title: 'Copied', body, state: 'open', labels: [], updated_at: '1', user_login: 'human', comments: [] },
      { number: 20, title: 'Copied (copy)', body, state: 'open', labels: [], updated_at: '1', user_login: 'human', comments: [] },
    ] });
  assert.strictEqual(r.ops.length, 0, 'an ambiguous link writes nothing');
  assert.strictEqual(r.recoveryErrors.length, 1);
  assert.strictEqual(r.recoveryErrors[0].task_uid, 'u16');
  assert.strictEqual(r.recoveryErrors[0].issue_number, '19,20');
  console.log('SCENARIO 16 (same marker on two issues: no duplicate task, no duplicate issue) PASS');
}

// 17) finished work is not exported (mirror of "closed issues are not
//     imported"): a tagged DONE/CANCELLED/ARCHIVED task with no link creates
//     nothing — a create can only OPEN an issue, and that open issue would
//     read as a GitHub reopen next cycle. A LINKED finished task still closes
//     its issue with the matching reason, and a human reopening a completed
//     issue pulls the task back to IN_PROGRESS.
{
  const run = (status, issues) => core.computeOps({ pair, syncTag: 'gh-sync', syncLogin: 'a', writeCap: 10,
    tasks: [{ uid: 'u17', title: 'Ship it', note: 'D', status, tags: ['gh-sync'], updated_at: '2', tagged: true }], issues });
  for (const s of [core.TUDUDI_STATUS.DONE, core.TUDUDI_STATUS.CANCELLED, core.TUDUDI_STATUS.ARCHIVED]) {
    const r = run(s, []);
    assert.strictEqual(r.ops.length, 0, `finished status ${s} creates nothing`);
    assert.strictEqual(r.recoveryErrors.length, 0);
    assert.strictEqual(r.stats.skipped_finished, 1);
  }
  const proj = core.projections({ title: 'Ship it', description: 'D', statusMapped: 'open', labelNames: [] }, 'gh-sync');
  const openBody = core.withMarker('D', { uid: 'u17', baselines: core.baselinesOf(proj), tududi_updated_at: '1', github_updated_at: '1' });
  // linked + DONE in tududi -> issue closed as completed
  let r = run(core.TUDUDI_STATUS.DONE, [{ number: 3, title: 'Ship it', body: openBody, state: 'open', labels: [], updated_at: '1', user_login: 'a', comments: [] }]);
  const close = r.ops.find(o => o.type === 'update_issue');
  assert.ok(close && close.patch.state === 'closed' && close.patch.state_reason === 'completed', 'DONE closes the issue as completed');
  assert.ok(!r.ops.some(o => o.type === 'update_task'));
  // linked + human reopened the completed issue -> task back to IN_PROGRESS
  const doneProj = core.projections({ title: 'Ship it', description: 'D', statusMapped: 'closed:completed', labelNames: [] }, 'gh-sync');
  const doneBody = core.withMarker('D', { uid: 'u17', baselines: core.baselinesOf(doneProj), tududi_updated_at: '1', github_updated_at: '1' });
  r = run(core.TUDUDI_STATUS.DONE, [{ number: 3, title: 'Ship it', body: doneBody, state: 'open', state_reason: 'reopened', labels: [], updated_at: '3', user_login: 'a', comments: [] }]);
  const tp = r.ops.find(o => o.type === 'update_task');
  assert.ok(tp && tp.patch.status === core.TUDUDI_STATUS.IN_PROGRESS, 'reopen pulls the task to IN_PROGRESS');
  console.log('SCENARIO 17 (finished work is not exported; linked finished work converges) PASS');
}

// 18) HIERARCHY (design D8). One linked parent pair; children on both sides.
//     Fixtures: parent task uP (id 100) <-> issue #8 (id 800), quiet.
{
  const quiet = (uid, title) => {
    const p = core.projections({ title, description: 'D', statusMapped: 'open', labelNames: [] }, 'gh-sync');
    return core.withMarker('D', { uid, baselines: core.baselinesOf(p), tududi_updated_at: '1', github_updated_at: '1' });
  };
  const parentTask = { uid: 'uP', id: 100, title: 'Parent', note: 'D', status: 0, tags: ['gh-sync'], updated_at: '1', tagged: true, parent_uid: null };
  const parentIssue = { number: 8, id: 800, title: 'Parent', body: quiet('uP', 'Parent'), state: 'open', labels: [], updated_at: '1', user_login: 'a', comments: [], parent_number: null };
  const run = (tasks, issues) => core.computeOps({ ...cfg, tasks: [parentTask, ...tasks], issues: [parentIssue, ...issues] });

  // a) tagged subtask, parent linked -> create_issue (hop 1); nothing else
  let r = run([{ uid: 'uC', id: 101, title: 'Child', note: '', status: 0, tags: ['gh-sync'], updated_at: '2', tagged: true, parent_uid: 'uP' }], []);
  assert.deepStrictEqual(r.ops.map(o => o.type), ['create_issue']);
  assert.strictEqual(r.ops[0].task_uid, 'uC');

  // b) next cycle: the child issue exists top-level (no parent yet) -> add_sub_issue (hop 2), fields quiet
  const childIssue = { number: 12, id: 1200, title: 'Child', body: quiet('uC', 'Child'), state: 'open', labels: [], updated_at: '1', user_login: 'uhstray-sync', comments: [], parent_number: null };
  r = run([{ uid: 'uC', id: 101, title: 'Child', note: 'D', status: 0, tags: ['gh-sync'], updated_at: '1', tagged: true, parent_uid: 'uP' }], [childIssue]);
  assert.deepStrictEqual(r.ops, [{ type: 'add_sub_issue', repo: pair.github_repo, parent_number: 8, number: 12, sub_issue_id: 1200 }]);
  assert.strictEqual(r.stats.attached, 1);

  // c) attached and quiet -> zero ops (the same rule that re-attaches a human detach is silent once attached)
  r = run([{ uid: 'uC', id: 101, title: 'Child', note: 'D', status: 0, tags: ['gh-sync'], updated_at: '1', tagged: true, parent_uid: 'uP' }], [{ ...childIssue, parent_number: 8 }]);
  assert.strictEqual(r.ops.length, 0);
  assert.strictEqual(r.recoveryErrors.length, 0);

  // d) UNTAGGED subtask under a linked parent -> nothing, whatever the parent carries
  r = run([{ uid: 'uU', id: 102, title: 'Private child', note: '', status: 0, tags: [], updated_at: '2', tagged: false, parent_uid: 'uP' }], []);
  assert.strictEqual(r.ops.length, 0);

  // e) tagged subtask whose parent is NOT linked -> deferred, counted, no issue
  r = core.computeOps({ ...cfg,
    tasks: [{ uid: 'uQ', id: 200, title: 'Unlinked parent', note: '', status: 0, tags: [], updated_at: '1', tagged: false, parent_uid: null },
            { uid: 'uW', id: 201, title: 'Waiting child', note: '', status: 0, tags: ['gh-sync'], updated_at: '1', tagged: true, parent_uid: 'uQ' }],
    issues: [] });
  assert.strictEqual(r.ops.length, 0);
  assert.strictEqual(r.stats.skipped_parent_unlinked, 1);

  // f) GitHub-origin sub-issue under the linked parent -> create_task with parent_task_id = parent's integer id
  r = run([], [{ number: 13, id: 1300, title: 'Filed child', body: 'from gh', state: 'open', labels: [], updated_at: '3', user_login: 'human', comments: [], parent_number: 8 }]);
  const ct = r.ops.filter(o => o.type === 'create_task');
  assert.strictEqual(ct.length, 1);
  assert.strictEqual(ct[0].task.parent_task_id, 100);
  assert.deepStrictEqual(ct[0].task.tags.map(t => t.name), ['gh-sync']);

  // g) GitHub-origin sub-issue whose parent issue is NOT linked -> deferred, never imported top-level
  r = run([], [
    { number: 30, id: 3000, title: 'Foreign parent', body: 'plain', state: 'open', labels: [], updated_at: '1', user_login: 'human', comments: [], parent_number: null },
    { number: 31, id: 3100, title: 'Foreign child', body: '', state: 'open', labels: [], updated_at: '1', user_login: 'human', comments: [], parent_number: 30 },
  ]);
  assert.deepStrictEqual(r.ops.filter(o => o.type === 'create_task').map(o => o.number), [30], 'the parent imports; the child waits for it');
  assert.strictEqual(r.stats.skipped_parent_unlinked, 1);

  // h) adoption is scoped to the parent: a same-title top-level orphan is NOT the child's twin
  r = run([{ uid: 'uC2', id: 103, title: 'Same name', note: '', status: 0, tags: ['gh-sync'], updated_at: '2', tagged: true, parent_uid: 'uP' }],
          [{ number: 40, id: 4000, title: 'Same name', body: '', state: 'open', labels: [], updated_at: '1', user_login: 'human', comments: [], parent_number: null }]);
  assert.ok(r.ops.some(o => o.type === 'create_issue' && o.task_uid === 'uC2'), 'child is created, not adopted onto the top-level twin');
  assert.strictEqual(r.stats.adopted, 0);
  // ...and a same-title sub-issue of the SAME parent IS adopted
  r = run([{ uid: 'uC2', id: 103, title: 'Same name', note: '', status: 0, tags: ['gh-sync'], updated_at: '2', tagged: true, parent_uid: 'uP' }],
          [{ number: 41, id: 4100, title: 'Same name', body: '', state: 'open', labels: [], updated_at: '1', user_login: 'human', comments: [], parent_number: 8 }]);
  assert.strictEqual(r.stats.adopted, 1);
  assert.ok(!r.ops.some(o => o.type === 'create_issue' || o.type === 'create_task'));

  // i) hierarchy drift: linked child issue under a DIFFERENT parent than its task -> recovery error, zero ops
  r = run([{ uid: 'uC', id: 101, title: 'Child', note: 'D', status: 0, tags: ['gh-sync'], updated_at: '1', tagged: true, parent_uid: 'uP' }], [{ ...childIssue, parent_number: 99 }]);
  assert.strictEqual(r.ops.length, 0);
  assert.strictEqual(r.recoveryErrors.length, 1);
  assert.ok(r.recoveryErrors[0].reason.startsWith('hierarchy drift'), r.recoveryErrors[0].reason);
  // ...and a sub-issue linked to a TOP-LEVEL task is drift too (the live probe #12 <-> f39ipt4616rycn4 shape)
  r = run([{ uid: 'uC', id: 101, title: 'Child', note: 'D', status: 0, tags: ['gh-sync'], updated_at: '1', tagged: true, parent_uid: null }], [{ ...childIssue, parent_number: 8 }]);
  assert.strictEqual(r.ops.length, 0);
  assert.ok(r.recoveryErrors[0].reason.includes('is top-level in tududi'));

  // j) no delete at any level; add_sub_issue is the only new op type
  const src = require('fs').readFileSync(CORE, 'utf8');
  assert.ok(!/remove_sub_issue|delete_sub_issue/.test(src));
  console.log('SCENARIO 18 (hierarchy: create both ways, attach, wait, scope, drift) PASS');
}
