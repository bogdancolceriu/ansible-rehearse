# Changelog

## 0.1.1 (2026-09-15)

First release with outside contributions. Thanks to @breezeFur and @deepunyk.

- New distro profile: `fedora44` (Fedora 44, rpm family), with an end-to-end
  test alongside the existing rocky9 one. Contributed by @deepunyk in #8.
- Listening ports are now collected from `/proc/net` when `ss` is not present,
  so minimal images no longer need iproute2 just for the port snapshot. IPv4
  and IPv6 addresses are decoded from the kernel's little-endian hex form and
  only TCP sockets in state `0A` (LISTEN) are reported. The existing `ss` path
  is unchanged. Contributed by @breezeFur in #7.

## 0.1.0 (2026-08-07)

Initial release.

- `rehearse run PLAYBOOK`: executes the playbook for real inside an ephemeral
  container matched to the target distro and reports the observed state diff
  (packages, files, services, listening ports, users, groups).
- Fidelity honesty layer: every task is labeled exact / real-exec /
  needs-systemd / approximate / not-rehearsable / external.
- Refuse-by-default policy for tasks that would touch systems outside the
  container (cloud collections, notification modules, mutating `uri` calls);
  override with `--allow-external`.
- `--systemd` mode uses systemd-enabled images so service state is rehearsed
  for real.
- Distro profiles: ubuntu22, ubuntu24, debian12, rocky9; prepared containers
  are cached as images for fast reruns.
- Works from Windows, macOS and Linux hosts: Ansible runs inside the
  container, never on the host.
