import subprocess
import time

# Every database gets the same hard limit, so the comparison is fair and a runaway database
# (e.g. Redis with 10M transactions) is killed by Docker instead of freezing the whole machine.
MEMORY_LIMIT = "5g"
MEMORY_LIMIT_BYTES = 5 * 1024**3


def wait_until_healthy(name, timeout=120):
    """Block until the container's health check reports healthy."""
    deadline = time.time() + timeout
    status = "unknown"
    while time.time() < deadline:
        status = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", name],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if status == "healthy":
            print(f"[{name}] healthy")
            return
        if status == "unhealthy":
            break
        time.sleep(2)
    raise RuntimeError(f"[{name}] not healthy (status: {status}), see: docker logs {name}")


def stop_container(name):
    """Stop the container if it is running (its data stays, `docker start` brings it back)."""
    running = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if running:
        subprocess.run(["docker", "stop", name], check=True, capture_output=True)
        print(f"[{name}] stopped")


def warn_if_wrong_limit(name):
    """Containers created before the limit existed (or with another one) are not comparable."""
    value = subprocess.run(
        ["docker", "inspect", "--format", "{{.HostConfig.Memory}}", name],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if value != str(MEMORY_LIMIT_BYTES):
        print(f"WARNING: [{name}] memory limit is {value} bytes, expected {MEMORY_LIMIT}. "
              f"Recreate it: docker rm -f {name}")


def run_container(name, image, ports, env=None, health_cmd=None, command=None):
    """Run a container in the background and wait until it is healthy.

    If the container already exists, it is started again.
    """
    existing = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Status}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    if existing.startswith("Up"):
        print(f"[{name}] already running")
    elif existing:
        subprocess.run(["docker", "start", name], check=True)
        print(f"[{name}] restarted")
    else:
        cmd = ["docker", "run", "-d", "--name", name]
        for host_port, container_port in ports.items():
            cmd += ["-p", f"{host_port}:{container_port}"]
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        if health_cmd:
            cmd += [
                "--health-cmd", health_cmd,
                "--health-interval", "5s",
                "--health-timeout", "5s",
                "--health-retries", "10",
                "--health-start-period", "10s",
            ]
        cmd += ["--memory", MEMORY_LIMIT, "--memory-swap", MEMORY_LIMIT]  # no swap inside the container
        cmd.append(image)
        cmd += command or []
        subprocess.run(cmd, check=True)
        print(f"[{name}] created and started")

    if existing:
        warn_if_wrong_limit(name)
    if health_cmd:
        wait_until_healthy(name)
