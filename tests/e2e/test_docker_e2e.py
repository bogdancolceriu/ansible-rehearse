"""End-to-end rehearsal against a real container engine.

Run with: pytest -m docker
These tests need a working Docker (or Podman) daemon and network access; the first
run per distro downloads the base image and installs ansible-core (minutes), later
runs reuse the committed cache image.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ansible_rehearse.runner import RunConfig, rehearse

pytestmark = pytest.mark.docker

DEMO_PLAYBOOK = """\
---
- name: Rehearse demo
  hosts: demo
  become: true
  tasks:
    - name: Install zip
      ansible.builtin.package:
        name: zip
        state: present

    - name: Write a config file
      ansible.builtin.copy:
        dest: /etc/rehearse-demo.conf
        content: |
          setting=1
        mode: "0640"
        owner: root
        group: root

    - name: Create an application user
      ansible.builtin.user:
        name: demoapp
        system: true
        shell: /usr/sbin/nologin
        create_home: true

    - name: Schedule a cron job
      ansible.builtin.cron:
        name: demo job
        minute: "*/15"
        job: /usr/bin/true
        user: root

    - name: Leave a marker via shell (check mode would skip this)
      ansible.builtin.shell: echo rehearsed > /usr/local/rehearse-marker
      args:
        creates: /usr/local/rehearse-marker
"""


def _engine_available() -> bool:
    return shutil.which("docker") is not None or shutil.which("podman") is not None


@pytest.fixture()
def demo_project(tmp_path: Path) -> Path:
    (tmp_path / "demo.yml").write_text(DEMO_PLAYBOOK, encoding="utf-8")
    return tmp_path


@pytest.mark.skipif(not _engine_available(), reason="no container engine on PATH")
def test_full_rehearsal_ubuntu22(demo_project: Path) -> None:
    cfg = RunConfig(playbook=demo_project / "demo.yml", distro="ubuntu22")
    result = rehearse(cfg, progress=print)

    assert result.play_rc == 0, [t.msg for t in result.tasks if t.failed]

    added_packages = {c.item for c in result.diff.packages if c.action == "added"}
    assert "zip" in added_packages

    added_files = {c.item for c in result.diff.files if c.action == "added"}
    assert "/etc/rehearse-demo.conf" in added_files
    assert "/usr/local/rehearse-marker" in added_files

    conf = next(c for c in result.diff.files if c.item == "/etc/rehearse-demo.conf")
    assert "0640" in (conf.detail or "")

    added_users = {c.item for c in result.diff.users if c.action == "added"}
    assert "demoapp" in added_users

    # cron writes root's crontab under /var/spool/cron (watched by default)
    cron_paths = [c.item for c in result.diff.files if "/var/spool/cron" in c.item]
    assert cron_paths, "expected the root crontab to appear in the file diff"

    # plain container -> services not observable, and the runner says so
    assert result.diff.services is None
    assert any("plain container" in w for w in result.warnings)

    # fidelity annotations came through
    by_action = {t.action.rsplit(".", 1)[-1]: t for t in result.tasks}
    assert by_action["shell"].fidelity == "real-exec"
    assert by_action["package"].fidelity == "exact"

    # A second rehearsal starts from the cached prepared image (not the base image)
    # and, because every rehearsal is pristine, reproduces the same diff.
    result2 = rehearse(cfg, progress=print)
    assert result2.play_rc == 0
    assert result2.image.startswith("ansible-rehearse/prepared:")
    assert "zip" in {c.item for c in result2.diff.packages if c.action == "added"}


@pytest.mark.skipif(not _engine_available(), reason="no container engine on PATH")
def test_failing_playbook_still_reports_diff(tmp_path: Path) -> None:
    (tmp_path / "fail.yml").write_text(
        """\
---
- hosts: all
  become: true
  tasks:
    - name: Make a change first
      ansible.builtin.copy:
        dest: /etc/partial-change.conf
        content: "made it\\n"
    - name: Then explode
      ansible.builtin.command: /bin/false
""",
        encoding="utf-8",
    )
    cfg = RunConfig(playbook=tmp_path / "fail.yml", distro="ubuntu22")
    result = rehearse(cfg, progress=print)
    assert result.play_failed
    assert any(t.failed for t in result.tasks)
    added = {c.item for c in result.diff.files if c.action == "added"}
    assert "/etc/partial-change.conf" in added
