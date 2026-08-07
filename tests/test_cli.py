"""CLI smoke tests (no container engine required)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ansible_rehearse import __version__
from ansible_rehearse.cli import EXIT_ENGINE, EXIT_EXTERNAL_BLOCKED, EXIT_USAGE, app

runner = CliRunner()


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "rehearse" in result.output.lower()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_distros_listing() -> None:
    result = runner.invoke(app, ["distros"])
    assert result.exit_code == 0
    assert "ubuntu22" in result.output
    assert "rocky9" in result.output


def test_modules_matrix() -> None:
    result = runner.invoke(app, ["modules"])
    assert result.exit_code == 0
    assert "exact" in result.output
    assert "real-exec" in result.output


def test_run_missing_playbook() -> None:
    result = runner.invoke(app, ["run", "does-not-exist.yml"])
    assert result.exit_code == EXIT_USAGE


def test_run_unknown_distro(tmp_path: Path) -> None:
    playbook = tmp_path / "p.yml"
    playbook.write_text("---\n- hosts: all\n  tasks: []\n", encoding="utf-8")
    result = runner.invoke(app, ["run", str(playbook), "--distro", "gentoo"])
    assert result.exit_code == EXIT_USAGE


def test_run_engine_error_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ansible_rehearse.engine import EngineError
    from ansible_rehearse.runner import ContainerEngine

    playbook = tmp_path / "p.yml"
    playbook.write_text("---\n- hosts: all\n  tasks: []\n", encoding="utf-8")

    def raise_engine_error(_preferred=None):
        raise EngineError("no container engine found - install Docker")

    monkeypatch.setattr(ContainerEngine, "detect", staticmethod(raise_engine_error))
    result = runner.invoke(app, ["run", str(playbook)])
    assert result.exit_code == EXIT_ENGINE
    assert "container engine" in result.output.lower()


def test_run_invalid_playbook_yaml_is_a_clean_error(tmp_path: Path) -> None:
    playbook = tmp_path / "broken.yml"
    playbook.write_text("key: value\n", encoding="utf-8")  # a dict, not a playbook
    result = runner.invoke(app, ["run", str(playbook)])
    assert result.exit_code == EXIT_USAGE
    assert "playbook error" in result.output.lower()


def test_run_blocks_external_before_touching_any_engine(tmp_path: Path) -> None:
    playbook = tmp_path / "cloud.yml"
    playbook.write_text(
        """\
---
- hosts: all
  tasks:
    - name: Create instance
      amazon.aws.ec2_instance:
        name: prod
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run", str(playbook)])
    assert result.exit_code == EXIT_EXTERNAL_BLOCKED
    assert "outside" in result.output.lower()
