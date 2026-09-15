"""Supported target distributions and the container preparation recipe."""

from __future__ import annotations

from dataclasses import dataclass

from . import __version__

SELF_DIR = "/opt/ansible-rehearse"


@dataclass(frozen=True)
class DistroSpec:
    key: str
    family: str  # "deb" | "rpm"
    plain_image: str
    systemd_image: str
    venv_python: str  # interpreter used for the in-container controller venv
    ansible_pin: str  # controller ansible-core version range, matched to venv_python
    target_python: str = "/usr/bin/python3"
    notes: str = ""


# Images are fully qualified so podman's short-name resolution cannot pick a
# different registry. ansible-core is pinned per distro (the floor of the range
# is what the venv python supports; the cap keeps cached images predictable).
DISTROS: dict[str, DistroSpec] = {
    "ubuntu22": DistroSpec(
        key="ubuntu22",
        family="deb",
        plain_image="docker.io/library/ubuntu:22.04",
        systemd_image="docker.io/geerlingguy/docker-ubuntu2204-ansible:latest",
        venv_python="python3",
        ansible_pin="ansible-core>=2.17,<2.18",
        notes="python3.10 -> ansible-core 2.17",
    ),
    "ubuntu24": DistroSpec(
        key="ubuntu24",
        family="deb",
        plain_image="docker.io/library/ubuntu:24.04",
        systemd_image="docker.io/geerlingguy/docker-ubuntu2404-ansible:latest",
        venv_python="python3",
        ansible_pin="ansible-core>=2.18,<2.19",
    ),
    "debian12": DistroSpec(
        key="debian12",
        family="deb",
        plain_image="docker.io/library/debian:12",
        systemd_image="docker.io/geerlingguy/docker-debian12-ansible:latest",
        venv_python="python3",
        ansible_pin="ansible-core>=2.18,<2.19",
    ),
    "rocky9": DistroSpec(
        key="rocky9",
        family="rpm",
        plain_image="docker.io/library/rockylinux:9",
        systemd_image="docker.io/geerlingguy/docker-rockylinux9-ansible:latest",
        venv_python="python3.12",
        ansible_pin="ansible-core>=2.18,<2.19",
        notes="controller venv uses python3.12 (system python3 is 3.9)",
    ),
    "fedora44": DistroSpec(
        key="fedora44",
        family="rpm",
        plain_image="docker.io/library/fedora:44",
        systemd_image="docker.io/geerlingguy/docker-fedora44-ansible:latest",
        venv_python="python3",
        ansible_pin="ansible-core>=2.20,<2.21",
        notes="python3.14 -> ansible-core 2.20",
    ),
}

DEFAULT_DISTRO = "ubuntu22"

_PREPARE_DEB = f"""set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \\
    {{python}} {{python}}-venv python3-apt \\
    iproute2 findutils procps ca-certificates cron tar
mkdir -p {SELF_DIR}
{{python}} -m venv {SELF_DIR}/venv
{SELF_DIR}/venv/bin/pip install --quiet --upgrade pip
{SELF_DIR}/venv/bin/pip install --quiet "{{pin}}"
"""

_PREPARE_RPM = f"""set -eu
dnf install -y -q {{python}} iproute procps-ng findutils cronie tar ca-certificates
mkdir -p {SELF_DIR}
{{python}} -m venv {SELF_DIR}/venv
{SELF_DIR}/venv/bin/pip install --quiet --upgrade pip
{SELF_DIR}/venv/bin/pip install --quiet "{{pin}}"
"""


def prepare_script(spec: DistroSpec) -> str:
    template = _PREPARE_DEB if spec.family == "deb" else _PREPARE_RPM
    return template.format(python=spec.venv_python, pin=spec.ansible_pin)


def cache_tag(spec: DistroSpec, systemd: bool) -> str:
    suffix = "-systemd" if systemd else ""
    return f"ansible-rehearse/prepared:{spec.key}{suffix}-v{__version__}"
