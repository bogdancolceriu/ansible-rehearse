"""Compare two snapshots and produce a categorized state diff."""

from __future__ import annotations

from .models import Change, FileEntry, Snapshot, StateDiff


def _diff_packages(before: dict[str, str], after: dict[str, str]) -> list[Change]:
    changes: list[Change] = []
    for name in sorted(after.keys() - before.keys()):
        changes.append(Change("added", name, after=after[name]))
    for name in sorted(before.keys() - after.keys()):
        changes.append(Change("removed", name, before=before[name]))
    for name in sorted(before.keys() & after.keys()):
        if before[name] != after[name]:
            changes.append(Change("changed", name, before=before[name], after=after[name]))
    return changes


def _describe_file(entry: FileEntry) -> str:
    kind = {"f": "file", "d": "dir", "l": "symlink"}.get(entry.ftype, entry.ftype)
    desc = f"{kind}, {entry.mode} {entry.owner}:{entry.group}"
    if entry.ftype == "f":
        desc += f", {entry.size} B"
    if entry.ftype == "l" and entry.target:
        desc += f" -> {entry.target}"
    return desc


def _file_change_detail(old: FileEntry, new: FileEntry) -> str | None:
    details: list[str] = []
    if old.ftype != new.ftype:
        details.append(f"type {old.ftype} -> {new.ftype}")
    elif old.ftype == "f" and (
        (old.md5 and new.md5 and old.md5 != new.md5)
        or ((not old.md5 or not new.md5) and old.size != new.size)
    ):
        details.append("content")
    if old.mode != new.mode:
        details.append(f"mode {old.mode} -> {new.mode}")
    if (old.owner, old.group) != (new.owner, new.group):
        details.append(f"owner {old.owner}:{old.group} -> {new.owner}:{new.group}")
    if old.ftype == "l" and new.ftype == "l" and old.target != new.target:
        details.append(f"target {old.target} -> {new.target}")
    if not details:
        return None
    return ", ".join(details)


def _diff_files(before: dict[str, FileEntry], after: dict[str, FileEntry]) -> list[Change]:
    changes: list[Change] = []
    for path in sorted(after.keys() - before.keys()):
        changes.append(Change("added", path, detail=_describe_file(after[path])))
    for path in sorted(before.keys() - after.keys()):
        changes.append(Change("removed", path, detail=_describe_file(before[path])))
    for path in sorted(before.keys() & after.keys()):
        detail = _file_change_detail(before[path], after[path])
        if detail:
            changes.append(Change("changed", path, detail=detail))
    return changes


def _diff_services(before: Snapshot, after: Snapshot) -> list[Change] | None:
    if before.services is None or after.services is None:
        return None
    changes: list[Change] = []
    for name in sorted(after.services.keys() - before.services.keys()):
        entry = after.services[name]
        state = f"{entry.enabled}, {'running' if entry.running else 'stopped'}"
        changes.append(Change("added", name, after=state))
    for name in sorted(before.services.keys() - after.services.keys()):
        changes.append(Change("removed", name))
    for name in sorted(before.services.keys() & after.services.keys()):
        old, new = before.services[name], after.services[name]
        if old == new:
            continue
        details: list[str] = []
        if old.enabled != new.enabled:
            details.append(f"{old.enabled} -> {new.enabled}")
        if old.running != new.running:
            details.append(
                f"{'running' if old.running else 'stopped'} -> "
                f"{'running' if new.running else 'stopped'}"
            )
        changes.append(Change("changed", name, detail=", ".join(details)))
    return changes


def _diff_ports(before: Snapshot, after: Snapshot) -> list[Change] | None:
    if before.ports is None or after.ports is None:
        return None
    changes: list[Change] = []
    for key in sorted(after.ports.keys() - before.ports.keys()):
        entry = after.ports[key]
        label = f"{entry.proto} {entry.addr}:{entry.port}"
        changes.append(Change("added", label, detail=entry.process))
    for key in sorted(before.ports.keys() - after.ports.keys()):
        entry = before.ports[key]
        label = f"{entry.proto} {entry.addr}:{entry.port}"
        changes.append(Change("removed", label, detail=entry.process))
    return changes


def _diff_users(before: Snapshot, after: Snapshot) -> list[Change]:
    changes: list[Change] = []
    for name in sorted(after.users.keys() - before.users.keys()):
        entry = after.users[name]
        changes.append(
            Change("added", name, after=f"uid={entry.uid} gid={entry.gid} shell={entry.shell}")
        )
    for name in sorted(before.users.keys() - after.users.keys()):
        changes.append(Change("removed", name))
    for name in sorted(before.users.keys() & after.users.keys()):
        old, new = before.users[name], after.users[name]
        if old == new:
            continue
        details = [
            f"{fname} {getattr(old, fname)} -> {getattr(new, fname)}"
            for fname in ("uid", "gid", "home", "shell")
            if getattr(old, fname) != getattr(new, fname)
        ]
        changes.append(Change("changed", name, detail=", ".join(details)))
    return changes


def _diff_groups(before: Snapshot, after: Snapshot) -> list[Change]:
    changes: list[Change] = []
    for name in sorted(after.groups.keys() - before.groups.keys()):
        entry = after.groups[name]
        changes.append(Change("added", name, after=f"gid={entry.gid}"))
    for name in sorted(before.groups.keys() - after.groups.keys()):
        changes.append(Change("removed", name))
    for name in sorted(before.groups.keys() & after.groups.keys()):
        old, new = before.groups[name], after.groups[name]
        if old == new:
            continue
        details: list[str] = []
        if old.gid != new.gid:
            details.append(f"gid {old.gid} -> {new.gid}")
        added_members = set(new.members) - set(old.members)
        removed_members = set(old.members) - set(new.members)
        if added_members:
            details.append("members +" + ",".join(sorted(added_members)))
        if removed_members:
            details.append("members -" + ",".join(sorted(removed_members)))
        changes.append(Change("changed", name, detail=", ".join(details)))
    return changes


def diff_snapshots(before: Snapshot, after: Snapshot) -> StateDiff:
    return StateDiff(
        packages=_diff_packages(before.packages, after.packages),
        files=_diff_files(before.files, after.files),
        services=_diff_services(before, after),
        ports=_diff_ports(before, after),
        users=_diff_users(before, after),
        groups=_diff_groups(before, after),
    )
