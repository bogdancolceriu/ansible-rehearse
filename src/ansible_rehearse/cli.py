"""rehearse - command line interface."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from .engine import EngineError
from .images import DEFAULT_DISTRO, DISTROS
from .playbook import PlaybookError
from .report import _esc, render, render_fidelity_matrix
from .runner import ExternalTasksError, RehearseError, RunConfig, rehearse

app = typer.Typer(
    name="rehearse",
    help=(
        "Rehearse Ansible playbooks in a throwaway container and see the real state "
        "diff (packages, files, services, ports, users) before touching production."
    ),
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)

EXIT_PLAY_FAILED = 1
EXIT_USAGE = 2
EXIT_EXTERNAL_BLOCKED = 3
EXIT_ENGINE = 4


def _print_error(prefix: str, exc: Exception) -> None:
    """Print a (possibly multi-line, untrusted) error message, sanitized per line."""
    lines = str(exc).splitlines() or [""]
    err_console.print(f"[red]{prefix}:[/red] {_esc(lines[0])}")
    for line in lines[1:]:
        err_console.print(f"  {_esc(line)}")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"ansible-rehearse {__version__}")
        raise typer.Exit()


@app.callback()
def _main_options(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Common options."""


@app.command()
def run(
    playbook: Path = typer.Argument(..., help="Playbook to rehearse."),
    distro: str = typer.Option(
        DEFAULT_DISTRO,
        "--distro",
        "-d",
        help=f"Target distribution profile ({', '.join(sorted(DISTROS))}).",
    ),
    image: str | None = typer.Option(
        None, "--image", help="Explicit container image (overrides --distro's image)."
    ),
    systemd: bool = typer.Option(
        False,
        "--systemd",
        help="Use a systemd-enabled image so service state is rehearsed for real.",
    ),
    project_dir: Path | None = typer.Option(
        None,
        "--project-dir",
        help="Project root copied into the container (default: playbook's directory).",
    ),
    keep: bool = typer.Option(
        False, "--keep", help="Keep the container after the rehearsal, for inspection."
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Do not reuse the prepared-image cache."
    ),
    allow_external: bool = typer.Option(
        False,
        "--allow-external",
        help="Run tasks that would touch systems OUTSIDE the container (dangerous).",
    ),
    engine: str | None = typer.Option(
        None, "--engine", help="Container engine binary (default: docker, then podman)."
    ),
    watch: list[str] = typer.Option(
        [], "--watch", help="Extra directory to include in the file diff (repeatable)."
    ),
    ansible_arg: list[str] = typer.Option(
        [],
        "--ansible-arg",
        help="Extra argument passed to ansible-playbook (repeatable), e.g. "
        "--ansible-arg=--extra-vars --ansible-arg=env=prod.",
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Also write the full result as JSON to this file."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="No progress messages."),
) -> None:
    """Rehearse PLAYBOOK in an ephemeral container and print the state diff."""
    if not playbook.is_file():
        err_console.print(f"[red]error:[/red] playbook not found: {_esc(str(playbook))}")
        raise typer.Exit(EXIT_USAGE)
    if distro not in DISTROS:
        err_console.print(
            f"[red]error:[/red] unknown distro {_esc(distro)!s}; "
            f"available: {', '.join(sorted(DISTROS))}"
        )
        raise typer.Exit(EXIT_USAGE)

    progress = (
        None if quiet else (lambda msg: err_console.print(f"[dim]rehearse: {_esc(msg)}[/dim]"))
    )
    if systemd:
        # Say it BEFORE the (long) run, not only in the final report.
        err_console.print(
            "[bold yellow]note:[/bold yellow] --systemd runs a PRIVILEGED container "
            "sharing host cgroups; kernel-level tasks can affect this machine. "
            "Rehearse only playbooks you trust."
        )
    cfg = RunConfig(
        playbook=playbook,
        project_dir=project_dir,
        distro=distro,
        image=image,
        systemd=systemd,
        engine=engine,
        keep=keep,
        no_cache=no_cache,
        allow_external=allow_external,
        extra_watch=list(watch),
        ansible_args=list(ansible_arg),
    )
    try:
        result = rehearse(cfg, progress=progress)
    except ExternalTasksError as exc:
        err_console.print(
            "[bold red]Refusing to run:[/bold red] this playbook contains tasks that "
            "would touch systems [bold]outside[/bold] the rehearsal container:"
        )
        for task, reason in exc.external:
            err_console.print(
                f"  [red]-[/red] {_esc(task.name)} [dim]({_esc(task.source)})[/dim]: {_esc(reason)}"
            )
        err_console.print(
            "\nRerun with [bold]--allow-external[/bold] only if you are certain "
            "(those tasks would run for real, against real endpoints)."
        )
        raise typer.Exit(EXIT_EXTERNAL_BLOCKED) from None
    except EngineError as exc:
        _print_error("container engine error", exc)
        raise typer.Exit(EXIT_ENGINE) from None
    except PlaybookError as exc:
        _print_error("playbook error", exc)
        raise typer.Exit(EXIT_USAGE) from None
    except RehearseError as exc:
        _print_error("error", exc)
        raise typer.Exit(EXIT_USAGE) from None

    render(result, console)
    if json_out:
        json_out.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        console.print(f"[dim]JSON written to {_esc(str(json_out))}[/dim]")
    if result.play_failed:
        raise typer.Exit(EXIT_PLAY_FAILED)


@app.command()
def distros() -> None:
    """List supported target distribution profiles."""
    from rich.table import Table

    table = Table(show_lines=False)
    table.add_column("Key")
    table.add_column("Plain image")
    table.add_column("Systemd image")
    table.add_column("Notes", style="dim")
    for spec in DISTROS.values():
        table.add_row(spec.key, spec.plain_image, spec.systemd_image, spec.notes)
    console.print(table)


@app.command()
def modules() -> None:
    """Show the module fidelity matrix (how honestly each module can be rehearsed)."""
    render_fidelity_matrix(console)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
