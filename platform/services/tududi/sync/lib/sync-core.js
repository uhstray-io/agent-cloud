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
//   - creation is gated by the uid search AND the sync-identity/title
//     second gate; a suspect match blocks creation with a recovery error (D4)
//   - audit comments carry a stable audit-event key and are suppressed when
//     the key already appears in the issue's comments (D6)
//   - the per-cycle write cap fails the cycle loudly when exceeded (risk 3)

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

const FIELDS = ['title', 'description', 'status', 'labels'];

// Build the canonical field projections for one side.
// side: {title, description, statusMapped, labelNames}
function projections(side, syncTag) {
  return {
    title: projectTitle(side.title),
    description: projectDescription(side.description),
    status: side.statusMapped,
    labels: projectLabels(side.labelNames, syncTag),
  };
}

function baselinesOf(proj) {
  const out = {};
  for (const f of FIELDS) out[f] = hash(proj[f]);
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
    ...FIELDS.map((f) => `base_${f}: ${state.baselines[f]}`),
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
  for (const f of FIELDS) baselines[f] = get(`base_${f}`);
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
//   tasks: tududi tasks of the project, each
//     {uid, title, note, status(int), tags:[names], updated_at, tagged:bool,
//      archived:bool, issue_ref: {repo, number} | null}
//   issues: GitHub issues of the repo, each
//     {number, title, body, state, state_reason, labels:[names],
//      updated_at, user_login, comments:[{body}]}
//
// ops emitted (executor maps them to HTTP): create_issue, update_issue,
// comment_issue, update_task. NO delete op exists.

function computeOps(input) {
  const { pair, syncTag, syncLogin, writeCap, tasks, issues } = input;
  const ops = [];
  const recoveryErrors = [];
  const stats = { pairs_seen: 1, matched: 0, created: 0, skipped_quiet: 0 };

  // Index issues by marker uid; collect sync-identity-authored open issues
  // for the second creation gate.
  const byUid = new Map();
  for (const issue of issues) {
    const marker = parseMarker(issue.body);
    if (marker && marker.uid) {
      byUid.set(marker.uid, { issue, marker });
    }
  }

  for (const task of tasks) {
    if (!task.tagged && !byUid.has(task.uid)) continue; // untagged, never linked: untouched

    const linked = byUid.get(task.uid) || null;

    // ── Creation path ───────────────────────────────────────────────────
    if (!linked) {
      if (!task.tagged) continue;
      // Second gate (D4): a sync-authored open issue with the same canonical
      // title blocks creation and records a recovery error.
      const suspect = issues.find(
        (i) =>
          i.state === 'open' &&
          i.user_login === syncLogin &&
          projectTitle(i.title) === projectTitle(task.title)
      );
      if (suspect) {
        recoveryErrors.push({
          pair: pair.github_repo,
          task_uid: task.uid,
          issue_number: suspect.number,
          reason:
            'suspect duplicate: open sync-authored issue with identical canonical title but no marker linkage — re-link or rename before the sync will create',
        });
        continue;
      }
      const proj = projections(
        {
          title: task.title,
          description: task.note,
          statusMapped: mapTududiStatus(task.status),
          labelNames: task.tags,
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
          baselines: baselinesOf(proj),
          tududi_updated_at: task.updated_at,
          github_updated_at: '',
        }),
        labels: proj.labels ? proj.labels.split(',') : [],
      });
      stats.created++;
      continue;
    }

    // ── Linked pair: converge ───────────────────────────────────────────
    stats.matched++;
    const { issue, marker } = linked;

    // Un-tag / archive → close-as-not-planned with keyed audit comment (D6).
    const terminalEvent = !task.tagged
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
        ops.push({
          type: 'update_issue',
          repo: pair.github_repo,
          number: issue.number,
          patch: { state: 'closed', state_reason: 'not_planned' },
        });
      }
      continue;
    }

    // Re-tagged after un-tag: reopen the same issue (D6).
    if (task.tagged && issue.state === 'closed' && issue.state_reason === 'not_planned'
        && mapTududiStatus(task.status) === 'open') {
      ops.push({
        type: 'update_issue',
        repo: pair.github_repo,
        number: issue.number,
        patch: { state: 'open' },
      });
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
      },
      syncTag
    );
    const gSide = projections(
      {
        title: issue.title,
        description: issue.body,
        statusMapped: mapIssueState(issue),
        labelNames: issue.labels,
      },
      syncTag
    );

    const issuePatch = {};
    const taskPatch = {};
    let losing = null;

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
        losing = losing || {
          field: f,
          value: winner === 't' ? gSide[f] : tSide[f],
          from: winner === 't' ? 'github' : 'tududi',
        };
      } else if (tChanged) {
        winner = 't';
      } else {
        winner = 'g';
      }
      if (winner === 't' && tHash !== gHash) applyToIssue(issuePatch, f, tSide[f]);
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
    if (!changed) {
      stats.skipped_quiet++;
      continue;
    }

    if (losing) {
      const key = auditKey(task.uid, `conflict_${losing.field}`,
        losing.from === 'github' ? issue.updated_at : task.updated_at);
      if (!commentHasKey(issue.comments, key)) {
        ops.push({
          type: 'comment_issue',
          repo: pair.github_repo,
          number: issue.number,
          body: `Sync conflict on \`${losing.field}\`: the ${losing.from} value lost last-writer-wins and is preserved here:\n\n\`\`\`\n${losing.value}\n\`\`\`\n\n${key}`,
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
    return { title: 'title', description: 'body', status: 'state', labels: 'labels' }[f];
  }
  function fieldToTaskKey(f) {
    return { title: 'name', description: 'note', status: 'status', labels: 'tags' }[f];
  }
  function applyToIssue(patch, f, value) {
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
  }
  function applyToTask(patch, f, value, task) {
    if (f === 'title') patch.name = value;
    else if (f === 'description') patch.note = value;
    else if (f === 'labels') patch.tags = value ? value.split(',') : [];
    else if (f === 'status') {
      const next = issueStateToTududiStatus(value, task.status);
      if (next !== null) patch.status = next;
    }
  }
}

module.exports = {
  TUDUDI_STATUS,
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
};
