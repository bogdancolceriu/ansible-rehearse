"""Static playbook scanner tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ansible_rehearse.playbook import PlaybookError, scan_playbook

MAIN_PLAYBOOK = """\
---
- name: Web tier
  hosts: webservers
  become: true
  pre_tasks:
    - name: Update apt cache
      ansible.builtin.apt:
        update_cache: true
  tasks:
    - name: Install nginx
      ansible.builtin.apt:
        name: nginx
        state: present
    - name: Configure limits
      block:
        - name: Write config
          ansible.builtin.template:
            src: site.conf.j2
            dest: /etc/nginx/sites-available/site.conf
        - name: Run migration script
          ansible.builtin.shell: /opt/app/migrate.sh
      rescue:
        - name: Report failure
          ansible.builtin.debug:
            msg: "migration failed"
  handlers:
    - name: Restart nginx
      ansible.builtin.service:
        name: nginx
        state: restarted

- name: DB tier
  hosts:
    - dbservers
    - "!excluded"
  tasks:
    - name: Ensure postgres
      ansible.builtin.package:
        name: postgresql
        state: present
      with_items:
        - one
"""


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "site.yml").write_text(MAIN_PLAYBOOK, encoding="utf-8")
    return tmp_path


def test_hosts_patterns_collected(project: Path) -> None:
    scan = scan_playbook(project / "site.yml")
    assert "webservers" in scan.hosts_patterns
    assert "dbservers" in scan.hosts_patterns
    assert "!excluded" in scan.hosts_patterns


def test_tasks_found_including_blocks_and_handlers(project: Path) -> None:
    scan = scan_playbook(project / "site.yml")
    actions = [t.action for t in scan.tasks]
    assert "ansible.builtin.apt" in actions
    assert "ansible.builtin.template" in actions
    assert "ansible.builtin.shell" in actions  # inside a block
    assert "ansible.builtin.debug" in actions  # inside rescue
    assert "ansible.builtin.service" in actions  # handler
    assert "ansible.builtin.package" in actions
    # with_items is a loop keyword, not a module
    assert "with_items" not in actions


def test_no_external_tasks_in_clean_playbook(project: Path) -> None:
    scan = scan_playbook(project / "site.yml")
    assert scan.external == []


EXTERNAL_PLAYBOOK = """\
---
- hosts: all
  tasks:
    - name: Spin up an instance
      amazon.aws.ec2_instance:
        name: prod-web
    - name: Notify the team
      community.general.slack:
        token: "{{ slack_token }}"
        msg: deployed
    - name: Call the API
      ansible.builtin.uri:
        url: https://api.example.com/deploy
        method: POST
    - name: Health check (safe)
      ansible.builtin.uri:
        url: https://api.example.com/health
    - name: Short-name cloud module
      ec2_snapshot:
        instance_id: i-123
"""


def test_external_detection(tmp_path: Path) -> None:
    (tmp_path / "ext.yml").write_text(EXTERNAL_PLAYBOOK, encoding="utf-8")
    scan = scan_playbook(tmp_path / "ext.yml")
    external_names = {t.name for t, _reason in scan.external}
    assert "Spin up an instance" in external_names
    assert "Notify the team" in external_names
    assert "Call the API" in external_names  # POST mutates
    assert "Health check (safe)" not in external_names  # GET is allowed
    assert "Short-name cloud module" in external_names


def test_include_tasks_followed(tmp_path: Path) -> None:
    (tmp_path / "main.yml").write_text(
        """\
---
- hosts: all
  tasks:
    - name: Include extra tasks
      ansible.builtin.include_tasks: extra.yml
""",
        encoding="utf-8",
    )
    (tmp_path / "extra.yml").write_text(
        """\
---
- name: Task from include
  ansible.builtin.copy:
    dest: /etc/x
    content: hi
""",
        encoding="utf-8",
    )
    scan = scan_playbook(tmp_path / "main.yml")
    assert any(t.action == "ansible.builtin.copy" for t in scan.tasks)


def test_dynamic_include_warns_instead_of_failing(tmp_path: Path) -> None:
    (tmp_path / "main.yml").write_text(
        """\
---
- hosts: all
  tasks:
    - name: Dynamic include
      ansible.builtin.include_tasks: "{{ variable_file }}.yml"
""",
        encoding="utf-8",
    )
    scan = scan_playbook(tmp_path / "main.yml")
    assert any("not followed" in w for w in scan.warnings)


def test_roles_scanned(tmp_path: Path) -> None:
    (tmp_path / "site.yml").write_text(
        """\
---
- hosts: all
  roles:
    - common
    - role: missing_role
""",
        encoding="utf-8",
    )
    tasks_dir = tmp_path / "roles" / "common" / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "main.yml").write_text(
        """\
---
- name: Role task
  ansible.builtin.file:
    path: /etc/from-role
    state: touch
""",
        encoding="utf-8",
    )
    scan = scan_playbook(tmp_path / "site.yml")
    assert any(t.action == "ansible.builtin.file" for t in scan.tasks)
    assert any("missing_role" in w for w in scan.warnings)


def test_import_playbook_followed(tmp_path: Path) -> None:
    (tmp_path / "site.yml").write_text("---\n- import_playbook: other.yml\n", encoding="utf-8")
    (tmp_path / "other.yml").write_text(
        """\
---
- hosts: imported_group
  tasks:
    - name: Imported task
      ansible.builtin.file:
        path: /tmp/x
        state: touch
""",
        encoding="utf-8",
    )
    scan = scan_playbook(tmp_path / "site.yml")
    assert "imported_group" in scan.hosts_patterns


def test_vault_tag_tolerated(tmp_path: Path) -> None:
    (tmp_path / "v.yml").write_text(
        """\
---
- hosts: all
  vars:
    secret: !vault |
      $ANSIBLE_VAULT;1.1;AES256
      6338646562
  tasks:
    - name: Use secret
      ansible.builtin.copy:
        dest: /etc/secret
        content: "{{ secret }}"
""",
        encoding="utf-8",
    )
    scan = scan_playbook(tmp_path / "v.yml")
    assert any(t.action == "ansible.builtin.copy" for t in scan.tasks)


def test_not_a_playbook_raises(tmp_path: Path) -> None:
    (tmp_path / "vars.yml").write_text("key: value\n", encoding="utf-8")
    with pytest.raises(PlaybookError):
        scan_playbook(tmp_path / "vars.yml")


LEGACY_ACTION_PLAYBOOK = """\
---
- hosts: all
  tasks:
    - name: Legacy string action form
      action: ec2_instance name=prod-web state=running
    - name: Legacy local_action string form
      local_action: uri url=https://api.example.com/deploy method=POST
    - name: Legacy dict action form
      action:
        module: amazon.aws.s3_bucket
        name: my-bucket
    - name: Harmless legacy action
      action: copy src=a dest=/etc/b
"""


def test_legacy_action_forms_do_not_bypass_the_gate(tmp_path: Path) -> None:
    (tmp_path / "legacy.yml").write_text(LEGACY_ACTION_PLAYBOOK, encoding="utf-8")
    scan = scan_playbook(tmp_path / "legacy.yml")
    actions = {t.action for t in scan.tasks}
    assert "ec2_instance" in actions
    assert "uri" in actions
    assert "amazon.aws.s3_bucket" in actions
    assert "copy" in actions
    external_names = {t.name for t, _reason in scan.external}
    assert "Legacy string action form" in external_names
    assert "Legacy local_action string form" in external_names  # POST parsed from k=v
    assert "Legacy dict action form" in external_names
    assert "Harmless legacy action" not in external_names


def test_kv_string_args_feed_external_detection(tmp_path: Path) -> None:
    (tmp_path / "kv.yml").write_text(
        """\
---
- hosts: all
  tasks:
    - name: Inline POST
      ansible.builtin.uri: url=https://api.example.com/x method=POST
    - name: Inline GET
      ansible.builtin.uri: url=https://api.example.com/x
""",
        encoding="utf-8",
    )
    scan = scan_playbook(tmp_path / "kv.yml")
    external_names = {t.name for t, _reason in scan.external}
    assert "Inline POST" in external_names
    assert "Inline GET" not in external_names


def test_args_keyword_merges_into_module_args(tmp_path: Path) -> None:
    (tmp_path / "args.yml").write_text(
        """\
---
- hosts: all
  tasks:
    - name: POST via args
      ansible.builtin.uri:
        url: https://api.example.com/x
      args:
        method: POST
""",
        encoding="utf-8",
    )
    scan = scan_playbook(tmp_path / "args.yml")
    assert {t.name for t, _r in scan.external} == {"POST via args"}


def test_role_meta_dependencies_are_scanned(tmp_path: Path) -> None:
    (tmp_path / "site.yml").write_text(
        "---\n- hosts: all\n  roles:\n    - webapp\n", encoding="utf-8"
    )
    webapp_tasks = tmp_path / "roles" / "webapp" / "tasks"
    webapp_tasks.mkdir(parents=True)
    (webapp_tasks / "main.yml").write_text(
        "---\n- name: App task\n  ansible.builtin.file:\n    path: /etc/app\n    state: touch\n",
        encoding="utf-8",
    )
    webapp_meta = tmp_path / "roles" / "webapp" / "meta"
    webapp_meta.mkdir(parents=True)
    (webapp_meta / "main.yml").write_text(
        "---\ndependencies:\n  - role: clouddep\n", encoding="utf-8"
    )
    dep_tasks = tmp_path / "roles" / "clouddep" / "tasks"
    dep_tasks.mkdir(parents=True)
    (dep_tasks / "main.yml").write_text(
        "---\n- name: Sneaky external\n  amazon.aws.ec2_instance:\n    name: prod\n",
        encoding="utf-8",
    )
    scan = scan_playbook(tmp_path / "site.yml")
    assert any(t.action == "amazon.aws.ec2_instance" for t in scan.tasks)
    assert any(t.name == "Sneaky external" for t, _r in scan.external)


def test_fqcn_import_playbook_followed(tmp_path: Path) -> None:
    (tmp_path / "site.yml").write_text(
        "---\n- ansible.builtin.import_playbook: sub/other.yml\n", encoding="utf-8"
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "other.yml").write_text(
        """\
---
- hosts: fqcn_imported
  tasks:
    - name: T
      ansible.builtin.file:
        path: /tmp/x
        state: touch
""",
        encoding="utf-8",
    )
    scan = scan_playbook(tmp_path / "site.yml")
    assert "fqcn_imported" in scan.hosts_patterns
