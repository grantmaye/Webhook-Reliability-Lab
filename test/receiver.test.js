import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { openInbox, createReceiver, sign, verifySignature } from '../src/receiver.js';

const secret = 'a-long-development-test-secret-not-for-deployment';
const event = { id: 'evt_1', type: 'order.created', data: { order_id: 'ORD1', currency: 'USD', amount_cents: 12999 } };

test('signature authenticates raw bytes and rejects stale, future and malformed headers', () => {
  const body = Buffer.from(JSON.stringify(event));
  const stamp = '1800000000';
  const signature = sign(secret, stamp, body);
  assert.equal(verifySignature(secret, stamp, body, signature, Number(stamp) * 1000), true);
  assert.equal(verifySignature(secret, stamp, Buffer.from('{}'), signature, Number(stamp) * 1000), false);
  assert.equal(verifySignature(secret, stamp, body, signature, (Number(stamp) + 301) * 1000), false);
  assert.equal(verifySignature(secret, stamp, body, signature, (Number(stamp) - 301) * 1000), false);
  assert.equal(verifySignature(secret, stamp, body, 'bad', Number(stamp) * 1000), false);
  assert.equal(verifySignature(secret, undefined, body, signature), false);
});

test('persistent inbox deduplicates delivery and rejects conflicting event IDs', () => {
  const folder = mkdtempSync(join(tmpdir(), 'inbox-test-'));
  let inbox;
  try {
    const filename = join(folder, 'inbox.sqlite');
    const body = Buffer.from(JSON.stringify(event));
    inbox = openInbox(filename);
    assert.equal(inbox.record(event, body).duplicate, false);
    inbox.close(); inbox = null;
    inbox = openInbox(filename);
    assert.equal(inbox.record(event, body).duplicate, true);
    assert.throws(() => inbox.record(event, Buffer.from('{}')), { status: 409 });
    assert.equal(inbox.count(), 1);
  } finally { inbox?.close(); rmSync(folder, { recursive: true, force: true }); }
});

test('HTTP receiver verifies auth, simulates failures, deduplicates concurrent deliveries', async t => {
  const inbox = openInbox();
  const app = createReceiver({ inbox, secret, failFirst: 1 });
  app.listen(0, '127.0.0.1'); await once(app, 'listening');
  t.after(async () => { await new Promise(resolve => app.close(resolve)); inbox.close(); });
  const base = `http://127.0.0.1:${app.address().port}`;
  const post = (data, options = {}) => {
    const body = typeof data === 'string' ? data : JSON.stringify(data);
    const stamp = String(options.timestamp ?? Math.floor(Date.now() / 1000));
    return fetch(base + '/webhooks/orders', { method: 'POST', body, headers: {
      'content-type': 'application/json', 'x-webhook-timestamp': stamp,
      'x-webhook-signature': options.signature ?? sign(secret, stamp, body)
    } });
  };
  assert.equal((await post(event, { signature: '0'.repeat(64) })).status, 401);
  assert.equal((await post(event, { timestamp: 1000000000 })).status, 401);
  assert.equal((await post('{')).status, 400);
  assert.equal((await post({ ...event, type: 'unsupported' })).status, 422);
  assert.equal((await post(' '.repeat(65537))).status, 413);
  const failure = await post(event);
  assert.equal(failure.status, 503);
  assert.equal(failure.headers.get('retry-after'), '1');
  assert.equal(inbox.count(), 0);
  const concurrent = await Promise.all(Array.from({ length: 8 }, () => post(event)));
  assert.equal(concurrent.filter(r => r.status === 202).length, 1);
  assert.equal(concurrent.filter(r => r.status === 200).length, 7);
  assert.equal((await post({ ...event, data: { ...event.data, amount_cents: 1 } })).status, 409);
  assert.equal(inbox.count(), 1);
  assert.equal((await fetch(base + '/stats')).status, 401);
  const stats = await (await fetch(base + '/stats', { headers: { authorization: `Bearer ${secret}` } })).json();
  assert.equal(stats.accepted, 1);
  assert.equal(stats.duplicates, 7);
  assert.equal(stats.transient_failures, 1);
});

test('a database insert failure leaves the event retryable without a partial inbox row', async () => {
  const { DatabaseSync } = await import('node:sqlite');
  const folder = mkdtempSync(join(tmpdir(), 'inbox-failure-'));
  let inbox; let raw;
  try {
    const filename = join(folder, 'inbox.sqlite');
    inbox = openInbox(filename);
    raw = new DatabaseSync(filename);
    raw.exec(`CREATE TRIGGER reject_event BEFORE INSERT ON events
      BEGIN SELECT RAISE(ABORT, 'injected inbox failure'); END;`);
    const body = Buffer.from(JSON.stringify(event));
    assert.throws(() => inbox.record(event, body), /injected inbox failure/);
    assert.equal(inbox.count(), 0);
    raw.exec('DROP TRIGGER reject_event');
    assert.equal(inbox.record(event, body).duplicate, false);
    assert.equal(inbox.record(event, body).duplicate, true);
    assert.equal(inbox.count(), 1);
  } finally { raw?.close(); inbox?.close(); rmSync(folder, { recursive: true, force: true }); }
});
