"""Engine detection and error-path tests (no real container engine involved)."""

from __future__ import annotations

import subprocess

import pytest

from ansible_rehearse.engine import ContainerEngine, EngineError, EngineTimeout


def test_detect_no_engine_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ansible_rehearse.engine.shutil.which", lambda _name: None)
    with pytest.raises(EngineError, match="no container engine found"):
        ContainerEngine.detect()


def test_detect_daemon_not_responding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ansible_rehearse.engine.shutil.which", lambda name: f"C:\\fake\\{name}.exe"
    )

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout=b"", stderr=b"dead")

    monkeypatch.setattr("ansible_rehearse.engine.subprocess.run", fake_run)
    with pytest.raises(EngineError, match="not responding"):
        ContainerEngine.detect()


def test_detect_prefers_working_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ansible_rehearse.engine.shutil.which", lambda name: f"C:\\fake\\{name}.exe"
    )

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr("ansible_rehearse.engine.subprocess.run", fake_run)
    engine = ContainerEngine.detect()
    assert engine.name == "docker"


def test_timeout_preserves_partial_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"), output=b"partial", stderr=b"e")

    monkeypatch.setattr("ansible_rehearse.engine.subprocess.run", fake_run)
    engine = ContainerEngine("docker")
    with pytest.raises(EngineTimeout) as excinfo:
        engine.exec("c", ["true"], timeout=1)
    assert excinfo.value.stdout == "partial"
    assert excinfo.value.stderr == "e"
    assert isinstance(excinfo.value, EngineError)  # callers catching EngineError still work


def test_failed_command_raises_with_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=125, stdout=b"", stderr=b"boom")

    monkeypatch.setattr("ansible_rehearse.engine.subprocess.run", fake_run)
    engine = ContainerEngine("docker")
    with pytest.raises(EngineError, match="boom"):
        engine.exec("c", ["true"])
