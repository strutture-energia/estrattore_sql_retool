"""
Estrazione dei riferimenti a oggetti del database dalle query SQL di una pagina Retool.

Una query SQL in Retool è un plugin con `subtype` in SQL_SUBTYPES. Dentro `template`:
  - editorMode "sql": il testo è in `query`, con placeholder JS `{{ ... }}`;
  - editorMode "gui": la tabella è in `tableName` (+ `actionType`), `query` va ignorato
    perché può contenere testo residuo di quando la query era in modalità SQL;
  - isImported: query della Query Library. Con `playgroundQuerySaveId == "latest"` Retool
    esegue l'ultima versione della Library: la query è un componente a sé (come un modulo),
    viene registrata come riferimento e analizzata una sola volta, fuori dalla pagina.
    Con una versione fissata la copia salvata nella pagina conta come query propria.

Si analizza solo `page.plugins` della pagina stessa: le query dei moduli incorporati
appartengono al modulo, che viene analizzato come pagina a sé.

Il risultato sono nomi "grezzi" (ancora da classificare contro information_schema):
  - procedures: nomi dopo CALL
  - relations:  tabelle o viste (FROM, JOIN, INTO, UPDATE, tableName GUI...)
  - functions:  chiamate a funzione non native per sqlglot (candidate funzioni utente)
  - writes:     le relazioni SCRITTE, con operazione e colonne (vedi `Refs.writes`)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import sqlglot
import sqlglot.expressions as exp
from sqlglot.errors import SqlglotError

log = logging.getLogger(__name__)
logging.getLogger("sqlglot").setLevel(logging.ERROR)

SQL_SUBTYPES = {"SqlQueryUnified", "SqlQuery"}

# Placeholder Retool `{{ ... }}`. Non-greedy fino alla prima `}}` così gestisce anche
# espressioni JS con graffe singole, es. {{ JSON.stringify({a: 1}) }}.
_RETOOL_PLACEHOLDER = re.compile(r"\{\{.*?\}\}", re.DOTALL)

# I placeholder diventano parametri con un nome riconoscibile: se finiscono in posizione
# di tabella (`FROM {{ x }}`) sqlglot li restituisce come tabella e li scartiamo.
_PH_PREFIX = "retool_ph_"

# Nome SQL eventualmente qualificato e/o tra backtick: `db`.`tab`, db.tab, tab
_IDENT = r"(?:`[^`]+`|[A-Za-z_$][\w$]*)"
_QUALIFIED = rf"{_IDENT}(?:\s*\.\s*{_IDENT})?"

_CALL_NAME = re.compile(rf"^\s*({_QUALIFIED})")

# Rimozione di stringhe e commenti in un solo passaggio: l'alternanza trova sempre
# il token che inizia prima, così `'#'` (sentinel Ecodomus) non viene preso per un
# commento e un apostrofo dentro un commento non apre una stringa.
_STRINGS_AND_COMMENTS = re.compile(
    r"'(?:[^'\\]|\\.|'')*'"
    r'|"(?:[^"\\]|\\.|"")*"'
    r"|/\*.*?\*/"
    r"|--[^\n]*"
    r"|#[^\n]*",
    re.DOTALL,
)

# Estrazione di riserva (quando sqlglot non riesce a parsare)
_RE_RELATION = re.compile(rf"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+({_QUALIFIED})", re.IGNORECASE)
_RE_CALL = re.compile(rf"\bCALL\s+({_QUALIFIED})", re.IGNORECASE)
_RE_FUNCTION = re.compile(rf"({_IDENT})\s*\(")
# `ON DUPLICATE KEY UPDATE col = ...`: quell'UPDATE è seguito da una colonna, non da una tabella.
_RE_ON_DUPLICATE = re.compile(r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", re.IGNORECASE)
# `SELECT ... FOR UPDATE` è un lock, non una scrittura: senza toglierlo la regex di UPDATE
# prenderebbe per tabella la parola che segue.
_RE_FOR_UPDATE = re.compile(r"\bFOR\s+UPDATE\b", re.IGNORECASE)

# Scritture nell'estrazione di riserva: le colonne non si ricavano, restano ignote (None).
_RE_WRITE = [
    (re.compile(rf"\b(?:INSERT|REPLACE)\s+(?:IGNORE\s+)?(?:INTO\s+)?({_QUALIFIED})", re.IGNORECASE), "I"),
    (re.compile(rf"\bUPDATE\s+(?:IGNORE\s+)?({_QUALIFIED})", re.IGNORECASE), "U"),
    (re.compile(rf"\bDELETE\s+FROM\s+({_QUALIFIED})", re.IGNORECASE), "D"),
    (re.compile(rf"\bTRUNCATE\s+(?:TABLE\s+)?({_QUALIFIED})", re.IGNORECASE), "D"),
]

# Query GUI: `actionType` -> operazioni. Le colonne vengono da un changeset o da record
# costruiti a runtime, quindi restano ignote. Il tipo non riconosciuto si tratta come
# scrittura generica "?": meglio dichiararla che perderla. In modalità GUI Retool fa solo
# scritture, quindi anche un `actionType` vuoto è una scrittura di tipo sconosciuto.
GUI_ACTIONS = {
    "INSERT": ("I",),
    "BULK_INSERT": ("I",),
    "UPDATE_BY": ("U",),
    "BULK_UPDATE_BY_KEY": ("U",),
    "UPSERT_BY": ("I", "U"),
    "BULK_UPSERT_BY_KEY": ("I", "U"),
    "DELETE_BY": ("D",),
    "BULK_DELETE_BY_KEY": ("D",),
}

# Nomi che le regex catturano ma non sono oggetti del DB
_NOT_RELATIONS = {"dual", "select", "values", "set", "where", "lateral"}


@dataclass
class Refs:
    """Riferimenti grezzi estratti da una o più query."""
    procedures: set[str] = field(default_factory=set)
    relations: set[str] = field(default_factory=set)
    functions: set[str] = field(default_factory=set)
    # relazione -> {operazione: colonne}. Operazioni: "I" insert, "I*" insert senza elenco
    # colonne (riga intera), "U" update, "D" delete/truncate (riga intera, colonne vuote),
    # "?" scrittura GUI di tipo sconosciuto. Colonne None = scritte ma non ricavabili.
    writes: dict[str, dict[str, set[str] | None]] = field(default_factory=dict)

    def add_write(self, relation: str, op: str, columns: set[str] | None) -> None:
        ops = self.writes.setdefault(relation, {})
        if op not in ops:
            ops[op] = None if columns is None else set(columns)
        elif ops[op] is not None:
            # Una sola query con colonne ignote rende ignote quelle dell'operazione intera.
            ops[op] = None if columns is None else ops[op] | columns

    def update(self, other: "Refs") -> None:
        self.procedures |= other.procedures
        self.relations |= other.relations
        self.functions |= other.functions
        for rel, ops in other.writes.items():
            for op, cols in ops.items():
                self.add_write(rel, op, cols)


def _clean_name(raw: str) -> str:
    """`db` . `tab` -> db.tab (senza backtick né spazi)."""
    parts = [p.strip().strip("`") for p in raw.split(".")]
    return ".".join(p for p in parts if p)


def _sanitize_placeholders(sql: str) -> str:
    counter = 0

    def _sub(_: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        return f":{_PH_PREFIX}{counter}"

    return _RETOOL_PLACEHOLDER.sub(_sub, sql)


def _strip_strings_and_comments(sql: str) -> str:
    def _sub(m: re.Match[str]) -> str:
        tok = m.group(0)
        return "''" if tok[0] in "'\"" else " "

    return _STRINGS_AND_COMMENTS.sub(_sub, sql)


def _is_placeholder(name: str) -> bool:
    return name.lower().split(".")[-1].startswith(_PH_PREFIX)


def _add_relation(refs: Refs, name: str) -> None:
    name = _clean_name(name)
    if name and not _is_placeholder(name) and name.lower() not in _NOT_RELATIONS:
        refs.relations.add(name)


def _add_write(refs: Refs, name: str, op: str, columns: set[str] | None) -> None:
    name = _clean_name(name)
    if name and not _is_placeholder(name) and name.lower() not in _NOT_RELATIONS:
        refs.add_write(name, op, columns)


def _refs_from_regex(sql: str) -> Refs:
    """Estrazione di riserva: regex su testo senza stringhe né commenti."""
    refs = Refs()
    text = _RE_ON_DUPLICATE.sub(" ", _strip_strings_and_comments(sql))
    text = _RE_FOR_UPDATE.sub(" ", text)
    for rx, op in _RE_WRITE:
        for m in rx.finditer(text):
            _add_write(refs, m.group(1), op, None)
    for m in _RE_CALL.finditer(text):
        refs.procedures.add(_clean_name(m.group(1)))
    for m in _RE_RELATION.finditer(text):
        _add_relation(refs, m.group(1))
    for m in _RE_FUNCTION.finditer(text):
        refs.functions.add(_clean_name(m.group(1)))
    return refs


def _table_name(t: exp.Table) -> str:
    return f"{t.db}.{t.name}" if t.db else t.name


def _writes_from_statement(stmt: exp.Expression, refs: Refs) -> None:
    """Scritture di un INSERT / UPDATE / DELETE / TRUNCATE analizzato da sqlglot."""
    if isinstance(stmt, exp.Insert):
        target = stmt.this
        if isinstance(target, exp.Schema) and isinstance(target.this, exp.Table):
            cols = {c.name for c in target.expressions if c.name}
            _add_write(refs, _table_name(target.this), "I" if cols else "I*", cols)
            table = target.this
        elif isinstance(target, exp.Table):
            _add_write(refs, _table_name(target), "I*", set())
            table = target
        else:
            return
        conflict = stmt.args.get("conflict")
        if conflict is not None:
            # ON DUPLICATE KEY UPDATE: la riga esistente viene aggiornata su queste colonne.
            cols = {e.this.name for e in conflict.expressions or [] if isinstance(e, exp.EQ) and isinstance(e.this, exp.Column)}
            _add_write(refs, _table_name(table), "U", cols)

    elif isinstance(stmt, exp.Update):
        if not isinstance(stmt.this, exp.Table):
            return
        # UPDATE a x JOIN b y ... SET x.c = 1, y.d = 2: la colonna va alla tabella del suo alias.
        tables = [stmt.this] + [j.this for j in stmt.this.args.get("joins") or [] if isinstance(j.this, exp.Table)]
        by_alias = {}
        for t in tables:
            by_alias[(t.alias or t.name).lower()] = t
            by_alias.setdefault(t.name.lower(), t)
        cols: dict[str, set[str]] = {}
        for e in stmt.expressions:
            if not (isinstance(e, exp.EQ) and isinstance(e.this, exp.Column)):
                continue
            t = by_alias.get(e.this.table.lower(), stmt.this) if e.this.table else stmt.this
            cols.setdefault(_table_name(t), set()).add(e.this.name)
        for name, c in cols.items():
            _add_write(refs, name, "U", c)

    elif isinstance(stmt, exp.Delete):
        # DELETE a FROM a JOIN b ...: cancella solo da `tables`; altrimenti dalla tabella principale.
        targets = stmt.args.get("tables") or [stmt.this]
        for t in targets:
            if isinstance(t, exp.Table):
                _add_write(refs, _table_name(t), "D", set())

    elif isinstance(stmt, exp.TruncateTable):
        for t in stmt.expressions:
            if isinstance(t, exp.Table):
                _add_write(refs, _table_name(t), "D", set())


def _refs_from_statement(stmt: exp.Expression) -> Refs:
    refs = Refs()

    if isinstance(stmt, exp.Command):
        # sqlglot non analizza CALL (né blocchi IF...THEN ecc.): il resto dello statement
        # è un Literal di testo oppure, a seconda del comando, una semplice str.
        expr = stmt.expression
        rest = expr.name if isinstance(expr, exp.Expression) else str(expr or "")
        if str(stmt.this).upper() == "CALL":
            m = _CALL_NAME.match(rest)
            if m:
                refs.procedures.add(_clean_name(m.group(1)))
            # Gli argomenti possono contenere funzioni utente: fn_x(...)
            refs.functions |= _refs_from_regex(rest[m.end():] if m else rest).functions
        else:
            # Sintassi non supportata da sqlglot: si ripiega sulle regex.
            refs.update(_refs_from_regex(f"{stmt.this} {rest}"))
        return refs

    cte_names = {c.alias.lower() for c in stmt.find_all(exp.CTE) if c.alias}
    for t in stmt.find_all(exp.Table):
        if not t.name:
            continue
        # SELECT ... INTO @var: sqlglot la rappresenta come tabella, ma in MySQL è una variabile.
        if isinstance(t.parent, exp.Into):
            continue
        if not t.db and t.name.lower() in cte_names:
            continue
        _add_relation(refs, f"{t.db}.{t.name}" if t.db else t.name)

    for fn in stmt.find_all(exp.Anonymous):
        if isinstance(fn.this, str) and fn.this:
            refs.functions.add(fn.this)

    _writes_from_statement(stmt, refs)
    return refs


def refs_from_sql(sql: str) -> Refs:
    """Estrae i riferimenti da un testo SQL Retool (con eventuali `{{ }}`)."""
    sanitized = _sanitize_placeholders(sql)
    try:
        statements = sqlglot.parse(sanitized, read="mysql")
    except SqlglotError:
        return _refs_from_regex(sanitized)

    refs = Refs()
    for stmt in statements:
        if stmt is not None:
            refs.update(_refs_from_statement(stmt))
    return refs


def library_uuid(template: dict, library: dict) -> str | None:
    """
    Uuid della query della Query Library a cui il plugin fa riferimento, se la pagina
    esegue la versione corrente della Library (`playgroundQuerySaveId` "latest").
    Con una versione fissata, o con una query della Library non visibile (privata di un
    altro utente), restituisce None: la copia salvata nella pagina conta come query propria.
    """
    if not template.get("isImported"):
        return None
    uuid = template.get("playgroundQueryUuid")
    if uuid and uuid in library and template.get("playgroundQuerySaveId") in (None, "", "latest"):
        return uuid
    return None


def refs_from_template(template: dict) -> Refs:
    """Estrae i riferimenti dal `template` di un plugin SQL (o di una query della Library)."""
    if template.get("editorMode") == "gui":
        refs = Refs()
        table = (template.get("tableName") or "").strip()
        if table and "{{" not in table:
            _add_relation(refs, table)
            action = template.get("actionType") or ""
            for op in GUI_ACTIONS.get(action, ("?",)):
                _add_write(refs, table, op, None)
        return refs

    query = template.get("query")
    if not isinstance(query, str) or not query.strip():
        return Refs()
    return refs_from_sql(query)


@dataclass
class PageRefs:
    """Riferimenti di una pagina: oggetti delle sue query + query della Library che importa."""
    own: Refs = field(default_factory=Refs)
    library_uuids: set[str] = field(default_factory=set)


def refs_from_app_state(app_state: dict, library: dict | None = None) -> PageRefs:
    """
    Analizza le query SQL di `page.plugins` (moduli incorporati esclusi).
    Le query importate dalla Library in versione "latest" non contribuiscono agli oggetti
    della pagina: vengono solo registrate in `library_uuids` e analizzate a parte.
    """
    library = library or {}
    result = PageRefs()
    plugins = app_state.get("plugins") or {}
    if not isinstance(plugins, dict):
        return result
    for pdef in plugins.values():
        if not isinstance(pdef, dict) or pdef.get("subtype") not in SQL_SUBTYPES:
            continue
        template = pdef.get("template")
        if not isinstance(template, dict):
            continue
        uuid = library_uuid(template, library)
        if uuid:
            result.library_uuids.add(uuid)
        else:
            result.own.update(refs_from_template(template))
    return result
