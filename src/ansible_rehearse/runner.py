"""Orchestrate a rehearsal: prepare container, snapshot, run playbook, snapshot, diff."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .engine import ContainerEngine
from .fidelity import classify
from .images import DEFAULT_DISTRO, DISTROS, SELF_DIR, cache_tag, prepare_script
from .inventory import build_inventory
from .models import RehearsalResult, TaskRecord
from .playbook import PlaybookScan, ScannedTask, scan_playbook
from .snapshot import collector_script, parse_snapshot
from .statediff import diff_snapshots

_PLAY_TIMEOUT = 3600
_SNAPSHOT_TIMEOUT = 600
_PREPARE_TIMEOUT = 1200


class RehearseError(RuntimeError):
    pass


class ExternalTasksError(RehearseError):
    """The playbook contains tasks that would touch systems outside the container."""

    def __init__(self, external: list[tuple[ScannedTask, str]]):
        self.external = external
        listing = "; ".join(f"{task.name} ({reason})" for task, reason in external[:5])
        super().__init__(f"playbook contains external-effect tasks: {listing}")


@dataclass
class RunConfig:
    playbook: Path
    project_dir: Path | None = None
    distro: str = DEFAULT_DISTRO
    image: str | None = None  # explicit image override (disables the prepare cache)
    systemd: bool = False
    engine: str | None = None
    keep: bool = False
    no_cache: bool = False
    allow_external: bool = False
    extra_watch: list[str] = field(default_factory=list)
    ansible_args: list[str] = field(default_factory=list)


def parse_play_events(stdout: str) -> tuple[list[TaskRecord], dict[str, Any] | None]:
    """Parse the JSONL emitted by our rehearse_jsonl stdout callback."""
    records: list[TaskRecord] = []
    stats: dict[str, Any] | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = data.get("event")
        if event == "task_result":
            status = data.get("status", "ok")
            msg = data.get("msg")
            if status == "failed" and data.get("ignore_errors"):
                msg = f"{msg or 'failed'} (ignore_errors: play continued)"
            records.append(
                TaskRecord(
                    name=str(data.get("task") or "") or "unnamed task",
                    action=str(data.get("action") or "unknown"),
                    changed=bool(data.get("changed", False)),
                    failed=status == "failed",
                    skipped=status == "skipped",
                    unreachable=status == "unreachable",
                    msg=str(msg) if msg else None,
                )
            )
        elif event == "stats" and isinstance(data.get("stats"), dict):
            stats = data["stats"]
    return records, stats


def callback_plugin_source() -> str:
    """Return the rehearse_jsonl callback plugin shipped with the package."""
    from importlib import resources

    return resources.files("ansible_rehearse").joinpath("data/rehearse_jsonl.py").read_text("utf-8")


def _annotate_fidelity(records: list[TaskRecord], systemd: bool) -> None:
    for record in records:
        record.fidelity, record.note = classify(record.action, systemd=systemd)


def _container_playbook_path(playbook: Path, project_dir: Path) -> str:
    try:
        rel = playbook.resolve().relative_to(project_dir.resolve())
    except ValueError as exc:
        raise RehearseError(
            f"playbook {playbook} is not inside the project directory {project_dir}; "
            "pass --project-dir explicitly"
        ) from exc
    return f"{SELF_DIR}/project/{rel.as_posix()}"


def rehearse(
    cfg: RunConfig,
    progress: Callable[[str], None] | None = None,
) -> RehearsalResult:
    say = progress or (lambda _msg: None)
    playbook = cfg.playbook.resolve()
    if not playbook.is_file():
        raise RehearseError(f"playbook not found: {playbook}")
    project_dir = (cfg.project_dir or playbook.parent).resolve()
    if cfg.distro not in DISTROS:
        raise RehearseError(
            f"unknown distro {cfg.distro!r}; available: {', '.join(sorted(DISTROS))}"
        )
    spec = DISTROS[cfg.distro]

    say("scanning playbook")
    scan: PlaybookScan = scan_playbook(playbook, project_dir)
    if scan.external and not cfg.allow_external:
        raise ExternalTasksError(scan.external)

    warnings = list(scan.warnings)
    if scan.external and cfg.allow_external:
        warnings.extend(
            f"external task allowed to run: {task.name} - {reason}"
            for task, reason in scan.external
        )

    engine = ContainerEngine.detect(cfg.engine)
    base_image = cfg.image or (spec.systemd_image if cfg.systemd else spec.plain_image)
    cached_tag = cache_tag(spec, cfg.systemd)
    use_cache = not cfg.image and not cfg.no_cache and engine.image_exists(cached_tag)
    image = cached_tag if use_cache else base_image
    name = f"rehearse-{secrets.token_hex(4)}"

    inventory_text, inv_warnings = build_inventory(
        scan.hosts_patterns, python_interpreter=spec.target_python
    )
    warnings.extend(inv_warnings)

    try:
        say(f"starting container from {image}")
        engine.start(image=image, name=name, systemd=cfg.systemd)
        if not use_cache:
            say(
                "preparing container (package index, python, ansible-core) - first run "
                "per distro is slow, later runs reuse a cached image"
            )
            engine.exec_script(name, prepare_script(spec), timeout=_PREPARE_TIMEOUT)
            if not cfg.image:
                engine.commit(name, cached_tag)

        say("copying project into the container")
        engine.exec(name, ["mkdir", "-p", f"{SELF_DIR}/project"], timeout=60)
        engine.cp_dir_in(project_dir, name, f"{SELF_DIR}/project")
        engine.write_file(name, f"{SELF_DIR}/collect_state.sh", collector_script())
        engine.write_file(name, f"{SELF_DIR}/inventory.ini", inventory_text)
        engine.exec(name, ["mkdir", "-p", f"{SELF_DIR}/callbacks"], timeout=60)
        engine.write_file(name, f"{SELF_DIR}/callbacks/rehearse_jsonl.py", callback_plugin_source())

        requirements = project_dir / "requirements.yml"
        if requirements.is_file():
            say("installing Galaxy requirements")
            galaxy = f"{SELF_DIR}/venv/bin/ansible-galaxy"
            req_path = f"{SELF_DIR}/project/requirements.yml"
            for sub in (["collection", "install", "-r", req_path], ["install", "-r", req_path]):
                proc = engine.exec(name, [galaxy, *sub], check=False, timeout=_PREPARE_TIMEOUT)
                if proc.returncode != 0:
                    warnings.append(f"ansible-galaxy {sub[0]} failed: {proc.stderr.strip()[:200]}")

        snapshot_env = {}
        if cfg.extra_watch:
            defaults = "/etc /usr/local /opt /srv /root /home /var/spool/cron /var/www"
            snapshot_env["REHEARSE_WATCH_DIRS"] = defaults + " " + " ".join(cfg.extra_watch)

        say("taking the before snapshot")
        before_proc = engine.exec(
            name,
            ["sh", f"{SELF_DIR}/collect_state.sh"],
            env=snapshot_env,
            timeout=_SNAPSHOT_TIMEOUT,
        )
        before = parse_snapshot(before_proc.stdout)

        say("running the playbook (for real, inside the container)")
        play_env = {
            "ANSIBLE_STDOUT_CALLBACK": "rehearse_jsonl",
            "ANSIBLE_CALLBACK_PLUGINS": f"{SELF_DIR}/callbacks",
            "ANSIBLE_RETRY_FILES_ENABLED": "0",
            "ANSIBLE_HOST_PATTERN_MISMATCH": "warning",
            "ANSIBLE_LOCALHOST_WARNING": "0",
            "ANSIBLE_DEPRECATION_WARNINGS": "0",
            "ANSIBLE_FORCE_COLOR": "0",
            "PY_COLORS": "0",
        }
        play_cmd = [
            f"{SELF_DIR}/venv/bin/ansible-playbook",
            "-i",
            f"{SELF_DIR}/inventory.ini",
            _container_playbook_path(playbook, project_dir),
            *cfg.ansible_args,
        ]
        play_proc = engine.exec(
            name,
            play_cmd,
            env=play_env,
            workdir=f"{SELF_DIR}/project",
            check=False,
            timeout=_PLAY_TIMEOUT,
        )
        tasks, stats = parse_play_events(play_proc.stdout)
        if not tasks and play_proc.returncode != 0:
            stderr_tail = play_proc.stderr.strip().splitlines()[-15:]
            raise RehearseError(
                "ansible-playbook failed before producing task results "
                f"(rc={play_proc.returncode}):\n" + "\n".join(stderr_tail)
            )
        _annotate_fidelity(tasks, cfg.systemd)

        say("taking the after snapshot")
        after_proc = engine.exec(
            name,
            ["sh", f"{SELF_DIR}/collect_state.sh"],
            env=snapshot_env,
            timeout=_SNAPSHOT_TIMEOUT,
        )
        after = parse_snapshot(after_proc.stdout)

        diff = diff_snapshots(before, after)

        if not cfg.systemd:
            warnings.append(
                "plain container: service state is not observable (rerun with --systemd "
                "for service/port fidelity)"
            )

        return RehearsalResult(
            playbook=str(cfg.playbook),
            distro=cfg.distro,
            image=image,
            systemd=cfg.systemd,
            engine=engine.name,
            play_rc=play_proc.returncode,
            diff=diff,
            tasks=tasks,
            stats=stats,
            warnings=warnings,
        )
    finally:
        if cfg.keep:
            say(f"container kept for inspection: {name}")
        else:
            engine.rm(name)
