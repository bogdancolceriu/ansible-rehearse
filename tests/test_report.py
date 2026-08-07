"""Rendering tests: output must be complete and markup-injection-safe."""

from __future__ import annotations

from rich.console import Console

from ansible_rehearse.models import Change, RehearsalResult, StateDiff, TaskRecord
from ansible_rehearse.report import render


def _render_to_text(result: RehearsalResult) -> str:
    console = Console(record=True, width=120, force_terminal=False, no_color=True)
    render(result, console)
    return console.export_text()


def _result(**overrides) -> RehearsalResult:
    defaults: dict = {
        "playbook": "site.yml",
        "distro": "ubuntu22",
        "image": "ubuntu:22.04",
        "systemd": False,
        "engine": "docker",
        "play_rc": 0,
        "diff": StateDiff(
            packages=[Change("added", "nginx", after="1.18.0")],
            files=[Change("changed", "/etc/nginx/nginx.conf", detail="content")],
            services=None,
            ports=None,
            users=[],
            groups=[],
        ),
        "tasks": [
            TaskRecord(
                name="Install nginx",
                action="ansible.builtin.apt",
                changed=True,
                failed=False,
                skipped=False,
                fidelity="exact",
            )
        ],
        "warnings": ["plain container: service state is not observable"],
    }
    defaults.update(overrides)
    return RehearsalResult(**defaults)


def test_render_basic_sections() -> None:
    text = _render_to_text(_result())
    assert "Rehearsal of site.yml" in text
    assert "+ nginx" in text
    assert "~ /etc/nginx/nginx.conf" in text
    assert "not observable in plain mode" in text
    assert "1 to add" in text
    assert "Install nginx" in text
    assert "warning:" in text


def test_render_failed_play() -> None:
    result = _result(
        play_rc=2,
        tasks=[
            TaskRecord(
                name="Bad task",
                action="ansible.builtin.shell",
                changed=False,
                failed=True,
                skipped=False,
                msg="boom",
                fidelity="real-exec",
            )
        ],
    )
    text = _render_to_text(result)
    assert "FAILED" in text
    assert "Bad task" in text
    assert "boom" in text


def test_render_empty_diff() -> None:
    result = _result(
        diff=StateDiff(services=None, ports=None),
        tasks=[],
        warnings=[],
    )
    text = _render_to_text(result)
    assert "No observable state changes" in text


def test_ansi_escapes_in_task_names_are_neutralized() -> None:
    hostile = "\x1b[2J\x1b[31mfake-clean-screen\x07"
    result = _result(
        tasks=[
            TaskRecord(
                name=hostile,
                action="ansible.builtin.debug",
                changed=False,
                failed=False,
                skipped=False,
                fidelity="exact",
            )
        ]
    )
    text = _render_to_text(result)
    assert "\x1b" not in text  # no raw ESC may survive into the output
    assert "\x07" not in text
    assert "fake-clean-screen" in text


def test_failed_play_summary_is_labeled_partial() -> None:
    text = _render_to_text(_result(play_rc=2))
    assert "Partial plan" in text
    ok_text = _render_to_text(_result(play_rc=0))
    assert "Partial plan" not in ok_text


def test_markup_injection_in_task_names_is_escaped() -> None:
    hostile = "[red]evil[/red] [link=http://x]task[/link]"
    result = _result(
        tasks=[
            TaskRecord(
                name=hostile,
                action="ansible.builtin.debug",
                changed=False,
                failed=False,
                skipped=False,
                fidelity="exact",
            )
        ],
        diff=StateDiff(
            files=[Change("added", "/etc/[bold]x[/bold].conf", detail="file, 0644 root:root")],
            services=None,
            ports=None,
        ),
    )
    # Must not raise MarkupError, and the literal brackets must survive.
    text = _render_to_text(result)
    assert "evil" in text
    assert "[red]" in text  # rendered literally, not interpreted
    assert "[bold]x[/bold]" in text


def test_json_roundtrip() -> None:
    import json

    payload = json.dumps(_result().to_dict())
    data = json.loads(payload)
    assert data["distro"] == "ubuntu22"
    assert data["diff"]["services"] is None
    assert data["tasks"][0]["fidelity"] == "exact"
