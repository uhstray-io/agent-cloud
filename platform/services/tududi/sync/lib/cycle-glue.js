// cycle-glue.js — the n8n Code-node harness around sync-core.js
// (integrate-tududi-github-issue-sync, design D1/D2-as-amended).
//
// The workflow template embeds sync-core.js followed by this file into ONE
// Code node; the sync pairs, tag, login and write cap arrive as a JSON
// constant the template renders above both (SYNC_CONFIG). Items arriving at
// the node carry, per enabled pair, the pair's tududi task list and GitHub
// issue list (fetched by the upstream HTTP Request nodes, which own the
// credentials — a Code node deliberately cannot touch credentials).
//
// Emits one n8n item per operation, tagged with the executor that must run
// it ({exec: 'github'|'tududi'}), plus one summary item. Recovery errors are
// attached to the summary — the per-pair verification check (task 5.1) reads
// them; a write-cap breach throws, failing the run loudly (risk 3).

/* global SYNC_CONFIG, computeOps, $input */

const results = [];
const summary = { cycles: [], recovery_errors: [] };

for (const item of $input.all()) {
  const { pair, tasks, issues } = item.json;
  const { ops, recoveryErrors, stats } = computeOps({
    pair,
    syncTag: SYNC_CONFIG.sync_tag,
    syncLogin: SYNC_CONFIG.sync_login,
    writeCap: SYNC_CONFIG.write_cap,
    tasks,
    issues,
  });
  summary.cycles.push({ pair: pair.github_repo, ...stats, ops: ops.length });
  summary.recovery_errors.push(...recoveryErrors);
  for (const op of ops) {
    results.push({
      json: {
        exec: op.type === 'update_task' ? 'tududi' : 'github',
        op,
      },
    });
  }
}

results.push({ json: { exec: 'summary', summary } });
return results;
