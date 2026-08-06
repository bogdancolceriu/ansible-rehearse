# ansible-rehearse

**Rehearse your Ansible playbooks in a throwaway container and see the real state diff
— packages, files, services, ports, users — before you touch production.**

`ansible-playbook --check` lies by omission: modules without check-mode support are
skipped, `shell`/`command` tasks are never run, and registered variables stay
undefined, so conditionals silently take the wrong branch. `rehearse` takes the
opposite approach: it **actually executes** your playbook inside an ephemeral
container matched to your target's distribution, snapshots the system state before
and after, and shows you a terraform-plan-style diff of what really changed.

> Full README with demo, fidelity model and usage is being written — v0.1 is under
> active development.

## License

MIT
