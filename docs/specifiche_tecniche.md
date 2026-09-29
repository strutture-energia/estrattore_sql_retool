# Estrattore SQL Retool — Specifiche tecniche

Per il *cosa fa* vedi [specifiche_funzionali.md](specifiche_funzionali.md). Questo documento descrive *come funziona*.

## 1. Architettura

```
estrattore.py (Python, orchestratore)
   │
   ├─ 1. DbCatalog.load()            MySQL information_schema → catalogo in memoria
   │                                 (prima di tutto: se il DB non risponde ci si ferma subito)
   │
   ├─ 2. node sync_retool.js cache/  Retool API → cache/ (download incrementale)
   │
   ├─ 3. per ogni pagina in cache/pages.json:
   │      decode_transit_string()    page.data.appState (Transit) → dict
   │      refs_from_app_state()      plugin SQL → oggetti propri + uuid delle query Library importate
   │      DbCatalog.classify()       nomi grezzi → stored_procedure / viste / tabelle / funzioni / non_trovati
   │
   ├─ 4. per ogni query in cache/query_library.json:
   │      refs_from_template()       corpo della query → nomi grezzi → DbCatalog.classify()
   │      usata_da                   pagine che l'hanno importata al passo 3
   │
   └─ 5. output/estrazione.json      scrittura atomica
```

Il download è in Node perché le credenziali di `retool login` stanno nel keychain di sistema e si leggono
con i moduli interni di `retool-cli` (Node). L'analisi è in Python per `sqlglot` e `transit-python`.

## 2. File

```
estrattore_sql_retool/
├── estrattore.py            CLI e orchestrazione
├── sync_retool.js           sincronizzazione con Retool (un solo processo Node)
├── src/
│   ├── transit_decode.py    decoder Transit (copiato da analizzatore_retool/src/minifier.py)
│   ├── sql_extract.py       estrazione dei riferimenti dai plugin SQL
│   └── db_catalog.py        catalogo information_schema e classificazione
├── tests/test_sql_extract.py
├── requirements.txt
├── .env / .env.example      credenziali MySQL
├── cache/                   copia locale di Retool (persistente, ~320 MB)
└── output/estrazione.json
```

## 3. Sincronizzazione con Retool (`sync_retool.js`)

### 3.1 Autenticazione
Individua `retool-cli` a partire da `which retool` (`.../bin/retool` → `.../lib/node_modules/retool-cli`) e ne usa:
- `lib/utils/credentials.getCredentials()` → `{origin, accessToken, xsrf}` dal keychain;
- `node_modules/axios`, con header `x-xsrf-token` e cookie `accessToken`.

Stessa logica di `analizzatore_retool/scripts/retool_helper.js`.

### 3.2 API usate (interne, non documentate da Retool)

| Chiamata | Uso |
|---|---|
| `GET /api/pages?mobileAppsOnly=false` | Elenco pagine (`uuid, name, folderId, updatedAt, isGlobalWidget…`) e cartelle (`folders[]`) |
| `POST /api/pages/uuids/{uuid}/export` | JSON completo di una pagina |
| `GET /api/playground` | Query Library: `{orgQueries, userQueries}` con `uuid, name, saveId, updatedAt, template` |

`isGlobalWidget = true` identifica un **modulo**.

### 3.3 Cache

| File | Contenuto |
|---|---|
| `cache/pages.json` | `[{uuid, nome, cartella, tipo, aggiornata_il}]`: elenco completo delle pagine all'ultima esecuzione |
| `cache/pages/<uuid>.json` | Export di ogni pagina, così come restituito da Retool |
| `cache/manifest.json` | `{uuid: updatedAt}` delle pagine scaricate con successo |
| `cache/errors.json` | `{uuid: messaggio}` delle pagine il cui download è fallito nell'ultima esecuzione |
| `cache/query_library.json` | `{uuid: {nome, saveId, aggiornata_il, template: {editorMode, query, tableName, actionType}}}` |

Il file della pagina usa l'uuid e non il nome: su Retool esistono 12 nomi duplicati in cartelle diverse e nomi con
caratteri non validi nei file (`/`, spazi iniziali).

### 3.4 Algoritmo incrementale
1. Scarica l'elenco pagine e scrive `pages.json`.
2. Carica `manifest.json` (vuoto con `--full`).
3. Elimina da `cache/pages/` e dal manifest le pagine che non esistono più su Retool.
4. Da scaricare = pagine con `manifest[uuid] ≠ updatedAt` oppure senza file in cache.
5. Scarica con **4 worker in parallelo**. Dopo ogni download riuscito aggiorna il manifest su disco.
   Se l'esecuzione si interrompe, quella successiva riparte dalle pagine mancanti.
6. Scrive `errors.json`, poi scarica la Query Library.

Tutte le scritture sono **atomiche** (file `.tmp` + `rename`): un'interruzione non lascia JSON troncati.

### 3.5 Gestione errori
| Situazione | Comportamento |
|---|---|
| Nessuna credenziale / 401 o 403 sulla lista pagine | Esce con codice 3: "esegui `retool login`" |
| HTTP 429, 5xx, errori di rete | Fino a 4 tentativi con attesa esponenziale (2 s, 4 s, 8 s) |
| HTTP 401 durante i download | Si ferma subito (sessione scaduta), codice 3 |
| HTTP 403 | Errore della singola pagina; dopo 5 consecutivi si ferma (sessione scaduta), codice 3 |
| Risposta che non è un export (manca `page`) | Errore della singola pagina |
| Query Library non leggibile | Non bloccante: elimina il `query_library.json` vecchio; le query importate vengono trattate come query proprie delle pagine (copia salvata) e la sezione `query_library` dell'output resta vuota |

Una pagina in errore mantiene nel manifest il vecchio `updatedAt`, quindi viene ritentata all'esecuzione successiva.

## 4. Decodifica Transit (`src/transit_decode.py`)

La logica di una pagina sta in `export.page.data.appState`: una stringa **Transit-JSON** che serializza strutture
Immutable.js. `decode_transit_string()` la legge con `transit-python` e `_normalize_transit()` converte i tag:

| Tag | Tipo Immutable.js | Diventa |
|---|---|---|
| `iR` | Record `{n, v}` | dict di `v` + chiave `__iR__` con il nome del record |
| `iM`, `iOM` | Map / OrderedMap `[k, v, k, v…]` | dict |
| `iL` | List | list |
| altri | — | `{"__tag__", "rep"}` |

`transit-python` è del periodo Python 2: prima dell'import si rimappano su `collections` le ABC spostate in
`collections.abc` (Python ≥ 3.10). La patch va eseguita **prima** di `import transit`.

Struttura rilevante dopo la decodifica:
```
appState
└── plugins: {id_plugin: {subtype, resourceName, resourceDisplayName, template: {...}}}
```
La chiave `modules` dell'export (contenuto dei moduli incorporati) **non viene letta**: vedi regola R1.

## 5. Estrazione dei riferimenti (`src/sql_extract.py`)

### 5.1 Selezione dei plugin
Si considerano solo i plugin di `appState.plugins` con `subtype ∈ {SqlQueryUnified, SqlQuery}`.

### 5.2 Query importate dalla Library (`library_uuid`, `refs_from_app_state`)
Un plugin è un **riferimento alla Library** se `template.isImported` è vero,
`playgroundQuerySaveId ∈ {"latest", "", None}` e `playgroundQueryUuid` è in `query_library.json`.
In quel caso `refs_from_app_state()` aggiunge l'uuid a `PageRefs.library_uuids` e **non** analizza il testo
(né la copia nell'app né la Library): gli oggetti della query vanno solo nella sua voce di `query_library`.

In tutti gli altri casi (query non importata, versione fissata, query della Library non visibile) si analizza il
template dell'app e gli oggetti finiscono in `PageRefs.own`.

`refs_from_template()` è la stessa funzione usata sia per i plugin delle pagine sia per il corpo delle query
della Library: GUI e SQL vengono trattati allo stesso modo.

### 5.3 Modalità GUI
`template.editorMode == "gui"` → l'unico riferimento è `template.tableName` (tipo relazione), se non vuoto e
senza `{{`. `template.query` viene ignorato: può contenere SQL residuo non eseguito
(es. `modulo_CSV_Abaco`: `tableName = abaco_infissi`, `query` con `insert into input_misure…`).

### 5.4 Modalità SQL
1. **Placeholder**: ogni `{{ … }}` (regex non-greedy `\{\{.*?\}\}`, DOTALL) diventa `:retool_ph_N`. Il prefisso
   riconoscibile serve a scartare i placeholder finiti in posizione di tabella (`FROM {{ x }}` → tabella `retool_ph_1`).
2. **Parsing**: `sqlglot.parse(sql, read="mysql")`, uno statement alla volta:

| Nodo sqlglot | Estrazione |
|---|---|
| `exp.Command` con `this == "CALL"` | Nome procedura con regex sull'inizio del testo restante (gestisce backtick e `schema.nome`); funzioni negli argomenti con regex |
| `exp.Command` diverso (es. blocchi `IF … THEN`) | sqlglot non lo analizza: si applica l'estrazione di riserva al testo |
| `exp.Table` | Relazione (`db.nome` se qualificata), esclusi: nomi di CTE (`exp.CTE.alias`), tabelle figlie di `exp.Into` (`SELECT … INTO @var`), placeholder, `DUAL` |
| `exp.Anonymous` | Candidata funzione utente (le funzioni native note a sqlglot hanno nodi dedicati) |

Il testo restante di `exp.Command` può essere un `exp.Literal` oppure una `str`, a seconda del comando: sono gestiti entrambi i casi.

3. **Estrazione di riserva** (se sqlglot solleva `SqlglotError`, ~1% delle query):
   - rimozione di stringhe e commenti con **un'unica regex ad alternanza** (`'…'`, `"…"`, `/*…*/`, `--…`, `#…`):
     trova sempre il token che inizia prima, così `'#'` (sentinel Ecodomus) non viene preso per un commento
     e un apostrofo in un commento non apre una stringa;
   - rimozione di `ON DUPLICATE KEY UPDATE` (seguito da colonne, non da tabelle);
   - regex: `(FROM|JOIN|INTO|UPDATE|TABLE) nome` → relazioni, `CALL nome` → procedure, `nome(` → funzioni candidate.

Il risultato è un oggetto `Refs(procedures, relations, functions)` con i nomi grezzi (senza backtick, eventualmente
qualificati `schema.nome`). I `Refs` di tutte le query di una pagina vengono uniti.

### 5.5 Statistiche sulla prima esecuzione
| Tipo di query | N. |
|---|---|
| Analizzate da sqlglot | 3.213 (di cui 110 con comandi non-CALL passati alle regex) |
| Modalità GUI | 339 |
| Importate dalla Query Library (fra quelle in modalità SQL) | 237 |
| Estrazione di riserva (parse fallito) | 32 |
| Vuote | 112 |

## 6. Classificazione (`src/db_catalog.py`)

`DbCatalog.load()` esegue due query sullo schema `MYSQL_DATABASE`:
```sql
SELECT TABLE_NAME, TABLE_TYPE     FROM information_schema.TABLES   WHERE TABLE_SCHEMA  = %s;
SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s;
```
e costruisce due mappe `nome_minuscolo → (nome_reale, tipo)`.

`classify(refs)`:
- nome qualificato `schema.x`: se `schema` è lo schema configurato si cerca `x`, altrimenti è `non_trovati`;
- `procedures` → PROCEDURE ? `stored_procedure` : `non_trovati`;
- `relations` → `TABLE_TYPE == VIEW` ? `viste` : tabella ? `tabelle` : `non_trovati`;
- `functions` → FUNCTION ? `funzioni` : scartata (funzione nativa MySQL);
- `non_trovati` deduplicato senza distinzione di maiuscole; tutte le liste ordinate alfabeticamente.

## 7. Orchestrazione e output (`estrattore.py`)

1. Legge `.env` (`MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`; gli ultimi tre obbligatori).
2. Carica il catalogo DB.
3. Esegue `node sync_retool.js cache/ [--full]` con lo stderr non catturato (l'avanzamento resta visibile); se l'exit code è ≠ 0 si ferma.
4. Per ogni pagina di `pages.json` (ordinata per nome, poi cartella) chiama `_analyze_page()`:
   - export mancante → pagina con liste vuote e `errore`;
   - `appState` assente o non decodificabile in dict → `errore`;
   - eccezione durante l'analisi → `errore` con il messaggio (la pagina non blocca le altre);
   - uuid in `errors.json` ma export presente → analisi della copia precedente + `errore` esplicativo.

   Ogni pagina restituisce anche gli uuid delle query della Library importate; vengono raccolti in un indice
   `uuid → [pagine]`.
5. `_analyze_library()`: per ogni query di `query_library.json` classifica gli oggetti del suo template e aggiunge
   `usata_da` dall'indice del passo 4. Nella voce della pagina, `query_library` contiene i **nomi** delle query.
6. Scrive il JSON (UTF-8, `ensure_ascii=False`, indentato) su file `.tmp` e poi `replace` atomico.
7. Riepilogo su stderr: pagine totali, con oggetti, con errori; query della Library totali e usate.

## 8. Test

`tests/test_sql_extract.py` (pytest, nessuna dipendenza da Retool o DB):

| Area | Casi |
|---|---|
| CALL | più statement, schema qualificato, commento finale con placeholder |
| Esclusioni | CTE, SQL commentato, placeholder in FROM, `SELECT … INTO @var`, `ON DUPLICATE KEY UPDATE` in riserva |
| Placeholder | espressioni JS con graffe singole |
| DML | DELETE/UPDATE con subquery, sentinel `'#'` |
| Riserva | query sintatticamente errata, blocchi `IF … THEN` |
| Template | modalità GUI con SQL residuo |
| Query Library | import `latest` = solo riferimento (nessun oggetto nella pagina); versione fissata o query non visibile = query propria; corpo con transazione, UPDATE, CALL e codice commentato |
| Pagina | solo subtype SQL, plugin non SQL ignorati |
| Classificazione | procedure, tabelle, viste, funzioni, schema diverso, inesistenti, maiuscole |

```bash
.venv/bin/python -m pytest -q tests
```

## 9. Dipendenze e ambiente

| Componente | Versione verificata | Note |
|---|---|---|
| Python | 3.14 | |
| sqlglot | 30.20 | dialetto `mysql` |
| transit-python | 0.8.302 | richiede la patch di `collections` |
| PyMySQL | 1.2 | |
| python-dotenv | 1.2 | |
| Node.js | 24 | |
| retool-cli | 1.0.29 | installato globalmente (`npm i -g retool-cli`) |

## 10. Punti di estensione

- **Nuovi tipi di query SQL**: aggiungere il subtype a `SQL_SUBTYPES` in `src/sql_extract.py`.
- **Filtro per risorsa Retool**: il plugin espone `resourceName` (uuid) e `resourceDisplayName`; il filtro va applicato
  in `refs_from_app_state()` prima di `refs_from_template()`.
- **Altri schemi MySQL**: `DbCatalog` lavora su un solo schema; per più schemi servono più cataloghi e una regola
  su quale usare per ogni risorsa.
- **Parallelismo download**: `CONCURRENCY` in `sync_retool.js` (4 è un valore prudente per non essere limitati da Retool).

## 11. Differenze rispetto ad `analizzatore_retool`

| Aspetto | analizzatore_retool | estrattore_sql_retool |
|---|---|---|
| Query GUI | ignorate, o lette dal testo residuo | lette da `tableName` |
| Query Library | copia nell'app, oggetti mescolati a quelli dell'app | voce a sé con `usata_da`, versione `latest` |
| Viste vs tabelle | non distinte (`information_schema.columns`) | distinte (`TABLES.TABLE_TYPE`) |
| CALL multiple | solo la prima | tutte |
| CTE, `INTO @var` | contate come tabelle | escluse |
| Parse fallito | query persa | estrazione di riserva con regex |
| Download | una chiamata CLI/Node per app, ogni volta | un processo, incrementale, per uuid |
| Pagine | app nei menu + lista manuale | tutte le 746 pagine |
