#!/usr/bin/env python3
"""
Estrattore SQL Retool.

Per ogni app e modulo Retool (tutti, nessuno escluso) produce l'elenco di stored procedure,
viste, tabelle e funzioni MySQL usate direttamente dalle sue query, più le query della
Query Library che importa. Ogni query della Library ha una voce a sé con i propri oggetti
e le pagine che la usano (`usata_da`). Tutto in un unico file JSON.

Flusso:
  1. `node sync_retool.js cache/` aggiorna la cache locale (download incrementale via updatedAt)
  2. decodifica Transit di `page.data.appState` di ogni pagina in cache
  3. estrazione dei riferimenti dalle query SQL (src/sql_extract.py)
  4. classificazione contro information_schema (src/db_catalog.py)
  5. scrittura di output/estrazione.json

Uso:
    .venv/bin/python estrattore.py                  # sync incrementale + estrazione
    .venv/bin/python estrattore.py --full           # riscarica tutte le pagine
    .venv/bin/python estrattore.py -o altro.json    # file di output diverso
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from src.db_catalog import DbCatalog, open_connection
from src.sql_extract import refs_from_app_state, refs_from_template
from src.transit_decode import decode_transit_string

ROOT_DIR = Path(__file__).resolve().parent
CACHE_DIR = ROOT_DIR / "cache"
SYNC_SCRIPT = ROOT_DIR / "sync_retool.js"
DEFAULT_OUTPUT = ROOT_DIR / "output" / "estrazione.json"

log = logging.getLogger("estrattore")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Estrae SP, viste, tabelle e funzioni usate da ogni app/modulo Retool.")
    p.add_argument("--full", action="store_true", help="Riscarica tutte le pagine ignorando la cache.")
    p.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="File JSON di output.")
    return p.parse_args()


def _mysql_config() -> dict:
    load_dotenv(ROOT_DIR / ".env")
    cfg = {
        "host": os.environ.get("MYSQL_HOST", "localhost"),
        "port": int(os.environ.get("MYSQL_PORT") or 3306),
        "user": os.environ.get("MYSQL_USER", ""),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": os.environ.get("MYSQL_DATABASE", ""),
    }
    missing = [k for k in ("user", "password", "database") if not cfg[k]]
    if missing:
        raise SystemExit(f"Config MySQL incompleta nel file .env: {', '.join('MYSQL_' + m.upper() for m in missing)}")
    return cfg


def _sync(full: bool) -> None:
    cmd = ["node", str(SYNC_SCRIPT), str(CACHE_DIR)] + (["--full"] if full else [])
    # stderr non catturato: l'avanzamento del download resta visibile in console.
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise SystemExit(f"Sincronizzazione Retool fallita (exit {result.returncode}).")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


_EMPTY = {"stored_procedure": [], "viste": [], "tabelle": [], "funzioni": [], "non_trovati": []}


def _analyze_page(
    page: dict, library: dict, catalog: DbCatalog, download_error: str | None,
) -> tuple[dict, set[str]]:
    """Restituisce la voce della pagina e gli uuid delle query della Library che importa."""
    entry = {
        "nome": page["nome"],
        "cartella": page["cartella"],
        "uuid": page["uuid"],
        "tipo": page["tipo"],
        "aggiornata_il": page["aggiornata_il"],
    }

    def _error(msg: str) -> tuple[dict, set[str]]:
        return {**entry, "query_library": [], **_EMPTY, "errore": msg}, set()

    export_file = CACHE_DIR / "pages" / f"{page['uuid']}.json"
    if not export_file.exists():
        return _error(download_error or "export non presente in cache")

    try:
        export = json.loads(export_file.read_text(encoding="utf-8"))
        app_state_raw = ((export.get("page") or {}).get("data") or {}).get("appState")
        if not isinstance(app_state_raw, str):
            return _error("page.data.appState assente (formato pagina non supportato)")
        app_state = decode_transit_string(app_state_raw)
        if not isinstance(app_state, dict):
            return _error("appState decodificato non è un oggetto")
        page_refs = refs_from_app_state(app_state, library)
    except Exception as e:  # una pagina malformata non deve fermare le altre 700
        log.exception("[%s] analisi fallita", page["nome"])
        return _error(f"analisi fallita: {e}")

    lib_names = sorted({library[u]["nome"] for u in page_refs.library_uuids}, key=str.lower)
    result = {**entry, "query_library": lib_names, **catalog.classify(page_refs.own)}
    if download_error:
        # Il download di questa esecuzione è fallito: i dati vengono dalla copia precedente.
        result["errore"] = f"{download_error} (dati della copia in cache)"
    return result, page_refs.library_uuids


def _analyze_library(library: dict, catalog: DbCatalog, users: dict[str, list[dict]]) -> list[dict]:
    """Una voce per ogni query della Query Library, con gli oggetti del DB e le pagine che la importano."""
    out = []
    for uuid, q in library.items():
        out.append({
            "nome": q["nome"],
            "uuid": uuid,
            "aggiornata_il": q.get("aggiornata_il"),
            **catalog.classify(refs_from_template(q.get("template") or {})),
            "usata_da": sorted(users.get(uuid, []), key=lambda p: (p["nome"].strip().lower(), p["cartella"].lower())),
        })
    return sorted(out, key=lambda q: (q["nome"].lower(), q["uuid"]))


def main() -> int:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    # Prima il DB: se non è raggiungibile ci si ferma subito, non dopo centinaia di download.
    mysql_cfg = _mysql_config()
    conn = open_connection(**mysql_cfg)
    try:
        catalog = DbCatalog.load(conn, mysql_cfg["database"])
    finally:
        conn.close()

    _sync(args.full)

    pages = _read_json(CACHE_DIR / "pages.json", [])
    errors = _read_json(CACHE_DIR / "errors.json", {})
    library = _read_json(CACHE_DIR / "query_library.json", {})

    results = []
    users: dict[str, list[dict]] = {}  # uuid query Library -> pagine che la importano
    for p in sorted(pages, key=lambda p: (p["nome"].strip().lower(), p["cartella"].lower())):
        entry, lib_uuids = _analyze_page(p, library, catalog, errors.get(p["uuid"]))
        results.append(entry)
        for u in lib_uuids:
            users.setdefault(u, []).append({"nome": p["nome"], "cartella": p["cartella"], "uuid": p["uuid"]})

    library_entries = _analyze_library(library, catalog, users)

    output = {
        "generato_il": datetime.now().isoformat(timespec="seconds"),
        "database": mysql_cfg["database"],
        "totale_pagine": len(results),
        "totale_query_library": len(library_entries),
        "pagine": results,
        "query_library": library_entries,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(args.output)

    n_err = sum(1 for r in results if "errore" in r)
    n_sql = sum(1 for r in results if any(r[k] for k in ("stored_procedure", "viste", "tabelle", "funzioni")))
    n_lib_used = sum(1 for q in library_entries if q["usata_da"])
    log.info("Pagine: %d (%d con oggetti DB, %d con errori) -> %s", len(results), n_sql, n_err, args.output)
    log.info("Query Library: %d (%d importate da almeno una pagina)", len(library_entries), n_lib_used)
    return 0


if __name__ == "__main__":
    sys.exit(main())
