"""Diff engine tests on hand-crafted snapshots."""

from __future__ import annotations

from ansible_rehearse.models import (
    FileEntry,
    GroupEntry,
    PortEntry,
    ServiceEntry,
    Snapshot,
    UserEntry,
)
from ansible_rehearse.statediff import diff_snapshots


def _file(md5: str | None = "aa", mode: str = "0644", size: int = 10) -> FileEntry:
    return FileEntry(ftype="f", mode=mode, owner="root", group="root", size=size, md5=md5)


def test_package_add_remove_upgrade() -> None:
    before = Snapshot(packages={"bash": "5.1", "old": "1.0", "nginx": "1.18"})
    after = Snapshot(packages={"bash": "5.1", "nginx": "1.20", "new": "2.0"})
    diff = diff_snapshots(before, after)
    by_action = {(c.action, c.item) for c in diff.packages}
    assert ("added", "new") in by_action
    assert ("removed", "old") in by_action
    assert ("changed", "nginx") in by_action
    changed = next(c for c in diff.packages if c.item == "nginx")
    assert changed.before == "1.18"
    assert changed.after == "1.20"


def test_file_content_change_via_hash() -> None:
    before = Snapshot(files={"/etc/a.conf": _file(md5="aa")})
    after = Snapshot(files={"/etc/a.conf": _file(md5="bb")})
    diff = diff_snapshots(before, after)
    assert len(diff.files) == 1
    assert diff.files[0].action == "changed"
    assert "content" in (diff.files[0].detail or "")


def test_file_same_hash_no_change() -> None:
    before = Snapshot(files={"/etc/a.conf": _file(md5="aa")})
    after = Snapshot(files={"/etc/a.conf": _file(md5="aa")})
    assert diff_snapshots(before, after).files == []


def test_file_unhashed_falls_back_to_size() -> None:
    before = Snapshot(files={"/big.bin": _file(md5=None, size=10)})
    after = Snapshot(files={"/big.bin": _file(md5=None, size=20)})
    diff = diff_snapshots(before, after)
    assert diff.files[0].action == "changed"
    assert "content" in (diff.files[0].detail or "")


def test_file_mode_and_owner_change() -> None:
    before = Snapshot(files={"/etc/a": _file(mode="0644")})
    after = Snapshot(
        files={
            "/etc/a": FileEntry(ftype="f", mode="0600", owner="app", group="app", size=10, md5="aa")
        }
    )
    diff = diff_snapshots(before, after)
    detail = diff.files[0].detail or ""
    assert "mode 0644 -> 0600" in detail
    assert "owner root:root -> app:app" in detail


def test_file_added_and_removed_with_description() -> None:
    before = Snapshot(files={"/etc/gone": _file()})
    after = Snapshot(files={"/etc/new": _file(size=42)})
    diff = diff_snapshots(before, after)
    added = next(c for c in diff.files if c.action == "added")
    assert added.item == "/etc/new"
    assert "42 B" in (added.detail or "")
    removed = next(c for c in diff.files if c.action == "removed")
    assert removed.item == "/etc/gone"


def test_symlink_target_change() -> None:
    before = Snapshot(files={"/etc/link": FileEntry("l", "0777", "root", "root", 0, target="/old")})
    after = Snapshot(files={"/etc/link": FileEntry("l", "0777", "root", "root", 0, target="/new")})
    diff = diff_snapshots(before, after)
    assert "target /old -> /new" in (diff.files[0].detail or "")


def test_services_none_propagates() -> None:
    before = Snapshot(services=None)
    after = Snapshot(services={"x.service": ServiceEntry("enabled", True)})
    assert diff_snapshots(before, after).services is None


def test_service_enable_and_start() -> None:
    before = Snapshot(services={"nginx.service": ServiceEntry("disabled", False)})
    after = Snapshot(services={"nginx.service": ServiceEntry("enabled", True)})
    diff = diff_snapshots(before, after)
    assert diff.services is not None
    detail = diff.services[0].detail or ""
    assert "disabled -> enabled" in detail
    assert "stopped -> running" in detail


def test_new_service_unit() -> None:
    before = Snapshot(services={})
    after = Snapshot(services={"demo.service": ServiceEntry("enabled", True)})
    diff = diff_snapshots(before, after)
    assert diff.services is not None
    assert diff.services[0].action == "added"
    assert diff.services[0].after == "enabled, running"


def test_ports_opened_and_closed() -> None:
    before = Snapshot(ports={("tcp", "0.0.0.0", 22): PortEntry("tcp", "0.0.0.0", 22, "sshd")})
    after = Snapshot(ports={("tcp", "0.0.0.0", 80): PortEntry("tcp", "0.0.0.0", 80, "nginx")})
    diff = diff_snapshots(before, after)
    assert diff.ports is not None
    actions = {(c.action, c.item) for c in diff.ports}
    assert ("added", "tcp 0.0.0.0:80") in actions
    assert ("removed", "tcp 0.0.0.0:22") in actions


def test_users_and_groups() -> None:
    before = Snapshot(
        users={"root": UserEntry("0", "0", "/root", "/bin/bash")},
        groups={"sudo": GroupEntry("27", ())},
    )
    after = Snapshot(
        users={
            "root": UserEntry("0", "0", "/root", "/bin/bash"),
            "app": UserEntry("999", "999", "/home/app", "/usr/sbin/nologin"),
        },
        groups={"sudo": GroupEntry("27", ("app",))},
    )
    diff = diff_snapshots(before, after)
    assert diff.users[0].action == "added"
    assert diff.users[0].item == "app"
    assert "uid=999" in (diff.users[0].after or "")
    assert diff.groups[0].action == "changed"
    assert "members +app" in (diff.groups[0].detail or "")


def test_empty_diff_counts() -> None:
    snap = Snapshot(packages={"bash": "5.1"})
    diff = diff_snapshots(snap, snap)
    assert diff.total_changes() == 0
    assert diff.counts() == {"added": 0, "removed": 0, "changed": 0}


def test_to_dict_serializable() -> None:
    import json

    before = Snapshot(packages={"a": "1"})
    after = Snapshot(packages={"a": "2"}, services={"s.service": ServiceEntry("enabled", True)})
    diff = diff_snapshots(before, after)
    payload = json.dumps(diff.to_dict())
    assert '"changed"' in payload
    # services diff is None on the before side -> serialized as null
    assert json.loads(payload)["services"] is None
