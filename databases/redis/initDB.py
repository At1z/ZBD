from zbd.docker import run_container


def init():
    run_container(
        name="zbd-redis",
        image="redis:8.10.3",
        ports={6379: 6379},
        health_cmd='redis-cli ping',
        # below the 6g container limit: when full, writes fail with an error instead of the container being killed
        command=["redis-server", "--maxmemory", "5gb", "--maxmemory-policy", "noeviction"],
    )
