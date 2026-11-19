#!/usr/bin/env node
/**
 * Backward-compatibility shim.
 *
 * Independent DSH review has been unified into the standalone Python skill:
 *   ~/.agents/skills/dsh-review/scripts/dsh.py
 *
 * This CLI shim forwards invocations to dsh.py (using the unified result.response
 * and compact target schema).
 */
import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const TARGET = resolve(HERE, '../../dsh-review/scripts/dsh.py');

const res = spawnSync('python3', [TARGET, ...process.argv.slice(2)], { stdio: 'inherit' });
if (res.error) {
  process.stdout.write(JSON.stringify({ error: `Failed to delegate to ${TARGET}: ${res.error.message}` }) + '\n');
  process.exit(1);
}
process.exit(res.status ?? 1);
