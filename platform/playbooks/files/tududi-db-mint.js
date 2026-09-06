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
// NON-DESTRUCTIVE CONTRACT: only an initial INSERT is supported. Existing
// active labelled rows cause insertion to refuse; no revoke action exists.
// Every action prints one JSON line of names/counts, never a token value.
//
// Usage (podman exec, stdin carries the raw token for `insert` and `prove`):
//   node tududi-db-mint.js status|insert|prove
//   env: TUDUDI_SYNC_EMAIL (account), TUDUDI_SYNC_LABEL (token name)
//
// `prove` calls the LIVE API from inside the container, on loopback — the one
// address that is the same on local-dev and prod. The public edge URL is not:
// from the Semaphore runner the local dev zone resolves to 127.0.0.1 (nothing
// listens there inside its container) and prod's goes out through Cloudflare.
// Same vantage point deploy-tududi's health check already uses.

'use strict';

const http = require('http');

const path = '/app/backend';
const { User, ApiToken } = require(`${path}/models`);
const bcrypt = require(`${path}/node_modules/bcrypt`);

const EMAIL = process.env.TUDUDI_SYNC_EMAIL;
const LABEL = process.env.TUDUDI_SYNC_LABEL;
const ACTION = process.argv[2];
const PREFIX_LENGTH = 12; // upstream TOKEN_PREFIX_LENGTH at v1.1.1
const API_PORT = 3002; // the container's fixed listen port (deploy-tududi health check)

async function readStdin() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  return Buffer.concat(chunks).toString('utf8').trim();
}

function bearerStatus(raw) {
  return new Promise((resolve, reject) => {
    const req = http.get(
      { host: '127.0.0.1', port: API_PORT, path: '/api/profile/api-keys',
        headers: { Authorization: `Bearer ${raw}` }, timeout: 5000 },
      (res) => { res.resume(); resolve(res.statusCode); }
    );
    req.on('error', reject);
    req.on('timeout', () => req.abort()); // surfaces as 'error' → reject
  });
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
  } else if (ACTION === 'insert') {
    if (labelled.length) throw new Error('active labelled rows exist; refusing to replace access');
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
  } else if (ACTION === 'prove') {
    // Two facts, both required: the live API accepts the token, AND it is
    // one of OUR user's active labelled rows (prefix + bcrypt.compare, the
    // app's own findValidTokenByValue semantics). The second is what makes
    // "OpenBao holds a token" mean "OpenBao holds THIS identity's token" —
    // a stored token minted for a different user is live but wrong, and
    // presence alone reported it as converged.
    const raw = await readStdin();
    if (!/^tt_[0-9a-f]{64}$/.test(raw)) throw new Error('stdin is not a tt_<64 hex> token');
    const status = await bearerStatus(raw);
    if (status !== 200) throw new Error(`live API refused the token: HTTP ${status}`);
    let owned = false;
    for (const row of labelled) {
      if (row.token_prefix === raw.slice(0, PREFIX_LENGTH) && (await bcrypt.compare(raw, row.token_hash))) owned = true;
    }
    if (!owned) throw new Error('live token is not an active labelled row of the configured user');
    console.log(JSON.stringify({ proven: true, status }));
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
