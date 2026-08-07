"""Parser tests against realistic collector output."""

from __future__ import annotations

import pytest

from ansible_rehearse.snapshot import (
    SnapshotError,
    collector_script,
    parse_snapshot,
    split_sections,
)

DPKG_OUTPUT = """\
###REHEARSE:BEGIN meta###
format=1
###REHEARSE:END###
###REHEARSE:BEGIN packages.dpkg###
adduser\t3.118ubuntu5
base-files\t12ubuntu4.7
nginx\t1.18.0-6ubuntu14.6
###REHEARSE:END###
###REHEARSE:BEGIN files###
d|0755|root|root|4096|/etc
f|0644|root|root|769|/etc/passwd
f|0600|root|shadow|501|/etc/shadow
l|0777|root|root|21|/etc/mtab -> ../proc/self/mounts
f|0644|root|root|12|/etc/pipe|name.conf
###REHEARSE:END###
###REHEARSE:BEGIN hashes###
d41d8cd98f00b204e9800998ecf8427e  /etc/passwd
0123456789abcdef0123456789abcdef  /etc/pipe|name.conf
###REHEARSE:END###
###REHEARSE:BEGIN services.unavailable###
###REHEARSE:END###
###REHEARSE:BEGIN ports###
tcp   LISTEN 0      511          0.0.0.0:80        0.0.0.0:*    users:(("nginx",pid=123,fd=6))
tcp   LISTEN 0      128          [::]:22           [::]:*
udp   UNCONN 0      0            127.0.0.53%lo:53  0.0.0.0:*
###REHEARSE:END###
###REHEARSE:BEGIN users###
root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
###REHEARSE:END###
###REHEARSE:BEGIN groups###
root:x:0:
sudo:x:27:alice,bob
###REHEARSE:END###
###REHEARSE:DONE###
"""


def test_parse_dpkg_snapshot() -> None:
    snap = parse_snapshot(DPKG_OUTPUT)
    assert snap.package_manager == "dpkg"
    assert snap.packages["nginx"] == "1.18.0-6ubuntu14.6"
    assert len(snap.packages) == 3


def test_parse_files_and_hashes() -> None:
    snap = parse_snapshot(DPKG_OUTPUT)
    assert snap.files["/etc"].ftype == "d"
    passwd = snap.files["/etc/passwd"]
    assert passwd.mode == "0644"
    assert passwd.size == 769
    assert passwd.md5 == "d41d8cd98f00b204e9800998ecf8427e"
    shadow = snap.files["/etc/shadow"]
    assert shadow.group == "shadow"
    assert shadow.md5 is None  # not hashed
    link = snap.files["/etc/mtab"]
    assert link.ftype == "l"
    assert link.target == "../proc/self/mounts"
    # A path containing '|' still parses (only the first 5 pipes split fields).
    weird = snap.files["/etc/pipe|name.conf"]
    assert weird.md5 == "0123456789abcdef0123456789abcdef"


def test_parse_services_unavailable_is_none() -> None:
    snap = parse_snapshot(DPKG_OUTPUT)
    assert snap.services is None


def test_parse_ports() -> None:
    snap = parse_snapshot(DPKG_OUTPUT)
    assert snap.ports is not None
    http = snap.ports[("tcp", "0.0.0.0", 80)]
    assert http.process == "nginx"
    assert ("tcp", "[::]", 22) in snap.ports
    assert ("udp", "127.0.0.53%lo", 53) in snap.ports


def test_parse_users_groups() -> None:
    snap = parse_snapshot(DPKG_OUTPUT)
    assert snap.users["root"].shell == "/bin/bash"
    assert snap.groups["sudo"].members == ("alice", "bob")
    assert snap.groups["root"].members == ()


RPM_WITH_SYSTEMD = """\
###REHEARSE:BEGIN meta###
format=1
###REHEARSE:END###
###REHEARSE:BEGIN packages.rpm###
bash\t5.1.8-9.el9
systemd\t252-46.el9
###REHEARSE:END###
###REHEARSE:BEGIN services.unitfiles###
sshd.service enabled enabled
nginx.service disabled disabled
getty@.service enabled enabled
###REHEARSE:END###
###REHEARSE:BEGIN services.running###
sshd.service loaded active running OpenSSH server daemon
###REHEARSE:END###
###REHEARSE:BEGIN users###
root:x:0:0:root:/root:/bin/bash
###REHEARSE:END###
###REHEARSE:BEGIN groups###
root:x:0:
###REHEARSE:END###
###REHEARSE:DONE###
"""


def test_parse_rpm_and_services() -> None:
    snap = parse_snapshot(RPM_WITH_SYSTEMD)
    assert snap.package_manager == "rpm"
    assert snap.packages["bash"] == "5.1.8-9.el9"
    assert snap.services is not None
    assert snap.services["sshd.service"].running is True
    assert snap.services["sshd.service"].enabled == "enabled"
    assert snap.services["nginx.service"].running is False
    # Two-column output (older systemd, no PRESET column) still parses.
    assert "getty@.service" in snap.services
    # No ports section -> not observable.
    assert snap.ports is None


def test_missing_meta_raises() -> None:
    with pytest.raises(SnapshotError):
        parse_snapshot("random garbage\nno sections here\n")


def test_crlf_tolerated() -> None:
    snap = parse_snapshot(DPKG_OUTPUT.replace("\n", "\r\n"))
    assert snap.packages["nginx"] == "1.18.0-6ubuntu14.6"


def test_split_sections_ignores_text_outside_sections() -> None:
    text = "noise before\n###REHEARSE:BEGIN meta###\nformat=1\n###REHEARSE:END###\nnoise after\n"
    sections = split_sections(text)
    assert sections == {"meta": ["format=1"]}


def test_collector_script_ships_with_package() -> None:
    script = collector_script()
    assert script.startswith("#!/bin/sh")
    assert "###REHEARSE:BEGIN" in script
    # The workspace must be pruned from the diff, and the script must not use bashisms.
    assert "/opt/ansible-rehearse" in script
    assert "[[" not in script
