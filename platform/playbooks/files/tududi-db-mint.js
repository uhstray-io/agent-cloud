// tududi-db-mint.js — mint the sync API token INSIDE the tududi container
// (integrate-tududi-github-issue-sync task 3.1; design D7 as amended).
//
// Why DB-side: the deploy is SSO-only by design (PASSWORD_AUTH_ENABLED=false),
// so the session mint route is deliberately unreachable. This script runs
// through the app's OWN stack — its sequelize models and its bcrypt — so the
// row it inserts is exactly what /api/profile/api-keys would have created
// (verified against v1.1.1: apiTokenService.createApiToken hashes with
// bcrypt cost 12 and stores a 12-char prefix; findValidTokenByValue matches
// by prefix + bcrypt.compare).
//
// NON-DESTRUCTIVE CONTRACT (operator requirement): the only writes are one
// INSERT into api_tokens, and setting revoked_at (the app's own revoke
// semantic, reversible) on rows that carry OUR label under OUR user. No
// DELETE exists in this file; no other table is touched; every action prints
// one JSON line of names and counts, never a token value.
//
// Usage (podman exec, stdin carries the raw token for `insert` only):
//   node tududi-db-mint.js status|revoke-label|insert
//   env: TUDUDI_SYNC_EMAIL (account), TUDUDI_SYNC_LABEL (token name)

'use strict';

const path = '/app/backend';
const { User, ApiToken } = require(`${path}/models`);
const bcrypt = require(`${path}/node_modules/bcrypt`);

const EMAIL = process.env.TUDUDI_SYNC_EMAIL;
const LABEL = process.env.TUDUDI_SYNC_LABEL;
const ACTION = process.argv[2];
const PREFIX_LENGTH = 12; // upstream TOKEN_PREFIX_LENGTH at v1.1.1

async function readStdin() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  return Buffer.concat(chunks).toString('utf8').trim();
}

async function main() {
  if (!EMAIL || !LABEL) throw new Error('TUDUDI_SYNC_EMAIL and TUDUDI_SYNC_LABEL are required');
  const user = await User.findOne({ where: { email: EMAIL } });
  if (!user) throw new Error(`no tududi user with the configured email`);

  const labelled = await ApiToken.findAll({
    where: { user_id: user.id, name: LABEL, revoked_at: null },
  });

  if (ACTION === 'status') {
    console.log(JSON.stringify({ user_found: true, active_label_rows: labelled.length }));
  } else if (ACTION === 'revoke-label') {
    // The app's own revoke semantic, scoped to OUR label under OUR user.
    for (const row of labelled) {
      row.revoked_at = new Date();
      await row.save();
    }
    console.log(JSON.stringify({ revoked: labelled.length }));
  } else if (ACTION === 'insert') {
    const raw = await readStdin();
    if (!/^tt_[0-9a-f]{64}$/.test(raw)) throw new Error('stdin is not a tt_<64 hex> token');
    const row = await ApiToken.create({
      user_id: user.id,
      name: LABEL,
      token_hash: await bcrypt.hash(raw, 12),
      token_prefix: raw.slice(0, PREFIX_LENGTH),
      abilities: null,
      expires_at: null,
    });
    console.log(JSON.stringify({ created: true, row_id: row.id }));
  } else {
    throw new Error(`unknown action: ${ACTION}`);
  }
}

main().then(
  () => process.exit(0),
  (e) => {
    console.error(`ERROR: ${e.message}`);
    process.exit(1);
  }
);
