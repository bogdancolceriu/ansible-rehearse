"""Thin wrapper around the docker/podman CLI.

Deliberately shells out to the CLI instead of using an SDK: it keeps the dependency
surface at zero, works identically with Docker Desktop, docker-ce and podman, and the
host tool stays pure Python - Ansible itself only ever runs inside the container.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class EngineError(RuntimeError):
    def __init__(self, message: str, stderr: str = ""):
        detail = f"\n{stderr.strip()}" if stderr.strip() else ""
        super().__init__(f"{message}{detail}")


class EngineTimeout(EngineError):
    """A container command exceeded its timeout; partial output is preserved."""

    def __init__(self, message: str, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


class ContainerEngine:
    def __init__(self, binary: str):
        self.binary = binary

    @property
    def name(self) -> str:
        return Path(self.binary).stem

    @classmethod
    def detect(cls, preferred: str | None = None) -> ContainerEngine:
        candidates = [preferred] if preferred else ["docker", "podman"]
        found_but_dead: list[str] = []
        for candidate in candidates:
            path = shutil.which(candidate)
            if not path:
                continue
            engine = cls(path)
            # Plain `info` works for both docker and podman (podman has no
            # .ServerVersion template field); rc 0 means the engine is usable.
            probe = engine._run("info", check=False, timeout=20)
            if probe.returncode == 0:
                return engine
            found_but_dead.append(candidate)
        if found_but_dead:
            raise EngineError(
                f"{' / '.join(found_but_dead)} found, but the daemon is not responding. "
                "Is Docker Desktop / the podman machine running?"
            )
        raise EngineError(
            "no container engine found - install Docker (docker.com) or Podman "
            "(podman.io), or pass --engine with the binary to use"
        )

    def _run(
        self,
        *args: str,
        check: bool = True,
        input_text: str | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self.binary, *args]
        try:
            # Binary pipes on purpose: Windows text mode would rewrite "\n" to
            # "\r\n" on stdin, and CRLF corrupts shell scripts piped into the
            # container ("set: Illegal option -").
            raw = subprocess.run(
                cmd,
                capture_output=True,
                input=input_text.encode("utf-8") if input_text is not None else None,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineTimeout(
                f"'{self.name} {args[0]}' timed out after {timeout}s",
                stdout=(exc.stdout or b"").decode("utf-8", errors="replace"),
                stderr=(exc.stderr or b"").decode("utf-8", errors="replace"),
            ) from exc
        proc = subprocess.CompletedProcess(
            args=cmd,
            returncode=raw.returncode,
            stdout=raw.stdout.decode("utf-8", errors="replace"),
            stderr=raw.stderr.decode("utf-8", errors="replace"),
        )
        if check and proc.returncode != 0:
            raise EngineError(
                f"'{self.name} {' '.join(args[:3])}...' failed (rc={proc.returncode})",
                proc.stderr,
            )
        return proc

    # -- images ---------------------------------------------------------------

    def image_exists(self, tag: str) -> bool:
        return self._run("image", "inspect", tag, check=False, timeout=30).returncode == 0

    def commit(self, container: str, tag: str) -> None:
        self._run("commit", container, tag, timeout=300)

    # -- container lifecycle --------------------------------------------------

    def start(self, image: str, name: str, systemd: bool) -> None:
        args = ["run", "-d", "--name", name]
        if systemd:
            # systemd images (e.g. geerlingguy/docker-*-ansible) define their own
            # init CMD; they need privileges and a writable cgroup mount.
            args += [
                "--privileged",
                "--cgroupns=host",
                "-v",
                "/sys/fs/cgroup:/sys/fs/cgroup:rw",
                "--tmpfs",
                "/run",
                "--tmpfs",
                "/tmp",
                image,
            ]
        else:
            args += [image, "sleep", "infinity"]
        # First run may pull the image; give it time.
        self._run(*args, timeout=600)

    def rm(self, name: str) -> None:
        self._run("rm", "-f", name, check=False, timeout=120)

    # -- interaction ----------------------------------------------------------

    def exec(
        self,
        name: str,
        cmd: list[str],
        env: dict[str, str] | None = None,
        workdir: str | None = None,
        check: bool = True,
        input_text: str | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        args = ["exec"]
        if input_text is not None:
            args.append("-i")
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        if workdir:
            args += ["-w", workdir]
        args.append(name)
        args += cmd
        return self._run(*args, check=check, input_text=input_text, timeout=timeout)

    def exec_script(
        self,
        name: str,
        script: str,
        env: dict[str, str] | None = None,
        check: bool = True,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a shell script inside the container, passed on stdin (no cp needed)."""
        return self.exec(
            name, ["sh", "-s"], env=env, check=check, input_text=script, timeout=timeout
        )

    def write_file(self, name: str, dest: str, content: str) -> None:
        self.exec(
            name,
            ["sh", "-c", f"cat > '{dest}'"],
            input_text=content,
            timeout=60,
        )

    def cp_dir_in(self, src: Path, name: str, dest: str) -> None:
        """Copy the CONTENTS of src into dest (which must already exist)."""
        # "src/." copies directory contents rather than the directory itself.
        self._run(
            "cp",
            f"{src}{'/' if not str(src).endswith('/') else ''}.",
            f"{name}:{dest}",
            timeout=300,
        )
