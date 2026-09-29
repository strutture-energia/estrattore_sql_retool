from src.db_catalog import DbCatalog
from src.sql_extract import refs_from_app_state, refs_from_sql, refs_from_template


def test_call_multiple_statements_and_schema():
    refs = refs_from_sql("CALL `ecodomus`.st_a({{ x.value }});\nCALL st_b()")
    assert refs.procedures == {"ecodomus.st_a", "st_b"}


def test_call_with_trailing_comment_placeholder():
    sql = "call st_get_elenco({{a}}, {{b || null}})\n\n-- trigger {{ input_trigger_refresh.value }}"
    assert refs_from_sql(sql).procedures == {"st_get_elenco"}


def test_cte_names_are_not_relations():
    refs = refs_from_sql("WITH ultimi AS (SELECT * FROM documenti) SELECT * FROM ultimi u JOIN v_x ON 1=1")
    assert refs.relations == {"documenti", "v_x"}


def test_commented_sql_is_ignored():
    refs = refs_from_sql("/*SELECT * FROM vecchia_tabella*/ SELECT * FROM nuova -- JOIN altra")
    assert refs.relations == {"nuova"}


def test_placeholder_in_from_is_ignored():
    refs = refs_from_sql("SELECT * FROM {{ tabella.value }} WHERE id = {{ id }}")
    assert refs.relations == set()


def test_placeholder_with_braces():
    refs = refs_from_sql("SELECT * FROM t WHERE j = {{ JSON.stringify({a: 1}) }}")
    assert refs.relations == {"t"}


def test_dml_and_subqueries():
    refs = refs_from_sql("DELETE FROM t2 WHERE id IN (SELECT id FROM t3); UPDATE progetti SET a = '#' WHERE id = 1")
    assert refs.relations == {"t2", "t3", "progetti"}


def test_regex_fallback_on_parse_error():
    # Sintassi non valida: sqlglot fallisce, le regex recuperano comunque gli oggetti.
    sql = "SELECT a,, FROM tab_a JOIN tab_b ON (( WHERE x = '#' -- FROM commentata\n; CALL st_c(1)"
    refs = refs_from_sql(sql)
    assert {"tab_a", "tab_b"} <= refs.relations
    assert "commentata" not in refs.relations
    assert refs.procedures == {"st_c"}


def test_procedural_if_block_falls_back_to_regex():
    sql = (
        "IF @id IS NULL THEN\n  SELECT id INTO @id FROM declinazioni WHERE x = {{ a }};\n"
        "END IF;\nCALL st_d(@id)"
    )
    refs = refs_from_sql(sql)
    assert "declinazioni" in refs.relations
    assert "st_d" in refs.procedures


def test_select_into_variable_is_not_relation():
    refs = refs_from_sql("SELECT id INTO @id_decl FROM declinazioni; INSERT INTO t2 SELECT * FROM t3")
    assert refs.relations == {"declinazioni", "t2", "t3"}


def test_regex_fallback_ignores_on_duplicate_key_update():
    # Query rotta (INSERT commentato a metà): va in fallback regex.
    sql = (
        "-- INSERT INTO proprietari (a, is_proprietario)\n"
        "VALUES ({{ a }}, 1) ON DUPLICATE KEY UPDATE is_proprietario = 1;\n"
        "DELETE FROM proprietari WHERE a = {{ a }}"
    )
    refs = refs_from_sql(sql)
    assert refs.relations == {"proprietari"}


def test_gui_mode_uses_table_name_not_stale_query():
    tpl = {"editorMode": "gui", "tableName": "dizionario_documenti", "query": "update altra_tabella set x"}
    refs = refs_from_template(tpl)
    assert refs.relations == {"dizionario_documenti"}


def _plugin(template):
    return {"subtype": "SqlQueryUnified", "template": template}


def test_imported_latest_is_library_reference_not_own_objects():
    library = {"u1": {"nome": "lib_q", "template": {"editorMode": "sql", "query": "call st_nuova({{ x }})"}}}
    app_state = {"plugins": {
        "imp": _plugin({"isImported": True, "playgroundQueryUuid": "u1",
                        "playgroundQuerySaveId": "latest", "query": "call st_vecchia()"}),
        "own": _plugin({"editorMode": "sql", "query": "SELECT * FROM propria"}),
    }}
    page = refs_from_app_state(app_state, library)
    assert page.library_uuids == {"u1"}
    assert page.own.procedures == set()          # né la copia né la Library finiscono nella pagina
    assert page.own.relations == {"propria"}
    assert refs_from_template(library["u1"]["template"]).procedures == {"st_nuova"}


def test_imported_pinned_or_unknown_counts_as_own_query():
    library = {"u1": {"nome": "lib_q", "template": {"query": "call st_nuova()"}}}
    pinned = _plugin({"isImported": True, "playgroundQueryUuid": "u1",
                      "playgroundQuerySaveId": "123", "query": "call st_fissata()"})
    unknown = _plugin({"isImported": True, "playgroundQueryUuid": "privata-altrui",
                       "playgroundQuerySaveId": "latest", "query": "call st_privata()"})
    page = refs_from_app_state({"plugins": {"a": pinned, "b": unknown}}, library)
    assert page.library_uuids == set()
    assert page.own.procedures == {"st_fissata", "st_privata"}


def test_library_query_with_internal_logic():
    sql = """START TRANSACTION;
UPDATE anagrafica SET codice_fiscale = COALESCE(NULLIF({{ codice_fiscale }}, '#'), codice_fiscale)
WHERE id_anagrafica = {{ id_anagrafica }};
CALL st_upsert_recapito({{ id_anagrafica }}, {{ id_email }}, 'email', {{ email }}, @return_id_email);
/*
CALL st_upsert_indirizzo([ id_anagrafica ], 'sede legale', @return_id_indirizzo);
*/
COMMIT;"""
    refs = refs_from_template({"editorMode": "sql", "query": sql})
    assert refs.procedures == {"st_upsert_recapito"}
    assert refs.relations == {"anagrafica"}


def test_only_own_plugins_and_sql_subtypes():
    app_state = {
        "plugins": {
            "q1": {"subtype": "SqlQueryUnified", "template": {"editorMode": "sql", "query": "SELECT * FROM a"}},
            "js": {"subtype": "JavascriptQuery", "template": {"query": "SELECT * FROM non_sql"}},
            "mod": {"subtype": "GlobalWidget", "template": {}},
        }
    }
    assert refs_from_app_state(app_state).own.relations == {"a"}


def test_classification():
    catalog = DbCatalog(
        "ecodomus",
        tables={"Progetti": "BASE TABLE", "vw_gant": "VIEW"},
        routines={"st_a": "PROCEDURE", "fn_calc": "FUNCTION"},
    )
    refs = refs_from_sql(
        "CALL ecodomus.st_a(); CALL st_sparita();"
        "SELECT fn_calc(x), JSON_ARRAYAGG(y) FROM progetti JOIN vw_gant JOIN altro_db.t JOIN inesistente"
    )
    out = catalog.classify(refs)
    assert out["stored_procedure"] == ["st_a"]
    assert out["tabelle"] == ["Progetti"]
    assert out["viste"] == ["vw_gant"]
    assert out["funzioni"] == ["fn_calc"]
    assert out["non_trovati"] == ["altro_db.t", "inesistente", "st_sparita"]


# --- Scritture: operazione e colonne ---------------------------------------------------

def test_select_is_not_a_write():
    refs = refs_from_sql("SELECT * FROM anagrafica a JOIN progetti p ON 1=1 FOR UPDATE")
    assert refs.relations == {"anagrafica", "progetti"}
    assert refs.writes == {}


def test_insert_update_delete_with_columns():
    refs = refs_from_sql(
        "INSERT INTO t (a, b) SELECT a, b FROM s;"
        "UPDATE anagrafica SET pec = {{ pec }}, email = {{ email }} WHERE id = {{ id }};"
        "DELETE FROM log WHERE id = 1; TRUNCATE TABLE tmp"
    )
    assert refs.writes == {
        "t": {"I": {"a", "b"}},
        "anagrafica": {"U": {"pec", "email"}},
        "log": {"D": set()},
        "tmp": {"D": set()},
    }
    assert "s" not in refs.writes   # letta, non scritta


def test_insert_without_columns_and_upsert():
    refs = refs_from_sql(
        "INSERT INTO t VALUES (1, 2);"
        "INSERT INTO r (a, b) VALUES (1, 2) ON DUPLICATE KEY UPDATE b = VALUES(b)"
    )
    assert refs.writes == {"t": {"I*": set()}, "r": {"I": {"a", "b"}, "U": {"b"}}}


def test_multi_table_update_and_delete_follow_aliases():
    refs = refs_from_sql(
        "UPDATE a x JOIN b y ON x.id = y.id SET x.c = 1, y.d = 2, e = 3;"
        "DELETE a FROM a JOIN b ON a.id = b.id"
    )
    assert refs.writes == {"a": {"U": {"c", "e"}, "D": set()}, "b": {"U": {"d"}}}


def test_regex_fallback_writes_have_unknown_columns():
    refs = refs_from_sql("REPLACE INTO t (a) VALUES (1)")   # sqlglot non lo analizza
    assert refs.writes == {"t": {"I": None}}
    sql = "IF @x IS NULL THEN\n UPDATE progetti SET a = 1;\n SELECT * FROM altra FOR UPDATE;\nEND IF"
    assert refs_from_sql(sql).writes == {"progetti": {"U": None}}


def test_unknown_columns_absorb_known_ones():
    a, b = refs_from_sql("UPDATE t SET x = 1"), refs_from_sql("IF 1 THEN UPDATE t SET y = 1; END IF")
    a.update(b)
    assert a.writes == {"t": {"U": None}}


def test_gui_actions_are_writes_with_unknown_columns():
    upsert = refs_from_template({"editorMode": "gui", "tableName": "t", "actionType": "BULK_UPSERT_BY_KEY"})
    assert upsert.writes == {"t": {"I": None, "U": None}}
    blank = refs_from_template({"editorMode": "gui", "tableName": "t", "actionType": ""})
    assert blank.writes == {"t": {"?": None}}


def test_classification_of_writes():
    catalog = DbCatalog("ecodomus", tables={"Anagrafica": "BASE TABLE", "vw_x": "VIEW"}, routines={})
    refs = refs_from_sql(
        "UPDATE anagrafica SET pec = 1; UPDATE ecodomus.vw_x SET a = 1;"
        "INSERT INTO sparita (a) VALUES (1); SELECT * FROM anagrafica"
    )
    out = catalog.classify(refs)
    assert out["scritture"] == {"Anagrafica": {"U": ["pec"]}, "vw_x": {"U": ["a"]}}
    assert out["non_trovati"] == ["sparita"]
