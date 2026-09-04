// sync-core.js — the pure logic of the tududi↔GitHub issue sync
// (integrate-tududi-github-issue-sync, design D4/D5/D6; contract:
// platform/services/tududi/context/github-sync-contract.md).
//
// PURE: no I/O, no clock reads, no engine APIs. The n8n workflow templates
// embed this file verbatim into their Code nodes at render time (Jinja
// include), and hand it the two systems' payloads; it returns the operations
// to execute. Keeping it a standalone file makes the sync's brain reviewable
// as code and testable outside the engine (BATS drives it through `node`).
//
// Invariants owned here (spec-level):
//   - no delete operation is ever emitted, for anything
//   - a write is only emitted for a field whose current canonical hash
//     differs from its marker baseline (echo suppression, D5)
//   - conflicts resolve per FIELD by the two systems' own updated_at values
//     (never a wall clock), losing value preserved via audit comment (D5)
//   - creation is gated by the uid search AND the canonical-title second
//     gate: ONE unlinked open issue with the task's title is ADOPTED (linked,
//     never re-created — this is how an interrupted creation from either
//     side recovers); more than one is a recovery error, never a guess (D4)
//   - an open, human-authored, unlinked issue in a paired repo creates a
//     tagged task in the paired project (GitHub-origin creation); closed
//     unlinked issues are history and are never backfilled
//   - audit comments carry a stable audit-event key and are suppressed when
//     the key already appears in the issue's comments (D6)
//   - the per-cycle write cap fails the cycle loudly when exceeded (risk 3)
//   - hierarchy is one level, native on both sides (D8): a subtask crosses
//     only when it is tagged ITSELF and its parent is a linked pair; a child
//     issue observed without a parent is re-attached from the tududi truth;
//     a child whose two parents disagree is a recovery error, never a guess

'use strict';

// ── Status mapping (contract table; tududi Task.STATUS ints at v1.1.1) ──────
const TUDUDI_STATUS = {
  NOT_STARTED: 0,
  IN_PROGRESS: 1,
  DONE: 2,
  ARCHIVED: 3,
  WAITING: 4,
  CANCELLED: 5,
  PLANNED: 6,
};

// tududi status int -> the GitHub-facing mapped value both sides hash.
function mapTududiStatus(statusInt) {
  switch (statusInt) {
    case TUDUDI_STATUS.DONE:
      return 'closed:completed';
    case TUDUDI_STATUS.CANCELLED:
    case TUDUDI_STATUS.ARCHIVED:
      return 'closed:not_planned';
    default:
      return 'open';
  }
}

// GitHub issue -> the same mapped value space.
function mapIssueState(issue) {
  if (issue.state === 'closed') {
    return issue.state_reason === 'not_planned' ? 'closed:not_planned' : 'closed:completed';
  }
  return 'open';
}

// Reverse: the tududi status a GitHub transition implies (contract table).
// Returns null when the issue state does not force a tududi change.
function issueStateToTududiStatus(mapped, currentTududiStatus) {
  if (mapped === 'closed:completed') return TUDUDI_STATUS.DONE;
  if (mapped === 'closed:not_planned') return TUDUDI_STATUS.CANCELLED;
  // open: only forces a change when the task was terminal (reopen).
  if (
    currentTududiStatus === TUDUDI_STATUS.DONE ||
    currentTududiStatus === TUDUDI_STATUS.CANCELLED
  ) {
    return TUDUDI_STATUS.IN_PROGRESS;
  }
  return null;
}

// ── Canonical projections (contract: what gets hashed) ──────────────────────

// FNV-1a 32-bit — deterministic, dependency-free; collision resistance is not
// a security property here (baselines only gate echo suppression, and a
// collision degrades to a skipped write, never a wrong write).
function hash(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16).padStart(8, '0');
}

function projectTitle(s) {
  return (s || '').trim();
}

// description: the task note / issue body minus the marker block.
function projectDescription(s) {
  return stripMarker(s || '').trim();
}

// tags/labels: case-folded, sorted, sync tag excluded, comma-joined.
function projectLabels(names, syncTag) {
  const fold = (x) => x.toLowerCase();
  return (names || [])
    .map(fold)
    .filter((n) => n !== fold(syncTag || ''))
    .sort()
    .join(',');
}

// priority: GitHub's native single-select carries four options, tududi three
// (LOW/MEDIUM/HIGH, or null for none). Urgent folds onto high — the operator's
// rule — and folding at the PROJECTION is what makes the lossy pair safe: a
// tududi 'high' can never demote a GitHub 'Urgent', because after projection
// the two sides hold the same value and the field is quiet. Lowering the task
// to medium/low does differ, and does reach GitHub.
const PRIORITY_FOLD = { urgent: 'high', high: 'high', medium: 'medium', low: 'low' };
// tududi's Task.PRIORITY (models/task.js): LOW 0, MEDIUM 1, HIGH 2; null = none.
const TUDUDI_PRIORITY_NAME = ['low', 'medium', 'high'];

function projectPriority(name) {
  return PRIORITY_FOLD[String(name == null ? '' : name).toLowerCase()] || '';
}

function tududiPriorityName(p) {
  return p == null ? '' : TUDUDI_PRIORITY_NAME[p] || '';
}

const BASE_FIELDS = ['title', 'description', 'status', 'labels'];

// Priority is synced only when the org's Priority field id is declared: the
// WRITE requires the numeric field_id, and the App installation token cannot
// enumerate field definitions to discover it (403 on /orgs/{org}/issue-fields,
// measured). Without the id the field is not synced at all, rather than read
// in one direction and silently dropped in the other.
const ALL_FIELDS = BASE_FIELDS.concat('priority');

function fieldsFor(priorityFieldId) {
  return priorityFieldId ? BASE_FIELDS.concat('priority') : BASE_FIELDS;
}

// Build the canonical field projections for one side.
// side: {title, description, statusMapped, labelNames, priorityName}
function projections(side, syncTag) {
  return {
    title: projectTitle(side.title),
    description: projectDescription(side.description),
    status: side.statusMapped,
    labels: projectLabels(side.labelNames, syncTag),
    priority: projectPriority(side.priorityName),
  };
}

function baselinesOf(proj, fields) {
  const out = {};
  for (const f of fields || BASE_FIELDS) out[f] = hash(proj[f]);
  return out;
}

// ── Marker block (D4) ───────────────────────────────────────────────────────

const MARKER_START = '<!-- tududi-sync';
const MARKER_END = 'tududi-sync-end -->';

function renderMarker(state) {
  // state: {uid, baselines:{field:hash}, tududi_updated_at, github_updated_at}
  const lines = [
    MARKER_START,
    `uid: ${state.uid}`,
    ...Object.keys(state.baselines).map((f) => `base_${f}: ${state.baselines[f]}`),
    `tududi_updated_at: ${state.tududi_updated_at}`,
    `github_updated_at: ${state.github_updated_at}`,
    MARKER_END,
  ];
  return lines.join('\n');
}

function parseMarker(body) {
  const text = body || '';
  const start = text.indexOf(MARKER_START);
  const end = text.indexOf(MARKER_END);
  if (start === -1 || end === -1 || end < start) return null;
  const block = text.slice(start, end);
  const get = (key) => {
    const m = block.match(new RegExp(`^${key}: (.*)$`, 'm'));
    return m ? m[1].trim() : null;
  };
  const uid = get('uid');
  if (!uid) return null; // damaged beyond the uid line -> caller logs, never guesses
  const baselines = {};
  // ALL fields, so a marker written before priority sync was enabled parses
  // with base_priority null — the field then reads as changed on both sides
  // and converges by updated_at on the first cycle, instead of being invisible.
  for (const f of ALL_FIELDS) baselines[f] = get(`base_${f}`);
  return {
    uid,
    baselines,
    tududi_updated_at: get('tududi_updated_at'),
    github_updated_at: get('github_updated_at'),
  };
}

function stripMarker(body) {
  const start = body.indexOf(MARKER_START);
  if (start === -1) return body;
  const end = body.indexOf(MARKER_END);
  if (end === -1) return body.slice(0, start);
  return body.slice(0, start) + body.slice(end + MARKER_END.length);
}

function withMarker(body, state) {
  return `${stripMarker(body || '').trimEnd()}\n\n${renderMarker(state)}`;
}

// ── Audit-event keys (D6) ───────────────────────────────────────────────────

function auditKey(uid, eventType, triggerUpdatedAt) {
  return `tududi-sync-event: ${uid}/${eventType}/${triggerUpdatedAt}`;
}

function commentHasKey(comments, key) {
  return (comments || []).some((c) => (c.body || '').includes(key));
}

// ── The cycle engine ────────────────────────────────────────────────────────
//
// computeOps(input) -> { ops, recoveryErrors, stats }
//
// input:
//   pair: {tududi_project, github_repo}
//   syncTag: control tag name
//   syncLogin: the GitHub sync identity's login (echo/authorship filter, D5)
//   writeCap: max writes this pair may emit per cycle
//   tasks: tududi tasks of the project — top-level AND their subtasks,
//     flattened — each
//     {uid, id(int), title, note, status(int), priority: 0|1|2|null,
//      tags:[names], updated_at, tagged:bool, archived:bool,
//      parent_uid: uid | null}
//   issues: GitHub issues of the repo — the flat list, which already carries
//     the hierarchy — each
//     {number, id(int), title, body, state, state_reason, labels:[names],
//      updated_at, user_login, comments:[{body}], parent_number: n | null,
//      field_values: [{field_id(int), field_name, option_name}]}
//   priorityFieldId: the org's native Priority field id, or null to leave
//     priority out of the sync entirely (see fieldsFor)
//   issuesTruncated: true when the issue read hit its page window — the pair
//     is refused, because a partial set cannot answer "does this already
//     exist?" and the creation gates would duplicate
//   issuePageSize: the window, for the refusal message (default 100)
//
// ops emitted (executor maps them to HTTP): create_issue, update_issue,
// comment_issue, update_task, create_task, add_sub_issue. NO delete op exists.
//
// create_task carries `link_body`: the issue body with a marker whose uid is
// the literal LINK_UID_PLACEHOLDER — the executor substitutes the uid the
// tududi create returns and PATCHes it onto the issue. Until that lands the
// pair is unlinked on both sides, and the next cycle's adoption gate links
// them by title instead of creating again.

const LINK_UID_PLACEHOLDER = '__UID__';

function computeOps(input) {
  const { pair, syncTag, syncLogin, writeCap, tasks, issues } = input;
  // Coerced, not trusted: the field id is compared with === against the ids
  // GitHub returns as NUMBERS, and a caller that assembles this payload
  // through a templating layer hands over the STRING "22329653" — which
  // matches nothing and makes the mismatch guard fire on every issue that
  // carries a priority. The gate did exactly that.
  const priorityFieldId = input.priorityFieldId ? Number(input.priorityFieldId) || null : null;
  const FIELDS = fieldsFor(priorityFieldId);
  // GitHub's issue_field_values PATCH is a REPLACE, not a merge: a
  // priority-only write wipes every other field value on the issue (measured
  // — it silently cleared an 'Effort' value). So a priority write echoes the
  // issue's other field values back verbatim, and clearing priority means
  // omitting only its entry.
  const priorityOf = (issue) =>
    ((issue.field_values || []).find((v) => v.field_id === priorityFieldId) || {}).option_name || '';
  const issueFieldWrite = (issue, name) =>
    (issue.field_values || [])
      .filter((v) => v.field_id !== priorityFieldId)
      .map((v) => ({ field_id: v.field_id, value: v.option_name }))
      .concat(name ? [{ field_id: priorityFieldId, value: name }] : []);

  const ops = [];
  const recoveryErrors = [];
  const stats = {
    pairs_seen: 1, matched: 0, created: 0, adopted: 0, created_tasks: 0,
    skipped_quiet: 0, skipped_finished: 0, skipped_parent_unlinked: 0, attached: 0,
  };

  // A TRUNCATED issue snapshot is the one read error that can cause a
  // DUPLICATE: the same-title orphan gate and the uid search both scan the
  // fetched set, so an issue past the page limit looks like it does not
  // exist and the pair gets a second issue created for it. The page window
  // is 100 (REST per_page and the GraphQL selection alike). Refuse the pair
  // rather than write from a snapshot that cannot answer "does this already
  // exist?". A repo sitting at exactly 100 issues trips this too — a false
  // refusal is the safe direction, and the fix is the same either way.
  if (input.issuesTruncated) {
    recoveryErrors.push({
      pair: pair.github_repo,
      task_uid: '',
      issue_number: 0,
      reason: `issue snapshot truncated at the ${input.issuePageSize || 100}-issue page window — refusing to write, because an issue past the window reads as absent and would be created a second time; paginate the read before enabling this pair`,
    });
    return { ops: [], recoveryErrors, stats };
  }

  // A declared id that does not match the org's actual Priority field would
  // read every issue as un-prioritised and write into the WRONG field. The
  // engine cannot look the definition up, but the issues it already read name
  // their fields — so a mismatch is caught here rather than silently mangling
  // an unrelated field.
  if (priorityFieldId) {
    const wrong = issues.find(
      (i) => (i.field_values || []).some(
        (v) => String(v.field_name || '').toLowerCase() === 'priority' && v.field_id !== priorityFieldId
      )
    );
    if (wrong) {
      recoveryErrors.push({
        pair: pair.github_repo,
        task_uid: '',
        issue_number: wrong.number,
        reason: `declared Priority field id ${priorityFieldId} does not match this repo's Priority field — correct github_priority_field_id in the mapping, or set it to null to stop syncing priority`,
      });
      return { ops: [], recoveryErrors, stats };
    }
  }

  // Index issues by marker uid — the linkage. EVERY marker-carrying issue is
  // linked (never an adoption/creation candidate), even when a uid appears on
  // two issues — a body copy-pasted marker and all. That uid is then
  // ambiguous: it is dropped from the index (its task gets no ops) and named
  // in a recovery error, so neither issue is ever re-created as a task.
  const byUid = new Map();
  const linkedNumbers = new Set();
  const dupUids = new Map();
  for (const issue of issues) {
    const marker = parseMarker(issue.body);
    if (!(marker && marker.uid)) continue;
    linkedNumbers.add(issue.number);
    if (byUid.has(marker.uid)) {
      if (!dupUids.has(marker.uid)) dupUids.set(marker.uid, [byUid.get(marker.uid).issue.number]);
      dupUids.get(marker.uid).push(issue.number);
      continue;
    }
    byUid.set(marker.uid, { issue, marker });
  }
  for (const [uid, numbers] of dupUids) {
    byUid.delete(uid);
    recoveryErrors.push({
      pair: pair.github_repo,
      task_uid: uid,
      issue_number: numbers.join(','),
      reason: `duplicate link: issues #${numbers.join(', #')} all carry the marker for task ${uid} — remove the marker from every copy but one`,
    });
  }
  // A marker whose task is not in this project is a DANGLING link: the task
  // was deleted, moved, or the marker was written by a different tududi
  // instance (a local-dev run against a real repo leaves exactly this). Never
  // silently skip it — the issue would then never become a task anywhere.
  // Closed dangling issues are finished business and stay quiet.
  const taskUids = new Set(tasks.map((t) => t.uid));
  for (const { issue, marker } of byUid.values()) {
    if (issue.state === 'open' && !taskUids.has(marker.uid)) {
      recoveryErrors.push({
        pair: pair.github_repo,
        task_uid: marker.uid,
        issue_number: issue.number,
        reason: `dangling link: the issue's marker names a task that is not in tududi project '${pair.tududi_project}' — the task was deleted or moved, or the marker came from another tududi instance; remove the marker to let the issue be re-adopted or created`,
      });
    }
  }
  // Open issues no marker points at — adoption candidates, then GitHub-origin
  // creation candidates. Claimed as they are linked this cycle.
  const orphans = issues.filter((i) => i.state === 'open' && !linkedNumbers.has(i.number));
  const claimed = new Set();
  const sameTitle = (a, b) => projectTitle(a) === projectTitle(b);

  // Hierarchy (D8). The parent side of a child is resolved through the
  // LINKAGE, never by title: a subtask's parent issue is the issue its parent
  // task is linked to, and a sub-issue's parent task is the task its parent
  // issue's marker names. `undefined` = parent not linked (child waits).
  const taskByUid = new Map(tasks.map((t) => [t.uid, t]));
  const uidByNumber = new Map([...byUid.values()].map(({ issue, marker }) => [issue.number, marker.uid]));
  const parentIssueOf = (task) => (task.parent_uid ? (byUid.get(task.parent_uid) || {}).issue : null);
  const parentTaskOf = (issue) => (issue.parent_number ? taskByUid.get(uidByNumber.get(issue.parent_number)) : null);
  const parentNumOf = (issue) => issue.parent_number || null;

  // A subtask INHERITS the gate from its parent (operator's decision,
  // 2026-09-04). tududi 1.1.1 has no way to tag a subtask in its UI at all:
  // the inline subtask editor sends no tags and the backend whitelists fields
  // that exclude them, and clicking a subtask row opens the PARENT's page —
  // so a per-item tag would make the tududi→GitHub direction unreachable by
  // hand. Tagging the parent is the explicit opt-in for the whole item; its
  // issue is already public. A child of an UNtagged parent still exports
  // nothing, and a child may still carry the tag itself.
  const effTagged = (task) => {
    if (task.tagged) return true;
    if (!task.parent_uid) return false;
    const parent = taskByUid.get(task.parent_uid);
    return !!(parent && parent.tagged);
  };

  for (const task of tasks) {
    if (dupUids.has(task.uid)) continue; // ambiguous link reported above: no ops, no new issue
    const tagged = effTagged(task);
    if (!tagged && !byUid.has(task.uid)) continue; // untagged, never linked: untouched

    let linked = byUid.get(task.uid) || null;
    const parentIssue = parentIssueOf(task);

    // ── Creation path ───────────────────────────────────────────────────
    if (!linked) {
      if (!tagged) continue;
      // A tagged subtask waits until its parent is a linked pair — its issue
      // needs a parent issue to attach to. The parent's own creation is the
      // ordinary path above/below, so the child lands on a later cycle.
      if (task.parent_uid && !parentIssue) {
        stats.skipped_parent_unlinked++;
        continue;
      }
      // Second gate (D4): an unlinked open issue with the same canonical
      // title is the SAME item — an interrupted creation from either side,
      // or a human filing it in both places. Exactly one is adopted below;
      // more than one is a recovery error, because picking is guessing.
      // Scoped to the same parent: a twin under a DIFFERENT parent, or a
      // top-level twin of a child, is not the same item (D8 rule 4).
      const scope = parentIssue ? parentIssue.number : null;
      const matches = orphans.filter(
        (i) => !claimed.has(i.number) && parentNumOf(i) === scope && sameTitle(i.title, task.title)
      );
      if (matches.length > 1) {
        recoveryErrors.push({
          pair: pair.github_repo,
          task_uid: task.uid,
          issue_number: matches.map((i) => i.number).join(','),
          reason:
            'ambiguous adoption: several unlinked open issues carry this task\'s canonical title — rename all but one before the sync will link',
        });
        continue;
      }
      if (matches.length === 1) {
        claimed.add(matches[0].number);
        stats.adopted++;
        // Empty baselines: every field reads as changed on BOTH sides, so
        // equal fields stay quiet and differing fields resolve as a
        // last-writer-wins conflict with the loser preserved (D5) — no
        // side's value is silently overwritten by an adoption.
        linked = { issue: matches[0], marker: { uid: task.uid, baselines: {} }, adopted: true };
        const key = auditKey(task.uid, 'linked', task.updated_at);
        if (!commentHasKey(matches[0].comments, key)) {
          ops.push({
            type: 'comment_issue',
            repo: pair.github_repo,
            number: matches[0].number,
            body: `Linked to the tududi task with the same title (uid ${task.uid}); no duplicate was created. Differing fields are merged last-writer-wins below.\n\n${key}`,
          });
        }
      }
    }
    if (!linked) {
      // Finished work is not exported — the mirror of "closed issues are not
      // imported". A create always opens the issue (POST /issues has no
      // state), so a DONE/CANCELLED/ARCHIVED task would land as an OPEN issue
      // carrying a closed baseline, and the very next cycle would read that
      // as a GitHub reopen and drag the task back to IN_PROGRESS. Adoption
      // above still applies: a finished task with a live same-title issue is
      // the same item, and status then converges last-writer-wins.
      if (mapTududiStatus(task.status) !== 'open') {
        stats.skipped_finished++;
        continue;
      }
      const proj = projections(
        {
          title: task.title,
          description: task.note,
          statusMapped: mapTududiStatus(task.status),
          labelNames: task.tags,
          priorityName: tududiPriorityName(task.priority),
        },
        syncTag
      );
      ops.push({
        type: 'create_issue',
        repo: pair.github_repo,
        task_uid: task.uid,
        title: proj.title,
        body: withMarker(proj.description, {
          uid: task.uid,
          baselines: baselinesOf(proj, FIELDS),
          tududi_updated_at: task.updated_at,
          github_updated_at: '',
        }),
        labels: proj.labels ? proj.labels.split(',') : [],
        // A create can carry field values in the same call (measured), so a
        // prioritised task never spends a cycle at no-priority on GitHub.
        ...(priorityFieldId && proj.priority
          ? { issue_field_values: [{ field_id: priorityFieldId, value: proj.priority }] }
          : {}),
      });
      stats.created++;
      continue;
    }

    // ── Linked pair: converge ───────────────────────────────────────────
    stats.matched++;
    const { issue, marker } = linked;

    // Hierarchy drift (D8 rule 4): the child's parent on GitHub disagrees
    // with its parent in tududi — moved on one side, or a child linked to a
    // top-level task. No re-parenting exists in v1, so nothing is written
    // for the pair until a human resolves it.
    const observedParent = parentNumOf(issue);
    const expectedParent = task.parent_uid ? (parentIssue ? parentIssue.number : undefined) : null;
    if (observedParent !== null && observedParent !== expectedParent) {
      recoveryErrors.push({
        pair: pair.github_repo,
        task_uid: task.uid,
        issue_number: issue.number,
        reason: `hierarchy drift: issue #${issue.number} is a sub-issue of #${observedParent} but its task ${task.uid} ${
          task.parent_uid ? `belongs to parent task ${task.parent_uid}${expectedParent ? ` (issue #${expectedParent})` : ' (not linked)'}` : 'is top-level in tududi'
        } — move one side to match the other; the sync does not re-parent`,
      });
      continue;
    }

    // Un-tag / archive → close-as-not-planned with keyed audit comment (D6).
    const terminalEvent = !tagged
      ? 'untagged'
      : task.status === TUDUDI_STATUS.ARCHIVED
        ? 'archived'
        : null;
    if (terminalEvent) {
      if (issue.state === 'open') {
        const key = auditKey(task.uid, terminalEvent, task.updated_at);
        if (!commentHasKey(issue.comments, key)) {
          ops.push({
            type: 'comment_issue',
            repo: pair.github_repo,
            number: issue.number,
            body: `${terminalEvent === 'untagged' ? 'Sync tag removed in tududi' : 'Task archived in tududi'} — closing as not planned. The linkage survives; re-tagging reopens this issue.\n\n${key}`,
          });
        }
        // The close is a sync write, so it moves the status BASELINE with it.
        // That is what makes a later re-tag a plain tududi-side change the
        // field loop reopens on its own — and what tells a human's own
        // "close as not planned" on GitHub apart from ours: theirs leaves
        // the baseline at open, so it reads as a GitHub change and cancels
        // the task instead of being reverted (found live on dev-test #2,
        // cycle 118: the sync reopened an issue a human had just closed).
        ops.push({
          type: 'update_issue',
          repo: pair.github_repo,
          number: issue.number,
          patch: {
            state: 'closed',
            state_reason: 'not_planned',
            body: withMarker(issue.body, {
              uid: task.uid,
              baselines: { ...marker.baselines, status: hash('closed:not_planned') },
              tududi_updated_at: task.updated_at,
              github_updated_at: issue.updated_at,
            }),
          },
        });
      }
      continue;
    }

    // Attach from observed state (D8 rule 2): a linked child issue with no
    // parent on GitHub — freshly created, an executor that died between the
    // two hops, or a human detach — is (re-)attached under the parent's
    // linked issue. The tududi parent is the declared truth.
    if (task.parent_uid && parentIssue && observedParent === null) {
      ops.push({
        type: 'add_sub_issue',
        repo: pair.github_repo,
        parent_number: parentIssue.number,
        number: issue.number,
        sub_issue_id: issue.id,
      });
      stats.attached++;
    }

    // Echo suppression: skip everything the sync identity itself last wrote
    // is handled per FIELD via baselines; authorship filtering applies to
    // comment-driven flows only (v1 syncs no comments), so baselines carry it.
    const tSide = projections(
      {
        title: task.title,
        description: task.note,
        statusMapped: mapTududiStatus(task.status),
        labelNames: task.tags,
        priorityName: tududiPriorityName(task.priority),
      },
      syncTag
    );
    const gSide = projections(
      {
        title: issue.title,
        description: issue.body,
        statusMapped: mapIssueState(issue),
        labelNames: issue.labels,
        priorityName: priorityOf(issue),
      },
      syncTag
    );

    const issuePatch = {};
    const taskPatch = {};
    const losing = [];

    for (const f of FIELDS) {
      const tHash = hash(tSide[f]);
      const gHash = hash(gSide[f]);
      const base = marker.baselines[f];
      const tChanged = tHash !== base;
      const gChanged = gHash !== base;
      if (!tChanged && !gChanged) continue; // quiet field
      let winner;
      if (tChanged && gChanged && tHash !== gHash) {
        // Same-field conflict: the newer side's own updated_at wins (D5) —
        // never the poller's clock.
        winner = String(task.updated_at) >= String(issue.updated_at) ? 't' : 'g';
        // EVERY conflicting field is preserved, not just the first: two
        // fields can lose in one cycle (an adoption starts with empty
        // baselines, so several fields conflict at once), and dropping the
        // second silently destroys the value this comment exists to keep.
        losing.push({
          field: f,
          value: winner === 't' ? gSide[f] : tSide[f],
          from: winner === 't' ? 'github' : 'tududi',
        });
      } else if (tChanged) {
        winner = 't';
      } else {
        winner = 'g';
      }
      if (winner === 't' && tHash !== gHash) applyToIssue(issuePatch, f, tSide[f], issue);
      if (winner === 'g' && tHash !== gHash) applyToTask(taskPatch, f, gSide[f], task);
    }

    const newBase = {};
    for (const f of FIELDS) {
      // The converged value's hash becomes the new baseline.
      const winnerVal =
        issuePatch[fieldToIssueKey(f)] !== undefined
          ? tSide[f]
          : taskPatch[fieldToTaskKey(f)] !== undefined
            ? gSide[f]
            : tSide[f]; // unchanged fields agree
      newBase[f] = hash(winnerVal);
    }

    const changed = Object.keys(issuePatch).length > 0 || Object.keys(taskPatch).length > 0;
    // An adoption writes its marker even when every field already agrees —
    // the marker IS the link; without it the next cycle adopts again.
    if (!changed && !linked.adopted) {
      stats.skipped_quiet++;
      continue;
    }

    for (const lost of losing) {
      const key = auditKey(task.uid, `conflict_${lost.field}`,
        lost.from === 'github' ? issue.updated_at : task.updated_at);
      if (!commentHasKey(issue.comments, key)) {
        ops.push({
          type: 'comment_issue',
          repo: pair.github_repo,
          number: issue.number,
          body: `Sync conflict on \`${lost.field}\`: the ${lost.from} value lost last-writer-wins and is preserved here:\n\n\`\`\`\n${lost.value}\n\`\`\`\n\n${key}`,
        });
      }
    }

    // Marker rewrite always rides the issue update (comment first, marker
    // second — D6 ordering).
    ops.push({
      type: 'update_issue',
      repo: pair.github_repo,
      number: issue.number,
      patch: {
        ...issuePatch,
        body: withMarker(
          issuePatch.body !== undefined ? issuePatch.body : gSide.description,
          {
            uid: task.uid,
            baselines: newBase,
            tududi_updated_at: task.updated_at,
            github_updated_at: issue.updated_at,
          }
        ),
      },
    });
    if (Object.keys(taskPatch).length > 0) {
      ops.push({ type: 'update_task', task_uid: task.uid, patch: taskPatch });
    }
  }

  // ── GitHub-origin creation ──────────────────────────────────────────
  // Every open issue still unclaimed here has no task: a human filed it on
  // GitHub. It becomes a tagged task in the paired project — unless an
  // UNTAGGED task already carries its title, in which case creating would
  // duplicate a task the owner chose to keep private; that is theirs to
  // resolve (tag it to link, or rename). A sync-authored orphan is an
  // interrupted tududi-origin creation whose task no longer qualifies —
  // reported, never turned into a task.
  for (const issue of orphans) {
    if (claimed.has(issue.number)) continue;
    if (issue.user_login === syncLogin) {
      recoveryErrors.push({
        pair: pair.github_repo,
        task_uid: null,
        issue_number: issue.number,
        reason: 'orphan: sync-authored open issue with no marker and no tagged task carrying its title — re-tag the task, or close the issue',
      });
      continue;
    }
    // A sub-issue waits for its parent issue to be linked: tududi has no
    // re-parenting here, so importing it top-level now would strand it.
    const parentTask = parentTaskOf(issue);
    if (issue.parent_number && !parentTask) {
      stats.skipped_parent_unlinked++;
      continue;
    }
    // ANY unlinked task carrying the title blocks creation. A tagged one that
    // reached here unclaimed was already reported above — ambiguous adoption
    // or a duplicated marker — and creating would turn one reported ambiguity
    // into N extra tasks (found by the unit suite: 1 task + 2 twin issues
    // produced 3 tasks). An untagged one is the owner's private twin. Scoped
    // to the same parent (D8 rule 4).
    const scopeUid = parentTask ? parentTask.uid : null;
    const shadow = tasks.find(
      (t) => !byUid.has(t.uid) && (t.parent_uid || null) === scopeUid && sameTitle(t.title, issue.title)
    );
    if (shadow && effTagged(shadow)) continue;
    if (shadow) {
      recoveryErrors.push({
        pair: pair.github_repo,
        task_uid: shadow.uid,
        issue_number: issue.number,
        reason: 'untagged task carries this GitHub issue\'s canonical title — tag it to link them, or rename one; the sync will not create a second task',
      });
      continue;
    }
    const proj = projections(
      {
        title: issue.title,
        description: issue.body,
        statusMapped: 'open',
        labelNames: issue.labels,
        priorityName: priorityOf(issue),
      },
      syncTag
    );
    ops.push({
      type: 'create_task',
      repo: pair.github_repo,
      number: issue.number,
      project: pair.tududi_project,
      task: {
        name: proj.title,
        note: proj.description,
        // GitHub-origin work lands as PLANNED, not NOT_STARTED (operator's
        // rule): it is scheduled work someone else filed, not something the
        // owner has already picked up. Both map to 'open', so the projection
        // is unchanged and the pair is quiet on arrival.
        status: TUDUDI_STATUS.PLANNED,
        ...(proj.priority ? { priority: proj.priority } : {}),
        tags: (proj.labels ? proj.labels.split(',') : []).concat(syncTag).map((n) => ({ name: n })),
        // A sub-issue lands as a SUBTASK of the linked parent task (D8 rule
        // 3) — tududi's own hierarchy, keyed by its integer id, not the uid.
        ...(parentTask ? { parent_task_id: parentTask.id } : {}),
      },
      link_body: withMarker(proj.description, {
        uid: LINK_UID_PLACEHOLDER,
        baselines: baselinesOf(proj, FIELDS),
        tududi_updated_at: '',
        github_updated_at: issue.updated_at,
      }),
    });
    stats.created_tasks++;
  }

  // Per-cycle write cap: exceeding it is a loud failure, not a truncation —
  // a truncated cycle would half-apply and look healthy (risk 3).
  const writes = ops.filter((o) => o.type !== 'noop').length;
  if (writes > writeCap) {
    throw new Error(
      `write cap exceeded for ${pair.github_repo}: ${writes} > ${writeCap} — refusing the whole cycle`
    );
  }

  return { ops, recoveryErrors, stats };

  function fieldToIssueKey(f) {
    return { title: 'title', description: 'body', status: 'state', labels: 'labels',
      priority: 'issue_field_values' }[f];
  }
  function fieldToTaskKey(f) {
    return { title: 'name', description: 'note', status: 'status', labels: 'tags',
      priority: 'priority' }[f];
  }
  function applyToIssue(patch, f, value, issue) {
    if (f === 'title') patch.title = value;
    else if (f === 'description') patch.body = value;
    else if (f === 'labels') patch.labels = value ? value.split(',') : [];
    else if (f === 'status') {
      if (value === 'open') patch.state = 'open';
      else {
        patch.state = 'closed';
        patch.state_reason = value === 'closed:not_planned' ? 'not_planned' : 'completed';
      }
    }
    // The option name match is case-insensitive (measured), so the canonical
    // lowercase value writes directly — no option-name table to drift.
    else if (f === 'priority') patch.issue_field_values = issueFieldWrite(issue, value);
  }
  function applyToTask(patch, f, value, task) {
    if (f === 'title') patch.name = value;
    else if (f === 'description') patch.note = value;
    // tududi's updateTaskTags reads tag.name (v1.1.1), so update_task ops
    // carry tag OBJECTS, not the bare strings a bare split would give — and
    // the sync control tag is re-attached so a labels merge never strips it
    // off the task (it is excluded from the projection, never from the task).
    else if (f === 'labels') {
      patch.tags = (value ? value.split(',') : [])
        .concat(syncTag)
        .map((n) => ({ name: n }));
    }
    else if (f === 'status') {
      const next = issueStateToTududiStatus(value, task.status);
      if (next !== null) patch.status = next;
    }
    // tududi accepts the priority name or null-for-none on PATCH (measured).
    else if (f === 'priority') patch.priority = value || null;
  }
}

module.exports = {
  TUDUDI_STATUS,
  PRIORITY_FOLD,
  projectPriority,
  tududiPriorityName,
  fieldsFor,
  mapTududiStatus,
  mapIssueState,
  issueStateToTududiStatus,
  hash,
  projections,
  baselinesOf,
  renderMarker,
  parseMarker,
  stripMarker,
  withMarker,
  auditKey,
  commentHasKey,
  computeOps,
  LINK_UID_PLACEHOLDER,
};
