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

# Nomi che le regex catturano ma non sono oggetti del DB
_NOT_RELATIONS = {"dual", "select", "values", "set", "where", "lateral"}


@dataclass
class Refs:
    """Riferimenti grezzi estratti da una o più query."""
    procedures: set[str] = field(default_factory=set)
    relations: set[str] = field(default_factory=set)
    functions: set[str] = field(default_factory=set)

    def update(self, other: "Refs") -> None:
        self.procedures |= other.procedures
        self.relations |= other.relations
        self.functions |= other.functions


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


def _refs_from_regex(sql: str) -> Refs:
    """Estrazione di riserva: regex su testo senza stringhe né commenti."""
    refs = Refs()
    text = _RE_ON_DUPLICATE.sub(" ", _strip_strings_and_comments(sql))
    for m in _RE_CALL.finditer(text):
        refs.procedures.add(_clean_name(m.group(1)))
    for m in _RE_RELATION.finditer(text):
        _add_relation(refs, m.group(1))
    for m in _RE_FUNCTION.finditer(text):
        refs.functions.add(_clean_name(m.group(1)))
    return refs


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
