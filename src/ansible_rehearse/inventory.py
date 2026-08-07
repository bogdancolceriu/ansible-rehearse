"""Generate a container-local inventory that satisfies the playbook's hosts patterns."""

from __future__ import annotations

import re

HOSTNAME = "rehearsal-host"

# Names that already resolve without a dedicated group.
_IMPLICIT = {"all", "*", "localhost", "127.0.0.1"}

_VALID_GROUP = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def hosts_groups(patterns: list[str]) -> tuple[dict[str, bool], list[str]]:
    """Extract group names from hosts patterns.

    Returns ({group: host_belongs}, warnings). Groups referenced by exclusion
    (`!group`) must EXIST but must NOT contain the rehearsal host, otherwise the
    exclusion would remove our only host from the play. Wildcard or regex
    patterns cannot be mapped to a single synthetic host, so they are surfaced
    as warnings instead.
    """
    groups: dict[str, bool] = {}
    warnings: list[str] = []
    for pattern in patterns:
        for token in re.split(r"[:,]", str(pattern)):
            token = token.strip()
            if not token:
                continue
            negated = token.startswith("!")
            token = token.lstrip("!&")
            if not token or token.lower() in _IMPLICIT:
                continue
            if token.lower() == "ungrouped":
                warnings.append(
                    "hosts pattern 'ungrouped' will not match: the rehearsal host "
                    "always belongs to the [rehearsal] group"
                )
                continue
            if _VALID_GROUP.match(token):
                # Include the host unless the group is ONLY ever excluded.
                groups[token] = groups.get(token, False) or not negated
            else:
                warnings.append(
                    f"hosts pattern {token!r} is not a plain group name; "
                    f"the rehearsal host may not match it"
                )
    return groups, warnings


def build_inventory(
    patterns: list[str],
    python_interpreter: str = "/usr/bin/python3",
) -> tuple[str, list[str]]:
    """Build an INI inventory placing one local host in every referenced group."""
    groups, warnings = hosts_groups(patterns)
    host_vars = f"ansible_connection=local ansible_python_interpreter={python_interpreter}"
    lines = ["[rehearsal]", f"{HOSTNAME} {host_vars}"]
    wants_localhost = any(str(p).strip() == "localhost" for p in patterns)
    if wants_localhost:
        lines.append(f"localhost {host_vars}")
        if len(set(patterns)) > 1:
            warnings.append(
                "playbook mixes 'localhost' with other hosts patterns; both map to the "
                "same rehearsal container, so 'all' plays will run tasks on it twice"
            )
    lines.append("")
    for group in sorted(groups):
        lines.append(f"[{group}]")
        if groups[group]:
            lines.append(HOSTNAME)
        lines.append("")
    return "\n".join(lines) + "\n", warnings
