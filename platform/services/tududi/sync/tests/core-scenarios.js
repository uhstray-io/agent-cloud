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

// 2) suspect duplicate blocks creation
r = core.computeOps({ ...cfg, tasks: [{ uid: 'u2', title: 'Fix login', note: '', status: 0, tags: ['gh-sync'], updated_at: '2026-09-01T10:00:00Z', tagged: true }], issues: [{ number: 7, title: 'Fix login', body: 'no marker here', state: 'open', labels: [], updated_at: '2026-09-01T09:00:00Z', user_login: 'uhstray-sync', comments: [] }] });
assert.strictEqual(r.ops.length, 0);
assert.strictEqual(r.recoveryErrors.length, 1);

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
