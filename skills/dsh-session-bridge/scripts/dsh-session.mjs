#!/usr/bin/env node
/**
 * dsh-session-bridge — read and write DeepSeek Harness (dsh) conversations by name or id.
 *
 * Talks to the running `dsh web` server's own Remote API on loopback, authenticating
 * with the host's browser-session cookie (minted from the signing secret in
 * $DSH_HOME/.credentials.yaml). No extra daemon, no browser automation.
 *
 * Commands:
 *   list    [--query TEXT] [--all] [--limit N] [--json]
 *   resolve TARGET [--json]
 *   read    TARGET [--limit N] [--all] [--after-seq N] [--tools] [--json]
 *   digest  TARGET [--chars N] [--json]
 *   questions TARGET [--all] [--json]
 *   answer  TARGET "text" | answer TARGET --select id=label [--no-cancel] [--wait]
 *   handoff FROM TO [--message TEXT] [--wait] [--timeout SEC] [--json]
 *   search  QUERY [--limit N] [--days N] [--cwd DIR] [--json]
 *   send    TARGET MESSAGE... [--mode queue|steer] [--wait] [--timeout SEC]
 *           [--dedupe-id ID] [--no-verify] [--heartbeat SEC] [--json]
 *   queue   list TARGET | steer TARGET ITEM_ID | remove TARGET ITEM_ID
 *   cancel  TARGET
 *   wait    TARGET [--after-seq N] [--timeout SEC] [--heartbeat SEC] [--json]
 *   watch   TARGET [--after-seq N] [--interval SEC] [--timeout SEC] [--json]
 *   new     [--cwd DIR] [--title TEXT] [--message TEXT] [--create-workspace] [--no-workspace] [--json]
 *   adopt   TARGET [--json]
 *   rename  TARGET TITLE
 *   alias   set NAME TARGET | list | remove NAME
 *
 * Global options: --url URL, --dsh-home DIR, --cwd DIR (ranking hint), --help
 *
 * TARGET is a conversation name (fuzzy title match), a sessionId (prefix ok), a
 * saved alias, or `latest`.
 */

import { readFileSync, writeFileSync, mkdirSync, readdirSync, existsSync, realpathSync } from 'node:fs';
import { createHash, createHmac } from 'node:crypto';
import { spawn } from 'node:child_process';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

// ---------------------------------------------------------------- small utils

const b64url = (buf) =>
  Buffer.from(buf).toString('base64').replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/u, '');

function b64urlDecode(value) {
  const padding = '='.repeat((4 - (value.length % 4)) % 4);
  return Buffer.from(value.replaceAll('-', '+').replaceAll('_', '/') + padding, 'base64');
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function fail(message, code = 1) {
  process.stderr.write(`dsh-session: ${message}\n`);
  process.exit(code);
}

function truncate(text, max) {
  const value = String(text ?? '');
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

function localTime(ms) {
  try {
    return new Date(ms).toLocaleString('sv-SE').replace('T', ' ');
  } catch {
    return String(ms);
  }
}

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const SKILL_ROOT = dirname(dirname(SCRIPT_PATH));
const STATE_DIR = process.env.DSH_BRIDGE_STATE_DIR || join(homedir(), '.agents', 'state', 'dsh-session-bridge');

// -------------------------------------------------------------- option parsing

function parseArgs(argv) {
  const options = {
    url: process.env.DSH_WEB_URL || 'http://127.0.0.1:3080',
    dshHome: process.env.DSH_HOME || join(homedir(), '.dsh'),
    cwd: process.cwd(),
    cwdExplicit: false,
    limit: 40,
    chars: 900,
    days: 0,
    interval: 2,
    heartbeat: 15,
    verify: true,
    all: false,
    tools: false,
    json: false,
    steer: false,
    mode: undefined,
    wait: false,
    noWorkspace: false,
    createWorkspace: false,
    timeout: 180,
    query: undefined,
    afterSeq: undefined,
    workspace: undefined,
    message: undefined,
    title: undefined,
    dedupeId: undefined,
    select: [],
    contract: false,
    noCancel: false,
    positional: [],
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const next = () => {
      const value = argv[++i];
      if (value === undefined) fail(`option ${arg} needs a value`);
      return value;
    };
    switch (arg) {
      case '--': options.positional.push(...argv.slice(i + 1)); i = argv.length; break;
      case '--url': options.url = next(); break;
      case '--dsh-home': options.dshHome = next(); break;
      case '--cwd': options.cwd = next(); options.cwdExplicit = true; break;
      case '--limit': options.limit = Number(next()); break;
      case '--chars': options.chars = Number(next()); break;
      case '--days': options.days = Number(next()); break;
      case '--interval': options.interval = Number(next()); break;
      case '--heartbeat': options.heartbeat = Number(next()); break;
      case '--timeout': options.timeout = Number(next()); break;
      case '--after-seq': options.afterSeq = Number(next()); break;
      case '--workspace': options.workspace = next(); break;
      case '--query': options.query = next(); break;
      case '--message': options.message = next(); break;
      case '--title': options.title = next(); break;
      case '--dedupe-id': options.dedupeId = next(); break;
      case '--select': options.select.push(next()); break;
      case '--mode': options.mode = next(); break;
      case '--contract': options.contract = true; break;
      case '--no-cancel': options.noCancel = true; break;
      case '--all': options.all = true; break;
      case '--tools': options.tools = true; break;
      case '--json': options.json = true; break;
      case '--steer': options.steer = true; break;
      case '--wait': options.wait = true; break;
      case '--no-verify': options.verify = false; break;
      case '--no-workspace': options.noWorkspace = true; break;
      case '--create-workspace': options.createWorkspace = true; break;
      case '-h': case '--help': options.help = true; break;
      default:
        if (arg.startsWith('--')) fail(`unknown option ${arg}`);
        options.positional.push(arg);
    }
  }
  return options;
}

// ------------------------------------------------------------------- transport

function loadSecret(dshHome) {
  let yaml;
  try {
    yaml = readFileSync(join(dshHome, '.credentials.yaml'), 'utf8');
  } catch (error) {
    fail(`cannot read ${join(dshHome, '.credentials.yaml')} (${error.code || error.message}). ` +
      `Set --dsh-home or $DSH_HOME to the home of the running dsh web process.`);
  }
  const match = /client-connection\/browser-session:[\s\S]*?secret:\s*([A-Za-z0-9_-]+)/.exec(yaml);
  if (!match) {
    fail('no client-connection/browser-session secret in .credentials.yaml — ' +
      'is `dsh web` running against this DSH home?');
  }
  const secret = b64urlDecode(match[1]);
  if (secret.byteLength !== 32) fail('browser-session secret has an unexpected length');
  return secret;
}

function cookieFor(authority, secret) {
  const name = 'dsh-auth-' + b64url(createHash('sha256').update(authority).digest());
  const issuedAt = Date.now();
  const body = b64url(Buffer.from(JSON.stringify({
    version: 1,
    authority,
    issuedAt,
    expiresAt: issuedAt + 30 * 24 * 60 * 60 * 1000,
  }), 'utf8'));
  const value = `v1.${body}.${b64url(createHmac('sha256', secret).update(body).digest())}`;
  return `${name}=${value}`;
}

async function makeClient(options) {
  const transportError = message => { throw new Error(message); };
  let base;
  try {
    base = new URL(options.url);
  } catch {
    transportError(`invalid --url ${options.url}`);
  }
  const authority = base.host; // exactly what the server compares the cookie against
  const secret = loadSecret(options.dshHome);
  const cookie = cookieFor(authority, secret);

  const rpc = async (endpoint, args) => {
    let response;
    try {
      response = await fetch(new URL(`/api/${endpoint}`, base), {
        method: 'POST',
        signal: AbortSignal.timeout(30000),
        headers: { 'content-type': 'application/json', cookie },
        body: JSON.stringify({
          type: 'client-request',
          rpcId: 'dsh-session-' + Math.random().toString(36).slice(2),
          method: endpoint,
          payload: { args },
        }),
      });
    } catch (error) {
      transportError(`cannot reach dsh web at ${base.origin} (${error.cause?.code || error.message}). ` +
        'Start it with `dsh web` (or point --url / $DSH_WEB_URL at the right port).');
    }
    if (response.status === 401) {
      transportError(`authentication rejected by ${base.origin} (401). The browser-session cookie did not match; ` +
        'check that --dsh-home / $DSH_HOME is the home of the running dsh web process.');
    }
    if (response.status === 403) transportError(`request refused by ${base.origin} (403): host/origin trust fence.`);
    let envelope;
    try {
      envelope = await response.json();
    } catch {
      transportError(`unexpected non-JSON reply from ${endpoint} (HTTP ${response.status}).`);
    }
    const result = envelope?.result;
    if (!result) transportError(`malformed reply from ${endpoint}: ${truncate(JSON.stringify(envelope), 300)}`);
    if (result.ok === false) {
      const detail = result.error?.details?.issues?.[0]?.message;
      const error = new Error(`${endpoint} failed: ${result.error.code}: ${result.error.message}${detail ? ` (${detail})` : ''}`);
      error.code = result.error.code;
      error.endpoint = endpoint;
      throw error;
    }
    return result.value;
  };

  return { base, rpc, dshHome: options.dshHome };
}

// ---------------------------------------------------------------- local state

function readJson(file, fallback) {
  try {
    return JSON.parse(readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
}

function writeJson(file, value) {
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(value, null, 2));
}

function loadAliases(dshHome) {
  return readJson(join(STATE_DIR, 'aliases.json'), {})[dshHome] ?? {};
}

function saveAlias(dshHome, name, sessionId) {
  const all = readJson(join(STATE_DIR, 'aliases.json'), {});
  all[dshHome] = all[dshHome] ?? {};
  all[dshHome][name] = { sessionId, addedAt: Date.now() };
  writeJson(join(STATE_DIR, 'aliases.json'), all);
}

function removeAlias(dshHome, name) {
  const all = readJson(join(STATE_DIR, 'aliases.json'), {});
  if (all[dshHome]?.[name]) {
    delete all[dshHome][name];
    writeJson(join(STATE_DIR, 'aliases.json'), all);
    return true;
  }
  return false;
}

function loadOutbox(dshHome) {
  return readJson(join(STATE_DIR, 'outbox.json'), {})[dshHome] ?? {};
}

function rememberSend(dshHome, key, record) {
  const all = readJson(join(STATE_DIR, 'outbox.json'), {});
  all[dshHome] = all[dshHome] ?? {};
  all[dshHome][key] = { ...record, at: Date.now() };
  const entries = Object.entries(all[dshHome]).sort((a, b) => b[1].at - a[1].at).slice(0, 500);
  all[dshHome] = Object.fromEntries(entries);
  writeJson(join(STATE_DIR, 'outbox.json'), all);
}

// ------------------------------------------------------------- session catalog

function archivedIds(dshHome) {
  const store = readJson(join(dshHome, 'storages', 'workspace.json'), {});
  return new Set(store?.global?.archivedSessionIds ?? []);
}

function workspaceRecords(dshHome) {
  const store = readJson(join(dshHome, 'storages', 'workspace.json'), {});
  return Object.entries(store?.tables?.workspaces ?? {}).map(([workspaceId, record]) => ({
    workspaceId,
    path: record.path,
    title: record.title,
    sessionIds: record.sessionIds ?? [],
  }));
}

async function listSessions(client) {
  const archived = archivedIds(client.dshHome);
  const value = await client.rpc('session/list', { _request: {} });
  return (value.items || []).map((item) => ({
    sessionId: item.sessionId,
    title: item.projections?.values?.title ?? '',
    cwd: item.cwd ?? '',
    running: Boolean(item.running),
    blank: Boolean(item.blank),
    archived: archived.has(item.sessionId),
    updatedAt: item.updatedAt ?? 0,
    asOfSeq: item.projections?.asOfSeq ?? 0,
    turns: item.projections?.values?.sessionStats?.turns ?? 0,
    steps: item.projections?.values?.sessionStats?.steps ?? 0,
    inbox: item.projections?.values?.inbox ?? { 'next-turn': [], 'next-step': [] },
    tokenUsage: item.projections?.values?.tokenUsage ?? null,
  }));
}

function similarity(a, b) {
  const left = [...a];
  const right = new Set([...b]);
  if (!left.length || !right.size) return 0;
  let hits = 0;
  for (const char of left) if (right.has(char)) hits++;
  return (2 * hits) / (left.length + right.size);
}

function scoreCandidate(item, query, cwd) {
  const title = (item.title || '').trim();
  const needle = query.trim();
  let score = 0;
  if (!needle) {
    score = 1;
  } else if (title === needle) {
    score = 1000;
  } else if (title.toLowerCase() === needle.toLowerCase()) {
    score = 950;
  } else if (item.sessionId === needle || item.sessionId.includes(needle)) {
    score = 900;
  } else if (title.includes(needle)) {
    score = 800 - Math.min(80, title.length - needle.length);
  } else if (needle.includes(title) && title.length >= 2) {
    score = 700;
  } else {
    score = Math.round(600 * similarity(title, needle));
  }
  if (cwd && item.cwd === cwd && score > 1) score += 60;
  if (item.running && score > 1) score += 20;
  if (item.archived && score > 1) score -= 120;
  return score;
}

function asArray(item) {
  return Array.isArray(item) ? item : [];
}

async function resolveTarget(client, target, cwd, dshHome) {
  const sessions = await listSessions(client);
  if (!sessions.length) fail('this dsh has no conversations yet');
  const byId = new Map(sessions.map((item) => [item.sessionId, item]));

  // 1. saved alias
  const aliases = loadAliases(dshHome);
  const aliasKey = Object.keys(aliases).find((name) => name.toLowerCase() === target.trim().toLowerCase());
  if (aliasKey) {
    const hit = byId.get(aliases[aliasKey].sessionId);
    if (hit) return { ...hit, score: 1100, via: `alias:${aliasKey}` };
    fail(`alias "${aliasKey}" points at ${aliases[aliasKey].sessionId}, which no longer exists. Remove it with: alias remove ${aliasKey}`);
  }

  // 2. `latest`
  if (/^(latest|last)$/i.test(target.trim())) {
    const newest = [...sessions].sort((a, b) => b.updatedAt - a.updatedAt)[0];
    return { ...newest, score: 1100, via: 'latest' };
  }

  // 3. ranked title / id match
  const ranked = sessions
    .map((item) => ({ ...item, score: scoreCandidate(item, target, cwd) }))
    .sort((a, b) => b.score - a.score || b.updatedAt - a.updatedAt);
  const top = ranked[0];
  const second = ranked[1];

  if (!top || top.score < 400) {
    const error = new Error(`no dsh conversation matches ${JSON.stringify(target)}`);
    error.candidates = ranked.slice(0, 8).map((i) => ({ sessionId: i.sessionId, title: i.title, score: i.score }));
    error.kind = 'not-found';
    throw error;
  }
  if (second && second.score >= 800 && top.score - second.score < 50) {
    const error = new Error(`"${target}" is ambiguous between ${ranked.filter((i) => i.score >= top.score - 50).length} conversations`);
    error.candidates = ranked.slice(0, 8).map((i) => ({ sessionId: i.sessionId, title: i.title, score: i.score }));
    error.kind = 'ambiguous';
    throw error;
  }
  return { ...top, via: 'title' };
}

async function resolveOrExit(client, target, options) {
  try {
    return await resolveTarget(client, target, options.cwd, options.dshHome);
  } catch (error) {
    if (options.json) {
      process.stdout.write(JSON.stringify({
        ok: false,
        error: error.kind || 'error',
        message: error.message,
        candidates: error.candidates || [],
      }, null, 2) + '\n');
      process.exit(error.kind === 'ambiguous' ? 3 : 1);
    }
    process.stderr.write(`dsh-session: ${error.message}\n`);
    if (error.candidates?.length) {
      process.stderr.write('closest conversations:\n');
      for (const item of error.candidates) {
        process.stderr.write(`  ${item.sessionId}  [${item.score}]  ${item.title || '(untitled)'}\n`);
      }
      process.stderr.write('Retry with a more exact title, save an alias, or pass the session id directly.\n');
    }
    process.exit(error.kind === 'ambiguous' ? 3 : 1);
  }
}

async function sessionState(client, sessionId) {
  const sessions = await listSessions(client);
  return sessions.find((item) => item.sessionId === sessionId);
}

async function cursorOf(client, sessionId) {
  const found = await sessionState(client, sessionId);
  return found ? found.asOfSeq : 0;
}

// ------------------------------------------------------------------- reading

async function fetchEvents(client, sessionId, throughSeq, maxMessages, beforeSeq) {
  return client.rpc('session/page', {
    request: {
      address: { kind: 'session', sessionId },
      throughSeq,
      ...(beforeSeq === undefined ? {} : { beforeSeq }),
      maxMessages,
    },
  });
}

/** Collect message events newest-first page by page, then return them in seq order. */
async function collectEvents(client, sessionId, options) {
  const cursor = await cursorOf(client, sessionId);
  const events = [];
  let throughSeq = cursor;
  let beforeSeq;
  let guard = 0;
  const budget = options.all ? 4000 : 0;
  for (;;) {
    guard++;
    if (guard > 200) break;
    const page = await fetchEvents(client, sessionId, throughSeq, options.limit, beforeSeq);
    const pageEvents = (page.records || []).filter((r) => r.type === 'event').map((r) => r.event);
    pageEvents.sort((a, b) => a.seq - b.seq);
    events.unshift(...pageEvents);
    if (!page.hasMore) break;
    const oldest = pageEvents[0]?.seq;
    if (oldest === undefined || oldest === 0) break;
    if (options.all) {
      if (events.length >= budget) break;
      throughSeq = oldest - 1;
      beforeSeq = undefined;
    } else {
      break;
    }
  }
  const unique = new Map();
  for (const event of events) unique.set(event.seq, event);
  const ordered = [...unique.values()].sort((a, b) => a.seq - b.seq);
  return options.afterSeq === undefined
    ? ordered
    : ordered.filter((event) => event.seq > options.afterSeq);
}

function textOf(content) {
  if (!Array.isArray(content)) return '';
  return content
    .filter((block) => block?.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n')
    .trim();
}

function toolLinesOf(content) {
  if (!Array.isArray(content)) return [];
  const out = [];
  for (const block of content) {
    if (block?.type === 'tool-call') {
      out.push(`[tool-call ${block.name}] ${truncate(block.arguments, 300)}`);
    } else if (block?.type === 'tool-result') {
      const text = textOf(block.content);
      out.push(`[tool-result${block.isError ? ' error' : ''}] ${truncate(text, 200)}`);
    }
  }
  return out;
}

function renderTranscript(session, events, options) {
  const lines = [];
  lines.push(`=== DSH conversation: ${session.title || '(untitled)'}`);
  lines.push(`sessionId: ${session.sessionId}${session.cwd ? `   cwd: ${session.cwd}` : ''}` +
    `${session.running ? '   [running]' : ''}`);
  lines.push('---');
  let shown = 0;
  for (const event of events) {
    if (event.type === 'user/message' && event.data?.source?.kind === 'user') {
      const text = textOf(event.data.content);
      if (!text) continue;
      lines.push(`\n[user #${event.seq} ${localTime(event.time)}]\n${text}`);
      shown++;
    } else if (event.type === 'assistant/message') {
      const message = event.data?.message;
      const text = textOf(message?.content);
      const toolLines = options.tools ? toolLinesOf(message?.content) : [];
      if (!text && !toolLines.length) continue;
      lines.push(`\n[assistant #${event.seq} ${localTime(event.time)}]\n${[text, ...toolLines].filter(Boolean).join('\n')}`);
      shown++;
    }
  }
  if (!shown) lines.push('\n(no user/assistant messages in this range — try --all or --after-seq 0)');
  return lines.join('\n');
}

// ---------------------------------------------------------------- digest

const FILE_ARG_KEYS = ['file_path', 'path', 'notebook_path', 'filePath', 'target_file'];

function filesFromArguments(raw) {
  const out = [];
  if (typeof raw !== 'string') return out;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return out;
  }
  const visit = (value, depth) => {
    if (depth > 4 || value === null || typeof value !== 'object') return;
    if (Array.isArray(value)) {
      for (const item of value) visit(item, depth + 1);
      return;
    }
    for (const [key, item] of Object.entries(value)) {
      if (FILE_ARG_KEYS.includes(key) && typeof item === 'string') out.push(item);
      else visit(item, depth + 1);
    }
  };
  visit(parsed, 0);
  return out;
}

function buildDigest(session, events, chars) {
  const realUser = events.filter((e) => e.type === 'user/message' && e.data?.source?.kind === 'user');
  const assistants = events.filter((e) => e.type === 'assistant/message');
  const lastUser = realUser.at(-1);
  const lastAssistant = assistants.at(-1);

  const actions = [];
  const files = new Set();
  for (const event of assistants) {
    const content = event.data?.message?.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (block?.type !== 'tool-call') continue;
      const paths = filesFromArguments(block.arguments);
      for (const p of paths) files.add(p);
      actions.push({
        seq: event.seq,
        name: block.name,
        target: paths[0] ?? null,
      });
    }
  }

  return {
    sessionId: session.sessionId,
    title: session.title,
    cwd: session.cwd,
    running: session.running,
    turns: session.turns,
    updatedAt: session.updatedAt,
    asOfSeq: session.asOfSeq,
    lastUserText: lastUser ? truncate(textOf(lastUser.data?.content), chars) : null,
    lastUserSeq: lastUser?.seq ?? null,
    lastAssistantText: lastAssistant ? truncate(textOf(lastAssistant.data?.message?.content), chars) : null,
    lastAssistantSeq: lastAssistant?.seq ?? null,
    recentActions: actions.slice(-12),
    filesTouched: [...files].slice(0, 40),
    pendingQuestion: pendingQuestions(events).at(-1) ?? null,
  };
}

function renderDigest(digest) {
  const lines = [];
  lines.push(`# dsh 会话交接包`);
  lines.push(`- 标题: ${digest.title || '(untitled)'}`);
  lines.push(`- sessionId: ${digest.sessionId}`);
  lines.push(`- cwd: ${digest.cwd || '-'}`);
  lines.push(`- 状态: ${digest.running ? 'running' : 'idle'} · 回合 ${digest.turns} · 最后活动 ${localTime(digest.updatedAt)} · seq ${digest.asOfSeq}`);
  lines.push(`- 最后一次用户请求 (#${digest.lastUserSeq ?? '-'}):`);
  lines.push(digest.lastUserText ? digest.lastUserText : '  (无)');
  lines.push(`- 最后一次助手回复 (#${digest.lastAssistantSeq ?? '-'}):`);
  lines.push(digest.lastAssistantText ? digest.lastAssistantText : '  (无)');
  lines.push(`- 最近动作:`);
  if (!digest.recentActions.length) lines.push('  (无)');
  for (const action of digest.recentActions) {
    lines.push(`  - #${action.seq} ${action.name}${action.target ? ` → ${action.target}` : ''}`);
  }
  lines.push(`- 涉及文件:`);
  if (!digest.filesTouched.length) lines.push('  (无)');
  for (const file of digest.filesTouched) lines.push(`  - ${file}`);
  if (digest.pendingQuestion) {
    lines.push(`- ⚠️ 正在等待回答的问题:`);
    lines.push(renderQuestions([digest.pendingQuestion]).split('\n').map((l) => `  ${l}`).join('\n'));
  }
  return lines.join('\n');
}

// --------------------------------------------- questions (ask_user_question)

function parseAskArguments(raw) {
  try {
    const parsed = JSON.parse(raw);
    return asArray(parsed?.questions).map((q) => ({
      id: q?.id ?? null,
      question: q?.question ?? '',
      header: q?.header ?? null,
      detail: q?.detail ?? null,
      multiSelect: Boolean(q?.multiSelect),
      options: asArray(q?.options).map((o) => ({ label: o?.label ?? '', description: o?.description ?? null })),
    }));
  } catch {
    return [];
  }
}

/** Durable trace of every `ask_user_question` call, with its answer/abort result if any. */
function extractQuestions(events) {
  const calls = new Map();
  for (const event of events) {
    if (event.type === 'tool/call' && event.data?.name === 'ask_user_question') {
      calls.set(event.data.callId, {
        callId: event.data.callId,
        seq: event.seq,
        questions: parseAskArguments(event.data.arguments),
        resultSeq: null,
        resultText: '',
        isError: false,
      });
    } else if (event.type === 'tool/result') {
      const callId = event.data?.message?.source?.callId;
      const call = callId ? calls.get(callId) : undefined;
      if (!call) continue;
      const block = asArray(event.data?.message?.content).find((b) => b?.type === 'tool-result');
      const text = asArray(block?.content).filter((b) => b?.type === 'text').map((b) => b.text).join('\n');
      call.resultSeq = event.seq;
      call.resultText = text;
      call.isError = Boolean(block?.isError) || /^\s*Error:/i.test(text);
    }
  }
  return [...calls.values()];
}

/** Questions with no tool/result yet — the agent is blocked waiting for a human. */
function pendingQuestions(events) {
  return extractQuestions(events).filter((call) => call.resultSeq === null);
}

function renderQuestions(calls, chars = 400) {
  const lines = [];
  for (const call of calls) {
    const status = call.resultSeq === null ? 'PENDING 等待回答' : call.isError ? 'aborted/error' : 'answered';
    lines.push(`#${call.seq} [${status}] callId=${call.callId}`);
    for (const q of call.questions) {
      lines.push(`  Q(${q.id}) ${q.question}${q.header ? `  [${q.header}]` : ''}`);
      if (q.detail) lines.push(`    detail: ${truncate(q.detail, chars)}`);
      for (const o of q.options) lines.push(`    - ${o.label}${o.description ? ` — ${o.description}` : ''}`);
    }
    if (call.resultSeq !== null) lines.push(`  answer: ${truncate(call.resultText, chars)}`);
  }
  return lines.join('\n');
}

// ------------------------------------------------------------------ sending

/** Prepended with `send --contract`: tells the dsh worker a controller is driving it. */
const WORKER_CONTRACT = [
  '你现在由外部 controller 代用户驱动，请遵守：',
  '1. 能自行判断的技术性问题（环境、命名、工具选择、重试/超时、常规约定、实现细节）直接按最佳方案继续，不要调用 ask_user_question。',
  '2. 只有真正的业务/产品决策（范围、对外行为、成本、不可逆破坏、用户偏好）才需要提问，并在提问里给出你的推荐项。',
  '3. 提问请一次性把问题、选项、你的推荐列清楚。',
].join('\n');

function normalizeLabel(value) {
  return String(value ?? '').toLowerCase().replace(/\(recommended\)/gu, '').replace(/[\s_\-—:：]+/gu, '');
}

function matchOption(text, question) {
  const needle = normalizeLabel(text);
  if (!needle) return undefined;
  return asArray(question.options).find((option) => {
    const label = normalizeLabel(option.label);
    return label === needle || label.includes(needle) || needle.includes(label);
  });
}

/** Build the controller's answer message for a pending ask_user_question batch. */
function formatAnswer(pending, selections, freeText) {
  if (!pending) return freeText || '';
  const lines = ['[controller 代答] 回答你的提问：'];
  for (const question of pending.questions) {
    const chosen = selections.find((entry) => entry.split('=')[0].trim() === question.id);
    if (chosen !== undefined) {
      const value = chosen.slice(chosen.indexOf('=') + 1).trim();
      const option = matchOption(value, question);
      lines.push(`- ${question.id}: ${option ? option.label : value}`);
      continue;
    }
    if (pending.questions.length === 1 && freeText) {
      const option = matchOption(freeText, question);
      lines.push(`- ${question.id}: ${option ? option.label : freeText}`);
      continue;
    }
    const option = freeText ? matchOption(freeText, question) : undefined;
    lines.push(`- ${question.id}: ${option ? option.label : (freeText || '(见下)')}`);
  }
  if (freeText && pending.questions.length > 1) lines.push('', freeText);
  lines.push('', '（这是 controller 在用户授权下给出的回答，请据此继续。）');
  return lines.join('\n');
}

function newRequestId() {
  return 'dsh-session-' + Date.now() + '-' + Math.random().toString(36).slice(2);
}

const FLASH_MODEL_PROVIDER = 'deepseek-official';
const FLASH_MODEL_ID = 'deepseek-flash';

async function selectFlashMax(client, sessionId) {
  const catalog = await client.rpc('session/modelCatalog', {});
  if (catalog.default?.provider !== FLASH_MODEL_PROVIDER) {
    fail(`model selection requires the ${FLASH_MODEL_PROVIDER} default route`);
  }
  const flashAvailable = catalog.groups?.some((group) =>
    group.id === FLASH_MODEL_PROVIDER && group.models?.some((entry) => entry.id === FLASH_MODEL_ID));
  if (!flashAvailable) fail(`${FLASH_MODEL_PROVIDER}/${FLASH_MODEL_ID} is not available in the current catalog`);

  const result = await client.rpc('session/selectModel', {
    request: { sessionId, provider: FLASH_MODEL_PROVIDER, model: FLASH_MODEL_ID, reasoningEffort: 'max' },
  });
  const selected = result?.selected;
  if (selected?.provider !== FLASH_MODEL_PROVIDER || selected?.model !== FLASH_MODEL_ID || selected?.reasoningEffort !== 'max') {
    fail('DSH did not confirm deepseek-official/deepseek-flash with max reasoning');
  }
  return selected;
}

async function sendPrompt(client, sessionId, text, mode, requestId) {
  // Existing sessions can retain a previously selected Pro route in their saved
  // request headers. Re-select the allowed route before every bridge prompt,
  // including cold resumes and answer/handoff/new flows.
  await selectFlashMax(client, sessionId);
  const id = requestId ?? newRequestId();
  const value = await client.rpc('session/prompt', {
    request: {
      requestId: id,
      sessionId,
      mode,
      content: [{ type: 'text', text }],
      clientTimeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    },
  });
  return { ...value, requestId: id };
}

/** Pending inbox items: `queued` = next turn, `steering` = next step of the running turn. */
function inboxItems(session) {
  const inbox = session?.inbox ?? {};
  const map = (list, placement) => asArray(list).map((message) => ({
    placement,
    id: message?.id ?? null,
    rpcId: message?.source?.rpcId ?? null,
    text: textOf(message?.content),
  }));
  return {
    queued: map(inbox['next-turn'], 'queued'),
    steering: map(inbox['next-step'], 'steering'),
  };
}

function updateQueueItem(client, sessionId, itemId, kind) {
  return client.rpc('session/updateQueue', { request: { sessionId, itemId, action: { kind } } });
}

function cancelSession(client, sessionId) {
  // DSH cancels with keepInbox: true — the running turn stops, queued messages stay.
  return client.rpc('session/cancel', { request: { sessionId } });
}

function sendMode(options) {
  const mode = options.mode ?? (options.steer ? 'steer' : 'queue');
  if (mode !== 'queue' && mode !== 'steer') fail(`--mode must be "queue" or "steer", got ${JSON.stringify(mode)}`);
  return mode;
}

function placementHint(session, mode) {
  if (!session.running) return '目标当前空闲：这条消息会直接开启一个新回合（queue/steer 此时等价）';
  return mode === 'steer'
    ? '目标正在运行：这条消息会在**当前回合的下一个 step 边界**生效（引导，不打断当前 step）'
    : '目标正在运行：这条消息**排队到当前回合结束后的下一回合**；要立刻影响当前回合请用 --steer';
}


async function verifyDelivered(client, sessionId, requestId, timeoutSeconds = 25) {
  const deadline = Date.now() + timeoutSeconds * 1000;
  while (Date.now() < deadline) {
    const cursor = await cursorOf(client, sessionId);
    if (cursor > 0) {
      const page = await fetchEvents(client, sessionId, cursor, 200);
      const found = (page.records || [])
        .filter((r) => r.type === 'event')
        .map((r) => r.event)
        .find((event) => event.type === 'user/message' && event.data?.source?.rpcId === requestId);
      if (found) return { delivered: true, seq: found.seq };
    }
    await sleep(1000);
  }
  return { delivered: false, seq: null };
}

function progressLine(client, sessionId, startedAt, lastEvent) {
  return sessionState(client, sessionId).then((state) => {
    const elapsed = Math.round((Date.now() - startedAt) / 1000);
    const steps = state?.steps ?? '?';
    const last = lastEvent ? `last #${lastEvent.seq} ${lastEvent.type}` : 'no new events';
    return `… ${elapsed}s · ${state?.running ? 'running' : 'idle'} · steps ${steps} · ${last}`;
  });
}

/**
 * Wait for the reply to one prompt. With `requestId`, the wait baseline is the
 * user message that carries it — so a turn already running when we sent does not
 * get mistaken for the answer.
 */
async function waitForReply(client, sessionId, { afterSeq, timeoutSeconds, requestId, heartbeatSeconds = 0 }) {
  const deadline = Date.now() + timeoutSeconds * 1000;
  const startedAt = Date.now();
  let lastPartial = '';
  let baseline = afterSeq;
  let anchored = requestId === undefined;
  let lastEvent = null;
  let nextHeartbeat = Date.now() + heartbeatSeconds * 1000;
  while (Date.now() < deadline) {
    const cursor = await cursorOf(client, sessionId);
    if (cursor > baseline) {
      const page = await fetchEvents(client, sessionId, cursor, 400);
      const all = (page.records || [])
        .filter((r) => r.type === 'event')
        .map((r) => r.event)
        .sort((a, b) => a.seq - b.seq);
      if (!anchored) {
        const mine = all.find((event) =>
          event.type === 'user/message' && event.data?.source?.rpcId === requestId);
        if (mine === undefined) {
          await sleep(500);
          continue;
        }
        baseline = mine.seq;
        anchored = true;
      }
      const events = all.filter((event) => event.seq > baseline);
      const texts = [];
      let ended = false;
      let lastSeq = baseline;
      for (const event of events) {
        lastSeq = Math.max(lastSeq, event.seq);
        lastEvent = event;
        if (event.type === 'assistant/message') {
          const text = textOf(event.data?.message?.content);
          if (text) texts.push(text);
        }
        if (event.type === 'turn/end') ended = true;
      }
      lastPartial = texts.join('\n\n');
      if (ended && lastPartial) return { text: lastPartial, lastSeq, complete: true };
      // The agent can block on ask_user_question indefinitely: surface it instead of waiting out the clock.
      const pending = pendingQuestions(all.filter((event) => event.seq > afterSeq)).at(-1);
      if (pending && !ended) {
        return { text: lastPartial, lastSeq, complete: false, blocked: true, question: pending };
      }
    }
    if (heartbeatSeconds > 0 && Date.now() >= nextHeartbeat) {
      process.stderr.write(`[heartbeat] ${await progressLine(client, sessionId, startedAt, lastEvent)}\n`);
      nextHeartbeat = Date.now() + heartbeatSeconds * 1000;
    }
    await sleep(2000);
  }
  return { text: lastPartial, lastSeq: null, complete: false };
}

/** Poll for newly appended events and stream them out, with liveness heartbeats. */
async function watchSession(client, sessionId, options) {
  const startedAt = Date.now();
  const deadline = startedAt + options.timeout * 1000;
  let lastSeq = options.afterSeq ?? (await cursorOf(client, sessionId));
  let lastEvent = null;
  let lastProgress = 0;
  let sawTurnEnd = false;
  const openQuestions = new Map();
  process.stderr.write(`[watch] following ${sessionId} from seq ${lastSeq}\n`);
  while (Date.now() < deadline) {
    const cursor = await cursorOf(client, sessionId);
    if (cursor > lastSeq) {
      const page = await fetchEvents(client, sessionId, cursor, 200);
      const events = (page.records || [])
        .filter((r) => r.type === 'event')
        .map((r) => r.event)
        .filter((event) => event.seq > lastSeq)
        .sort((a, b) => a.seq - b.seq);
      for (const event of events) {
        lastSeq = Math.max(lastSeq, event.seq);
        lastEvent = event;
        if (event.type === 'tool/call' && event.data?.name === 'ask_user_question') {
          openQuestions.set(event.data.callId, {
            callId: event.data.callId, seq: event.seq, questions: parseAskArguments(event.data.arguments),
          });
        } else if (event.type === 'tool/result') {
          const callId = event.data?.message?.source?.callId;
          if (callId) openQuestions.delete(callId);
        }
        if (options.json) {
          process.stdout.write(JSON.stringify({
            seq: event.seq,
            time: event.time,
            type: event.type,
            text: event.type === 'assistant/message'
              ? textOf(event.data?.message?.content)
              : event.type === 'user/message' ? textOf(event.data?.content) : undefined,
            tools: event.type === 'assistant/message' ? toolLinesOf(event.data?.message?.content) : undefined,
          }) + '\n');
        } else if (event.type === 'user/message' && event.data?.source?.kind === 'user') {
          process.stdout.write(`[#${event.seq} user]\n${truncate(textOf(event.data?.content), 2000)}\n\n`);
        } else if (event.type === 'assistant/message') {
          const text = textOf(event.data?.message?.content);
          const tools = toolLinesOf(event.data?.message?.content);
          if (text) process.stdout.write(`[#${event.seq} assistant]\n${text}\n\n`);
          for (const line of tools) process.stdout.write(`${line}\n`);
        } else if (event.type === 'turn/end') {
          sawTurnEnd = true;
        }
      }
      const pendingCall = [...openQuestions.values()].at(-1);
      if (pendingCall) {
        const payload = { type: 'question', pending: true, ...pendingCall };
        if (options.json) process.stdout.write(JSON.stringify(payload) + '\n');
        else {
          process.stdout.write(`\n[QUESTION] 对方阻塞在提问，等待回答（#${pendingCall.seq}）：\n` +
            `${renderQuestions([{ ...pendingCall, resultSeq: null, resultText: '', isError: false }])}\n`);
        }
        process.stderr.write('[watch] blocked on a question — answer it in the dsh web UI, or run `cancel` then `send` the answer\n');
        return 6;
      }
    }
    if (!options.json && Date.now() - lastProgress > options.interval * 1000) {
      process.stderr.write(`[watch] ${await progressLine(client, sessionId, startedAt, lastEvent)}\n`);
      lastProgress = Date.now();
    }
    if (sawTurnEnd) {
      const state = await sessionState(client, sessionId);
      if (!state?.running) {
        process.stderr.write('[watch] turn complete\n');
        return 0;
      }
    }
    await sleep(options.interval * 1000);
  }
  process.stderr.write('[watch] timeout\n');
  return 4;
}

// -------------------------------------------------------------------- search

function sessionLogFiles(dshHome) {
  const root = join(dshHome, 'sessions');
  const map = new Map();
  let projects;
  try {
    projects = readdirSync(root);
  } catch {
    return map;
  }
  for (const project of projects) {
    let entries;
    try {
      entries = readdirSync(join(root, project));
    } catch {
      continue;
    }
    for (const sessionId of entries) {
      const dir = join(root, project, sessionId);
      for (const name of ['session.v3.jsonl.zstd', 'session.jsonl.zstd']) {
        const file = join(dir, name);
        if (existsSync(file)) {
          map.set(sessionId, file);
          break;
        }
      }
    }
  }
  return map;
}

function spawnCollect(command, args) {
  return new Promise((resolve) => {
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'ignore'] });
    const chunks = [];
    child.stdout.on('data', (chunk) => chunks.push(chunk));
    child.on('error', (error) => resolve({ error }));
    child.on('close', (code) => resolve({ code, buffer: Buffer.concat(chunks) }));
  });
}

async function mapPool(items, limit, fn) {
  const results = new Array(items.length);
  let next = 0;
  const workers = Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, async () => {
    for (;;) {
      const index = next++;
      if (index >= items.length) return;
      results[index] = await fn(items[index], index);
    }
  });
  await Promise.all(workers);
  return results;
}

function messageTextOf(event) {
  if (event.type === 'user/message') {
    return { role: 'user', text: textOf(event.data?.content), injected: event.data?.source?.kind !== 'user' };
  }
  if (event.type === 'assistant/message') {
    return { role: 'assistant', text: textOf(event.data?.message?.content), injected: false };
  }
  return null;
}

async function searchLocal(client, query, options) {
  const needle = query.toLowerCase();
  const files = sessionLogFiles(options.dshHome);
  const sessions = await listSessions(client);
  const byId = new Map(sessions.map((item) => [item.sessionId, item]));
  const candidates = [...files.entries()].filter(([sessionId]) => {
    const meta = byId.get(sessionId);
    if (options.days > 0 && meta && Date.now() - meta.updatedAt > options.days * 86400000) return false;
    if (options.cwdExplicit && meta && !(meta.cwd ?? '').startsWith(options.cwd)) return false;
    return true;
  });

  const zstdAvailable = await spawnCollect('zstd', ['--version']).then((r) => !r.error && r.code === 0);
  const results = await mapPool(candidates, 6, async ([sessionId, file]) => {
    let lines;
    if (zstdAvailable) {
      const out = await spawnCollect('zstd', ['-d', '-c', file]);
      if (out.error || !out.buffer) return null;
      lines = out.buffer.toString('utf8').split('\n');
    } else {
      // Fallback: API window for this session (recent messages only).
      const meta = byId.get(sessionId);
      if (!meta) return null;
      const page = await fetchEvents(client, sessionId, meta.asOfSeq, 60).catch(() => null);
      lines = (page?.records ?? []).filter((r) => r.type === 'event').map((r) => JSON.stringify(r.event));
    }
    let matches = 0;
    let snippet = null;
    let snippetSeq = null;
    for (const line of lines) {
      if (!line) continue;
      let event;
      try {
        event = JSON.parse(line);
      } catch {
        continue;
      }
      const info = messageTextOf(event);
      if (!info || !info.text || info.injected) continue;
      const at = info.text.toLowerCase().indexOf(needle);
      if (at < 0) continue;
      matches++;
      if (!snippet) {
        const from = Math.max(0, at - 60);
        snippet = `${from > 0 ? '…' : ''}${info.text.slice(from, at + needle.length + 100)}`;
        snippetSeq = event.seq;
      }
    }
    if (!matches) return null;
    const meta = byId.get(sessionId);
    return {
      sessionId,
      title: meta?.title ?? '',
      cwd: meta?.cwd ?? '',
      updatedAt: meta?.updatedAt ?? 0,
      running: meta?.running ?? false,
      archived: meta?.archived ?? false,
      matches,
      snippet,
      snippetSeq,
    };
  });

  return results
    .filter(Boolean)
    .sort((a, b) => b.matches - a.matches || b.updatedAt - a.updatedAt)
    .slice(0, options.limit);
}

// ----------------------------------------------------------- workspaces

function canonical(path) {
  try {
    return realpathSync(path);
  } catch {
    return path;
  }
}

function findWorkspace(dshHome, path) {
  const wanted = canonical(path);
  return workspaceRecords(dshHome).find((record) => canonical(record.path) === wanted);
}

async function ensureWorkspace(client, dshHome, path) {
  const target = canonical(path);
  const found = findWorkspace(dshHome, target);
  if (found) return { workspaceId: found.workspaceId, created: false };
  const created = await client.rpc('workspace/create', { request: { path: target } });
  return { workspaceId: created.workspace.workspaceId, created: true };
}

// ---------------------------------------------------------------------- main

async function main() {
  const [command, ...rest] = process.argv.slice(2);
  const options = parseArgs(rest);

  if (!command || command === 'help' || command === '--help' || command === '-h' || options.help) {
    const source = readFileSync(SCRIPT_PATH, 'utf8').split('\n');
    const header = [];
    for (const line of source.slice(1)) {
      if (line.trim() === '*/') break;
      header.push(line.replace(/^\s*\* ?/u, ''));
    }
    process.stdout.write(header.join('\n').replace(/^\/\*\*\n?/u, '') + '\n');
    return;
  }

  const client = await makeClient(options);

  if (command === 'list') {
    let sessions = await listSessions(client);
    if (!options.all) sessions = sessions.filter((item) => !item.archived);
    if (options.query) {
      sessions = sessions
        .map((item) => ({ ...item, score: scoreCandidate(item, options.query, options.cwd) }))
        .filter((item) => item.score >= 300)
        .sort((a, b) => b.score - a.score || b.updatedAt - a.updatedAt);
    } else {
      sessions.sort((a, b) => b.updatedAt - a.updatedAt);
    }
    if (options.limit) sessions = sessions.slice(0, options.limit);
    if (options.json) {
      process.stdout.write(JSON.stringify(sessions, null, 2) + '\n');
    } else if (!sessions.length) {
      process.stdout.write('(no conversations)\n');
    } else {
      for (const item of sessions) {
        const flag = item.running ? '●' : item.archived ? '×' : '○';
        process.stdout.write(`${flag} ${item.sessionId}  ${localTime(item.updatedAt)}  ${item.cwd || '-'}\n    ${item.title || '(untitled)'}\n`);
      }
    }
    return;
  }

  if (command === 'resolve') {
    const target = options.positional[0];
    if (!target) fail('usage: resolve TARGET');
    const session = await resolveOrExit(client, target, options);
    if (options.json) process.stdout.write(JSON.stringify({ ok: true, ...session }, null, 2) + '\n');
    else process.stdout.write(`${session.sessionId}\n${session.title}\n${session.cwd}\n`);
    return;
  }

  if (command === 'read') {
    const target = options.positional[0];
    if (!target) fail('usage: read TARGET [--limit N] [--all] [--after-seq N] [--tools] [--json]');
    const session = await resolveOrExit(client, target, options);
    const events = await collectEvents(client, session.sessionId, options);
    if (options.json) {
      process.stdout.write(JSON.stringify({
        ok: true,
        sessionId: session.sessionId,
        title: session.title,
        cwd: session.cwd,
        running: session.running,
        messages: events
          .filter((event) => event.type === 'user/message' || event.type === 'assistant/message')
          .map((event) => ({
            seq: event.seq,
            time: event.time,
            type: event.type,
            role: event.type === 'user/message' ? 'user' : 'assistant',
            text: textOf(event.type === 'user/message' ? event.data?.content : event.data?.message?.content),
            injected: event.type === 'user/message' && event.data?.source?.kind !== 'user',
          })),
      }, null, 2) + '\n');
      return;
    }
    process.stdout.write(renderTranscript(session, events, options) + '\n');
    return;
  }

  if (command === 'digest') {
    const target = options.positional[0];
    if (!target) fail('usage: digest TARGET [--chars N] [--json]');
    const session = await resolveOrExit(client, target, options);
    const events = await collectEvents(client, session.sessionId, { limit: 80, all: false, afterSeq: undefined });
    const digest = buildDigest(session, events, options.chars);
    if (options.json) process.stdout.write(JSON.stringify({ ok: true, ...digest }, null, 2) + '\n');
    else process.stdout.write(renderDigest(digest) + '\n');
    return;
  }

  if (command === 'questions') {
    const target = options.positional[0];
    if (!target) fail('usage: questions TARGET [--all] [--limit N] [--json]');
    const session = await resolveOrExit(client, target, options);
    const events = await collectEvents(client, session.sessionId, {
      limit: options.limit, all: options.all, afterSeq: undefined,
    });
    const calls = extractQuestions(events);
    const pending = calls.filter((call) => call.resultSeq === null);
    if (options.json) {
      process.stdout.write(JSON.stringify({
        ok: true, sessionId: session.sessionId, running: session.running, pending, calls,
      }, null, 2) + '\n');
      if (pending.length) process.exit(6);
      return;
    }
    if (!calls.length) {
      process.stdout.write('(no ask_user_question calls in this window — try --all)\n');
      return;
    }
    process.stdout.write(`${session.running ? 'running' : 'idle'} · pending ${pending.length} · total ${calls.length}\n`);
    process.stdout.write(renderQuestions(calls) + '\n');
    if (pending.length) {
      process.stdout.write('\n回答方式：在 dsh web 打开该会话直接回答；或 `cancel` 后用 `send` 把答案发过去。\n');
      process.exit(6);
    }
    return;
  }

  if (command === 'answer') {
    const target = options.positional[0];
    const freeText = (options.message ?? options.positional.slice(1).join(' ')).trim();
    if (!target || (!freeText && !options.select.length)) {
      fail('usage: answer TARGET "answer text" | answer TARGET --select id=label [--select ...] [--no-cancel] [--wait]');
    }
    const session = await resolveOrExit(client, target, options);
    const events = await collectEvents(client, session.sessionId, { limit: 80, all: false, afterSeq: undefined });
    const pending = pendingQuestions(events).at(-1);
    const body = formatAnswer(pending, options.select, freeText);
    let cancelled = false;
    if (pending && !options.noCancel) {
      await cancelSession(client, session.sessionId);
      cancelled = true;
      await sleep(1500); // let the abort land before queueing the answer
    }
    const before = await cursorOf(client, session.sessionId);
    const sent = await sendPrompt(client, session.sessionId, body, 'queue');
    const output = {
      ok: true,
      sessionId: session.sessionId,
      title: session.title,
      answeredCallId: pending?.callId ?? null,
      answeredQuestions: pending ? pending.questions.map((q) => q.id) : [],
      cancelledBlockingTurn: cancelled,
      sent: body,
      requestId: sent.requestId,
    };
    if (options.wait) {
      const reply = await waitForReply(client, session.sessionId, {
        afterSeq: before, timeoutSeconds: options.timeout, requestId: sent.requestId, heartbeatSeconds: options.heartbeat,
      });
      output.complete = reply.complete;
      output.reply = reply.text;
      if (reply.blocked) {
        output.blocked = true;
        output.question = reply.question;
      }
    }
    if (options.json) {
      process.stdout.write(JSON.stringify(output, null, 2) + '\n');
    } else {
      process.stdout.write(pending
        ? `answered ${pending.callId} (${pending.questions.map((q) => q.id).join(', ')})${cancelled ? ' · blocking turn cancelled' : ''}\n`
        : 'no pending question — sent the text as a normal message\n');
      if (options.wait && output.reply) process.stdout.write(`\n[reply]\n${output.reply}\n`);
      else if (output.blocked) process.stdout.write('\n[QUESTION] 又提出了新问题，参见 question 字段\n');
    }
    if (output.blocked) process.exit(6);
    return;
  }

  if (command === 'handoff') {
    const [from, to] = options.positional;
    if (!from || !to) fail('usage: handoff FROM TO [--message TEXT] [--wait]');
    const source = await resolveOrExit(client, from, options);
    const events = await collectEvents(client, source.sessionId, { limit: 80, all: false, afterSeq: undefined });
    const digest = buildDigest(source, events, options.chars);
    const text = [
      renderDigest(digest),
      options.message ? `\n- 后续任务: ${options.message}` : '',
      `\n请基于以上上下文继续。`,
    ].join('\n');
    const targetSession = await resolveOrExit(client, to, options);
    const before = await cursorOf(client, targetSession.sessionId);
    const sent = await sendPrompt(client, targetSession.sessionId, text, sendMode(options));
    const output = { ok: true, from: source.sessionId, to: targetSession.sessionId, sent: true, requestId: sent.requestId };
    if (options.verify) Object.assign(output, await verifyDelivered(client, targetSession.sessionId, sent.requestId));
    if (options.wait) Object.assign(output, await waitForReply(client, targetSession.sessionId, {
      afterSeq: before, timeoutSeconds: options.timeout, requestId: sent.requestId, heartbeatSeconds: options.heartbeat,
    }));
    if (options.json) process.stdout.write(JSON.stringify(output, null, 2) + '\n');
    else {
      process.stdout.write(`handoff ${source.title || source.sessionId} → ${targetSession.title || targetSession.sessionId}\n`);
      if (options.wait && output.text) process.stdout.write(`\n[reply]\n${output.text}\n`);
    }
    return;
  }

  if (command === 'search') {
    const query = options.positional[0] ?? options.query;
    if (!query) fail('usage: search QUERY [--limit N] [--days N] [--cwd DIR] [--json]');
    const started = Date.now();
    const results = await searchLocal(client, query, options);
    if (options.json) {
      process.stdout.write(JSON.stringify({ ok: true, query, tookMs: Date.now() - started, results }, null, 2) + '\n');
      return;
    }
    if (!results.length) {
      process.stdout.write(`(no matches for ${JSON.stringify(query)})\n`);
      return;
    }
    for (const item of results) {
      process.stdout.write(`● ${item.sessionId}  ${item.title || '(untitled)'}  (${item.matches} 处)\n`);
      process.stdout.write(`    ${item.cwd || '-'}  ${localTime(item.updatedAt)}\n`);
      process.stdout.write(`    …${item.snippet}…\n`);
    }
    return;
  }

  if (command === 'send') {
    const target = options.positional[0];
    let message = options.message ?? options.positional.slice(1).join(' ');
    if (!target || !message) fail('usage: send TARGET MESSAGE... [--mode queue|steer] [--wait] [--dedupe-id ID] [--no-verify] [--contract]');
    if (options.contract) message = `${WORKER_CONTRACT}\n\n---\n\n${message}`;
    const session = await resolveOrExit(client, target, options);
    const mode = sendMode(options);
    const before = await cursorOf(client, session.sessionId);

    let requestId;
    let deduped = false;
    if (options.dedupeId) {
      const key = `${session.sessionId}|${options.dedupeId}`;
      const known = loadOutbox(options.dshHome)[key];
      if (known) {
        requestId = known.requestId;
        deduped = true;
      }
    }
    const sent = await sendPrompt(client, session.sessionId, message, mode, requestId);
    if (options.dedupeId) {
      rememberSend(options.dshHome, `${session.sessionId}|${options.dedupeId}`, { requestId: sent.requestId, sessionId: session.sessionId });
    }

    const output = {
      ok: true,
      sessionId: session.sessionId,
      title: session.title,
      sent: message,
      mode,
      targetRunning: session.running,
      placement: mode === 'steer' ? 'next-step' : 'next-turn',
      hint: placementHint(session, mode),
      requestId: sent.requestId,
      deduped,
    };
    if (options.verify) Object.assign(output, await verifyDelivered(client, session.sessionId, sent.requestId));
    if (options.wait) {
      const reply = await waitForReply(client, session.sessionId, {
        afterSeq: before, timeoutSeconds: options.timeout, requestId: sent.requestId, heartbeatSeconds: options.heartbeat,
      });
      output.complete = reply.complete;
      output.reply = reply.text;
      output.replySeq = reply.lastSeq;
      if (reply.blocked) {
        output.blocked = true;
        output.question = reply.question;
      }
    }
    if (options.json) {
      process.stdout.write(JSON.stringify(output, null, 2) + '\n');
    } else {
      process.stdout.write(`sent to ${session.title || session.sessionId} (${mode}${session.running ? ', target running' : ', target idle'})` +
        `${deduped ? ' [deduped]' : ''}${options.verify ? (output.delivered ? ' · delivered' : ' · NOT confirmed') : ''}\n`);
      process.stdout.write(`${output.hint}\n`);
      if (output.blocked) {
        process.stdout.write(`\n[QUESTION] 对方阻塞在提问，等待回答：\n${renderQuestions([output.question])}\n` +
          '请在 dsh web 界面里打开该会话回答，或：cancel 后用 send 把答案发过去。\n');
      } else if (options.wait && output.reply) process.stdout.write(`\n[reply]\n${output.reply}\n`);
      else if (options.wait) process.stdout.write('(no completed reply before timeout)\n');
    }
    if (options.verify && output.delivered === false) process.exit(5);
    if (output.blocked) process.exit(6);
    return;
  }

  if (command === 'queue') {
    const sub = options.positional[0];
    const hasSub = ['list', 'steer', 'remove', 'rm'].includes(sub);
    const action = hasSub ? sub : 'list';
    const target = hasSub ? options.positional[1] : options.positional[0];
    const itemId = hasSub ? options.positional[2] : undefined;
    if (!target) fail('usage: queue list TARGET | queue steer TARGET ITEM_ID | queue remove TARGET ITEM_ID');
    const session = await resolveOrExit(client, target, options);
    if (action === 'list') {
      const state = await sessionState(client, session.sessionId);
      const items = inboxItems(state);
      if (options.json) {
        process.stdout.write(JSON.stringify({ ok: true, sessionId: session.sessionId, running: state?.running ?? false, ...items }, null, 2) + '\n');
        return;
      }
      process.stdout.write(`${state?.running ? 'running' : 'idle'} · queued(下一回合) ${items.queued.length} · steering(下一步) ${items.steering.length}\n`);
      for (const item of items.queued) process.stdout.write(`  [queued]   ${item.id}  ${truncate(item.text, 160)}\n`);
      for (const item of items.steering) process.stdout.write(`  [steering] ${item.id}  ${truncate(item.text, 160)}\n`);
      if (!items.queued.length && !items.steering.length) process.stdout.write('  (inbox empty)\n');
      return;
    }
    if (!itemId) fail(`usage: queue ${action} TARGET ITEM_ID  (get ITEM_ID from: queue list TARGET)`);
    const kind = action === 'steer' ? 'steer' : 'remove';
    await updateQueueItem(client, session.sessionId, itemId, kind);
    process.stdout.write(kind === 'steer'
      ? `promoted ${itemId} to steering (生效于当前回合下一步)\n`
      : `removed ${itemId} from the queue\n`);
    return;
  }

  if (command === 'model') {
    // model                         -> print model catalog (providers/models/efforts)
    // model TARGET MODEL [EFFORT]   -> select model + reasoning effort for a session
    //   Only the configured DeepSeek Flash route at max effort is permitted.
    const [target, model, effort] = options.positional;
    if (!target) {
      const catalog = await client.rpc('session/modelCatalog', {});
      process.stdout.write(JSON.stringify(catalog, null, 2) + '\n');
      return;
    }
    if (!model) fail('usage: model TARGET MODEL [EFFORT]');
    if (model !== 'deepseek-flash') fail('model selection is locked to deepseek-flash');
    if (effort !== undefined && effort !== 'max') fail('reasoning effort is locked to max');
    const session = await resolveOrExit(client, target, options);
    process.stdout.write(JSON.stringify(await selectFlashMax(client, session.sessionId)) + '\n');
    return;
  }

  if (command === 'cancel') {
    const target = options.positional[0];
    if (!target) fail('usage: cancel TARGET');
    const session = await resolveOrExit(client, target, options);
    await cancelSession(client, session.sessionId);
    process.stdout.write(`cancelled current turn on ${session.title || session.sessionId} (queued messages are kept)\n`);
    return;
  }

  if (command === 'wait') {
    const target = options.positional[0];
    if (!target) fail('usage: wait TARGET [--after-seq N] [--timeout SEC] [--heartbeat SEC]');
    const session = await resolveOrExit(client, target, options);
    const after = options.afterSeq ?? 0;
    const reply = await waitForReply(client, session.sessionId, {
      afterSeq: after, timeoutSeconds: options.timeout, heartbeatSeconds: options.heartbeat,
    });
    if (options.json) {
      process.stdout.write(JSON.stringify({
        ok: true,
        sessionId: session.sessionId,
        complete: reply.complete,
        blocked: Boolean(reply.blocked),
        question: reply.question ?? null,
        reply: reply.text,
      }, null, 2) + '\n');
    } else if (reply.blocked) {
      process.stdout.write(`[QUESTION] 对方阻塞在提问，等待回答：\n${renderQuestions([reply.question])}\n` +
        '请在 dsh web 界面里打开该会话回答，或：cancel 后用 send 把答案发过去。\n');
    } else {
      process.stdout.write(reply.text ? reply.text + '\n' : '(no reply before timeout)\n');
    }
    if (reply.blocked) process.exit(6);
    if (!reply.complete) process.exit(4);
    return;
  }

  if (command === 'watch') {
    const target = options.positional[0];
    if (!target) fail('usage: watch TARGET [--after-seq N] [--interval SEC] [--timeout SEC] [--json]');
    const session = await resolveOrExit(client, target, options);
    process.exit(await watchSession(client, session.sessionId, options));
  }

  if (command === 'new') {
    const cwd = canonical(options.cwd);
    let workspaceId = options.workspace;
    let createdWorkspace = false;
    if (!workspaceId && !options.noWorkspace) {
      // Registering a Workspace is a durable, user-visible decision. External
      // agents must not turn every scratch cwd into a sidebar folder, so only
      // attach to an existing Workspace unless --create-workspace is explicit.
      const existing = findWorkspace(options.dshHome, cwd);
      if (existing) {
        workspaceId = existing.workspaceId;
      } else if (options.createWorkspace) {
        const ensured = await ensureWorkspace(client, options.dshHome, cwd);
        workspaceId = ensured.workspaceId;
        createdWorkspace = ensured.created;
      }
    }
    const request = workspaceId ? { workspaceId } : { cwd };
    const created = await client.rpc('session/create', { request });
    if (options.title) {
      await client.rpc('session/rename', { request: { sessionId: created.sessionId, title: options.title } });
    }
    if (options.message) {
      await sendPrompt(client, created.sessionId, options.message, 'queue');
    }
    if (options.json) {
      process.stdout.write(JSON.stringify({
        ok: true,
        sessionId: created.sessionId,
        workspaceId: workspaceId ?? null,
        workspaceCreated: createdWorkspace,
        visibleInGui: Boolean(workspaceId),
        agentPreset: created.agentPreset,
      }, null, 2) + '\n');
    } else {
      process.stdout.write(`${created.sessionId}\n`);
      process.stderr.write(workspaceId
        ? `attached to workspace ${workspaceId}${createdWorkspace ? ' (created)' : ''} —可见于 web 侧边栏\n`
        : 'created without a workspace — hidden from the web sidebar ' +
          '(use --create-workspace to register one, or adopt it later)\n');
    }
    return;
  }

  if (command === 'adopt') {
    const target = options.positional[0];
    if (!target) fail('usage: adopt TARGET');
    const session = await resolveOrExit(client, target, options);
    if (!session.cwd) fail(`session ${session.sessionId} has no cwd recorded; cannot place it in a workspace`);
    const { workspaceId, created } = await ensureWorkspace(client, options.dshHome, session.cwd);
    try {
      await client.rpc('session/create', { request: { workspaceId, sessionId: session.sessionId } });
    } catch (error) {
      if (error.code === 'session/create failed' || /cwd/i.test(error.message)) {
        fail(`could not attach ${session.sessionId} to workspace ${workspaceId}: ${error.message}. ` +
          'The session cwd may not be canonical (e.g. /tmp vs /private/tmp).');
      }
      throw error;
    }
    if (options.json) {
      process.stdout.write(JSON.stringify({ ok: true, sessionId: session.sessionId, workspaceId, workspaceCreated: created }, null, 2) + '\n');
    } else {
      process.stdout.write(`attached ${session.sessionId} to workspace ${workspaceId}${created ? ' (created)' : ''}\n`);
    }
    return;
  }

  if (command === 'rename') {
    const [target, ...titleParts] = options.positional;
    const title = options.title ?? titleParts.join(' ');
    if (!target || !title) fail('usage: rename TARGET TITLE');
    const session = await resolveOrExit(client, target, options);
    const renamed = await client.rpc('session/rename', { request: { sessionId: session.sessionId, title } });
    if (options.json) process.stdout.write(JSON.stringify({ ok: true, sessionId: session.sessionId, title: renamed.title }, null, 2) + '\n');
    else process.stdout.write(`renamed ${session.sessionId} → ${renamed.title}\n`);
    return;
  }

  if (command === 'alias') {
    const sub = options.positional[0];
    if (sub === 'set') {
      const [, name, target] = options.positional;
      if (!name || !target) fail('usage: alias set NAME TARGET');
      const session = await resolveOrExit(client, target, options);
      saveAlias(options.dshHome, name, session.sessionId);
      process.stdout.write(`alias ${name} → ${session.sessionId} (${session.title || 'untitled'})\n`);
      return;
    }
    if (sub === 'remove' || sub === 'rm') {
      const name = options.positional[1];
      if (!name) fail('usage: alias remove NAME');
      const removed = removeAlias(options.dshHome, name);
      process.stdout.write(removed ? `removed alias ${name}\n` : `no such alias ${name}\n`);
      return;
    }
    if (!sub || sub === 'list') {
      const aliases = loadAliases(options.dshHome);
      if (options.json) {
        process.stdout.write(JSON.stringify({ ok: true, aliases }, null, 2) + '\n');
        return;
      }
      const names = Object.keys(aliases);
      if (!names.length) {
        process.stdout.write('(no aliases)\n');
        return;
      }
      const sessions = await listSessions(client);
      const byId = new Map(sessions.map((item) => [item.sessionId, item]));
      for (const name of names) {
        const meta = byId.get(aliases[name].sessionId);
        process.stdout.write(`${name} → ${aliases[name].sessionId}  ${meta ? meta.title : '(missing session)'}\n`);
      }
      return;
    }
    fail('usage: alias set NAME TARGET | alias list | alias remove NAME');
  }

  fail(`unknown command ${command} (try: list, resolve, read, digest, questions, answer, handoff, search, send, queue, cancel, wait, watch, new, adopt, rename, alias)`);
}

// Reuse this transport in the persistent review monitor without running the CLI.
export { makeClient, listSessions, sessionState, fetchEvents, collectEvents, textOf,
  sendPrompt, verifyDelivered, ensureWorkspace, canonical, cancelSession, pendingQuestions, inboxItems };

if (process.argv[1] && canonical(process.argv[1]) === canonical(SCRIPT_PATH)) {
  main().catch((error) => fail(error?.message || String(error)));
}
