"""Fidelity classification tests."""

from __future__ import annotations

from ansible_rehearse.fidelity import (
    FIDELITY_APPROXIMATE,
    FIDELITY_DESCRIPTIONS,
    FIDELITY_EXACT,
    FIDELITY_EXTERNAL,
    FIDELITY_NEEDS_SYSTEMD,
    FIDELITY_NOT_REHEARSABLE,
    FIDELITY_REAL_EXEC,
    FIDELITY_UNKNOWN,
    classify,
    external_reason,
    matrix,
    short_name,
)


def test_short_name_strips_collection() -> None:
    assert short_name("ansible.builtin.apt") == "apt"
    assert short_name("apt") == "apt"
    assert short_name("community.general.htpasswd") == "htpasswd"


def test_fqcn_and_short_name_classify_identically() -> None:
    assert classify("ansible.builtin.apt")[0] == classify("apt")[0] == FIDELITY_EXACT


def test_shell_is_real_exec() -> None:
    fidelity, note = classify("ansible.builtin.shell")
    assert fidelity == FIDELITY_REAL_EXEC
    assert "--check" in note


def test_service_depends_on_systemd_mode() -> None:
    assert classify("ansible.builtin.service", systemd=False)[0] == FIDELITY_NEEDS_SYSTEMD
    assert classify("ansible.builtin.service", systemd=True)[0] == FIDELITY_EXACT


def test_sysctl_approximate() -> None:
    assert classify("ansible.posix.sysctl")[0] == FIDELITY_APPROXIMATE


def test_reboot_not_rehearsable() -> None:
    assert classify("ansible.builtin.reboot")[0] == FIDELITY_NOT_REHEARSABLE


def test_unknown_module_is_flagged() -> None:
    fidelity, note = classify("community.general.some_exotic_module")
    assert fidelity == FIDELITY_UNKNOWN
    assert "approximate" in note


def test_cloud_collection_external() -> None:
    assert classify("amazon.aws.ec2_instance")[0] == FIDELITY_EXTERNAL
    assert external_reason("amazon.aws.ec2_instance") is not None


def test_short_cloud_prefix_external() -> None:
    assert external_reason("ec2_instance") is not None
    assert external_reason("route53") is not None


def test_notification_module_external() -> None:
    assert external_reason("community.general.mail") is not None


def test_uri_method_sensitivity() -> None:
    assert external_reason("ansible.builtin.uri", {"method": "POST"}) is not None
    assert external_reason("ansible.builtin.uri", {"method": "get"}) is None
    assert external_reason("ansible.builtin.uri", {}) is None  # default GET
    assert external_reason("ansible.builtin.uri", None) is None


def test_normal_modules_not_external() -> None:
    for module in ("apt", "copy", "template", "user", "service", "shell"):
        assert external_reason(module) is None, module


def test_matrix_covers_all_descriptions() -> None:
    m = matrix()
    assert set(m) <= set(FIDELITY_DESCRIPTIONS)
    # every class in the matrix is non-empty and sorted
    for modules in m.values():
        assert modules == sorted(modules)
        assert modules
