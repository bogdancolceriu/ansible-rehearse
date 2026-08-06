"""Render a rehearsal result: terraform-plan-style diff plus a task fidelity table.

Every dynamic string coming from playbooks or container output goes through
rich.markup.escape - task names and file paths must never be interpreted as markup.
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .fidelity import (
    FIDELITY_APPROXIMATE,
    FIDELITY_DESCRIPTIONS,
    FIDELITY_EXACT,
    FIDELITY_EXTERNAL,
    FIDELITY_NEEDS_SYSTEMD,
    FIDELITY_NOT_REHEARSABLE,
    FIDELITY_REAL_EXEC,
    FIDELITY_UNKNOWN,
)
from .models import Change, RehearsalResult

_ACTION_STYLE = {"added": "green", "removed": "red", "changed": "yellow"}
_ACTION_SYMBOL = {"added": "+", "removed": "-", "changed": "~"}

_FIDELITY_STYLE = {
    FIDELITY_EXACT: "green",
    FIDELITY_REAL_EXEC: "cyan",
    FIDELITY_NEEDS_SYSTEMD: "yellow",
    FIDELITY_APPROXIMATE: "yellow",
    FIDELITY_NOT_REHEARSABLE: "red",
    FIDELITY_EXTERNAL: "red",
    FIDELITY_UNKNOWN: "magenta",
}

_SECTION_TITLES = {
    "packages": "Packages",
    "files": "Files",
    "services": "Services",
    "ports": "Listening ports",
    "users": "Users",
    "groups": "Groups",
}


def _change_line(change: Change) -> str:
    style = _ACTION_STYLE[change.action]
    symbol = _ACTION_SYMBOL[change.action]
    parts = [f"  [{style}]{symbol}[/] [bold]{escape(change.item)}[/bold]"]
    if change.action == "changed" and change.before and change.after:
        parts.append(f"{escape(change.before)} -> {escape(change.after)}")
    elif change.after:
        parts.append(escape(change.after))
    elif change.before:
        parts.append(escape(change.before))
    if change.detail:
        parts.append(f"[dim]({escape(change.detail)})[/dim]")
    return " ".join(parts)


def render_diff(result: RehearsalResult, console: Console) -> None:
    console.print()
    mode = "systemd" if result.systemd else "plain"
    console.print(
        f"[bold]Rehearsal of[/bold] {escape(result.playbook)} "
        f"[dim]({result.distro}, {mode} container, {escape(result.image)}, "
        f"via {result.engine})[/dim]"
    )
    console.print()

    if result.play_failed:
        console.print(
            f"[bold red]Playbook FAILED (rc={result.play_rc}).[/bold red] "
            "The diff below shows what changed before the failure."
        )
        for task in result.tasks:
            if task.failed:
                msg = f" - {escape(task.msg)}" if task.msg else ""
                console.print(f"  [red]failed:[/red] {escape(task.name)}{msg}")
        console.print()

    for section, changes in result.diff.sections().items():
        title = _SECTION_TITLES[section]
        if changes is None:
            console.print(f"[bold]{title}[/bold]: [dim]not observable in plain mode[/dim]")
            continue
        if not changes:
            continue
        console.print(f"[bold]{title}[/bold] ({len(changes)}):")
        for change in changes:
            console.print(_change_line(change))
        console.print()

    if result.diff.total_changes() == 0:
        console.print("[green]No observable state changes.[/green]")
        console.print()


def render_tasks(result: RehearsalResult, console: Console) -> None:
    if not result.tasks:
        return
    table = Table(title="Task fidelity", title_justify="left", show_lines=False)
    table.add_column("Task", overflow="fold", max_width=48)
    table.add_column("Module", overflow="fold")
    table.add_column("Changed", justify="center")
    table.add_column("Fidelity")
    table.add_column("Note", overflow="fold", style="dim")
    for task in result.tasks:
        if task.skipped:
            changed = "[dim]skipped[/dim]"
        elif task.failed:
            changed = "[red]failed[/red]"
        elif task.changed:
            changed = "[yellow]yes[/yellow]"
        else:
            changed = "no"
        style = _FIDELITY_STYLE.get(task.fidelity, "white")
        table.add_row(
            escape(task.name),
            escape(task.action),
            changed,
            f"[{style}]{task.fidelity}[/{style}]",
            escape(task.note),
        )
    console.print(table)
    console.print()


def render_summary(result: RehearsalResult, console: Console) -> None:
    counts = result.diff.counts()
    console.print(
        f"[bold]Plan:[/bold] [green]{counts['added']} to add[/green], "
        f"[yellow]{counts['changed']} to change[/yellow], "
        f"[red]{counts['removed']} to remove[/red] "
        f"[dim](observed inside the rehearsal container)[/dim]"
    )
    fidelity_counts: dict[str, int] = {}
    for task in result.tasks:
        fidelity_counts[task.fidelity] = fidelity_counts.get(task.fidelity, 0) + 1
    if fidelity_counts:
        parts = [f"{count} {name}" for name, count in sorted(fidelity_counts.items())]
        console.print(f"[bold]Tasks:[/bold] {len(result.tasks)} total ({', '.join(parts)})")
    for warning in result.warnings:
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")
    console.print(
        "\n[dim]A rehearsal is not your production host: the diff reflects this "
        "container's state. Treat 'approximate' and 'unknown' tasks with care.[/dim]"
    )


def render(result: RehearsalResult, console: Console | None = None) -> None:
    console = console or Console()
    render_diff(result, console)
    render_tasks(result, console)
    render_summary(result, console)


def render_fidelity_matrix(console: Console | None = None) -> None:
    from .fidelity import matrix

    console = console or Console()
    for fidelity_class, modules in matrix().items():
        style = _FIDELITY_STYLE.get(fidelity_class, "white")
        console.print(
            f"[bold {style}]{fidelity_class}[/bold {style}]: "
            f"{FIDELITY_DESCRIPTIONS[fidelity_class]}"
        )
        console.print("  " + ", ".join(modules))
        console.print()
