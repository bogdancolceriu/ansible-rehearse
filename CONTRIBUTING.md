# Contributing to ansible-rehearse

Thanks for considering a contribution!

## Development setup

```bash
git clone https://github.com/bogdancolceriu/ansible-rehearse
cd ansible-rehearse
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Running the tests

```bash
# Unit tests (no container engine needed; run on Linux/macOS/Windows)
pytest

# End-to-end tests (need a running Docker or Podman daemon; slow on first run)
pytest -m docker

# Privileged systemd rehearsal e2e
pytest -m systemd

# Lint / format
ruff check src tests
ruff format src tests
```

## What contributions help most

- **Fidelity-matrix corrections** (`src/ansible_rehearse/fidelity.py`): if you
  know a module rehearses better or worse than its label says, a PR with a short
  justification is very welcome.
- **External-effect gate coverage**: modules/collections that touch real
  infrastructure and should be refused by default.
- **New distro profiles** (`src/ansible_rehearse/images.py`) with a passing e2e
  test.
- **Bug reports** with a minimal playbook that misbehaves.

## Regenerating the README demo

```bash
python docs/make_demo.py   # needs Docker; writes docs/demo.svg
```

## Notes

- The state collector (`src/ansible_rehearse/data/collect_state.sh`) must stay
  POSIX sh (no bashisms) and must keep LF line endings (enforced via
  .gitattributes).
- Everything the host tool runs in the container goes through
  `engine.ContainerEngine`; keep the host side pure Python.
