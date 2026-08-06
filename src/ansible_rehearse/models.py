"""Data model for system state snapshots, diffs and rehearsal results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class FileEntry:
    ftype: str  # "f" regular file, "d" directory, "l" symlink
    mode: str  # octal string, e.g. "0644"
    owner: str
    group: str
    size: int
    md5: str | None = None  # regular files under the hash size cap only
    target: str | None = None  # symlink target


@dataclass(frozen=True)
class ServiceEntry:
    enabled: str  # enabled / disabled / static / masked / ...
    running: bool


@dataclass(frozen=True)
class PortEntry:
    proto: str  # tcp / udp
    addr: str
    port: int
    process: str | None = None


@dataclass(frozen=True)
class UserEntry:
    uid: str
    gid: str
    home: str
    shell: str


@dataclass(frozen=True)
class GroupEntry:
    gid: str
    members: tuple[str, ...] = ()


@dataclass
class Snapshot:
    """Observed state of the rehearsal container at one point in time."""

    packages: dict[str, str] = field(default_factory=dict)  # name -> version
    package_manager: str | None = None  # "dpkg" | "rpm" | None
    files: dict[str, FileEntry] = field(default_factory=dict)  # path -> entry
    # None means "not observable" (no systemd in the container), as opposed to "empty".
    services: dict[str, ServiceEntry] | None = None
    ports: dict[tuple[str, str, int], PortEntry] | None = None
    users: dict[str, UserEntry] = field(default_factory=dict)
    groups: dict[str, GroupEntry] = field(default_factory=dict)


@dataclass(frozen=True)
class Change:
    action: str  # "added" | "removed" | "changed"
    item: str  # package name, file path, service name, port, user, group
    before: str | None = None
    after: str | None = None
    detail: str | None = None  # e.g. "content", "mode 0644 -> 0600"


@dataclass
class StateDiff:
    packages: list[Change] = field(default_factory=list)
    files: list[Change] = field(default_factory=list)
    # None mirrors Snapshot.services: state was not observable on one of the sides.
    services: list[Change] | None = None
    ports: list[Change] | None = None
    users: list[Change] = field(default_factory=list)
    groups: list[Change] = field(default_factory=list)

    def sections(self) -> dict[str, list[Change] | None]:
        return {
            "packages": self.packages,
            "files": self.files,
            "services": self.services,
            "ports": self.ports,
            "users": self.users,
            "groups": self.groups,
        }

    def total_changes(self) -> int:
        return sum(len(changes) for changes in self.sections().values() if changes)

    def counts(self) -> dict[str, int]:
        out = {"added": 0, "removed": 0, "changed": 0}
        for changes in self.sections().values():
            for change in changes or []:
                out[change.action] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            name: None if changes is None else [asdict(c) for c in changes]
            for name, changes in self.sections().items()
        }


@dataclass
class TaskRecord:
    """One executed task, as reported by Ansible's JSON callback."""

    name: str
    action: str  # module name, possibly fully qualified
    changed: bool
    failed: bool
    skipped: bool
    unreachable: bool = False
    msg: str | None = None
    fidelity: str = "unknown"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RehearsalResult:
    playbook: str
    distro: str
    image: str
    systemd: bool
    engine: str
    play_rc: int
    diff: StateDiff
    tasks: list[TaskRecord] = field(default_factory=list)
    stats: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def play_failed(self) -> bool:
        return self.play_rc != 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "playbook": self.playbook,
            "distro": self.distro,
            "image": self.image,
            "systemd": self.systemd,
            "engine": self.engine,
            "play_rc": self.play_rc,
            "diff": self.diff.to_dict(),
            "tasks": [t.to_dict() for t in self.tasks],
            "stats": self.stats,
            "warnings": self.warnings,
        }
