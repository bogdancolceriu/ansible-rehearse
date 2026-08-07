"""Regenerate docs/demo.svg from a real rehearsal of examples/webserver.yml.

Run from the repo root (needs a working Docker/Podman daemon):

    python docs/make_demo.py

The rehearsal runs entirely inside a throwaway container, so the captured
output contains only synthetic demo data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.console import Console

from ansible_rehearse.report import render
from ansible_rehearse.runner import RunConfig, rehearse

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    # Run from the repo root with a relative path, so the captured header shows
    # "examples/webserver.yml" instead of the author's absolute local path.
    os.chdir(ROOT)
    cfg = RunConfig(
        playbook=Path("examples") / "webserver.yml",
        distro="ubuntu22",
        systemd=True,
    )
    result = rehearse(cfg, progress=lambda msg: print(f"rehearse: {msg}", file=sys.stderr))
    console = Console(record=True, width=100, force_terminal=True)
    render(result, console)
    out = ROOT / "docs" / "demo.svg"
    console.save_svg(str(out), title="rehearse run examples/webserver.yml --systemd")
    print(f"wrote {out}", file=sys.stderr)
    return 0 if not result.play_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
