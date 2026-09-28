# Estrattore SQL Retool

Per **ogni** app e modulo Retool produce l'elenco di stored procedure, viste, tabelle e funzioni
MySQL usate direttamente dalle sue query, in un unico file `output/estrazione.json`.

Documentazione completa:
- [Specifiche funzionali](docs/specifiche_funzionali.md): cosa fa, regole, formato dell'output, limiti
- [Specifiche tecniche](docs/specifiche_tecniche.md): architettura, API Retool, cache, algoritmo di estrazione, test

## Uso

```bash
.venv/bin/python estrattore.py            # sync incrementale + estrazione
.venv/bin/python estrattore.py --full     # riscarica tutte le pagine
```

Setup iniziale: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`,
poi copia `.env.example` in `.env` e compila le credenziali MySQL. Richiede Node.js ≥ 18.

### Sessione Retool

- **PC con browser**: installa il vecchio CLI (`npm i -g retool-cli`) ed esegui `retool login`.
  L'estrattore legge la sessione dal keyring di sistema.
- **Server senza browser** (es. via SSH): metti la sessione nel `.env` del server
  (`RETOOL_HOST`, `RETOOL_ACCESS_TOKEN`, `RETOOL_XSRF_TOKEN`). Per ottenerla, dal PC:
  ```bash
  node sync_retool.js --stampa-credenziali | ssh utente@server 'cat >> percorso/estrattore_sql_retool/.env'
  ```
  Quando la sessione scade, l'estrattore esce con codice 3: ripeti il comando dopo un nuovo `retool login` sul PC
  (e togli le righe RETOOL_* vecchie dal `.env` del server).

Il nuovo CLI `@tryretool/cli` (`retool auth login`) **non** è supportato: il suo token serve per le
React apps di Retool, non per l'export delle app classiche.

## Come funziona

1. `sync_retool.js` usa la sessione Retool (`.env` o keyring) e aggiorna `cache/`:
   riscarica solo le pagine il cui `updatedAt` è cambiato, elimina quelle non più su Retool,
   salva la Query Library.
2. Per ogni pagina decodifica `page.data.appState` (formato Transit) e legge i plugin
   `SqlQueryUnified`/`SqlQuery` della pagina stessa:
   - modalità SQL: parsing con sqlglot (regex di riserva se il parser fallisce);
   - modalità GUI: tabella da `tableName`;
   - query importate dalla Query Library in versione `latest`: si usa il testo della Library.
3. I nomi trovati vengono classificati con `information_schema.TABLES` / `ROUTINES`.

Le query dei moduli incorporati in un'app **non** vengono attribuite all'app: il modulo
compare come pagina a sé con i propri oggetti.

## Output

```json
{
  "generato_il": "2026-09-28T12:00:00",
  "database": "…",
  "totale_pagine": 746,
  "pagine": [{
    "nome": "modulo_elenco_messaggi", "cartella": "…", "uuid": "…",
    "tipo": "modulo", "aggiornata_il": "…",
    "stored_procedure": ["st_get_elenco_conversazioni", "st_update_stato_lettura_messaggi"],
    "viste": [], "tabelle": [], "funzioni": [], "non_trovati": []
  }]
}
```

- `non_trovati`: nomi presenti nelle query che non esistono nel DB (oggetti eliminati, refusi,
  tabelle temporanee, nomi parzialmente dinamici tipo `tabella_{{ x }}`).
- `errore`: presente solo se la pagina non è stata scaricata o decodificata.

## Limiti noti

- Solo riferimenti diretti: gli oggetti usati *dentro* una SP o una vista non vengono esplosi.
- Le app con una release vengono esportate nell'ultima versione salvata, non in quella rilasciata.
- Un `{{ }}` al posto dell'intero nome di tabella (`FROM {{ x }}`) non è risolvibile e viene ignorato.
# estrattore_sql_retool
# estrattore_sql_retool
