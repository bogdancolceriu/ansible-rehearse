"""Parser tests against realistic collector output (format 2)."""

from __future__ import annotations

import pytest

from ansible_rehearse.snapshot import (
    SnapshotError,
    collector_script,
    parse_snapshot,
    split_sections,
)

# File records are NUL-delimited: y|mode|owner|group|size|mtime|path
_FILE_RECORDS = "\0".join(
    [
        "d|0755|root|root|4096|1700000000.0000000000|/etc",
        "f|0644|root|root|769|1700000001.1234567890|/etc/passwd",
        "f|0600|root|shadow|501|1700000002.0000000000|/etc/shadow",
        "f|0644|root|root|12|1700000003.0000000000|/etc/pipe|name.conf",
        "f|0644|root|root|9|1700000004.0000000000|/etc/weird\nname.conf",
        "l|0777|root|root|21|1700000005.0000000000|/etc/mtab -> ../proc/self/mounts",
    ]
)

DPKG_OUTPUT = (
    """\
###REHEARSE[0]:BEGIN meta###
format=2
###REHEARSE[0]:END###
###REHEARSE[0]:BEGIN packages.dpkg###
adduser\t3.118ubuntu5
base-files\t12ubuntu4.7
nginx\t1.18.0-6ubuntu14.6
###REHEARSE[0]:END###
###REHEARSE[0]:BEGIN files###
"""
    + _FILE_RECORDS
    + """
###REHEARSE[0]:END###
###REHEARSE[0]:BEGIN hashes###
d41d8cd98f00b204e9800998ecf8427e  /etc/passwd
0123456789abcdef0123456789abcdef  /etc/pipe|name.conf
\\fedcba9876543210fedcba9876543210  /etc/weird\\nname.conf
###REHEARSE[0]:END###
###REHEARSE[0]:BEGIN services.unavailable###
###REHEARSE[0]:END###
###REHEARSE[0]:BEGIN ports###
tcp   LISTEN 0      511          0.0.0.0:80        0.0.0.0:*    users:(("nginx",pid=123,fd=6))
tcp   LISTEN 0      128          [::]:22           [::]:*
udp   UNCONN 0      0            127.0.0.53%lo:53  0.0.0.0:*
###REHEARSE[0]:END###
###REHEARSE[0]:BEGIN users###
root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
###REHEARSE[0]:END###
###REHEARSE[0]:BEGIN groups###
root:x:0:
sudo:x:27:alice,bob
###REHEARSE[0]:END###
###REHEARSE[0]:DONE###
"""
)


def test_parse_dpkg_snapshot() -> None:
    snap = parse_snapshot(DPKG_OUTPUT)
    assert snap.package_manager == "dpkg"
    assert snap.packages["nginx"] == "1.18.0-6ubuntu14.6"
    assert len(snap.packages) == 3


def test_parse_files_and_hashes() -> None:
    snap = parse_snapshot(DPKG_OUTPUT)
    assert snap.files["/etc"].ftype == "d"
    assert snap.files["/etc"].mtime is None  # mtime only kept for regular files
    passwd = snap.files["/etc/passwd"]
    assert passwd.mode == "0644"
    assert passwd.size == 769
    assert passwd.mtime == "1700000001.1234567890"
    assert passwd.md5 == "d41d8cd98f00b204e9800998ecf8427e"
    shadow = snap.files["/etc/shadow"]
    assert shadow.group == "shadow"
    assert shadow.md5 is None  # not hashed
    link = snap.files["/etc/mtab"]
    assert link.ftype == "l"
    assert link.target == "../proc/self/mounts"
    # A path containing '|' still parses (only the first 6 pipes split fields).
    assert snap.files["/etc/pipe|name.conf"].md5 == "0123456789abcdef0123456789abcdef"


def test_newline_in_filename_survives_nul_protocol() -> None:
    snap = parse_snapshot(DPKG_OUTPUT)
    weird = snap.files["/etc/weird\nname.conf"]
    assert weird.size == 9
    # GNU md5sum escapes the newline and prefixes the line with a backslash;
    # the parser reverses that, so the hash still attaches to the right path.
    assert weird.md5 == "fedcba9876543210fedcba9876543210"


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
###REHEARSE[0]:BEGIN meta###
format=2
###REHEARSE[0]:END###
###REHEARSE[0]:BEGIN packages.rpm###
bash\t5.1.8-9.el9
systemd\t252-46.el9
###REHEARSE[0]:END###
###REHEARSE[0]:BEGIN services.unitfiles###
sshd.service enabled enabled
nginx.service disabled disabled
getty@.service enabled enabled
###REHEARSE[0]:END###
###REHEARSE[0]:BEGIN services.running###
sshd.service loaded active running OpenSSH server daemon
###REHEARSE[0]:END###
###REHEARSE[0]:BEGIN users###
root:x:0:0:root:/root:/bin/bash
###REHEARSE[0]:END###
###REHEARSE[0]:BEGIN groups###
root:x:0:
###REHEARSE[0]:END###
###REHEARSE[0]:DONE###
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


def test_truncated_stream_raises() -> None:
    truncated = DPKG_OUTPUT.split("###REHEARSE[0]:DONE###")[0]
    with pytest.raises(SnapshotError, match="DONE"):
        parse_snapshot(truncated)


def test_crlf_tolerated() -> None:
    # CRLF can appear on line-oriented sections (docker on Windows); NUL records
    # are unaffected by definition.
    snap = parse_snapshot(RPM_WITH_SYSTEMD.replace("\n", "\r\n"))
    assert snap.packages["bash"] == "5.1.8-9.el9"


def test_forged_markers_without_the_nonce_are_inert() -> None:
    # A crafted filename embedding marker-looking lines cannot terminate or
    # inject sections, because real markers carry an unpredictable nonce.
    nonce = "a1b2c3d4e5f6a7b8"
    evil_record = (
        "f|0644|root|root|5|1700000009.0|/etc/evil\n###REHEARSE[0]:END###\n"
        "###REHEARSE[0]:BEGIN packages.dpkg###\nfakepkg\t9.9"
    )
    text = (
        f"###REHEARSE[{nonce}]:BEGIN meta###\nformat=2\n###REHEARSE[{nonce}]:END###\n"
        f"###REHEARSE[{nonce}]:BEGIN files###\n" + evil_record + "\0\n"
        f"###REHEARSE[{nonce}]:END###\n"
        f"###REHEARSE[{nonce}]:DONE###\n"
    )
    snap = parse_snapshot(text, nonce=nonce)
    # The forged package section never materializes...
    assert snap.packages == {}
    # ...and the hostile path is recorded as ONE file entry, markers included.
    assert any(path.startswith("/etc/evil") for path in snap.files)


def test_split_sections_ignores_text_outside_sections() -> None:
    text = (
        "noise before\n###REHEARSE[0]:BEGIN meta###\nformat=2\n###REHEARSE[0]:END###\nnoise after\n"
    )
    sections = split_sections(text)
    assert list(sections) == ["meta"]
    assert sections["meta"].strip() == "format=2"


def test_collector_script_ships_with_package() -> None:
    script = collector_script()
    assert script.startswith("#!/bin/sh")
    # Markers carry the per-snapshot nonce (REHEARSE_NONCE), default "0".
    assert "###REHEARSE[%s]:BEGIN" in script
    assert 'NONCE="${REHEARSE_NONCE:-0}"' in script
    # Held packages must still count as installed (dpkg_selections: hold).
    assert "'^[hi]i'" in script
    # Only known workspace subdirs are pruned, and no bashisms.
    assert '"$SELF_DIR/venv"' in script
    assert "[[" not in script
