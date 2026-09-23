import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { openInbox, createReceiver } from './receiver.js';

const secret = process.env.WEBHOOK_SECRET;
if (!secret || secret.length < 32) throw new Error('Set WEBHOOK_SECRET to a random value of at least 32 characters');
const filename = process.env.DB_PATH ?? './data/inbox.sqlite';
const port = Number(process.env.PORT ?? 3001);
const failFirst = Number(process.env.DEMO_FAIL_FIRST ?? 0);
if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('PORT must be 0 to 65535');
mkdirSync(dirname(filename), { recursive: true });
const inbox = openInbox(filename);
const app = createReceiver({ secret, inbox, failFirst });
app.listen(port, '127.0.0.1', () => console.log(JSON.stringify({ event: 'listening', url: `http://127.0.0.1:${app.address().port}` })));
for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, () => app.close(() => { inbox.close(); process.exit(0); }));
