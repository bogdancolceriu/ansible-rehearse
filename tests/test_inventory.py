"""Inventory generation tests."""

from __future__ import annotations

from ansible_rehearse.inventory import build_inventory, hosts_groups


def test_plain_groups() -> None:
    groups, warnings = hosts_groups(["webservers", "dbservers"])
    assert groups == {"webservers", "dbservers"}
    assert warnings == []


def test_implicit_patterns_need_no_group() -> None:
    groups, _ = hosts_groups(["all", "*", "localhost"])
    assert groups == set()


def test_compound_pattern_split() -> None:
    groups, _ = hosts_groups(["web:db", "app,cache"])
    assert groups == {"web", "db", "app", "cache"}


def test_exclusion_and_intersection_prefixes_stripped() -> None:
    groups, _ = hosts_groups(["all:!excluded", "web:&staging"])
    assert groups == {"excluded", "staging", "web"}


def test_wildcard_pattern_warns() -> None:
    groups, warnings = hosts_groups(["web*"])
    assert groups == set()
    assert any("web*" in w for w in warnings)


def test_build_inventory_contents() -> None:
    text, warnings = build_inventory(["webservers"], python_interpreter="/usr/bin/python3")
    assert "[rehearsal]" in text
    assert "rehearsal-host ansible_connection=local" in text
    assert "ansible_python_interpreter=/usr/bin/python3" in text
    assert "[webservers]" in text
    assert warnings == []


def test_localhost_gets_explicit_entry() -> None:
    text, _ = build_inventory(["localhost"])
    assert "\nlocalhost ansible_connection=local" in text


def test_localhost_mixed_with_groups_warns_about_double_run() -> None:
    text, warnings = build_inventory(["localhost", "webservers"])
    assert "localhost ansible_connection=local" in text
    assert any("twice" in w for w in warnings)


def test_no_localhost_entry_without_localhost_play() -> None:
    text, _ = build_inventory(["webservers"])
    assert "\nlocalhost" not in text
