"""Public smoke tests for SCIM filter expressions."""

from __future__ import annotations

from scimfilter import evaluate, parse, to_sql_where


def test_co_matches_regardless_of_case() -> None:
    assert evaluate('userName co "BJENSEN"', {"userName": "bjensen@example.com"}) is True


def test_presence_holds_for_an_empty_string() -> None:
    assert evaluate("displayName pr", {"displayName": ""}) is True


def test_string_literal_escapes_are_decoded() -> None:
    assert evaluate('userName eq "o\\"brien"', {"userName": 'o"brien'}) is True


def test_sql_parameters_follow_the_placeholders() -> None:
    assert to_sql_where('nick eq "a" and flag eq "b"') == (
        '("nick" = ? AND "flag" = ?)',
        ["a", "b"],
    )


def test_parsed_tree_keeps_the_path_spelling() -> None:
    assert parse('name.givenName eq "x"') == ("compare", "name.givenName", "eq", "x")