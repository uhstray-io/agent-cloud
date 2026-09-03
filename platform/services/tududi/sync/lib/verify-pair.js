// verify-pair.js — the per-pair verification check (task 5.1): the executable
// gate both the scratch validation (5.x) and the production rollout (6.x) run
// before a pair is promoted or kept enabled.
//
// Runs INSIDE the tududi container (the runner image has no node) with
// sync-core.js copied alongside — the verdict uses the EXACT engine the
// workflow embeds, so "converged" here means "the next cycle is a no-op",
// not an approximation of it.
//
// stdin: one JSON payload —
//   {
//     pair, syncTag, syncLogin, writeCap,
//     tasks:   [ the paired project's tasks, engine shape ],
//     issues:  [ the paired repo's issues, engine shape ],
//     lastExecution: { status } | null,      // n8n's latest cycle for the workflow
//     undeclaredMarkers: [ task uids ]       // sync markers found OUTSIDE declared projects
//   }
// stdout: one JSON verdict line; exit 0 = PASS, 1 = FAIL.
//
// PASS requires (task 5.1):
//   1. the last cycle completed without error (a write-cap breach fails the
//      cycle loudly, so it is covered here too)
//   2. the pair is CONVERGED: the engine, run on a fresh snapshot, emits zero
//      ops and zero recovery errors — every linked pair's per-field baselines
//      match both sides, no creation is pending, no conflict is brewing
//   3. zero sync activity outside the declaration: no marker traces on tasks
//      in undeclared projects

'use strict';

const path = require('path');
const core = require(path.join(__dirname, 'sync-core.js'));

async function readStdin() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  return Buffer.concat(chunks).toString('utf8');
}

async function main() {
  const input = JSON.parse(await readStdin());
  const failures = [];

  if (!input.lastExecution) {
    failures.push('no cycle execution found for the workflow — has the schedule fired?');
  } else if (input.lastExecution.status !== 'success') {
    failures.push(`last cycle finished '${input.lastExecution.status}', not success`);
  }

  let stats = null;
  try {
    const r = core.computeOps({
      pair: input.pair,
      syncTag: input.syncTag,
      syncLogin: input.syncLogin,
      writeCap: input.writeCap,
      tasks: input.tasks,
      issues: input.issues,
    });
    stats = r.stats;
    if (r.recoveryErrors.length > 0) {
      failures.push(`${r.recoveryErrors.length} recovery error(s) pending a human: ` +
        r.recoveryErrors.map((e) => `task ${e.task_uid} / issue #${e.issue_number}`).join('; '));
    }
    if (r.ops.length > 0) {
      failures.push(`not converged: a fresh cycle would emit ${r.ops.length} op(s) ` +
        `(${[...new Set(r.ops.map((o) => o.type))].join(', ')})`);
    }
  } catch (e) {
    failures.push(`engine refused the snapshot: ${e.message}`);
  }

  if ((input.undeclaredMarkers || []).length > 0) {
    failures.push(`sync traces on ${input.undeclaredMarkers.length} task(s) in UNDECLARED projects`);
  }

  const verdict = {
    pair: input.pair.github_repo,
    pass: failures.length === 0,
    failures,
    stats,
  };
  console.log(JSON.stringify(verdict));
  process.exit(verdict.pass ? 0 : 1);
}

main().catch((e) => {
  console.log(JSON.stringify({ pass: false, failures: [`verifier error: ${e.message}`] }));
  process.exit(1);
});
