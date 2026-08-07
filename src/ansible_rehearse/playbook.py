"""Static scan of a playbook: hosts patterns, modules used, external-effect tasks.

The scan is best-effort - it exists to (a) build a matching inventory, (b) refuse to
run tasks that would touch real infrastructure, and (c) pre-annotate fidelity. It is
not a full Ansible parser; dynamic includes with templated paths are reported as
warnings instead of being followed.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .fidelity import external_reason

# Task-level keywords that are not the module key.
_TASK_KEYWORDS = {
    "name",
    "when",
    "tags",
    "register",
    "vars",
    "args",
    "environment",
    "become",
    "become_user",
    "become_method",
    "become_flags",
    "become_exe",
    "delegate_to",
    "delegate_facts",
    "run_once",
    "ignore_errors",
    "ignore_unreachable",
    "changed_when",
    "failed_when",
    "notify",
    "listen",
    "loop",
    "loop_control",
    "until",
    "retries",
    "delay",
    "no_log",
    "check_mode",
    "diff",
    "any_errors_fatal",
    "throttle",
    "timeout",
    "connection",
    "port",
    "remote_user",
    "module_defaults",
    "collections",
    "debugger",
    "poll",
    "async",
}

_PLAY_TASK_SECTIONS = ("pre_tasks", "tasks", "post_tasks", "handlers")

_MAX_INCLUDE_DEPTH = 10


class PlaybookError(ValueError):
    """The playbook could not be read or parsed."""


class _AnsibleSafeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates Ansible-specific tags like !vault and !unsafe."""


def _unknown_scalar(loader: yaml.Loader, _tag_suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_AnsibleSafeLoader.add_multi_constructor("!", _unknown_scalar)


def load_yaml(text: str) -> object:
    return yaml.load(text, Loader=_AnsibleSafeLoader)


@dataclass(frozen=True)
class ScannedTask:
    name: str
    action: str
    args: dict = field(default_factory=dict, hash=False, compare=False)
    source: str = ""  # file the task was found in, relative to the project dir


@dataclass
class PlaybookScan:
    hosts_patterns: list[str] = field(default_factory=list)
    tasks: list[ScannedTask] = field(default_factory=list)
    external: list[tuple[ScannedTask, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _parse_kv_string(text: str) -> dict:
    """Parse one-line `key=value key2=value2` module args (best effort)."""
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    args: dict = {}
    for token in tokens:
        key, eq, value = token.partition("=")
        if eq and key:
            args[key] = value
    return args


def _module_args(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return _parse_kv_string(value)
    return {}


def _extract_action(task: dict) -> tuple[str, dict] | None:
    """Find the module key of a task dict. Returns (module, args) or None."""
    # Legacy `action:` / `local_action:` forms carry the module inside the value;
    # missing them would silently bypass the external-effect gate.
    for legacy_key in ("action", "local_action"):
        if legacy_key not in task:
            continue
        value = task[legacy_key]
        if isinstance(value, dict):
            module = value.get("module")
            if module:
                args = {k: v for k, v in value.items() if k != "module"}
                return str(module), args
        elif isinstance(value, str) and value.strip():
            head, _, rest = value.strip().partition(" ")
            return head, _parse_kv_string(rest)
        return None
    for key, value in task.items():
        if key in _TASK_KEYWORDS or key.startswith("with_"):
            continue
        if key == "block":
            return None
        args = _module_args(value)
        explicit = task.get("args")
        if isinstance(explicit, dict):
            args = {**args, **explicit}
        return str(key), args
    return None


def _rel_source(path: Path, project_dir: Path) -> str:
    try:
        return path.resolve().relative_to(project_dir.resolve()).as_posix()
    except ValueError:
        return path.name


class _Scanner:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.scan = PlaybookScan()
        self._visited: set[Path] = set()

    # -- file walking ---------------------------------------------------------

    def scan_playbook_file(self, path: Path, depth: int = 0) -> None:
        resolved = path.resolve()
        if resolved in self._visited:
            return
        self._visited.add(resolved)
        if depth > _MAX_INCLUDE_DEPTH:
            self.scan.warnings.append(f"include depth limit reached at {path.name}")
            return
        try:
            data = load_yaml(path.read_text("utf-8"))
        except OSError as exc:
            raise PlaybookError(f"cannot read playbook {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise PlaybookError(f"cannot parse YAML in {path}: {exc}") from exc
        if not isinstance(data, list):
            raise PlaybookError(f"{path} does not look like a playbook (expected a list of plays)")
        source = _rel_source(path, self.project_dir)
        for play in data:
            if not isinstance(play, dict):
                continue
            import_key = next(
                (k for k in ("import_playbook", "ansible.builtin.import_playbook") if k in play),
                None,
            )
            if import_key:
                self._follow_include(path, play[import_key], depth, playbook=True)
                continue
            hosts = play.get("hosts")
            if isinstance(hosts, list):
                self.scan.hosts_patterns.extend(str(h) for h in hosts)
            elif hosts is not None:
                self.scan.hosts_patterns.append(str(hosts))
            for section in _PLAY_TASK_SECTIONS:
                items = play.get(section)
                if isinstance(items, list):
                    self._walk_tasks(items, path, source, depth)
            roles = play.get("roles")
            if isinstance(roles, list):
                for role in roles:
                    role_name = role.get("role") if isinstance(role, dict) else role
                    if isinstance(role_name, str):
                        self._scan_role(role_name, depth)

    def _walk_tasks(self, items: list, file_path: Path, source: str, depth: int) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            if any(k in item for k in ("block", "rescue", "always")):
                for section in ("block", "rescue", "always"):
                    nested = item.get(section)
                    if isinstance(nested, list):
                        self._walk_tasks(nested, file_path, source, depth)
                continue
            extracted = _extract_action(item)
            if extracted is None:
                continue
            action, args = extracted
            task = ScannedTask(
                name=str(item.get("name", "")) or f"unnamed {action} task",
                action=action,
                args=args,
                source=source,
            )
            self.scan.tasks.append(task)
            reason = external_reason(action, args)
            if reason:
                self.scan.external.append((task, reason))
            base = action.rsplit(".", 1)[-1]
            if base in ("include_tasks", "import_tasks"):
                target = args.get("file") if args else None
                if target is None and not isinstance(item.get(action), dict):
                    target = item.get(action)
                self._follow_include(file_path, target, depth, playbook=False)
            elif base in ("include_role", "import_role"):
                role_name = args.get("name") if args else None
                if isinstance(role_name, str):
                    self._scan_role(role_name, depth)

    def _follow_include(self, from_file: Path, target: object, depth: int, playbook: bool) -> None:
        if not isinstance(target, str) or "{{" in target:
            self.scan.warnings.append(
                f"dynamic include in {from_file.name} not followed: {target!r}"
            )
            return
        candidate = (from_file.parent / target).resolve()
        if not candidate.is_file():
            self.scan.warnings.append(f"include target not found, not followed: {target}")
            return
        if playbook:
            self.scan_playbook_file(candidate, depth + 1)
        else:
            self._scan_task_file(candidate, depth + 1)

    def _scan_task_file(self, path: Path, depth: int) -> None:
        resolved = path.resolve()
        if resolved in self._visited or depth > _MAX_INCLUDE_DEPTH:
            return
        self._visited.add(resolved)
        try:
            data = load_yaml(path.read_text("utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            self.scan.warnings.append(f"cannot scan task file {path.name}: {exc}")
            return
        if isinstance(data, list):
            self._walk_tasks(data, path, _rel_source(path, self.project_dir), depth)

    def _scan_role(self, role_name: str, depth: int) -> None:
        role_dir = self.project_dir / "roles" / role_name
        if not role_dir.is_dir():
            self.scan.warnings.append(
                f"role '{role_name}' not found under roles/ - its tasks were not scanned"
            )
            return
        for sub in ("tasks", "handlers"):
            folder = role_dir / sub
            if not folder.is_dir():
                continue
            for task_file in sorted(folder.glob("*.yml")) + sorted(folder.glob("*.yaml")):
                self._scan_task_file(task_file, depth + 1)
        # Role dependencies (meta/main.yml) run before the role's own tasks and
        # must not escape the external-effect gate.
        for meta_name in ("main.yml", "main.yaml"):
            meta_file = role_dir / "meta" / meta_name
            if not meta_file.is_file():
                continue
            try:
                meta = load_yaml(meta_file.read_text("utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                self.scan.warnings.append(f"cannot scan {role_name}/meta/{meta_name}: {exc}")
                continue
            if not isinstance(meta, dict):
                continue
            for dep in meta.get("dependencies") or []:
                dep_name = dep.get("role") or dep.get("name") if isinstance(dep, dict) else dep
                if isinstance(dep_name, str) and depth < _MAX_INCLUDE_DEPTH:
                    self._scan_role(dep_name, depth + 1)


def scan_playbook(playbook: Path, project_dir: Path | None = None) -> PlaybookScan:
    project = project_dir or playbook.parent
    scanner = _Scanner(project)
    scanner.scan_playbook_file(playbook)
    return scanner.scan
