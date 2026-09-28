"""
Catalogo degli oggetti del database MySQL, letto una sola volta da information_schema,
e classificazione dei riferimenti grezzi estratti dalle query Retool.

Regole:
  - CALL x            -> stored_procedure se x è una PROCEDURE, altrimenti non_trovati
  - FROM/JOIN/... x   -> viste se TABLE_TYPE = VIEW, tabelle se tabella, altrimenti non_trovati
  - x(...)            -> funzioni solo se x è una FUNCTION; gli altri nomi sono funzioni
                         native di MySQL e vengono scartati
Il confronto è case-insensitive; in output si usa il nome come è scritto nel DB.
"""

from __future__ import annotations

import logging

import pymysql

from .sql_extract import Refs

log = logging.getLogger(__name__)


class DbCatalog:
    def __init__(
        self,
        database: str,
        tables: dict[str, str],
        routines: dict[str, str],
    ) -> None:
        """
        Args:
            database: nome dello schema (serve a riconoscere i nomi qualificati `db.oggetto`).
            tables:   {nome: TABLE_TYPE} ("BASE TABLE", "VIEW", ...)
            routines: {nome: ROUTINE_TYPE} ("PROCEDURE", "FUNCTION")
        """
        self.database = database
        # chiave minuscola -> (nome reale, tipo)
        self._tables = {n.lower(): (n, t) for n, t in tables.items()}
        self._routines = {n.lower(): (n, t) for n, t in routines.items()}

    @classmethod
    def load(cls, conn: pymysql.connections.Connection, database: str) -> "DbCatalog":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s",
                (database,),
            )
            tables = {name: ttype for name, ttype in cur.fetchall()}
            cur.execute(
                "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s",
                (database,),
            )
            routines = {name: rtype for name, rtype in cur.fetchall()}
        catalog = cls(database, tables, routines)
        log.info(
            "Catalogo DB `%s`: %d tabelle, %d viste, %d procedure, %d funzioni",
            database,
            sum(1 for t in tables.values() if t != "VIEW"),
            sum(1 for t in tables.values() if t == "VIEW"),
            sum(1 for t in routines.values() if t == "PROCEDURE"),
            sum(1 for t in routines.values() if t == "FUNCTION"),
        )
        return catalog

    def _local_key(self, name: str) -> str | None:
        """
        Chiave di lookup per un nome eventualmente qualificato.
        `db.x` con db = schema corrente -> "x"; qualificato su un altro schema -> None.
        """
        if "." in name:
            schema, obj = name.rsplit(".", 1)
            if schema.lower() != self.database.lower():
                return None
            name = obj
        return name.lower()

    def classify(self, refs: Refs) -> dict[str, list[str]]:
        out: dict[str, set[str]] = {
            "stored_procedure": set(),
            "viste": set(),
            "tabelle": set(),
            "funzioni": set(),
            "non_trovati": set(),
        }

        for name in refs.procedures:
            key = self._local_key(name)
            hit = self._routines.get(key) if key else None
            if hit and hit[1] == "PROCEDURE":
                out["stored_procedure"].add(hit[0])
            else:
                out["non_trovati"].add(name)

        for name in refs.relations:
            key = self._local_key(name)
            hit = self._tables.get(key) if key else None
            if hit:
                out["viste" if hit[1] == "VIEW" else "tabelle"].add(hit[0])
            else:
                out["non_trovati"].add(name)

        for name in refs.functions:
            key = self._local_key(name)
            hit = self._routines.get(key) if key else None
            if hit and hit[1] == "FUNCTION":
                out["funzioni"].add(hit[0])

        # Lo stesso nome non trovato può comparire con maiuscole diverse in query diverse.
        seen: dict[str, str] = {}
        for n in sorted(out["non_trovati"]):
            seen.setdefault(n.lower(), n)
        out["non_trovati"] = set(seen.values())

        return {k: sorted(v, key=str.lower) for k, v in out.items()}


def open_connection(host: str, port: int, user: str, password: str, database: str):
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
    )
