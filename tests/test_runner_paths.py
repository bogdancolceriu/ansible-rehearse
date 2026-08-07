"""Runner code paths that no e2e run can reach deterministically."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ansible_rehearse.engine import EngineTimeout
from ansible_rehearse.runner import RunConfig, _galaxy_subcommands, rehearse

# --- _galaxy_subcommands ------------------------------------------------------


def test_galaxy_roles_only() -> None:
    text = "roles:\n  - name: geerlingguy.nginx\n"
    assert _galaxy_subcommands(text) == [["install", "-r"]]


def test_galaxy_collections_only() -> None:
    text = "collections:\n  - community.general\n"
    assert _galaxy_subcommands(text) == [["collection", "install", "-r"]]


def test_galaxy_both() -> None:
    text = "roles:\n  - r1\ncollections:\n  - c1\n"
    assert _galaxy_subcommands(text) == [["install", "-r"], ["collection", "install", "-r"]]


def test_galaxy_legacy_list_is_roles() -> None:
    assert _galaxy_subcommands("- src: geerlingguy.nginx\n") == [["install", "-r"]]


def test_galaxy_empty_or_garbage() -> None:
    assert _galaxy_subcommands("") == []
    assert _galaxy_subcommands("just a string") == []
    assert _galaxy_subcommands(":: not yaml ::") == []


# --- EngineTimeout -> partial diff flow --------------------------------------

_SNAPSHOT_TEXT = (
    "###REHEARSE[{n}]:BEGIN meta###\nformat=2\n###REHEARSE[{n}]:END###\n"
    "###REHEARSE[{n}]:BEGIN packages.dpkg###\n{packages}\n###REHEARSE[{n}]:END###\n"
    "###REHEARSE[{n}]:DONE###\n"
)


class _FakeProc:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class _FakeEngine:
    """Scripted engine: snapshots differ, the playbook exec times out."""

    name = "fake"

    def __init__(self) -> None:
        self.snapshot_count = 0
        self.killed: list[list[str]] = []

    @classmethod
    def detect(cls, _preferred=None):  # patched onto ContainerEngine.detect
        return cls()

    def image_exists(self, _tag: str) -> bool:
        return True  # pretend the prepared image is cached

    def start(self, image: str, name: str, systemd: bool) -> None:
        pass

    def commit(self, _name: str, _tag: str) -> None:
        pass

    def rm(self, _name: str) -> None:
        pass

    def cp_dir_in(self, _src, _name, _dest) -> None:
        pass

    def write_file(self, _name, _dest, _content) -> None:
        pass

    def exec_script(self, _name, _script, env=None, check=True, timeout=None) -> _FakeProc:
        # Called for the two snapshots; each carries its own nonce in env.
        self.snapshot_count += 1
        nonce = (env or {}).get("REHEARSE_NONCE", "0")
        packages = "bash\t5.1" if self.snapshot_count == 1 else "bash\t5.1\nzip\t3.0"
        return _FakeProc(_SNAPSHOT_TEXT.format(n=nonce, packages=packages))

    def exec(self, _name, cmd, env=None, workdir=None, check=True, input_text=None, timeout=None):
        if cmd and str(cmd[0]).endswith("ansible-playbook"):
            partial = json.dumps(
                {
                    "event": "task_result",
                    "status": "ok",
                    "task": "Install zip",
                    "action": "ansible.builtin.package",
                    "changed": True,
                    "ignore_errors": False,
                }
            )
            raise EngineTimeout("'fake exec' timed out after 3600s", stdout=partial + "\n")
        if cmd and cmd[0] == "pkill":
            self.killed.append(list(cmd))
        return _FakeProc()


def test_play_timeout_yields_partial_diff_and_kills_stray_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    playbook = tmp_path / "p.yml"
    playbook.write_text(
        "---\n- hosts: all\n  tasks:\n"
        "    - name: Install zip\n"
        "      ansible.builtin.package:\n        name: zip\n",
        encoding="utf-8",
    )
    fake = _FakeEngine()
    monkeypatch.setattr(
        "ansible_rehearse.runner.ContainerEngine.detect",
        staticmethod(lambda _preferred=None: fake),
    )
    result = rehearse(RunConfig(playbook=playbook))

    assert result.play_rc == 124
    assert any("timeout" in w for w in result.warnings)
    # partial task output survived the timeout
    assert [t.name for t in result.tasks] == ["Install zip"]
    # the stray in-container process was killed before the after snapshot
    assert fake.killed and fake.killed[0][0] == "pkill"
    # and the diff still reflects observed state
    assert [c.item for c in result.diff.packages] == ["zip"]
