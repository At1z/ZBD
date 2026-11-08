import subprocess
import time


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


def run_container(name, image, ports, env=None, health_cmd=None):
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
        cmd.append(image)
        subprocess.run(cmd, check=True)
        print(f"[{name}] created and started")

    if health_cmd:
        wait_until_healthy(name)
