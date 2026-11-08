from docker_utils import run_container


def init():
    run_container(
        name="zbd-redis",
        image="redis:7",
        ports={6379: 6379},
        health_cmd='redis-cli ping',
    )
