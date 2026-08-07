"""Tests for parsing the JSONL emitted by the rehearse_jsonl stdout callback."""

from __future__ import annotations

import json

from ansible_rehearse.runner import callback_plugin_source, parse_play_events

JSONL_OUTPUT = "\n".join(
    [
        json.dumps(
            {
                "event": "task_result",
                "status": "ok",
                "task": "Install zip",
                "action": "ansible.builtin.package",
                "changed": True,
                "ignore_errors": False,
            }
        ),
        "[WARNING]: some ansible warning in between",
        json.dumps(
            {
                "event": "task_result",
                "status": "failed",
                "task": "Broken step",
                "action": "ansible.builtin.shell",
                "changed": False,
                "ignore_errors": False,
                "msg": "non-zero return code",
            }
        ),
        json.dumps(
            {
                "event": "task_result",
                "status": "failed",
                "task": "Allowed to fail",
                "action": "ansible.builtin.command",
                "changed": False,
                "ignore_errors": True,
                "msg": "boom",
            }
        ),
        json.dumps(
            {
                "event": "task_result",
                "status": "skipped",
                "task": "",
                "action": "ansible.builtin.copy",
                "changed": False,
                "ignore_errors": False,
            }
        ),
        json.dumps(
            {
                "event": "stats",
                "stats": {"rehearsal-host": {"ok": 2, "changed": 1, "failures": 1}},
            }
        ),
        "not json at all",
    ]
)


def test_parse_events_and_stats() -> None:
    records, stats = parse_play_events(JSONL_OUTPUT)
    assert len(records) == 4
    install = records[0]
    assert install.name == "Install zip"
    assert install.action == "ansible.builtin.package"
    assert install.changed is True
    assert install.failed is False
    broken = records[1]
    assert broken.failed is True
    assert broken.msg == "non-zero return code"
    ignored = records[2]
    assert ignored.failed is True
    assert "ignore_errors" in (ignored.msg or "")
    unnamed = records[3]
    assert unnamed.name == "unnamed task"
    assert unnamed.skipped is True
    assert stats is not None
    assert stats["rehearsal-host"]["failures"] == 1


def test_parse_empty_or_garbage() -> None:
    assert parse_play_events("") == ([], None)
    assert parse_play_events("no json here\n{broken json") == ([], None)


def test_callback_plugin_ships_with_package() -> None:
    source = callback_plugin_source()
    assert 'CALLBACK_NAME = "rehearse_jsonl"' in source
    assert "v2_runner_on_ok" in source
    assert "v2_playbook_on_stats" in source
    # The plugin must not import anything from ansible_rehearse itself: it runs
    # inside the container where only ansible-core is installed.
    assert "ansible_rehearse" not in source
