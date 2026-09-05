// Execute the real container helper with fake app models; no token or DB leaves this process.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { Readable } = require('node:stream');
const { EventEmitter } = require('node:events');
const source = fs.readFileSync(`${__dirname}/../../../../playbooks/files/tududi-db-mint.js`, 'utf8');
const raw = 'tt_' + 'a'.repeat(64);

async function run(action, rows, status = 200, matches = true) {
  const writes = [];
  const output = [];
  let finish;
  const done = new Promise(resolve => { finish = resolve; });
  vm.runInNewContext(source, {
    Buffer,
    process: { env: { TUDUDI_SYNC_EMAIL: 'fixture', TUDUDI_SYNC_LABEL: 'fixture' },
      argv: ['node', 'helper', action], stdin: Readable.from([Buffer.from(raw)]), exit: finish },
    console: { log: text => output.push(text), error: text => output.push(text) },
    require: name => {
      if (name === '/app/backend/models') return {
        User: { findOne: async () => ({ id: 1 }) },
        ApiToken: { findAll: async () => rows, create: async value => { writes.push(value); return { id: 2 }; } },
      };
      if (name.endsWith('/bcrypt')) return { hash: async () => 'fixture-hash', compare: async () => matches };
      if (name === 'http') return { get: (_, callback) => {
        callback({ resume() {}, statusCode: status });
        return new EventEmitter();
      } };
      throw new Error(`unexpected module ${name}`);
    },
  });
  const code = await done;
  assert(!output.join('\n').includes(raw));
  return { code, writes, output };
}

(async () => {
  const rows = [{ token_prefix: raw.slice(0, 12), token_hash: 'fixture-hash',
    save: async () => { throw new Error('existing row was modified'); } }];
  for (const action of ['status', 'prove']) {
    const result = await run(action, rows);
    assert.equal(result.code, 0);
    assert.equal(result.writes.length, 0);
  }
  for (const [action, existing, status] of [
    ['revoke-label', rows, 200], ['insert', rows, 200], ['prove', rows, 401], ['prove', [], 200],
  ]) {
    const result = await run(action, existing, status);
    assert.equal(result.code, 1);
    assert.equal(result.writes.length, 0);
    if (action === 'revoke-label') assert(result.output.join(' ').includes('unknown action'));
  }
  const mismatch = await run('prove', rows, 200, false);
  assert.equal(mismatch.code, 1);
  assert.equal(mismatch.writes.length, 0);
  const initial = await run('insert', []);
  assert.equal(initial.code, 0);
  assert.equal(initial.writes.length, 1);
  console.log('TOKEN SAFETY PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
