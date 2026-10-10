"""Thin wrapper around the docker CLI, so every action is a plain, auditable command."""

import subprocess

from .settings import COMPOSE_PROJECT, TARGET_LABEL


class DockerError(RuntimeError):
    pass


def docker(*args: str, check: bool = True, timeout: float = 60) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise DockerError(f"docker {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def targets() -> dict[str, str]:
    """Containers that may be faulted, mapped to their state (running, exited, ...)."""
    out = docker(
        "ps", "--all",
        "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
        "--filter", f"label={TARGET_LABEL}=true",
        "--format", "{{.Names}}\t{{.State}}",
    )
    return dict(line.split("\t") for line in out.splitlines() if line)


def health(name: str) -> str:
    """'running', 'running/healthy', 'exited', ... for one container."""
    return docker(
        "inspect", "--format",
        "{{.State.Status}}{{if .State.Health}}/{{.State.Health.Status}}{{end}}", name,
    )
