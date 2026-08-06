"""Honesty layer: classify how faithfully each module can be rehearsed in a container.

This is the core difference from `ansible-playbook --check`: check mode silently skips
what it cannot simulate, while rehearse executes everything for real inside the
container and tells you, per task, how much that execution resembles a real host.
"""

from __future__ import annotations

FIDELITY_EXACT = "exact"
FIDELITY_REAL_EXEC = "real-exec"
FIDELITY_NEEDS_SYSTEMD = "needs-systemd"
FIDELITY_APPROXIMATE = "approximate"
FIDELITY_NOT_REHEARSABLE = "not-rehearsable"
FIDELITY_EXTERNAL = "external"
FIDELITY_UNKNOWN = "unknown"

FIDELITY_DESCRIPTIONS = {
    FIDELITY_EXACT: "State change happens for real inside the rehearsal container.",
    FIDELITY_REAL_EXEC: (
        "Executed for real inside the container - unlike --check, which skips it entirely."
    ),
    FIDELITY_NEEDS_SYSTEMD: (
        "Exact in --systemd mode; in a plain container there is no service manager."
    ),
    FIDELITY_APPROXIMATE: (
        "Runs, but a container may behave differently from a real host (shared kernel, "
        "no firewall state, no SELinux policy of its own)."
    ),
    FIDELITY_NOT_REHEARSABLE: (
        "Cannot be meaningfully rehearsed in a container (kernel, hardware, boot)."
    ),
    FIDELITY_EXTERNAL: (
        "Would touch systems OUTSIDE the container (cloud APIs, network devices, "
        "notifications). Blocked unless --allow-external is given."
    ),
    FIDELITY_UNKNOWN: "Unclassified module - treat the result as approximate.",
}

# Modules whose full effect is contained in (and observable inside) the container.
_EXACT = {
    "apt",
    "apt_repository",
    "apt_key",
    "deb822_repository",
    "dpkg_selections",
    "dnf",
    "dnf5",
    "yum",
    "yum_repository",
    "rpm_key",
    "package",
    "pip",
    "npm",
    "gem",
    "file",
    "copy",
    "template",
    "lineinfile",
    "blockinfile",
    "replace",
    "assemble",
    "ini_file",
    "xml",
    "htpasswd",
    "tempfile",
    "synchronize",
    "user",
    "group",
    "authorized_key",
    "known_hosts",
    "git",
    "unarchive",
    "get_url",
    "cron",
    "cronvar",
    "at",
    "alternatives",
    "locale_gen",
    "timezone",
    # Read-only / control-flow modules: nothing to misrepresent.
    "stat",
    "slurp",
    "find",
    "fetch",
    "setup",
    "gather_facts",
    "debug",
    "assert",
    "fail",
    "meta",
    "set_fact",
    "include_vars",
    "wait_for",
    "wait_for_connection",
    "ping",
    "add_host",
    "group_by",
    "pause",
    "include_tasks",
    "import_tasks",
    "include_role",
    "import_role",
}

# Arbitrary code: runs for real in the container. That IS the selling point, but the
# outcome can still depend on things a container does not replicate.
_REAL_EXEC = {"command", "shell", "script", "raw", "expect"}

# Service state only exists when a service manager is running (--systemd mode).
_NEEDS_SYSTEMD = {"service", "systemd", "systemd_service", "service_facts", "runit", "sysvinit"}

# Runs, but the container's shared kernel / missing subsystems make results indicative.
_APPROXIMATE = {
    "sysctl",
    "pam_limits",
    "seboolean",
    "selinux",
    "sefcontext",
    "seport",
    "ufw",
    "iptables",
    "iptables_state",
    "nftables",
    "firewalld",
    "hostname",
    "mount",
    "capabilities",
    "listen_ports_facts",
}

# No meaningful container equivalent at all.
_NOT_REHEARSABLE = {
    "reboot",
    "modprobe",
    "kernel_blacklist",
    "grub_config",
    "lvg",
    "lvol",
    "parted",
    "filesystem",
    "mdadm",
    "zfs",
    "zpool",
    "swap",
    "swapfile",
}

# Modules that push data OUT of the machine being configured.
_EXTERNAL_MODULES = {
    "mail",
    "slack",
    "telegram",
    "matrix",
    "discord",
    "rocketchat",
    "hipchat",
    "irc",
    "jabber",
    "mqtt",
    "sns",
    "sendgrid",
    "twilio",
    "nexmo",
    "pushover",
    "pushbullet",
    "campfire",
    "flowdock",
    "grove",
    "typetalk",
    "syslogger",
    "logentries",
}

# Collections that manage infrastructure outside the container.
_EXTERNAL_COLLECTION_PREFIXES = (
    "amazon.aws.",
    "community.aws.",
    "azure.azcollection.",
    "google.cloud.",
    "oracle.oci.",
    "openstack.cloud.",
    "ovirt.ovirt.",
    "community.digitalocean.",
    "digitalocean.",
    "linode.cloud.",
    "hetzner.hcloud.",
    "vultr.cloud.",
    "kubernetes.core.",
    "community.okd.",
    "community.vmware.",
    "vmware.",
    "cisco.",
    "arista.",
    "junipernetworks.",
    "vyos.",
    "f5networks.",
    "fortinet.",
    "paloaltonetworks.",
    "dellemc.",
    "netapp.",
    "purestorage.",
    "infoblox.",
    "community.zabbix.",
    "community.grafana.",
    "netbox.",
    "servicenow.",
    "theforeman.",
    "community.hashi_vault.",
    "awx.awx.",
    "ansible.tower.",
    "ansible.eda.",
)

# Short-name prefixes that indicate external infrastructure modules even when the
# playbook does not use the fully qualified collection name.
_EXTERNAL_SHORT_PREFIXES = (
    "ec2_",
    "s3_",
    "rds_",
    "elb_",
    "iam_",
    "route53",
    "cloudformation",
    "lambda_",
    "gcp_",
    "gce_",
    "azure_",
    "aws_",
    "k8s",
    "helm",
    "vmware_",
    "vcenter_",
    "ios_",
    "nxos_",
    "junos_",
    "eos_",
    "asa_",
    "bigip_",
    "fortios_",
    "panos_",
    "zabbix_",
    "grafana_",
    "datadog_",
    "newrelic_",
    "pagerduty",
    "snow_",
    "tower_",
    "awx_",
)


def short_name(action: str) -> str:
    """Return the module name without its collection prefix."""
    return action.rsplit(".", 1)[-1]


def external_reason(action: str, args: dict | None = None) -> str | None:
    """Reason string if the task would touch systems outside the container, else None."""
    for prefix in _EXTERNAL_COLLECTION_PREFIXES:
        if action.startswith(prefix):
            return f"collection '{prefix.rstrip('.')}' manages external infrastructure"
    name = short_name(action)
    if name in _EXTERNAL_MODULES:
        return f"module '{name}' sends data to an external service"
    for prefix in _EXTERNAL_SHORT_PREFIXES:
        if name.startswith(prefix):
            return f"module '{name}' manages external infrastructure"
    if name == "uri":
        method = str((args or {}).get("method", "GET")).upper()
        if method not in ("GET", "HEAD", "OPTIONS"):
            return f"uri with method={method} would mutate an external endpoint"
    return None


def classify(action: str, systemd: bool = False, args: dict | None = None) -> tuple[str, str]:
    """Classify a module: (fidelity, short human note)."""
    if external_reason(action, args):
        return FIDELITY_EXTERNAL, external_reason(action, args) or ""
    name = short_name(action)
    if name in _REAL_EXEC:
        return FIDELITY_REAL_EXEC, "runs for real (--check would skip this)"
    if name in _NEEDS_SYSTEMD:
        if systemd:
            return FIDELITY_EXACT, "systemd is running in the rehearsal container"
        return (
            FIDELITY_NEEDS_SYSTEMD,
            "no service manager in a plain container - rerun with --systemd",
        )
    if name in _APPROXIMATE:
        return FIDELITY_APPROXIMATE, "container kernel/subsystems differ from a real host"
    if name in _NOT_REHEARSABLE:
        return FIDELITY_NOT_REHEARSABLE, "kernel/hardware/boot operation"
    if name in _EXACT:
        return FIDELITY_EXACT, ""
    return FIDELITY_UNKNOWN, "unclassified module - treat as approximate"


def matrix() -> dict[str, list[str]]:
    """Full fidelity matrix, for the `rehearse modules` command and the docs."""
    return {
        FIDELITY_EXACT: sorted(_EXACT),
        FIDELITY_REAL_EXEC: sorted(_REAL_EXEC),
        FIDELITY_NEEDS_SYSTEMD: sorted(_NEEDS_SYSTEMD),
        FIDELITY_APPROXIMATE: sorted(_APPROXIMATE),
        FIDELITY_NOT_REHEARSABLE: sorted(_NOT_REHEARSABLE),
        FIDELITY_EXTERNAL: sorted(_EXTERNAL_MODULES),
    }
