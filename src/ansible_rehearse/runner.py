"""Orchestrate a rehearsal: prepare container, snapshot, run playbook, snapshot, diff."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .engine import ContainerEngine, EngineTimeout
from .fidelity import FIDELITY_NEEDS_SYSTEMD, classify
from .images import DEFAULT_DISTRO, DISTROS, SELF_DIR, cache_tag, prepare_script
from .inventory import build_inventory
from .models import RehearsalResult, TaskRecord
from .playbook import PlaybookScan, ScannedTask, scan_playbook
from .snapshot import SnapshotError, collector_script, parse_snapshot
from .statediff import diff_snapshots

# Environment shared by every ansible invocation in the container: keep all of
# Ansible's own state (tmp dirs, galaxy content, async dir) under SELF_DIR so it
# never pollutes the state diff.
_ANSIBLE_ENV = {
    "ANSIBLE_HOME": f"{SELF_DIR}/ansible-home",
    "ANSIBLE_LOCAL_TEMP": f"{SELF_DIR}/tmp",
    "ANSIBLE_REMOTE_TMP": f"{SELF_DIR}/tmp",
}

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


def _galaxy_subcommands(requirements_text: str) -> list[list[str]]:
    """Decide which ansible-galaxy invocations a requirements.yml actually needs."""
    try:
        data = yaml.safe_load(requirements_text)
    except yaml.YAMLError:
        return []
    if isinstance(data, list):
        # Legacy format: a bare list of roles.
        return [["install", "-r"]]
    subcommands: list[list[str]] = []
    if isinstance(data, dict):
        if data.get("roles"):
            subcommands.append(["install", "-r"])
        if data.get("collections"):
            subcommands.append(["collection", "install", "-r"])
    return subcommands


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
    if not cfg.systemd:
        service_tasks = [
            t.name
            for t in scan.tasks
            if classify(t.action, systemd=False)[0] == FIDELITY_NEEDS_SYSTEMD
        ]
        if service_tasks:
            warnings.append(
                f"{len(service_tasks)} service task(s) found (e.g. "
                f"{service_tasks[0]!r}) but a plain container has no service "
                "manager - they will likely fail; rerun with --systemd"
            )
    else:
        warnings.append(
            "--systemd runs a PRIVILEGED container sharing host cgroups: "
            "kernel-level tasks (sysctl, mounts, firewall) can leak to the "
            "docker host - rehearse only playbooks you trust"
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
        # All of these live under SELF_DIR and exist BEFORE the first snapshot,
        # so neither our workspace nor Ansible's own state shows up in the diff.
        engine.exec(
            name,
            [
                "mkdir",
                "-p",
                f"{SELF_DIR}/project",
                f"{SELF_DIR}/callbacks",
                f"{SELF_DIR}/tmp",
                f"{SELF_DIR}/ansible-home",
            ],
            timeout=60,
        )
        engine.cp_dir_in(project_dir, name, f"{SELF_DIR}/project")
        engine.write_file(name, f"{SELF_DIR}/inventory.ini", inventory_text)
        engine.write_file(name, f"{SELF_DIR}/callbacks/rehearse_jsonl.py", callback_plugin_source())

        requirements = project_dir / "requirements.yml"
        if requirements.is_file():
            subcommands = _galaxy_subcommands(requirements.read_text("utf-8"))
            if subcommands:
                say("installing Galaxy requirements")
                warnings.append(
                    "requirements.yml content is installed at runtime and is NOT scanned "
                    "by the external-effect gate - review third-party roles yourself"
                )
            galaxy = f"{SELF_DIR}/venv/bin/ansible-galaxy"
            req_path = f"{SELF_DIR}/project/requirements.yml"
            for sub in subcommands:
                proc = engine.exec(
                    name,
                    [galaxy, *sub, req_path],
                    env=_ANSIBLE_ENV,
                    check=False,
                    timeout=_PREPARE_TIMEOUT,
                )
                if proc.returncode != 0:
                    warnings.append(f"ansible-galaxy {sub[0]} failed: {proc.stderr.strip()[:200]}")

        watch_env = {}
        if cfg.extra_watch:
            defaults = "/etc /usr/local /opt /srv /root /home /var/spool/cron /var/www"
            watch_env["REHEARSE_WATCH_DIRS"] = defaults + " " + " ".join(cfg.extra_watch)

        # The collector is piped over stdin on every use (no on-disk copy a
        # playbook could tamper with) and each snapshot gets a fresh random
        # nonce so section markers cannot be forged by crafted filenames.
        say("taking the before snapshot")
        before_nonce = secrets.token_hex(8)
        before_proc = engine.exec_script(
            name,
            collector_script(),
            env={**watch_env, "REHEARSE_NONCE": before_nonce},
            timeout=_SNAPSHOT_TIMEOUT,
        )
        try:
            before = parse_snapshot(before_proc.stdout, nonce=before_nonce)
        except SnapshotError as exc:
            raise RehearseError(f"could not parse the before snapshot: {exc}") from exc

        say("running the playbook (for real, inside the container)")
        play_env = {
            **_ANSIBLE_ENV,
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
        play_timed_out = False
        try:
            play_proc = engine.exec(
                name,
                play_cmd,
                env=play_env,
                workdir=f"{SELF_DIR}/project",
                check=False,
                timeout=_PLAY_TIMEOUT,
            )
            play_stdout, play_stderr = play_proc.stdout, play_proc.stderr
            play_rc = play_proc.returncode
        except EngineTimeout as exc:
            # Keep the partial task output instead of dying with an engine error.
            play_timed_out = True
            play_stdout, play_stderr = exc.stdout, exc.stderr
            play_rc = 124
            warnings.append(
                f"ansible-playbook exceeded the {_PLAY_TIMEOUT}s timeout and was "
                "aborted; the diff reflects whatever ran until then"
            )
            # The docker-exec timeout only kills the CLIENT; the playbook keeps
            # running in the container and would race the after-snapshot.
            engine.exec(name, ["pkill", "-9", "-f", "ansible-playbook"], check=False, timeout=30)
        tasks, stats = parse_play_events(play_stdout)
        if not tasks and play_rc != 0 and not play_timed_out:
            stderr_tail = play_stderr.strip().splitlines()[-15:]
            detail = "\n".join(stderr_tail)
            if warnings:
                detail += "\n\nwarnings so far:\n" + "\n".join(f"- {w}" for w in warnings)
            raise RehearseError(
                f"ansible-playbook failed before producing task results (rc={play_rc}):\n{detail}"
            )
        _annotate_fidelity(tasks, cfg.systemd)

        say("taking the after snapshot")
        after_nonce = secrets.token_hex(8)
        after_proc = engine.exec_script(
            name,
            collector_script(),
            env={**watch_env, "REHEARSE_NONCE": after_nonce},
            timeout=_SNAPSHOT_TIMEOUT,
        )
        try:
            after = parse_snapshot(after_proc.stdout, nonce=after_nonce)
        except SnapshotError as exc:
            raise RehearseError(f"could not parse the after snapshot: {exc}") from exc

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
            play_rc=play_rc,
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
