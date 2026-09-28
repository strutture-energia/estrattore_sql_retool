#!/usr/bin/env node
/**
 * Sincronizza in una cache locale TUTTE le pagine Retool (app + moduli) e la Query Library.
 *
 * Uso:
 *   node sync_retool.js <cache_dir> [--full]
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
 * Credenziali: riusa quelle salvate da `retool login` (keychain), come fa il CLI ufficiale.
 */

'use strict';

const { execSync } = require('child_process');
const path = require('path');
const fs = require('fs');

const CONCURRENCY = 4;
const MAX_ATTEMPTS = 4;
const EXPORT_TIMEOUT_MS = 180000;
// Dopo N errori di autorizzazione consecutivi la sessione è quasi certamente scaduta.
const MAX_CONSECUTIVE_AUTH_ERRORS = 5;

// --- Moduli interni di retool-cli (credenziali + axios) ---
// Il binario `retool` sta in .../bin/retool, il modulo in .../lib/node_modules/retool-cli/
function loadRetoolCli() {
  let retoolCliDir;
  try {
    const retoolBin = execSync('which retool', { encoding: 'utf8' }).trim();
    retoolCliDir = path.resolve(path.dirname(retoolBin), '..', 'lib', 'node_modules', 'retool-cli');
  } catch (_) {
    fail('Comando `retool` non trovato nel PATH. Installa: npm i -g retool-cli');
  }
  const { getCredentials } = require(path.join(retoolCliDir, 'lib/utils/credentials'));
  const axios = require(path.join(retoolCliDir, 'node_modules/axios'));
  return { getCredentials, axios };
}

function fail(msg, code = 1) {
  process.stderr.write(`ERRORE: ${msg}\n`);
  process.exit(code);
}

function log(msg) {
  process.stderr.write(`${msg}\n`);
}

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

function isAuthError(err) {
  const s = err.response && err.response.status;
  return s === 401 || s === 403;
}

function isRetryable(err) {
  const s = err.response && err.response.status;
  if (s === 429 || (s >= 500 && s < 600)) return true;
  // Errori di rete (ECONNRESET, ETIMEDOUT, timeout axios...)
  return !err.response;
}

function describe(err) {
  if (err.response) return `HTTP ${err.response.status}`;
  return err.code || err.message;
}

async function main() {
  const args = process.argv.slice(2);
  const cacheDir = args.find((a) => !a.startsWith('--'));
  const full = args.includes('--full');
  if (!cacheDir) fail('Uso: node sync_retool.js <cache_dir> [--full]');

  const pagesDir = path.join(cacheDir, 'pages');
  fs.mkdirSync(pagesDir, { recursive: true });

  const { getCredentials, axios } = loadRetoolCli();
  const credentials = getCredentials();
  if (!credentials) fail('Non sei loggato a Retool. Esegui: retool login', 3);
  axios.defaults.headers['x-xsrf-token'] = credentials.xsrf;
  axios.defaults.headers.cookie = `accessToken=${credentials.accessToken};`;
  const origin = credentials.origin;

  // --- 1. Elenco pagine + cartelle ---
  let pagesResp;
  try {
    pagesResp = await axios.get(`${origin}/api/pages?mobileAppsOnly=false`);
  } catch (err) {
    if (isAuthError(err)) fail('Sessione Retool scaduta. Esegui `retool login` e riprova.', 3);
    fail(`Impossibile leggere l'elenco pagine da Retool: ${describe(err)}`);
  }
  const folderById = {};
  for (const f of pagesResp.data.folders || []) folderById[f.id] = f;

  const pages = (pagesResp.data.pages || []).map((p) => ({
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
        const resp = await axios.post(`${origin}/api/pages/uuids/${p.uuid}/export`, {}, {
          responseType: 'arraybuffer',
          timeout: EXPORT_TIMEOUT_MS,
        });
        const text = Buffer.from(resp.data).toString('utf8');
        // Validazione minima: deve essere un export Retool, non una pagina d'errore HTML.
        const parsed = JSON.parse(text);
        if (!parsed || typeof parsed !== 'object' || !parsed.page) {
          throw new Error('risposta non riconosciuta come export Retool (manca "page")');
        }
        return text;
      } catch (err) {
        if (isAuthError(err) || attempt === MAX_ATTEMPTS || !(err.isAxiosError && isRetryable(err))) {
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
          if (err.response.status === 401 || ++consecutiveAuthErrors >= MAX_CONSECUTIVE_AUTH_ERRORS) {
            aborted = true;
          }
        }
      }
    }
  }

  await Promise.all(Array.from({ length: CONCURRENCY }, worker));
  writeJsonAtomic(path.join(cacheDir, 'errors.json'), errors);

  if (aborted) {
    fail('Sessione Retool scaduta durante il download. Esegui `retool login` e rilancia: ' +
         'le pagine già scaricate non verranno riscaricate.', 3);
  }

  // --- 4. Query Library ---
  try {
    const lib = await axios.get(`${origin}/api/playground`);
    const out = {};
    for (const q of [...(lib.data.orgQueries || []), ...(lib.data.userQueries || [])]) {
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
