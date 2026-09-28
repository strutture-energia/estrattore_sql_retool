#!/usr/bin/env node
/**
 * Sincronizza in una cache locale TUTTE le pagine Retool (app + moduli) e la Query Library.
 *
 * Uso:
 *   node sync_retool.js <cache_dir> [--full]
 *   node sync_retool.js --stampa-credenziali     righe RETOOL_* da copiare nel .env di un altro host
 *
 * Scrive in <cache_dir>:
 *   pages.json          elenco completo delle pagine: [{uuid, nome, cartella, tipo, aggiornata_il}]
 *   pages/<uuid>.json   export JSON di ogni pagina
 *   manifest.json       {uuid: updatedAt} delle pagine scaricate con successo
 *   errors.json         {uuid: messaggio} delle pagine non scaricate in questa esecuzione
 *   query_library.json  {uuid: {nome, saveId, template}} dalla Query Library
 *
 * Download incrementale: una pagina viene riscaricata solo se il suo `updatedAt` su Retool
 * differisce da quello in manifest.json (o se il file manca). `--full` riscarica tutto.
 *
 * Credenziali (sessione Retool: cookie accessToken + token XSRF), in ordine di priorità:
 *   1. variabili d'ambiente RETOOL_HOST, RETOOL_ACCESS_TOKEN, RETOOL_XSRF_TOKEN
 *      (le passa estrattore.py leggendole dal .env) — per server senza browser/keyring;
 *   2. keyring di sistema del vecchio `retool-cli` (npm i -g retool-cli, `retool login`).
 * Il nuovo `@tryretool/cli` (`retool auth login`) NON è supportato: usa un token OAuth diverso.
 */

'use strict';

const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const CONCURRENCY = 4;
const MAX_ATTEMPTS = 4;
const EXPORT_TIMEOUT_MS = 180000;
const REQUEST_TIMEOUT_MS = 60000;
// Dopo N errori di autorizzazione consecutivi la sessione è quasi certamente scaduta.
const MAX_CONSECUTIVE_AUTH_ERRORS = 5;

function fail(msg, code = 1) {
  process.stderr.write(`ERRORE: ${msg}\n`);
  process.exit(code);
}

function log(msg) {
  process.stderr.write(`${msg}\n`);
}

// ============================================================
// Credenziali
// ============================================================

function normalizeOrigin(host) {
  const h = host.trim().replace(/\/+$/, '');
  return /^https?:\/\//.test(h) ? h : `https://${h}`;
}

// Legge la sessione salvata da `retool login` del vecchio retool-cli (keyring di sistema).
// Il pacchetto si cerca nella root globale di npm e non tramite `which retool`: il binario
// `retool` può essere quello del nuovo @tryretool/cli, che ha lo stesso nome.
function credentialsFromRetoolCli() {
  const candidates = [];
  try {
    candidates.push(path.join(execSync('npm root -g', { encoding: 'utf8' }).trim(), 'retool-cli'));
  } catch (_) { /* npm non disponibile */ }
  try {
    const retoolBin = execSync('which retool', { encoding: 'utf8' }).trim();
    candidates.push(path.resolve(path.dirname(retoolBin), '..', 'lib', 'node_modules', 'retool-cli'));
  } catch (_) { /* retool non nel PATH */ }

  const dir = candidates.find((d) => fs.existsSync(path.join(d, 'lib', 'utils', 'credentials.js')));
  if (!dir) return null;
  let c;
  try {
    c = require(path.join(dir, 'lib/utils/credentials')).getCredentials();
  } catch (_) {
    return null; // keyring non disponibile (tipico su server via SSH)
  }
  if (!c || !c.accessToken || !c.xsrf || !c.origin) return null;
  return { origin: normalizeOrigin(c.origin), accessToken: c.accessToken, xsrf: c.xsrf, source: 'keyring' };
}

function loadCredentials() {
  const { RETOOL_HOST, RETOOL_ACCESS_TOKEN, RETOOL_XSRF_TOKEN } = process.env;
  if (RETOOL_HOST || RETOOL_ACCESS_TOKEN || RETOOL_XSRF_TOKEN) {
    if (!(RETOOL_HOST && RETOOL_ACCESS_TOKEN && RETOOL_XSRF_TOKEN)) {
      fail('Nel .env servono tutte e tre: RETOOL_HOST, RETOOL_ACCESS_TOKEN, RETOOL_XSRF_TOKEN', 3);
    }
    return {
      origin: normalizeOrigin(RETOOL_HOST),
      accessToken: RETOOL_ACCESS_TOKEN.trim(),
      xsrf: RETOOL_XSRF_TOKEN.trim(),
      source: 'env',
    };
  }
  const c = credentialsFromRetoolCli();
  if (!c) {
    fail('Credenziali Retool non trovate. Imposta RETOOL_HOST, RETOOL_ACCESS_TOKEN e RETOOL_XSRF_TOKEN ' +
         'nel .env, oppure (su un PC con browser) installa retool-cli ed esegui `retool login`.', 3);
  }
  return c;
}

function sessionExpiredMessage(credentials) {
  return credentials.source === 'env'
    ? 'Sessione Retool scaduta o non valida: aggiorna RETOOL_ACCESS_TOKEN e RETOOL_XSRF_TOKEN nel .env.'
    : 'Sessione Retool scaduta: esegui `retool login` e riprova.';
}

// ============================================================
// HTTP (fetch nativo di Node >= 18)
// ============================================================

class HttpError extends Error {
  constructor(status, url) {
    super(`HTTP ${status}`);
    this.status = status;
    this.url = url;
  }
}

function makeClient(credentials) {
  const headers = {
    'x-xsrf-token': credentials.xsrf,
    cookie: `accessToken=${credentials.accessToken};`,
  };

  async function request(method, urlPath, { timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
    const url = `${credentials.origin}${urlPath}`;
    const res = await fetch(url, {
      method,
      headers: method === 'POST' ? { ...headers, 'content-type': 'application/json' } : headers,
      body: method === 'POST' ? '{}' : undefined,
      redirect: 'manual', // una sessione non valida porta a un redirect verso il login
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (res.status >= 300 && res.status < 400) throw new HttpError(401, url);
    if (!res.ok) throw new HttpError(res.status, url);
    return res.text();
  }

  return {
    getJson: async (urlPath) => JSON.parse(await request('GET', urlPath)),
    postText: (urlPath, opts) => request('POST', urlPath, opts),
  };
}

function isAuthError(err) {
  return err instanceof HttpError && (err.status === 401 || err.status === 403);
}

function isRetryable(err) {
  if (err instanceof HttpError) return err.status === 429 || (err.status >= 500 && err.status < 600);
  // Errori di rete e timeout (fetch lancia TypeError / AbortError / TimeoutError)
  return err instanceof TypeError || err.name === 'AbortError' || err.name === 'TimeoutError';
}

function describe(err) {
  if (err instanceof HttpError) return `HTTP ${err.status}`;
  return (err.cause && err.cause.code) || err.name || err.message;
}

// ============================================================
// Cache
// ============================================================

// Scrittura atomica: un'interruzione non lascia mai file JSON troncati in cache.
function writeJsonAtomic(file, data) {
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, typeof data === 'string' ? data : JSON.stringify(data, null, 2));
  fs.renameSync(tmp, file);
}

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (_) {
    return fallback;
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ============================================================
// Main
// ============================================================

async function main() {
  const args = process.argv.slice(2);

  if (args.includes('--stampa-credenziali')) {
    const c = credentialsFromRetoolCli();
    if (!c) fail('Nessuna sessione di `retool login` trovata nel keyring di questo PC.', 3);
    process.stdout.write(`RETOOL_HOST=${c.origin}\nRETOOL_ACCESS_TOKEN=${c.accessToken}\nRETOOL_XSRF_TOKEN=${c.xsrf}\n`);
    return;
  }

  const cacheDir = args.find((a) => !a.startsWith('--'));
  const full = args.includes('--full');
  if (!cacheDir) fail('Uso: node sync_retool.js <cache_dir> [--full]');

  const pagesDir = path.join(cacheDir, 'pages');
  fs.mkdirSync(pagesDir, { recursive: true });

  const credentials = loadCredentials();
  const http = makeClient(credentials);
  log(`Retool: ${credentials.origin} (credenziali da ${credentials.source === 'env' ? '.env' : 'keyring di retool-cli'})`);

  // --- 1. Elenco pagine + cartelle ---
  let pagesResp;
  try {
    pagesResp = await http.getJson('/api/pages?mobileAppsOnly=false');
  } catch (err) {
    if (isAuthError(err)) fail(sessionExpiredMessage(credentials), 3);
    fail(`Impossibile leggere l'elenco pagine da Retool: ${describe(err)}`);
  }
  const folderById = {};
  for (const f of pagesResp.folders || []) folderById[f.id] = f;

  const pages = (pagesResp.pages || []).map((p) => ({
    uuid: p.uuid,
    nome: p.name,
    cartella: (folderById[p.folderId] || {}).name || '',
    tipo: p.isGlobalWidget ? 'modulo' : 'app',
    aggiornata_il: p.updatedAt,
  }));
  writeJsonAtomic(path.join(cacheDir, 'pages.json'), pages);
  log(`Retool: ${pages.length} pagine (${pages.filter((p) => p.tipo === 'app').length} app, ` +
      `${pages.filter((p) => p.tipo === 'modulo').length} moduli)`);

  // --- 2. Pulizia: pagine non più presenti su Retool ---
  const manifestFile = path.join(cacheDir, 'manifest.json');
  const manifest = full ? {} : readJson(manifestFile, {});
  const liveUuids = new Set(pages.map((p) => p.uuid));
  let removed = 0;
  for (const file of fs.readdirSync(pagesDir)) {
    const uuid = file.replace(/\.json$/, '');
    if (file.endsWith('.json') && !liveUuids.has(uuid)) {
      fs.unlinkSync(path.join(pagesDir, file));
      delete manifest[uuid];
      removed++;
    }
  }
  for (const uuid of Object.keys(manifest)) {
    if (!liveUuids.has(uuid)) delete manifest[uuid];
  }
  if (removed) log(`Rimosse dalla cache ${removed} pagine non più presenti su Retool`);

  // --- 3. Download incrementale ---
  const todo = pages.filter((p) =>
    manifest[p.uuid] !== p.aggiornata_il || !fs.existsSync(path.join(pagesDir, `${p.uuid}.json`)));
  log(`Da scaricare: ${todo.length} pagine${full ? ' (--full)' : ''}, ${pages.length - todo.length} già aggiornate`);

  const errors = {};
  let done = 0;
  let consecutiveAuthErrors = 0;
  let aborted = false;

  async function exportPage(p) {
    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
      try {
        const text = await http.postText(`/api/pages/uuids/${p.uuid}/export`, { timeoutMs: EXPORT_TIMEOUT_MS });
        // Validazione minima: deve essere un export Retool, non una pagina d'errore HTML.
        const parsed = JSON.parse(text);
        if (!parsed || typeof parsed !== 'object' || !parsed.page) {
          throw new Error('risposta non riconosciuta come export Retool (manca "page")');
        }
        return text;
      } catch (err) {
        if (isAuthError(err) || attempt === MAX_ATTEMPTS || !isRetryable(err)) {
          throw err;
        }
        await sleep(2000 * 2 ** (attempt - 1));
      }
    }
  }

  async function worker() {
    while (todo.length && !aborted) {
      const p = todo.shift();
      const label = p.cartella ? `${p.cartella}/${p.nome}` : p.nome;
      try {
        const text = await exportPage(p);
        writeJsonAtomic(path.join(pagesDir, `${p.uuid}.json`), text);
        manifest[p.uuid] = p.aggiornata_il;
        writeJsonAtomic(manifestFile, manifest);
        consecutiveAuthErrors = 0;
        done++;
        log(`  [${done}] ${label} ok (${Math.round(text.length / 1024)} KB)`);
      } catch (err) {
        errors[p.uuid] = `download fallito: ${describe(err)}`;
        done++;
        log(`  [${done}] ${label} ERRORE ${describe(err)}`);
        if (isAuthError(err)) {
          if (err.status === 401 || ++consecutiveAuthErrors >= MAX_CONSECUTIVE_AUTH_ERRORS) {
            aborted = true;
          }
        }
      }
    }
  }

  await Promise.all(Array.from({ length: CONCURRENCY }, worker));
  writeJsonAtomic(path.join(cacheDir, 'errors.json'), errors);

  if (aborted) {
    fail(`${sessionExpiredMessage(credentials)} Le pagine già scaricate non verranno riscaricate.`, 3);
  }

  // --- 4. Query Library ---
  try {
    const lib = await http.getJson('/api/playground');
    const out = {};
    for (const q of [...(lib.orgQueries || []), ...(lib.userQueries || [])]) {
      const t = q.template || {};
      out[q.uuid] = {
        nome: q.name,
        saveId: q.saveId,
        template: {
          editorMode: t.editorMode,
          query: t.query,
          tableName: t.tableName,
          actionType: t.actionType,
        },
      };
    }
    writeJsonAtomic(path.join(cacheDir, 'query_library.json'), out);
    log(`Query Library: ${Object.keys(out).length} query`);
  } catch (err) {
    // Non bloccante: senza Library si usa la copia del testo presente nell'app.
    // Il file vecchio va rimosso, altrimenti verrebbe usata una Library non aggiornata.
    fs.rmSync(path.join(cacheDir, 'query_library.json'), { force: true });
    log(`ATTENZIONE: Query Library non letta (${describe(err)}), uso la copia nelle app`);
  }

  const nErr = Object.keys(errors).length;
  log(`Sincronizzazione completata: ${done - nErr} scaricate, ${nErr} errori`);
}

main().catch((err) => fail(err.stack || err.message));
