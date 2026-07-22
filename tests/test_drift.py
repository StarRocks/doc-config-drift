"""Regression tests for the drift engine.

Each test encodes one of the parser/normalization gotchas that produced false
findings during development, so they can never silently regress. Run: pytest -q
(or: python3 -m pytest). No third-party deps required beyond pytest.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import drift_audit as da  # noqa: E402


# ── FE code parser ───────────────────────────────────────────────────────────
def test_fe_semicolon_in_comment_is_not_field_terminator():
    # A ';' inside @ConfField(comment="...") must not truncate the declaration.
    src = '''
    @ConfField(mutable = false, comment = "Immutable; requires an FE restart.")
    public static boolean enable_http_auth = false;
    '''
    p = da.parse_code_java(src)
    assert "enable_http_auth" in p
    assert p["enable_http_auth"]["default"] == "false"
    assert p["enable_http_auth"]["mutable"] is False


def test_fe_mutable_and_aliases():
    src = '''
    @ConfField(mutable = true, aliases = {"old_name"})
    public static long slow_query_analyze_threshold = 5000;
    '''
    p = da.parse_code_java(src)
    assert p["slow_query_analyze_threshold"]["mutable"] is True
    assert "old_name" in p["slow_query_analyze_threshold"]["aliases"]


# ── BE code parser ───────────────────────────────────────────────────────────
def test_be_alias_does_not_clobber_real_default():
    # CONF_Alias is not a declaration; it must not overwrite the real default.
    src = '''
    CONF_Int32(be_http_port, "8040");
    CONF_Alias(be_http_port, webserver_port);
    '''
    p = da.parse_code_cpp(src)
    assert p["be_http_port"]["default"] == "8040"


def test_be_strings_and_string_enum_are_parsed():
    src = '''
    CONF_Strings(sys_log_verbose_modules, "");
    CONF_String_enum(brpc_connection_type, "single", "single,pooled,short");
    '''
    p = da.parse_code_cpp(src)
    assert p["sys_log_verbose_modules"]["default"] == ""
    assert p["brpc_connection_type"]["default"] == "single"


def test_be_mutable_prefix():
    src = '''
    CONF_mBool(enable_json_flat, "true");
    CONF_Int32(be_port, "9060");
    '''
    p = da.parse_code_cpp(src)
    assert p["enable_json_flat"]["mutable"] is True
    assert p["be_port"]["mutable"] is False


# ── Docs parser ──────────────────────────────────────────────────────────────
def test_docs_bullets_and_fullwidth_colon(tmp_path):
    md = tmp_path / "x.md"
    md.write_text(
        "## Query\n\n"                       # section header — must be ignored
        "### `enable_json_flat`\n\n"
        "* Default: false\n* Is mutable: Yes\n\n"   # '*' bullet (rendered style)
        "### max_scalar_operator_flat_children\n\n"
        "- Default：10000\n- Is mutable: Yes\n"      # full-width colon '：'
    )
    d = da.parse_docs([md])
    assert "query" not in d                          # section header excluded
    assert d["enable_json_flat"]["default"] == "false"
    assert d["max_scalar_operator_flat_children"]["default"] == "10000"


# ── Normalization ────────────────────────────────────────────────────────────
def test_norm_default_arithmetic_and_suffixes():
    assert da.norm_default("2 \\* 3600") == da.norm_default("7200L")
    assert da.norm_default("256") == da.norm_default("256L")
    assert da.norm_default("10L * 60L") == ("num", 600)


def test_norm_default_empty_forms():
    for v in ('""', "Empty string", "an empty string", "{}", "-"):
        assert da.norm_default(v) == ("empty",)


# ── End-to-end diff behavior ─────────────────────────────────────────────────
def test_single_element_array_notation_is_not_drift():
    code = {"dump_log_modules": {"name": "dump_log_modules", "default": '{"query"}',
                                 "mutable": False, "aliases": []}}
    docs = {"dump_log_modules": {"name": "dump_log_modules", "default": "query",
                                 "mutable": "No", "source_file": "f.md"}}
    out = da.audit(code, docs)
    assert out["default_mismatch"] == []


def test_real_default_mismatch_is_reported():
    code = {"slow_query_analyze_threshold": {"name": "slow_query_analyze_threshold",
            "default": "5000", "mutable": True, "aliases": []}}
    docs = {"slow_query_analyze_threshold": {"name": "slow_query_analyze_threshold",
            "default": "5", "mutable": "Yes", "source_file": "f.md"}}
    out = da.audit(code, docs)
    assert [x["name"] for x in out["default_mismatch"]] == ["slow_query_analyze_threshold"]


def test_expression_default_is_skipped():
    # Runtime-expression code defaults can't be compared statically → not drift.
    code = {"p": {"name": "p", "default": "System.getenv(\"X\")", "mutable": False, "aliases": []}}
    docs = {"p": {"name": "p", "default": "somevalue", "mutable": "No", "source_file": "f.md"}}
    assert da.audit(code, docs)["default_mismatch"] == []


def test_mutability_mismatch_and_missing_default():
    code = {"a": {"name": "a", "default": "1", "mutable": True, "aliases": []},
            "b": {"name": "b", "default": "", "mutable": False, "aliases": []}}
    docs = {"a": {"name": "a", "default": "1", "mutable": "No", "source_file": "f.md"},
            "b": {"name": "b", "default": "", "mutable": "No", "source_file": "f.md"},
            "gone": {"name": "gone", "default": "1", "mutable": "No", "source_file": "f.md"}}
    out = da.audit(code, docs)
    assert [x["name"] for x in out["mutable_mismatch"]] == ["a"]
    assert [x["name"] for x in out["doc_missing_default"]] == ["b"]
    assert [x["name"] for x in out["stale_in_docs"]] == ["gone"]
