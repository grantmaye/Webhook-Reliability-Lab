import { createServer } from 'node:http';
import { createHash, createHmac, timingSafeEqual } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export function sign(secret, timestamp, body) {
  return createHmac('sha256', secret).update(`${timestamp}.`).update(body).digest('hex');
}

export function verifySignature(secret, timestamp, body, signature, now = Date.now()) {
  if (typeof timestamp !== 'string' || !/^\d{10}$/.test(timestamp) || Math.abs(now / 1000 - Number(timestamp)) > 300) return false;
  if (typeof signature !== 'string' || !/^[a-f0-9]{64}$/.test(signature)) return false;
  return timingSafeEqual(Buffer.from(sign(secret, timestamp, body), 'hex'), Buffer.from(signature, 'hex'));
}

export function openInbox(filename = ':memory:') {
  const db = new DatabaseSync(filename);
  db.exec(`PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS events (
      event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL, received_at TEXT NOT NULL
    ) STRICT;`);
  return {
    record(event, body) {
      const digest = createHash('sha256').update(body).digest('hex');
      db.exec('BEGIN IMMEDIATE');
      try {
        const prior = db.prepare('SELECT digest FROM events WHERE event_id = ?').get(event.id);
        if (prior && prior.digest !== digest) throw new HttpError(409, 'Event ID already used for different content');
        if (!prior) db.prepare('INSERT INTO events VALUES (?, ?, ?, ?)').run(event.id, digest, body.toString('utf8'), new Date().toISOString());
        db.exec('COMMIT');
        return { event_id: event.id, duplicate: Boolean(prior) };
      } catch (error) { db.exec('ROLLBACK'); throw error; }
    },
    count() { return db.prepare('SELECT COUNT(*) AS total FROM events').get().total; },
    close() { db.close(); }
  };
}

function validateEvent(event) {
  if (!event || typeof event !== 'object' || Array.isArray(event) || typeof event.id !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(event.id)) {
    throw new HttpError(422, 'Event requires a valid id');
  }
  if (event.type !== 'order.created') throw new HttpError(422, 'Supported type: order.created');
  const data = event.data;
  if (!data || typeof data.order_id !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(data.order_id)
      || data.currency !== 'USD' || !Number.isSafeInteger(data.amount_cents) || data.amount_cents < 0) {
    throw new HttpError(422, 'Invalid order data');
  }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0; let exceeded = false; const chunks = [];
    req.on('data', chunk => {
      size += chunk.length;
      if (size > 65536) {
        if (!exceeded) { exceeded = true; chunks.length = 0; reject(new HttpError(413, 'Payload exceeds 64 KiB')); }
      } else if (!exceeded) chunks.push(chunk);
    });
    req.on('end', () => { if (!exceeded) resolve(Buffer.concat(chunks)); });
    req.on('error', reject);
    req.on('aborted', () => reject(new HttpError(400, 'Request aborted')));
  });
}

export function createReceiver({ secret, inbox, failFirst = 0, now = () => Date.now() }) {
  if (typeof secret !== 'string' || secret.length < 32) throw new Error('WEBHOOK_SECRET must contain at least 32 characters');
  if (!Number.isInteger(failFirst) || failFirst < 0 || failFirst > 10) throw new Error('failFirst must be 0 to 10');
  let remainingFailures = failFirst;
  const metrics = { attempts: 0, accepted: 0, duplicates: 0, transient_failures: 0 };
  const app = createServer(async (req, res) => {
    function send(status, body, headers = {}) {
      res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff', ...headers });
      res.end(JSON.stringify(body));
    }
    try {
      if (req.method === 'GET' && req.url === '/health') return send(200, { status: 'ok' });
      if (req.method === 'GET' && req.url === '/stats') {
        const expected = Buffer.from(`Bearer ${secret}`), supplied = Buffer.from(req.headers.authorization ?? '');
        if (expected.length !== supplied.length || !timingSafeEqual(expected, supplied)) throw new HttpError(401, 'Valid bearer secret required');
        return send(200, { ...metrics, stored_events: inbox.count() });
      }
      if (req.method !== 'POST' || req.url !== '/webhooks/orders') { req.resume(); throw new HttpError(404, 'Route not found'); }
      if ((req.headers['content-type'] ?? '').split(';')[0].trim().toLowerCase() !== 'application/json') {
        req.resume(); throw new HttpError(415, 'Content-Type must be application/json');
      }
      const body = await readBody(req);
      if (!verifySignature(secret, req.headers['x-webhook-timestamp'], body, req.headers['x-webhook-signature'], now())) {
        throw new HttpError(401, 'Invalid signature or expired timestamp');
      }
      let event;
      try { event = JSON.parse(body.toString('utf8')); } catch { throw new HttpError(400, 'Malformed JSON'); }
      validateEvent(event);
      metrics.attempts++;
      if (remainingFailures > 0) {
        remainingFailures--; metrics.transient_failures++;
        return send(503, { error: 'Simulated temporary failure' }, { 'retry-after': '1' });
      }
      const result = inbox.record(event, body);
      metrics[result.duplicate ? 'duplicates' : 'accepted']++;
      return send(result.duplicate ? 200 : 202, result);
    } catch (error) {
      send(error instanceof HttpError ? error.status : 500, { error: error instanceof HttpError ? error.message : 'Internal server error' });
    }
  });
  app.requestTimeout = 15000;
  app.headersTimeout = 10000;
  return app;
}
