# Stdout callback plugin shipped by ansible-rehearse and injected into the
# rehearsal container. Emits one JSON line per task result; the host parses them.
# Uses only the stable CallbackBase v2 API so it works across ansible-core versions.
from __future__ import annotations

import json

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
    name: rehearse_jsonl
    type: stdout
    short_description: JSONL task results for ansible-rehearse
    description:
        - Emits one JSON object per line for every task result.
        - Consumed by the ansible-rehearse host tool; not meant for humans.
"""


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "rehearse_jsonl"

    def _record(self, status, result, ignore_errors=False):
        task = result._task
        res = result._result or {}
        msg = res.get("msg")
        if msg is not None and not isinstance(msg, str):
            msg = str(msg)
        payload = {
            "event": "task_result",
            "status": status,
            "task": task.get_name(),
            "action": getattr(task, "resolved_action", None) or task.action,
            "changed": bool(res.get("changed", False)),
            "ignore_errors": bool(ignore_errors),
        }
        if msg:
            payload["msg"] = msg[:2000]
        self._display.display(json.dumps(payload, default=str))

    def v2_runner_on_ok(self, result):
        self._record("ok", result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._record("failed", result, ignore_errors=ignore_errors)

    def v2_runner_on_skipped(self, result):
        self._record("skipped", result)

    def v2_runner_on_unreachable(self, result):
        self._record("unreachable", result)

    def v2_playbook_on_stats(self, stats):
        summary = {host: stats.summarize(host) for host in sorted(stats.processed.keys())}
        self._display.display(json.dumps({"event": "stats", "stats": summary}, default=str))
