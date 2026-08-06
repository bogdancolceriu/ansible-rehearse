"""Parse the sectioned output of collect_state.sh into a Snapshot."""

from __future__ import annotations

import re
from importlib import resources

from .models import FileEntry, GroupEntry, PortEntry, ServiceEntry, Snapshot, UserEntry

SECTION_BEGIN = re.compile(r"^###REHEARSE:BEGIN ([a-z.]+)###$")
SECTION_END = "###REHEARSE:END###"
DONE_MARKER = "###REHEARSE:DONE###"

_MD5_LINE = re.compile(r"^([0-9a-f]{32})  (.+)$")


class SnapshotError(ValueError):
    """The collector output could not be parsed."""


def collector_script() -> str:
    """Return the collect_state.sh source shipped with the package."""
    return resources.files("ansible_rehearse").joinpath("data/collect_state.sh").read_text("utf-8")


def split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        match = SECTION_BEGIN.match(line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
            continue
        if line in (SECTION_END, DONE_MARKER):
            current = None
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def _parse_packages(lines: list[str]) -> dict[str, str]:
    packages: dict[str, str] = {}
    for line in lines:
        name, _, version = line.partition("\t")
        if name:
            packages[name.strip()] = version.strip()
    return packages


def _parse_file_line(line: str) -> tuple[str, FileEntry] | None:
    parts = line.split("|", 5)
    if len(parts) != 6:
        return None
    ftype, mode, owner, group, size, rest = parts
    if ftype not in ("f", "d", "l"):
        return None
    try:
        size_int = int(size)
    except ValueError:
        return None
    target: str | None = None
    path = rest
    if ftype == "l":
        # collect_state.sh prints symlinks as "PATH -> TARGET".
        path, sep, target = rest.partition(" -> ")
        if not sep:
            target = ""
    return path, FileEntry(
        ftype=ftype, mode=mode, owner=owner, group=group, size=size_int, target=target
    )


def _parse_files(lines: list[str], hash_lines: list[str]) -> dict[str, FileEntry]:
    hashes: dict[str, str] = {}
    for line in hash_lines:
        match = _MD5_LINE.match(line)
        if match:
            hashes[match.group(2)] = match.group(1)
    files: dict[str, FileEntry] = {}
    for line in lines:
        parsed = _parse_file_line(line)
        if parsed is None:
            continue
        path, entry = parsed
        md5 = hashes.get(path) if entry.ftype == "f" else None
        if md5:
            entry = FileEntry(
                ftype=entry.ftype,
                mode=entry.mode,
                owner=entry.owner,
                group=entry.group,
                size=entry.size,
                md5=md5,
                target=entry.target,
            )
        files[path] = entry
    return files


def _parse_services(unitfile_lines: list[str], running_lines: list[str]) -> dict[str, ServiceEntry]:
    running: set[str] = set()
    for line in running_lines:
        # list-units --plain: UNIT LOAD ACTIVE SUB DESCRIPTION
        fields = line.split()
        if fields and fields[0].endswith(".service"):
            running.add(fields[0])
    services: dict[str, ServiceEntry] = {}
    for line in unitfile_lines:
        # list-unit-files --plain: UNIT-FILE STATE [PRESET]
        fields = line.split()
        if len(fields) < 2 or not fields[0].endswith(".service"):
            continue
        name = fields[0]
        services[name] = ServiceEntry(enabled=fields[1], running=name in running)
    # Running services without a unit file entry (e.g. transient units) still matter.
    for name in running:
        services.setdefault(name, ServiceEntry(enabled="transient", running=True))
    return services


_PROCESS_RE = re.compile(r'users:\(\("([^"]+)"')


def _parse_ports(lines: list[str]) -> dict[tuple[str, str, int], PortEntry]:
    ports: dict[tuple[str, str, int], PortEntry] = {}
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        proto = fields[0]
        if proto not in ("tcp", "udp", "tcp6", "udp6"):
            continue
        local = fields[4]
        addr, sep, port_str = local.rpartition(":")
        if not sep:
            continue
        try:
            port = int(port_str)
        except ValueError:
            continue
        process_match = _PROCESS_RE.search(line)
        process = process_match.group(1) if process_match else None
        key = (proto, addr, port)
        ports[key] = PortEntry(proto=proto, addr=addr, port=port, process=process)
    return ports


def _parse_users(lines: list[str]) -> dict[str, UserEntry]:
    users: dict[str, UserEntry] = {}
    for line in lines:
        fields = line.split(":")
        if len(fields) < 7:
            continue
        name, _pw, uid, gid, _gecos, home, shell = fields[:7]
        users[name] = UserEntry(uid=uid, gid=gid, home=home, shell=shell)
    return users


def _parse_groups(lines: list[str]) -> dict[str, GroupEntry]:
    groups: dict[str, GroupEntry] = {}
    for line in lines:
        fields = line.split(":")
        if len(fields) < 3:
            continue
        name, _pw, gid = fields[:3]
        members = fields[3] if len(fields) > 3 else ""
        member_tuple = tuple(m for m in members.split(",") if m)
        groups[name] = GroupEntry(gid=gid, members=member_tuple)
    return groups


def parse_snapshot(text: str) -> Snapshot:
    sections = split_sections(text)
    if "meta" not in sections:
        raise SnapshotError(
            "collector output is missing the meta section - "
            "the state script probably failed inside the container"
        )
    snap = Snapshot()
    if "packages.dpkg" in sections:
        snap.package_manager = "dpkg"
        snap.packages = _parse_packages(sections["packages.dpkg"])
    elif "packages.rpm" in sections:
        snap.package_manager = "rpm"
        snap.packages = _parse_packages(sections["packages.rpm"])
    snap.files = _parse_files(sections.get("files", []), sections.get("hashes", []))
    if "services.unavailable" in sections:
        snap.services = None
    else:
        snap.services = _parse_services(
            sections.get("services.unitfiles", []), sections.get("services.running", [])
        )
    snap.ports = _parse_ports(sections["ports"]) if "ports" in sections else None
    snap.users = _parse_users(sections.get("users", []))
    snap.groups = _parse_groups(sections.get("groups", []))
    return snap
