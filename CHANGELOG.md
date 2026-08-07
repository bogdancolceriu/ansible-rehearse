# Changelog

## 0.1.0 (unreleased)

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
