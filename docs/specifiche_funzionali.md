# Estrattore SQL Retool — Specifiche funzionali

## 1. Scopo

Sapere, per **ogni app e ogni modulo Retool**, quali oggetti del database MySQL usa direttamente:

- stored procedure
- viste
- tabelle
- funzioni

Il risultato è un **unico file JSON** con tutte le pagine Retool e tutte le query della Query Library, utile per:

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

### R4 — Query della Query Library: componenti a sé
Una query della Query Library non è un semplice wrapper: ha una sua logica (più UPDATE, CALL, transazioni…).
Come i moduli (R1), è un **componente a sé**:

- ogni query della Library ha **una propria voce** nella sezione `query_library` del JSON, con i suoi oggetti
  del DB e l'elenco delle pagine che la importano (`usata_da`); compaiono tutte, anche quelle non usate;
- la pagina che la importa riporta solo il **nome** della query in `query_library`; gli oggetti della query
  **non** finiscono negli array della pagina;
- si analizza **la versione attuale della Library**, perché è quella che Retool esegue (versione "latest");
- il nome è quello della query nella Library, non quello che ha nell'app
  (es. in `upload_documenti` il plugin `insertDocumento` → `st_update_documento_upload`).

Eccezioni, trattate come **query propria della pagina** (oggetti negli array della pagina, usando la copia
salvata nell'app):
- l'app è legata a una **versione fissa** della query (non "latest"): esegue quella copia, non la Library attuale;
- la query della Library **non è visibile** all'utente con cui si scarica (query privata di un altro utente).

> Per sapere quali pagine toccano un oggetto X servono due passaggi: le pagine che hanno X nei propri array,
> più le pagine in `usata_da` delle query della Library che hanno X nei loro array.

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
- Sessione Retool valida: `retool login` (la sessione scade periodicamente).
- Credenziali MySQL nel file `.env` (solo lettura di `information_schema`).

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
  "totale_query_library": 220,
  "pagine": [
    {
      "nome": "modulo_elenco_messaggi",
      "cartella": "root",
      "uuid": "cf1c5146-fdc8-11f0-823d-7fe65ea66425",
      "tipo": "modulo",
      "aggiornata_il": "2026-06-05T10:45:51.615Z",
      "query_library": ["st_update_stato_lettura_messaggi"],
      "stored_procedure": ["st_get_elenco_conversazioni"],
      "viste": [],
      "tabelle": [],
      "funzioni": [],
      "non_trovati": []
    }
  ],
  "query_library": [
    {
      "nome": "anagrafica_persona_giuridica_update",
      "uuid": "4e94813c-fb77-48b5-bab3-cc3f467f412a",
      "aggiornata_il": "2026-07-28T07:49:12.248Z",
      "stored_procedure": ["st_upsert_recapito"],
      "viste": [],
      "tabelle": ["anagrafica", "anagrafica_persona_giuridica"],
      "funzioni": [],
      "non_trovati": [],
      "usata_da": [
        {"nome": "Anagrafiche di progetto", "cartella": "root", "uuid": "…"},
        {"nome": "assegna ruoli", "cartella": "Trattativa", "uuid": "…"},
        {"nome": "edit anagrafica base pg", "cartella": "Sinergia", "uuid": "…"}
      ]
    }
  ]
}
```

| Campo | Significato |
|---|---|
| `generato_il` | Data e ora dell'estrazione |
| `database` | Schema MySQL usato per la classificazione |
| `totale_pagine` | Numero di pagine nel file (= pagine presenti su Retool) |
| `totale_query_library` | Numero di query della Query Library visibili |

Voce di una **pagina** (`pagine[]`):

| Campo | Significato |
|---|---|
| `nome` | Nome della pagina su Retool |
| `cartella` | Cartella Retool (distingue pagine con lo stesso nome) |
| `uuid` | Identificativo univoco della pagina (coincide con quello usato in `menu` / `dizionario_moduli`) |
| `tipo` | `app` oppure `modulo` |
| `aggiornata_il` | Ultima modifica della pagina su Retool |
| `query_library` | Nomi delle query della Query Library importate (versione "latest"), in ordine alfabetico |
| `stored_procedure`, `viste`, `tabelle`, `funzioni` | Oggetti del DB usati direttamente dalle query **proprie** della pagina, in ordine alfabetico |
| `non_trovati` | Nomi usati nelle query che nel DB non esistono |
| `errore` | Presente solo se la pagina non è stata scaricata o letta; in caso di download fallito i dati vengono dalla copia precedente |

Voce di una **query della Query Library** (`query_library[]`):

| Campo | Significato |
|---|---|
| `nome` | Nome della query nella Library |
| `uuid` | Identificativo della query nella Library |
| `aggiornata_il` | Ultima modifica della query nella Library |
| `stored_procedure`, `viste`, `tabelle`, `funzioni`, `non_trovati` | Come per le pagine, riferiti al corpo della query |
| `usata_da` | Pagine che la importano in versione "latest": `{nome, cartella, uuid}`; vuoto se nessuna la usa |

Pagine e query della Library sono ordinate per nome.

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

## 8. Risultati (29/09/2026)

| | |
|---|---|
| Pagine | 746 (655 app, 91 moduli) |
| Pagine con almeno un oggetto del DB nelle proprie query | 471 |
| Pagine che importano almeno una query della Library | 81 |
| Pagine con oggetti propri o query della Library | 488 |
| Query della Library | 220 (152 importate da almeno una pagina, max 9 pagine per query) |
| Stored procedure distinte (pagine / Library / totale) | 34 / 118 / 141 |
| Viste distinte (pagine / Library / totale) | 44 / 43 / 74 |
| Tabelle distinte (pagine / Library / totale) | 134 / 30 / 136 |
| Pagine con errore | 0 |
