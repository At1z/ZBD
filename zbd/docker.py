import json
import subprocess
import time

# Every database gets the same hard limits, so the comparison is fair and a runaway database
# (e.g. Redis with 10M transactions) is killed by Docker instead of freezing the whole machine.
# The CPU cap also keeps one database from winning just by using more cores (e.g. parallel scans in PostgreSQL)
# and leaves the rest of the cores to the benchmark client and the system.
MEMORY_LIMIT = "6g"
MEMORY_LIMIT_BYTES = 6 * 1024**3
CPU_LIMIT = "4"
CPU_LIMIT_NANO = 4 * 10**9

# Server settings passed to the containers. Without them every engine runs with tiny defaults (MySQL's InnoDB buffer
# pool is 128 MB) and a 10M-row table does not fit in memory, which measures the default settings, not the engine.
# MySQL gets 3 GB of buffer pool (half of the container); Postgres the usual quarter (1.5 GB) because it also relies on
# the OS page cache (effective_cache_size). MongoDB's WiredTiger cache is ~50% of the container by default and
# Redis is capped by maxmemory, so both are already in that range.
COMMANDS = {
    "zbd-postgres": ["-c", "shared_buffers=1536MB", "-c", "effective_cache_size=4GB"],
    "zbd-mysql": ["--innodb-buffer-pool-size=3G"],
    "zbd-mongodb": None,
    # Redis: no RDB snapshots / AOF. A snapshot forks the process and every page the server then changes is copied
    # (copy-on-write), so a 4.8 GB dataset needs far more than the 6 GB limit while it is written: with the default
    # snapshot policy the kernel killed Redis at 10M transactions (during S19 / S20).
    "zbd-redis": ["redis-server", "--maxmemory", "5gb", "--maxmemory-policy", "noeviction",
                  "--save", "", "--appendonly", "no"],
}


def docker(*args):
    """Run a docker command and return its output. On failure the error carries docker's own message
    (e.g. daemon not running, permission denied), not just an exit status."""
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("`docker` not found: is Docker installed and on PATH?") from None
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "no output"
        raise RuntimeError(f"`docker {' '.join(args)}` failed (exit {result.returncode}): {message}")
    return result.stdout.strip()


def wait_until_healthy(name, timeout=120):
    """Block until the container's health check reports healthy."""
    deadline = time.time() + timeout
    status = "unknown"
    while time.time() < deadline:
        status = docker("inspect", "--format", "{{.State.Health.Status}}", name)
        if status == "healthy":
            print(f"[{name}] healthy")
            return
        if status == "unhealthy":
            break
        time.sleep(2)
    raise RuntimeError(f"[{name}] not healthy (status: {status}), see: docker logs {name}")


def stop_container(name):
    """Stop the container if it is running (its data stays, `docker start` brings it back)."""
    running = docker("ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}")
    if running:
        docker("stop", name)
        print(f"[{name}] stopped")


def wrong_limits(name):
    """A message when the existing container has other limits / settings than MEMORY_LIMIT, CPU_LIMIT and COMMANDS,
    else None (also None when the container does not exist yet: it will be created with the right ones)."""
    if not docker("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"):
        return None
    memory, nano_cpus = docker("inspect", "--format", "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}", name).split()
    if memory != str(MEMORY_LIMIT_BYTES) or nano_cpus != str(CPU_LIMIT_NANO):
        return (f"[{name}] has memory limit {memory} bytes and cpu limit {nano_cpus} nano-cpus, "
                f"expected {MEMORY_LIMIT} and {CPU_LIMIT} cpus. Recreate it: docker rm -f {name}")
    expected = COMMANDS.get(name)
    if expected:
        actual = json.loads(docker("inspect", "--format", "{{json .Config.Cmd}}", name))
        if actual != expected:
            return (f"[{name}] runs with settings {actual}, expected {expected}. Recreate it: docker rm -f {name}")
    return None


def warn_if_wrong_limit(name):
    """Containers created before the limits existed (or with other ones) are not comparable."""
    message = wrong_limits(name)
    if message:
        print(f"WARNING: {message}")


def run_container(name, image, ports, env=None, health_cmd=None, command=None):
    """Run a container in the background and wait until it is healthy.

    If the container already exists, it is started again.
    """
    existing = docker("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Status}}")

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
        cmd += ["--cpus", CPU_LIMIT]
        cmd.append(image)
        cmd += command or []
        subprocess.run(cmd, check=True)
        print(f"[{name}] created and started")

    if existing:
        warn_if_wrong_limit(name)
    if health_cmd:
        wait_until_healthy(name)
