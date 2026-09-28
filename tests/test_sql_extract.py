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


def test_imported_latest_uses_query_library():
    tpl = {
        "editorMode": "sql",
        "isImported": True,
        "playgroundQueryUuid": "u1",
        "playgroundQuerySaveId": "latest",
        "query": "call st_vecchia()",
    }
    library = {"u1": {"template": {"editorMode": "sql", "query": "call st_nuova({{ x }})"}}}
    assert refs_from_template(tpl, library).procedures == {"st_nuova"}
    # Versione fissata: si usa la copia nell'app
    assert refs_from_template({**tpl, "playgroundQuerySaveId": "123"}, library).procedures == {"st_vecchia"}


def test_only_own_plugins_and_sql_subtypes():
    app_state = {
        "plugins": {
            "q1": {"subtype": "SqlQueryUnified", "template": {"editorMode": "sql", "query": "SELECT * FROM a"}},
            "js": {"subtype": "JavascriptQuery", "template": {"query": "SELECT * FROM non_sql"}},
            "mod": {"subtype": "GlobalWidget", "template": {}},
        }
    }
    assert refs_from_app_state(app_state).relations == {"a"}


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
