"""Parse the sectioned output of collect_state.sh into a Snapshot."""

from __future__ import annotations

import re
from importlib import resources

from .models import FileEntry, GroupEntry, PortEntry, ServiceEntry, Snapshot, UserEntry

_MARKER_RE = re.compile(r"^###REHEARSE:BEGIN ([a-z.]+)###\r?$", re.M)
_END_RE = re.compile(r"^###REHEARSE:(?:END|DONE)###\r?$", re.M)
_DONE_RE = re.compile(r"^###REHEARSE:DONE###\r?$", re.M)

# GNU md5sum escapes problematic filenames (\n -> \\n, \\ -> \\\\) and prefixes
# the whole line with a backslash.
_MD5_LINE = re.compile(r"^(\\)?([0-9a-f]{32})  (.*)$")


class SnapshotError(ValueError):
    """The collector output could not be parsed."""


def collector_script() -> str:
    """Return the collect_state.sh source shipped with the package."""
    return resources.files("ansible_rehearse").joinpath("data/collect_state.sh").read_text("utf-8")


def split_sections(text: str) -> dict[str, str]:
    """Split collector output into raw per-section text (markers excluded)."""
    sections: dict[str, str] = {}
    for match in _MARKER_RE.finditer(text):
        start = match.end()
        if start < len(text) and text[start] == "\n":
            start += 1
        end_match = _END_RE.search(text, start)
        end = end_match.start() if end_match else len(text)
        sections[match.group(1)] = text[start:end]
    return sections


def _lines(raw: str) -> list[str]:
    return [line.rstrip("\r") for line in raw.splitlines() if line.strip()]


def _parse_packages(raw: str) -> dict[str, str]:
    packages: dict[str, str] = {}
    for line in _lines(raw):
        name, _, version = line.partition("\t")
        if name:
            packages[name.strip()] = version.strip()
    return packages


def _parse_file_record(record: str) -> tuple[str, FileEntry] | None:
    parts = record.split("|", 6)
    if len(parts) != 7:
        return None
    ftype, mode, owner, group, size, mtime, rest = parts
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
        ftype=ftype,
        mode=mode,
        owner=owner,
        group=group,
        size=size_int,
        mtime=mtime if ftype == "f" else None,
        target=target,
    )


def _unescape_md5_path(path: str) -> str:
    # Reverse GNU coreutils escaping; the NUL placeholder cannot occur in paths.
    return path.replace("\\\\", "\0").replace("\\n", "\n").replace("\0", "\\")


def _parse_hashes(raw: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for line in _lines(raw):
        match = _MD5_LINE.match(line)
        if not match:
            continue
        escaped, digest, path = match.groups()
        if escaped:
            path = _unescape_md5_path(path)
        hashes[path] = digest
    return hashes


def _parse_files(raw: str, hashes_raw: str) -> dict[str, FileEntry]:
    hashes = _parse_hashes(hashes_raw)
    files: dict[str, FileEntry] = {}
    for record in raw.split("\0"):
        record = record.strip("\n").strip("\r")
        if not record:
            continue
        parsed = _parse_file_record(record)
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
                mtime=entry.mtime,
                md5=md5,
                target=entry.target,
            )
        files[path] = entry
    return files


def _parse_services(unitfiles_raw: str, running_raw: str) -> dict[str, ServiceEntry]:
    running: set[str] = set()
    for line in _lines(running_raw):
        # list-units --plain: UNIT LOAD ACTIVE SUB DESCRIPTION
        fields = line.split()
        if fields and fields[0].endswith(".service"):
            running.add(fields[0])
    services: dict[str, ServiceEntry] = {}
    for line in _lines(unitfiles_raw):
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


def _parse_ports(raw: str) -> dict[tuple[str, str, int], PortEntry]:
    ports: dict[tuple[str, str, int], PortEntry] = {}
    for line in _lines(raw):
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
        ports[(proto, addr, port)] = PortEntry(proto=proto, addr=addr, port=port, process=process)
    return ports


def _parse_users(raw: str) -> dict[str, UserEntry]:
    users: dict[str, UserEntry] = {}
    for line in _lines(raw):
        fields = line.split(":")
        if len(fields) < 7:
            continue
        name, _pw, uid, gid, _gecos, home, shell = fields[:7]
        users[name] = UserEntry(uid=uid, gid=gid, home=home, shell=shell)
    return users


def _parse_groups(raw: str) -> dict[str, GroupEntry]:
    groups: dict[str, GroupEntry] = {}
    for line in _lines(raw):
        fields = line.split(":")
        if len(fields) < 3:
            continue
        name, _pw, gid = fields[:3]
        members = fields[3] if len(fields) > 3 else ""
        groups[name] = GroupEntry(gid=gid, members=tuple(m for m in members.split(",") if m))
    return groups


def parse_snapshot(text: str) -> Snapshot:
    sections = split_sections(text)
    if "meta" not in sections:
        raise SnapshotError(
            "collector output is missing the meta section - "
            "the state script probably failed inside the container"
        )
    if not _DONE_RE.search(text):
        raise SnapshotError(
            "collector output is missing the DONE marker - "
            "the state script was interrupted before finishing"
        )
    snap = Snapshot()
    if "packages.dpkg" in sections:
        snap.package_manager = "dpkg"
        snap.packages = _parse_packages(sections["packages.dpkg"])
    elif "packages.rpm" in sections:
        snap.package_manager = "rpm"
        snap.packages = _parse_packages(sections["packages.rpm"])
    snap.files = _parse_files(sections.get("files", ""), sections.get("hashes", ""))
    if "services.unavailable" in sections:
        snap.services = None
    else:
        snap.services = _parse_services(
            sections.get("services.unitfiles", ""), sections.get("services.running", "")
        )
    snap.ports = _parse_ports(sections["ports"]) if "ports" in sections else None
    snap.users = _parse_users(sections.get("users", ""))
    snap.groups = _parse_groups(sections.get("groups", ""))
    return snap
