# Estrattore SQL Retool — Specifiche funzionali

## 1. Scopo

Sapere, per **ogni app e ogni modulo Retool**, quali oggetti del database MySQL usa direttamente:

- stored procedure
- viste
- tabelle
- funzioni

Il risultato è un **unico file JSON** con tutte le pagine Retool, utile per:

- capire l'impatto di una modifica o cancellazione di una SP, vista o tabella;
- trovare oggetti del DB non più usati da nessuna app;
- trovare app che usano oggetti che nel DB non esistono più (query destinate a fallire);
- documentare il data layer delle app (`apps_e_moduli/`).

## 2. Perimetro

| Incluso | Escluso |
|---|---|
| Tutte le pagine Retool: app e moduli, di qualsiasi cartella (anche bozze personali e archivio) | Workflow Retool |
| Query SQL scritte a mano (modalità SQL) | Query JavaScript, REST, GraphQL e altri tipi non SQL |
| Query SQL costruite con l'editor visuale (modalità GUI) | Oggetti usati *dentro* una SP o una vista (niente esplosione ricorsiva) |
| Query importate dalla Query Library di Retool | Relazione app → modulo incorporato |

## 3. Regole funzionali

### R1 — Attribuzione a modulo, non all'app che lo contiene
Se un'app incorpora un modulo e il modulo usa una stored procedure, la SP è **del modulo**, non dell'app.
Il modulo compare nel JSON come pagina a sé con i propri oggetti. Un'app riporta solo gli oggetti delle
**proprie** query.

> Esempio: `crono-programma sal` incorpora `menu_top`. Le SP di `menu_top`
> (`st_get_system_config`, `st_get_id_anagrafica_from_code`) compaiono solo sotto `menu_top`.

### R2 — Nessuna pagina esclusa
Tutte le pagine presenti su Retool compaiono nel JSON, comprese quelle senza query SQL (con liste vuote).

### R3 — Query in modalità GUI
Per le query costruite con l'editor visuale conta la **tabella selezionata** nell'editor. L'eventuale testo SQL
rimasto nella query (da quando era in modalità SQL) viene ignorato, perché Retool non lo esegue.

### R4 — Query della Query Library
Se un'app importa una query dalla Query Library in versione "latest", si analizza **la versione attuale della
Library**, perché è quella che Retool esegue. Se l'app è legata a una versione specifica, si analizza la copia
salvata nell'app.

> Esempio: in `upload_documenti` la copia nell'app chiama ancora `usp_aggiorna_upload_documento`,
> ma la Library oggi chiama `st_update_documento_upload`: nel JSON compare la seconda.

### R5 — Classificazione contro il database
Ogni nome trovato nelle query viene confrontato con il database configurato:

| Trovato nella query come… | Esiste nel DB come… | Finisce in |
|---|---|---|
| `CALL nome(...)` | procedura | `stored_procedure` |
| `FROM` / `JOIN` / `INTO` / `UPDATE` / tabella GUI | vista | `viste` |
| idem | tabella | `tabelle` |
| `nome(...)` in un'espressione | funzione definita nel DB | `funzioni` |
| `CALL` o tabella/vista | non esiste | `non_trovati` |
| `nome(...)` in un'espressione | non esiste | scartato (è una funzione nativa di MySQL, es. `COUNT`, `JSON_ARRAYAGG`) |

Il confronto non distingue maiuscole e minuscole; nel JSON si usa il nome come è scritto nel DB.

### R6 — Cosa NON è un oggetto del DB
Non vengono riportati:
- nomi di CTE (`WITH ultimi AS (...) SELECT * FROM ultimi`);
- variabili (`SELECT ... INTO @id`);
- colonne dopo `ON DUPLICATE KEY UPDATE`;
- codice SQL commentato (`/* ... */`, `-- ...`, `# ...`);
- nomi di tabella interamente dinamici (`FROM {{ tabella.value }}`).

### R7 — Dati sempre aggiornati
Ogni esecuzione controlla su Retool quali pagine sono cambiate e riscarica solo quelle. Le pagine cancellate
su Retool spariscono dalla copia locale. La copia locale resta disponibile fino all'esecuzione successiva.

## 4. Utilizzo

### Prerequisiti
- Credenziali MySQL nel file `.env` (solo lettura di `information_schema`).
- Una sessione Retool valida, che scade periodicamente. Due modi per fornirla:

| Dove gira | Come si fornisce la sessione |
|---|---|
| PC con browser | `retool login` del vecchio CLI `retool-cli`; la sessione resta nel keyring di sistema |
| Server senza browser (SSH) | Righe `RETOOL_HOST`, `RETOOL_ACCESS_TOKEN`, `RETOOL_XSRF_TOKEN` nel `.env`, generate sul PC con `node sync_retool.js --stampa-credenziali` |

Se la sessione è scaduta, l'estrattore si ferma con codice di uscita 3 e indica cosa aggiornare.
Il nuovo CLI `@tryretool/cli` (`retool auth login`) non è utilizzabile: il suo token è pensato per le React apps.

### Comandi
```bash
.venv/bin/python estrattore.py                  # aggiornamento incrementale + estrazione
.venv/bin/python estrattore.py --full           # riscarica tutte le pagine da zero
.venv/bin/python estrattore.py -o percorso.json # file di output diverso
```

### Tempi indicativi
| Situazione | Durata |
|---|---|
| Prima esecuzione (746 pagine da scaricare) | ~12 minuti |
| Esecuzioni successive senza modifiche su Retool | ~10 secondi |

## 5. Output

File: `output/estrazione.json` (sovrascritto a ogni esecuzione).

```json
{
  "generato_il": "2026-09-28T15:04:25",
  "database": "strutture_energia_it_retool_produzione",
  "totale_pagine": 746,
  "pagine": [
    {
      "nome": "modulo_elenco_messaggi",
      "cartella": "root",
      "uuid": "cf1c5146-fdc8-11f0-823d-7fe65ea66425",
      "tipo": "modulo",
      "aggiornata_il": "2026-06-05T10:45:51.615Z",
      "stored_procedure": ["st_get_elenco_conversazioni", "st_update_stato_lettura_messaggi"],
      "viste": [],
      "tabelle": [],
      "funzioni": [],
      "non_trovati": []
    }
  ]
}
```

| Campo | Significato |
|---|---|
| `generato_il` | Data e ora dell'estrazione |
| `database` | Schema MySQL usato per la classificazione |
| `totale_pagine` | Numero di pagine nel file (= pagine presenti su Retool) |
| `nome` | Nome della pagina su Retool |
| `cartella` | Cartella Retool (distingue pagine con lo stesso nome) |
| `uuid` | Identificativo univoco della pagina (coincide con quello usato in `menu` / `dizionario_moduli`) |
| `tipo` | `app` oppure `modulo` |
| `aggiornata_il` | Ultima modifica della pagina su Retool |
| `stored_procedure`, `viste`, `tabelle`, `funzioni` | Oggetti del DB usati direttamente, in ordine alfabetico |
| `non_trovati` | Nomi usati nelle query che nel DB non esistono |
| `errore` | Presente solo se la pagina non è stata scaricata o letta; in caso di download fallito i dati vengono dalla copia precedente |

Le pagine sono ordinate per nome.

### Come leggere `non_trovati`
Un nome in `non_trovati` può essere:
- un oggetto **eliminato o rinominato** nel DB (la query fallirà) — es. `catasto`, `vw_gant_intervento`;
- un oggetto di **un altro database** (Retool DB interno, DB di Stage) — es. `products`;
- una **tabella temporanea** creata dalla query stessa;
- un nome **parzialmente dinamico** (`tabella_{{ suffisso }}` → `tabella_`).

## 6. Limiti noti

- **Solo riferimenti diretti**: le tabelle usate dentro una SP o una vista non vengono esplose.
- **Release Retool**: per le app con una release pubblicata (68 al 28/09/2026) viene analizzata l'ultima versione
  salvata, che può differire da quella rilasciata agli utenti.
- **Risorse diverse dal DB di produzione**: le query che puntano a Stage, a `retool_db` o a `onboarding_db`
  vengono comunque classificate contro il DB di produzione (punto aperto, vedi §7).
- **Nomi dinamici**: `FROM {{ x }}` non è risolvibile.

## 7. Punti aperti

- Escludere le query che non puntano al MySQL di produzione, oppure riportare per ogni oggetto la risorsa
  Retool da cui arriva. Distribuzione attuale: MySqlDB 3.335 query, "MySqlDB - unsafe mode" 219,
  "MySql - Stage" 105, `retool_db` 32, `onboarding_db` 4.

## 8. Risultati della prima esecuzione (28/09/2026)

| | |
|---|---|
| Pagine | 746 (655 app, 91 moduli) |
| Pagine con almeno un oggetto del DB | 488 |
| Stored procedure distinte usate | 126 |
| Viste distinte usate | 63 |
| Tabelle distinte usate | 136 |
| Nomi distinti non trovati nel DB | 84 |
| Pagine con errore | 0 |
