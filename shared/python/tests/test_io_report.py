import csv
import json

import pytest

from firmable_client.errors import InputError
from firmable_client.io import (find_duplicates, normalise_domain, read_rows, safe_cell, write_csv)
from firmable_client.report import Report, esc, link, safe_url


def test_read_csv_assigns_row_ids_and_keeps_order(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("domain,name\nacme.example,Acme\nbeta.example,Beta\n", encoding="utf-8")
    rows = read_rows(p, required=["domain"])
    assert [r["row_id"] for r in rows] == ["row-1", "row-2"]
    assert rows[1]["name"] == "Beta"


def test_read_json_rows_object(tmp_path):
    p = tmp_path / "in.json"
    p.write_text(json.dumps({"rows": [{"row_id": "a", "domain": "acme.example"}]}), encoding="utf-8")
    assert read_rows(p, required=["domain"])[0]["row_id"] == "a"


def test_read_reports_all_problems(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("row_id,domain\nx,acme.example\nx,\n", encoding="utf-8")
    with pytest.raises(InputError) as info:
        read_rows(p, required=["domain"])
    msg = str(info.value)
    assert "repeats row 1" in msg and "domain is empty" in msg


@pytest.mark.parametrize("bad", ["", "   \n", "not,a\n"])
def test_read_rejects_empty_or_missing_columns(tmp_path, bad):
    p = tmp_path / "in.csv"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(InputError):
        read_rows(p, required=["domain"])


def test_read_rejects_wrong_extension_and_bad_json(tmp_path):
    p = tmp_path / "in.txt"
    p.write_text("x", encoding="utf-8")
    with pytest.raises(InputError):
        read_rows(p)
    j = tmp_path / "in.json"
    j.write_text("{nope", encoding="utf-8")
    with pytest.raises(InputError):
        read_rows(j)


@pytest.mark.parametrize("raw,expected", [
    ("https://www.Acme.example/about?x=1", "acme.example"),
    ("acme.example.", "acme.example"),
    ("user@acme.example", "acme.example"),
    ("acme.example:8443", "acme.example"),
    ("not a domain", None),
    ("localhost", None),
    ("", None),
])
def test_normalise_domain(raw, expected):
    assert normalise_domain(raw) == expected


def test_find_duplicates():
    rows = [{"row_id": "a", "d": "x"}, {"row_id": "b", "d": "x"}, {"row_id": "c", "d": ""}]
    assert find_duplicates(rows, "d") == {"b": "a"}


@pytest.mark.parametrize("value,expected", [
    ("=HYPERLINK(\"http://x\")", "'=HYPERLINK(\"http://x\")"),
    ("+cmd", "'+cmd"), ("@SUM(1)", "'@SUM(1)"), ("-2+3", "'-2+3"), ("\tx", "'\tx"),
    ("-5", "-5"), ("3.5", "3.5"), (None, ""), (True, "true"), (2.0, "2"), (["a", "=b"], "a; '=b"),
])
def test_safe_cell(value, expected):
    assert safe_cell(value) == expected


def test_write_csv_neutralises_formulas(tmp_path):
    p = write_csv(tmp_path / "o.csv", ["a"], [{"a": "=1+1"}])
    assert list(csv.reader(p.read_text().splitlines()))[1] == ["'=1+1"]


def test_escape_and_safe_links():
    assert esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert safe_url("javascript:alert(1)") is None
    assert safe_url("https://acme.example/x") == "https://acme.example/x"
    assert "<a " not in link("javascript:alert(1)", "x")
    assert 'href="https://acme.example/?a=1&amp;b=2"' in link("https://acme.example/?a=1&b=2", "x")


def test_report_has_no_external_resources_and_escapes():
    result = {"mode": "test-fixture", "generated_at": "t", "input": {"file": "<b>.csv", "row_count": 1, "config": {}},
              "coverage": ["<img src=x onerror=alert(1)>"], "errors": [], "credits": {}, "ledger": []}
    r = Report("T", "recipe", result)
    r.add_request_section("desc")
    r.add_standard_sections()
    out = r.render()
    assert "<img" not in out and "&lt;img" in out
    assert "<script" not in out
    for bad in ("http://", "https://"):
        # the only URLs allowed are inside escaped text; no src or href to external resources
        assert f'src="{bad}' not in out and f"@import url({bad}" not in out


@pytest.mark.parametrize("phone", ["+61 412 345 678", "+1 (415) 555 0100", "+6141234"])
def test_phone_numbers_are_not_prefixed(phone):
    assert safe_cell(phone) == phone


def test_phone_like_formula_is_still_prefixed():
    assert safe_cell("+1+cmd|' /C calc'!A0").startswith("'")
